//! Native Wayland `zwlr_layer_shell_v1` transparent click-through overlay stage
//! built on `smithay-client-toolkit` + `wayland-client`.
//!
//! Supports:
//! - Persistent double-buffered `wl_shm` slots with per-buffer dirty-rect clearing (zero ghosting)
//! - Multi-monitor `wl_output` enumeration, explicit switching, hotplug fallback recreation
//! - Cross-monitor adjacency lookup and Y-coordinate mapping
//! - Optional secondary overlay compositing (compact native RMB menu)

use std::io::{self, Read, Write};
use std::os::fd::{AsFd, AsRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::Command;
use std::time::Instant;

use smithay_client_toolkit::{
    compositor::{CompositorHandler, CompositorState, Region},
    delegate_compositor, delegate_keyboard, delegate_layer, delegate_output, delegate_pointer,
    delegate_registry, delegate_seat, delegate_shm,
    output::{OutputHandler, OutputState},
    registry::{ProvidesRegistryState, RegistryState},
    registry_handlers,
    seat::{
        keyboard::{KeyEvent, KeyboardHandler, Keysym, Modifiers},
        pointer::{PointerEvent, PointerEventKind, PointerHandler, BTN_LEFT, BTN_RIGHT},
        Capability, SeatHandler, SeatState,
    },
    shell::{
        wlr_layer::{
            Anchor, KeyboardInteractivity, Layer, LayerShell, LayerShellHandler, LayerSurface,
            LayerSurfaceConfigure,
        },
        WaylandSurface,
    },
    shm::{
        slot::{Buffer, SlotPool},
        Shm, ShmHandler,
    },
};
use tiny_skia::Pixmap;
use wayland_client::{
    globals::registry_queue_init,
    protocol::{wl_keyboard, wl_output, wl_pointer, wl_seat, wl_shm, wl_surface},
    Connection, EventQueue, Proxy, QueueHandle,
};

use crate::renderer::{blit_canvas_to_wayland_argb8888, CANVAS_H, CANVAS_W};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputDescriptor {
    pub name: String,
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}

#[derive(Debug, Clone)]
pub enum StageEvent {
    Configured { width: u32, height: u32 },
    PointerPress { x: f64, y: f64, button: u32 },
    PointerMove { x: f64, y: f64, at: Instant },
    PointerRelease { button: u32, shift: bool },
    OutputSwitched,
    HotplugFallbackNeeded,
    Closed,
}

struct PersistentBuffer {
    buffer: Buffer,
    prev_rect: Option<(i32, i32, i32, i32)>,
}

struct TrackedOutput {
    wl_output: wl_output::WlOutput,
    desc: OutputDescriptor,
}

struct AppWaylandState {
    registry_state: RegistryState,
    seat_state: SeatState,
    output_state: OutputState,
    compositor_state: CompositorState,
    shm: Shm,
    layer_shell: LayerShell,
    namespace: String,

    layer_surface: Option<LayerSurface>,
    pointer: Option<wl_pointer::WlPointer>,
    keyboard: Option<wl_keyboard::WlKeyboard>,

    outputs: Vec<TrackedOutput>,
    active_output_name: String,

    width: u32,
    height: u32,
    configured: bool,
    closed: bool,
    shift_held: bool,
    press_shift_held: bool,
    shift_keys_down: Vec<u32>,
    pointer_pressed: bool,
    kb_interactivity: KeyboardInteractivity,
    pointer_x: f64,
    pointer_y: f64,
    pointer_seen: bool,

    pool: Option<SlotPool>,
    pool_size: (u32, u32),
    buffers: Vec<PersistentBuffer>,
    last_committed_rect: Option<(i32, i32, i32, i32)>,
    last_input_rect: Option<(i32, i32, i32, i32)>,
    last_input_full: bool,

    events: Vec<StageEvent>,
}

impl AppWaylandState {
    fn sync_output_entry(&mut self, output: &wl_output::WlOutput) {
        let Some(info) = self.output_state.info(output) else {
            return;
        };
        let name = info
            .name
            .clone()
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| format!("output-{}", output.id().protocol_id()));
        let (x, y) = info.logical_position.unwrap_or(info.location);
        let (w, h) = info.logical_size.unwrap_or_else(|| {
            info.modes
                .iter()
                .find(|m| m.current)
                .or_else(|| info.modes.first())
                .map(|m| m.dimensions)
                .unwrap_or((1920, 1080))
        });
        let desc = OutputDescriptor {
            name,
            x,
            y,
            width: w.max(300),
            height: h.max(420),
        };
        if let Some(existing) = self.outputs.iter_mut().find(|o| o.wl_output == *output) {
            existing.desc = desc;
        } else {
            self.outputs.push(TrackedOutput {
                wl_output: output.clone(),
                desc,
            });
        }
    }

    fn reset_surface_resources(&mut self) {
        self.buffers.clear();
        self.pool = None;
        self.last_committed_rect = None;
        self.last_input_rect = None;
        self.last_input_full = false;
        self.configured = false;
    }

    fn bind_layer_surface_on(
        &mut self,
        qh: &QueueHandle<Self>,
        target: Option<&wl_output::WlOutput>,
    ) {
        self.layer_surface = None;
        self.reset_surface_resources();
        let surface = self.compositor_state.create_surface(qh);
        let layer_surface = self.layer_shell.create_layer_surface(
            qh,
            surface,
            Layer::Top,
            Some(self.namespace.clone()),
            target,
        );
        layer_surface.set_anchor(Anchor::TOP | Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT);
        // `exclusive_zone(0)`: do NOT reserve our own zone, DO respect top/bottom bars (e.g. end-4 quickshell bar).
        layer_surface.set_exclusive_zone(0);
        layer_surface.set_keyboard_interactivity(KeyboardInteractivity::None);
        layer_surface.set_size(0, 0);
        layer_surface.commit();
        self.kb_interactivity = KeyboardInteractivity::None;
        self.layer_surface = Some(layer_surface);
        self.closed = false;
    }

    fn is_shift_held(&self) -> bool {
        self.shift_held || !self.shift_keys_down.is_empty()
    }

    fn set_keyboard_interactivity(&mut self, mode: KeyboardInteractivity) {
        if self.kb_interactivity == mode {
            return;
        }
        if let Some(ref ls) = self.layer_surface {
            ls.set_keyboard_interactivity(mode);
            ls.wl_surface().commit();
            self.kb_interactivity = mode;
        }
    }
}

impl CompositorHandler for AppWaylandState {
    fn scale_factor_changed(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: i32,
    ) {
    }

    fn transform_changed(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: wl_output::Transform,
    ) {
    }

    fn frame(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: u32,
    ) {
    }

    fn surface_enter(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        output: &wl_output::WlOutput,
    ) {
        self.sync_output_entry(output);
        if let Some(found) = self.outputs.iter().find(|o| o.wl_output == *output) {
            if self.active_output_name != found.desc.name {
                self.active_output_name = found.desc.name.clone();
                self.events.push(StageEvent::OutputSwitched);
            }
        }
    }

    fn surface_leave(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: &wl_output::WlOutput,
    ) {
    }
}

impl OutputHandler for AppWaylandState {
    fn output_state(&mut self) -> &mut OutputState {
        &mut self.output_state
    }

    fn new_output(&mut self, _: &Connection, _: &QueueHandle<Self>, output: wl_output::WlOutput) {
        self.sync_output_entry(&output);
    }

    fn update_output(&mut self, _: &Connection, _: &QueueHandle<Self>, output: wl_output::WlOutput) {
        self.sync_output_entry(&output);
    }

    fn output_destroyed(&mut self, _: &Connection, _: &QueueHandle<Self>, output: wl_output::WlOutput) {
        let removed_name = self
            .outputs
            .iter()
            .find(|o| o.wl_output == output)
            .map(|o| o.desc.name.clone());
        self.outputs.retain(|o| o.wl_output != output);
        if let Some(name) = removed_name {
            if name == self.active_output_name {
                self.active_output_name.clear();
                if !self.outputs.is_empty() {
                    self.events.push(StageEvent::HotplugFallbackNeeded);
                }
            }
        }
    }
}

impl LayerShellHandler for AppWaylandState {
    fn closed(&mut self, _conn: &Connection, _qh: &QueueHandle<Self>, _layer: &LayerSurface) {
        if !self.outputs.is_empty() {
            // Compositor closed the surface because its output was unplugged/disabled:
            // recreate cleanly on a remaining fallback output instead of crashing.
            self.active_output_name.clear();
            self.events.push(StageEvent::HotplugFallbackNeeded);
        } else {
            self.closed = true;
            self.events.push(StageEvent::Closed);
        }
    }

    fn configure(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _layer: &LayerSurface,
        configure: LayerSurfaceConfigure,
        _serial: u32,
    ) {
        let w = if configure.new_size.0 > 0 {
            configure.new_size.0
        } else {
            self.width.max(1920)
        };
        let h = if configure.new_size.1 > 0 {
            configure.new_size.1
        } else {
            self.height.max(1080)
        };
        if w != self.width || h != self.height {
            self.width = w;
            self.height = h;
            self.buffers.clear();
            self.pool = None;
            self.last_committed_rect = None;
        }
        self.configured = true;
        self.events.push(StageEvent::Configured { width: w, height: h });
    }
}

impl SeatHandler for AppWaylandState {
    fn seat_state(&mut self) -> &mut SeatState {
        &mut self.seat_state
    }

    fn new_seat(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_seat::WlSeat) {}

    fn new_capability(
        &mut self,
        _conn: &Connection,
        qh: &QueueHandle<Self>,
        seat: wl_seat::WlSeat,
        capability: Capability,
    ) {
        if capability == Capability::Pointer && self.pointer.is_none() {
            if let Ok(p) = self.seat_state.get_pointer(qh, &seat) {
                self.pointer = Some(p);
            }
        }
        if capability == Capability::Keyboard && self.keyboard.is_none() {
            if let Ok(k) = self.seat_state.get_keyboard(qh, &seat, None) {
                self.keyboard = Some(k);
            }
        }
    }

    fn remove_capability(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _seat: wl_seat::WlSeat,
        capability: Capability,
    ) {
        if capability == Capability::Pointer {
            if let Some(p) = self.pointer.take() {
                p.release();
            }
        }
        if capability == Capability::Keyboard {
            if let Some(k) = self.keyboard.take() {
                k.release();
            }
        }
    }

    fn remove_seat(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_seat::WlSeat) {}
}

impl PointerHandler for AppWaylandState {
    fn pointer_frame(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _pointer: &wl_pointer::WlPointer,
        events: &[PointerEvent],
    ) {
        let now = Instant::now();
        for ev in events {
            self.pointer_x = ev.position.0;
            self.pointer_y = ev.position.1;
            self.pointer_seen = true;
            match ev.kind {
                PointerEventKind::Enter { .. } => {
                    // Request ON_DEMAND keyboard interactivity while hovering Capri so LMB press
                    // receives Shift modifiers/keys from Hyprland/Niri without stealing focus on hover.
                    self.set_keyboard_interactivity(KeyboardInteractivity::OnDemand);
                }
                PointerEventKind::Leave { .. } => {
                    if !self.pointer_pressed {
                        self.set_keyboard_interactivity(KeyboardInteractivity::None);
                    }
                }
                PointerEventKind::Motion { .. } => {
                    self.events.push(StageEvent::PointerMove {
                        x: self.pointer_x,
                        y: self.pointer_y,
                        at: now,
                    });
                }
                PointerEventKind::Press { button, .. }
                    if button == BTN_LEFT || button == BTN_RIGHT =>
                {
                    if button == BTN_LEFT {
                        self.pointer_pressed = true;
                        self.set_keyboard_interactivity(KeyboardInteractivity::OnDemand);
                        self.press_shift_held = self.is_shift_held();
                    }
                    self.events.push(StageEvent::PointerPress {
                        x: self.pointer_x,
                        y: self.pointer_y,
                        button,
                    });
                }
                PointerEventKind::Release { button, .. }
                    if button == BTN_LEFT || button == BTN_RIGHT =>
                {
                    let shift = self.is_shift_held() || self.press_shift_held;
                    if button == BTN_LEFT {
                        self.pointer_pressed = false;
                        self.press_shift_held = false;
                        // Relinquish keyboard interactivity immediately so focus returns to user apps.
                        self.set_keyboard_interactivity(KeyboardInteractivity::None);
                    }
                    self.events.push(StageEvent::PointerRelease { button, shift });
                }
                _ => {}
            }
        }
    }
}

impl KeyboardHandler for AppWaylandState {
    fn enter(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_keyboard::WlKeyboard,
        _: &wl_surface::WlSurface,
        _: u32,
        raw: &[u32],
        keysyms: &[Keysym],
    ) {
        self.shift_keys_down.clear();
        for (&code, &sym) in raw.iter().zip(keysyms.iter()) {
            if sym == Keysym::Shift_L || sym == Keysym::Shift_R {
                self.shift_keys_down.push(code);
            }
        }
        if self.pointer_pressed && !self.shift_keys_down.is_empty() {
            self.press_shift_held = true;
        }
    }

    fn leave(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_keyboard::WlKeyboard,
        _: &wl_surface::WlSurface,
        _: u32,
    ) {
        self.shift_held = false;
        self.shift_keys_down.clear();
    }

    fn press_key(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_keyboard::WlKeyboard,
        _: u32,
        event: KeyEvent,
    ) {
        if event.keysym == Keysym::Shift_L || event.keysym == Keysym::Shift_R {
            if !self.shift_keys_down.contains(&event.raw_code) {
                self.shift_keys_down.push(event.raw_code);
            }
            if self.pointer_pressed {
                self.press_shift_held = true;
            }
        }
    }

    fn release_key(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_keyboard::WlKeyboard,
        _: u32,
        event: KeyEvent,
    ) {
        if event.keysym == Keysym::Shift_L || event.keysym == Keysym::Shift_R {
            self.shift_keys_down.retain(|&c| c != event.raw_code);
        }
    }

    fn update_modifiers(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_keyboard::WlKeyboard,
        _: u32,
        modifiers: Modifiers,
        _: u32,
    ) {
        self.shift_held = modifiers.shift;
        if self.pointer_pressed && modifiers.shift {
            self.press_shift_held = true;
        }
    }
}

impl ShmHandler for AppWaylandState {
    fn shm_state(&mut self) -> &mut Shm {
        &mut self.shm
    }
}

impl ProvidesRegistryState for AppWaylandState {
    fn registry(&mut self) -> &mut RegistryState {
        &mut self.registry_state
    }
    registry_handlers![OutputState, SeatState];
}

delegate_compositor!(AppWaylandState);
delegate_output!(AppWaylandState);
delegate_shm!(AppWaylandState);
delegate_seat!(AppWaylandState);
delegate_pointer!(AppWaylandState);
delegate_keyboard!(AppWaylandState);
delegate_layer!(AppWaylandState);
delegate_registry!(AppWaylandState);

pub struct WaylandStage {
    conn: Connection,
    event_queue: EventQueue<AppWaylandState>,
    state: AppWaylandState,
}

impl WaylandStage {
    pub fn connect(namespace: &str) -> Result<Self, String> {
        let conn = Connection::connect_to_env()
            .map_err(|e| format!("Failed to connect to Wayland display: {e}"))?;
        let (globals, mut event_queue) = registry_queue_init::<AppWaylandState>(&conn)
            .map_err(|e| format!("Failed to init Wayland registry: {e}"))?;
        let qh = event_queue.handle();

        let compositor_state = CompositorState::bind(&globals, &qh)
            .map_err(|e| format!("wl_compositor unavailable: {e}"))?;
        let layer_shell = LayerShell::bind(&globals, &qh)
            .map_err(|e| format!("zwlr_layer_shell_v1 unavailable: {e}"))?;
        let shm = Shm::bind(&globals, &qh)
            .map_err(|e| format!("wl_shm unavailable: {e}"))?;

        let mut state = AppWaylandState {
            registry_state: RegistryState::new(&globals),
            seat_state: SeatState::new(&globals, &qh),
            output_state: OutputState::new(&globals, &qh),
            compositor_state,
            shm,
            layer_shell,
            namespace: namespace.to_string(),
            layer_surface: None,
            pointer: None,
            keyboard: None,
            outputs: Vec::new(),
            active_output_name: String::new(),
            width: 0,
            height: 0,
            configured: false,
            closed: false,
            shift_held: false,
            press_shift_held: false,
            shift_keys_down: Vec::new(),
            pointer_pressed: false,
            kb_interactivity: KeyboardInteractivity::None,
            pointer_x: 0.0,
            pointer_y: 0.0,
            pointer_seen: false,
            pool: None,
            pool_size: (0, 0),
            buffers: Vec::new(),
            last_committed_rect: None,
            last_input_rect: None,
            last_input_full: false,
            events: Vec::new(),
        };

        state.bind_layer_surface_on(&qh, None);

        event_queue
            .roundtrip(&mut state)
            .map_err(|e| format!("Wayland initial roundtrip failed: {e}"))?;
        if !state.configured {
            let _ = event_queue.roundtrip(&mut state);
        }

        Ok(Self {
            conn,
            event_queue,
            state,
        })
    }

    pub fn raw_fd(&self) -> RawFd {
        self.conn.as_fd().as_raw_fd()
    }

    pub fn is_configured(&self) -> bool {
        self.state.configured && self.state.width > 0 && self.state.height > 0
    }

    pub fn is_closed(&self) -> bool {
        self.state.closed
    }

    pub fn size(&self) -> (u32, u32) {
        (self.state.width, self.state.height)
    }

    pub fn last_pointer_pos(&self) -> Option<(f64, f64)> {
        if self.state.pointer_seen {
            Some((self.state.pointer_x, self.state.pointer_y))
        } else {
            None
        }
    }

    pub fn available_outputs(&self) -> Vec<OutputDescriptor> {
        self.state.outputs.iter().map(|o| o.desc.clone()).collect()
    }

    pub fn available_output_names(&self) -> Vec<String> {
        self.state.outputs.iter().map(|o| o.desc.name.clone()).collect()
    }

    pub fn active_output_name(&self) -> &str {
        &self.state.active_output_name
    }

    /// Recreate the layer surface explicitly bound to `target_name` (e.g. `DP-1`, `HDMI-A-1`).
    pub fn switch_output(&mut self, target_name: &str) -> bool {
        let Some(target) = self
            .state
            .outputs
            .iter()
            .find(|o| o.desc.name == target_name)
            .map(|o| (o.wl_output.clone(), o.desc.clone()))
        else {
            return false;
        };
        let qh = self.event_queue.handle();
        self.state.bind_layer_surface_on(&qh, Some(&target.0));
        self.state.active_output_name = target.1.name.clone();
        self.state.width = target.1.width.max(300) as u32;
        self.state.height = target.1.height.max(420) as u32;
        let _ = self.event_queue.roundtrip(&mut self.state);
        self.state.configured
    }

    /// Recover from an unplugged/disabled monitor by recreating the layer surface on a surviving output.
    pub fn recreate_on_fallback_output(&mut self) -> bool {
        if let Some(first_name) = self.state.outputs.first().map(|o| o.desc.name.clone()) {
            return self.switch_output(&first_name);
        }
        false
    }

    pub fn flush(&self) -> io::Result<()> {
        self.conn
            .flush()
            .map_err(|e| io::Error::new(io::ErrorKind::BrokenPipe, e.to_string()))
    }

    pub fn read_and_dispatch(&mut self) -> io::Result<Vec<StageEvent>> {
        if let Some(guard) = self.event_queue.prepare_read() {
            match guard.read() {
                Ok(_) => {}
                Err(wayland_client::backend::WaylandError::Io(ref e))
                    if e.kind() == io::ErrorKind::WouldBlock => {}
                Err(e) => {
                    return Err(io::Error::new(io::ErrorKind::BrokenPipe, e.to_string()));
                }
            }
        }
        self.event_queue
            .dispatch_pending(&mut self.state)
            .map_err(|e| io::Error::new(io::ErrorKind::BrokenPipe, e.to_string()))?;
        Ok(std::mem::take(&mut self.state.events))
    }

    pub fn drain_events(&mut self) -> Vec<StageEvent> {
        std::mem::take(&mut self.state.events)
    }

    /// Update click-through `wl_region` input mask: either full stage (while dragging or menu open)
    /// or tightly wrapped around Capri's visible sprite body (so empty margins & speech headroom stay click-through).
    pub fn update_input_region(&mut self, full_stage: bool, avatar_x: i32, avatar_y: i32) {
        let Some(ref ls) = self.state.layer_surface else {
            return;
        };
        let sw = self.state.width as i32;
        let sh = self.state.height as i32;
        let rect = if full_stage {
            (0, 0, sw, sh)
        } else {
            let x0 = (avatar_x + 56).clamp(0, (sw - 1).max(0));
            let y0 = (avatar_y + 72).clamp(0, (sh - 1).max(0));
            let x1 = (avatar_x + CANVAS_W as i32 - 56).clamp(x0 + 1, sw.max(1));
            let y1 = (avatar_y + CANVAS_H as i32 - 8).clamp(y0 + 1, sh.max(1));
            (x0, y0, (x1 - x0).max(1), (y1 - y0).max(1))
        };
        if self.state.last_input_full == full_stage && self.state.last_input_rect == Some(rect) {
            return;
        }
        if let Ok(region) = Region::new(&self.state.compositor_state) {
            region.add(rect.0, rect.1, rect.2.max(1), rect.3.max(1));
            ls.wl_surface().set_input_region(Some(region.wl_region()));
            self.state.last_input_full = full_stage;
            self.state.last_input_rect = Some(rect);
        }
    }

    /// Hide the avatar completely on non-home workspaces: empty `0x0` click-through input region
    /// and a clean transparent buffer commit so 100% of clicks pass through with 0 ghost pixels.
    pub fn clear_frame(&mut self) {
        if !self.is_configured() {
            return;
        }
        if let Some(ref ls) = self.state.layer_surface {
            if let Ok(empty_region) = Region::new(&self.state.compositor_state) {
                ls.wl_surface().set_input_region(Some(empty_region.wl_region()));
                self.state.last_input_full = false;
                self.state.last_input_rect = Some((0, 0, 0, 0));
            }
        }
        let w = self.state.width;
        let h = self.state.height;
        let Some(ref mut pool) = self.state.pool else {
            if let Some(ref ls) = self.state.layer_surface {
                ls.wl_surface().commit();
                let _ = self.conn.flush();
            }
            return;
        };
        let mut chosen_idx = None;
        for (idx, pbuf) in self.state.buffers.iter().enumerate() {
            if pool.canvas(&pbuf.buffer).is_some() {
                chosen_idx = Some(idx);
                break;
            }
        }
        let Some(idx) = chosen_idx else {
            if let Some(ref ls) = self.state.layer_surface {
                ls.wl_surface().commit();
                let _ = self.conn.flush();
            }
            return;
        };
        let pbuf = &mut self.state.buffers[idx];
        let Some(slice) = pool.canvas(&pbuf.buffer) else {
            return;
        };
        let prev = pbuf.prev_rect;
        if let Some((px, py, pw, ph)) = prev {
            let bw = w as i32;
            let bh = h as i32;
            let stride = (w as usize) * 4;
            let x0 = px.clamp(0, bw) as usize;
            let x1 = (px + pw).clamp(0, bw) as usize;
            let y0 = py.clamp(0, bh) as usize;
            let y1 = (py + ph).clamp(0, bh) as usize;
            if x1 > x0 {
                for y in y0..y1 {
                    let row = y * stride;
                    slice[row + x0 * 4..row + x1 * 4].fill(0);
                }
            }
        }
        pbuf.prev_rect = None;
        let damage = match (prev, self.state.last_committed_rect) {
            (Some(a), Some(b)) => union_rect(a, b),
            (Some(a), None) | (None, Some(a)) => a,
            (None, None) => (0, 0, 1, 1),
        };
        self.state.last_committed_rect = None;
        if let Some(ref ls) = self.state.layer_surface {
            let surf = ls.wl_surface();
            if pbuf.buffer.attach_to(surf).is_ok() {
                surf.damage_buffer(damage.0, damage.1, damage.2.max(1), damage.3.max(1));
                surf.commit();
                let _ = self.conn.flush();
            }
        }
    }

    /// Blit rendered 300x420 avatar `Pixmap` (and optional RMB menu overlay `Pixmap`) into one of
    /// two persistent double-buffered `wl_shm` slots with exact per-buffer dirty-rect clearing.
    pub fn present_frame(
        &mut self,
        canvas: &Pixmap,
        avatar_x: i32,
        avatar_y: i32,
        overlay: Option<(&Pixmap, i32, i32)>,
    ) -> bool {
        if !self.is_configured() {
            return false;
        }
        let w = self.state.width;
        let h = self.state.height;
        let stride = (w as i32) * 4;
        let frame_bytes = (w as usize) * (h as usize) * 4;

        if self.state.pool.is_none() || self.state.pool_size != (w, h) || self.state.buffers.is_empty() {
            self.state.buffers.clear();
            self.state.last_committed_rect = None;
            let Ok(mut pool) = SlotPool::new(frame_bytes * 2, &self.state.shm) else {
                return false;
            };
            let mut created = Vec::with_capacity(2);
            for _ in 0..2 {
                let Ok((buffer, _slice)) =
                    pool.create_buffer(w as i32, h as i32, stride, wl_shm::Format::Argb8888)
                else {
                    return false;
                };
                created.push(PersistentBuffer {
                    buffer,
                    prev_rect: None,
                });
            }
            self.state.pool = Some(pool);
            self.state.pool_size = (w, h);
            self.state.buffers = created;
        }

        let Some(ref mut pool) = self.state.pool else {
            return false;
        };

        let mut chosen_idx = None;
        for (idx, pbuf) in self.state.buffers.iter().enumerate() {
            if pool.canvas(&pbuf.buffer).is_some() {
                chosen_idx = Some(idx);
                break;
            }
        }
        let Some(idx) = chosen_idx else {
            return false;
        };

        let pbuf = &mut self.state.buffers[idx];
        let Some(slice) = pool.canvas(&pbuf.buffer) else {
            return false;
        };

        let buf_prev = pbuf.prev_rect;
        let mut new_rect = blit_canvas_to_wayland_argb8888(
            slice,
            w,
            h,
            canvas,
            avatar_x,
            avatar_y,
            buf_prev,
        );
        if let Some((ov_pm, ox, oy)) = overlay {
            let ov_rect = blit_overlay_over_argb8888(slice, w, h, ov_pm, ox, oy);
            new_rect = union_rect(new_rect, ov_rect);
        }
        pbuf.prev_rect = Some(new_rect);

        let mut damage = new_rect;
        if let Some(prev) = buf_prev {
            damage = union_rect(damage, prev);
        }
        if let Some(committed) = self.state.last_committed_rect {
            damage = union_rect(damage, committed);
        }
        self.state.last_committed_rect = Some(new_rect);

        if let Some(ref ls) = self.state.layer_surface {
            let surf = ls.wl_surface();
            if pbuf.buffer.attach_to(surf).is_err() {
                return false;
            }
            surf.damage_buffer(damage.0, damage.1, damage.2.max(1), damage.3.max(1));
            surf.commit();
            let _ = self.conn.flush();
            return true;
        }
        false
    }
}

/// Alpha-composite an overlay `Pixmap` (e.g. RMB menu) over an existing ARGB8888 buffer slice.
fn blit_overlay_over_argb8888(
    buf: &mut [u8],
    buf_w: u32,
    buf_h: u32,
    overlay: &Pixmap,
    offset_x: i32,
    offset_y: i32,
) -> (i32, i32, i32, i32) {
    let bw = buf_w as i32;
    let bh = buf_h as i32;
    let stride = (buf_w as usize) * 4;
    let cw = overlay.width() as i32;
    let ch = overlay.height() as i32;
    let src_pixels = overlay.pixels();

    let dst_x0 = offset_x.clamp(0, bw);
    let dst_y0 = offset_y.clamp(0, bh);
    let dst_x1 = (offset_x + cw).clamp(0, bw);
    let dst_y1 = (offset_y + ch).clamp(0, bh);

    if dst_x1 > dst_x0 && dst_y1 > dst_y0 {
        let copy_w = (dst_x1 - dst_x0) as usize;
        let src_x0 = (dst_x0 - offset_x) as usize;
        let dst_x0_bytes = (dst_x0 as usize) * 4;
        for dy in dst_y0..dst_y1 {
            let sy = (dy - offset_y) as usize;
            let dst_row = (dy as usize) * stride + dst_x0_bytes;
            let src_row = sy * (cw as usize) + src_x0;
            let dst_slice = &mut buf[dst_row..dst_row + copy_w * 4];
            let src_slice = &src_pixels[src_row..src_row + copy_w];
            let (dst_chunks, _) = dst_slice.as_chunks_mut::<4>();
            for (d, p) in dst_chunks.iter_mut().zip(src_slice.iter()) {
                let sa = p.alpha() as u16;
                if sa == 0 {
                    continue;
                }
                if sa == 255 {
                    *d = [p.blue(), p.green(), p.red(), 255];
                } else {
                    let inv = 255u16 - sa;
                    let ob = (p.blue() as u16 + (d[0] as u16 * inv + 127) / 255).min(255) as u8;
                    let og = (p.green() as u16 + (d[1] as u16 * inv + 127) / 255).min(255) as u8;
                    let or = (p.red() as u16 + (d[2] as u16 * inv + 127) / 255).min(255) as u8;
                    let oa = (sa + (d[3] as u16 * inv + 127) / 255).min(255) as u8;
                    *d = [ob, og, or, oa];
                }
            }
        }
    }

    (dst_x0, dst_y0, (dst_x1 - dst_x0).max(0), (dst_y1 - dst_y0).max(0))
}

fn union_rect(a: (i32, i32, i32, i32), b: (i32, i32, i32, i32)) -> (i32, i32, i32, i32) {
    let x0 = a.0.min(b.0);
    let y0 = a.1.min(b.1);
    let x1 = (a.0 + a.2).max(b.0 + b.2);
    let y1 = (a.1 + a.3).max(b.1 + b.3);
    (x0, y0, (x1 - x0).max(1), (y1 - y0).max(1))
}

/// Find an adjacent monitor touching the left (`side == -1`) or right (`side == +1`) seam
/// of `current_name` with vertical overlap.
pub fn find_adjacent_output(
    outputs: &[OutputDescriptor],
    current_name: &str,
    side: i8,
) -> Option<OutputDescriptor> {
    let cur = outputs.iter().find(|o| o.name == current_name)?;
    let cur_right = cur.x + cur.width;
    let cur_bottom = cur.y + cur.height;

    let mut best: Option<(i32, OutputDescriptor)> = None;
    for cand in outputs {
        if cand.name == cur.name {
            continue;
        }
        let cand_right = cand.x + cand.width;
        let cand_bottom = cand.y + cand.height;
        let v_overlap = cur_bottom.min(cand_bottom) - cur.y.max(cand.y);
        if v_overlap <= 0 {
            continue;
        }
        let gap = if side < 0 {
            cur.x - cand_right
        } else {
            cand.x - cur_right
        };
        if (-64..=64).contains(&gap) {
            let dist = gap.abs();
            if best.as_ref().is_none_or(|(bd, _)| dist < *bd) {
                best = Some((dist, cand.clone()));
            }
        }
    }
    best.map(|(_, o)| o)
}

/// Map an avatar's Y coordinate smoothly across monitors of potentially different heights.
pub fn map_y_between_outputs(y: f64, avatar_h: f64, src_h: f64, dst_h: f64) -> f64 {
    let src_span = (src_h - avatar_h).max(1.0);
    let dst_span = (dst_h - avatar_h).max(1.0);
    let frac = (y / src_span).clamp(0.0, 1.0);
    (frac * dst_span).clamp(0.0, dst_span)
}

/// Send a lightweight JSON command (`toggle`, `ask`, `voice_toggle`, `voice_stop`, `look_region`)
/// to running Capri Cyberdeck over `$XDG_RUNTIME_DIR/capri-cyberdeck.sock`.
pub fn send_cyberdeck_command(cmd: &str) -> bool {
    let sock_path = if let Ok(xdg) = std::env::var("XDG_RUNTIME_DIR") {
        let trimmed = xdg.trim();
        if !trimmed.is_empty() {
            format!("{trimmed}/capri-cyberdeck.sock")
        } else {
            format!("/tmp/capri-cyberdeck-{}.sock", unsafe { libc::getuid() })
        }
    } else {
        format!("/tmp/capri-cyberdeck-{}.sock", unsafe { libc::getuid() })
    };
    let Ok(mut stream) = UnixStream::connect(&sock_path) else {
        return false;
    };
    let _ = stream.set_write_timeout(Some(std::time::Duration::from_millis(150)));
    let payload = format!("{{\"cmd\":\"{}\"}}\n", cmd.trim().to_ascii_lowercase());
    stream.write_all(payload.as_bytes()).is_ok()
}

pub const POINTER_BTN_LEFT: u32 = BTN_LEFT;
pub const POINTER_BTN_RIGHT: u32 = BTN_RIGHT;

/// Non-blocking Hyprland `.socket2.sock` watcher integrated into the single `poll()` loop
/// (zero extra threads, instant workspace STAY/FOLLOW switching).
pub struct HyprlandWorkspaceWatcher {
    stream: UnixStream,
    buf: Vec<u8>,
}

impl HyprlandWorkspaceWatcher {
    pub fn connect() -> Option<Self> {
        let sig = std::env::var("HYPRLAND_INSTANCE_SIGNATURE").ok()?;
        let sig = sig.trim();
        if sig.is_empty() {
            return None;
        }
        let mut candidates = Vec::new();
        if let Ok(xdg) = std::env::var("XDG_RUNTIME_DIR") {
            let trimmed = xdg.trim();
            if !trimmed.is_empty() {
                candidates.push(PathBuf::from(trimmed).join("hypr").join(sig).join(".socket2.sock"));
            }
        }
        candidates.push(PathBuf::from("/tmp/hypr").join(sig).join(".socket2.sock"));
        for path in candidates {
            if let Ok(stream) = UnixStream::connect(&path) {
                if stream.set_nonblocking(true).is_ok() {
                    return Some(Self {
                        stream,
                        buf: Vec::with_capacity(512),
                    });
                }
            }
        }
        None
    }

    pub fn raw_fd(&self) -> RawFd {
        self.stream.as_raw_fd()
    }

    /// Read pending socket2 events and return the latest active workspace ID if changed.
    /// Returns `Err(())` if the socket disconnected.
    pub fn poll_workspace_change(&mut self) -> Result<Option<i64>, ()> {
        let mut tmp = [0u8; 2048];
        loop {
            match self.stream.read(&mut tmp) {
                Ok(0) => return Err(()),
                Ok(n) => self.buf.extend_from_slice(&tmp[..n]),
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(_) => return Err(()),
            }
        }
        let mut latest_ws: Option<i64> = None;
        while let Some(pos) = self.buf.iter().position(|&b| b == b'\n') {
            let line = String::from_utf8_lossy(&self.buf[..pos]).to_string();
            self.buf.drain(..=pos);
            if let Some(ws_id) = parse_hyprland_workspace_event(&line) {
                latest_ws = Some(ws_id);
            }
        }
        if self.buf.len() > 8192 {
            self.buf.clear();
        }
        Ok(latest_ws)
    }
}

pub fn parse_hyprland_workspace_event(line: &str) -> Option<i64> {
    let (ev, data) = line.split_once(">>")?;
    match ev.trim() {
        "workspacev2" => data.split(',').next()?.trim().parse::<i64>().ok(),
        "focusedmonv2" => data.split(',').nth(1)?.trim().parse::<i64>().ok(),
        "workspace" => data.trim().parse::<i64>().ok(),
        _ => None,
    }
}

pub fn query_active_hyprland_workspace_id() -> Option<i64> {
    std::env::var_os("HYPRLAND_INSTANCE_SIGNATURE")?;
    let out = Command::new("hyprctl")
        .args(["-j", "activeworkspace"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let val: serde_json::Value = serde_json::from_slice(&out.stdout).ok()?;
    val.get("id").and_then(serde_json::Value::as_i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adjacent_output_detection_and_y_mapping() {
        let outs = vec![
            OutputDescriptor {
                name: "DP-1".into(),
                x: 0,
                y: 0,
                width: 1920,
                height: 1080,
            },
            OutputDescriptor {
                name: "HDMI-A-1".into(),
                x: 1920,
                y: 0,
                width: 2560,
                height: 1440,
            },
        ];
        let right = find_adjacent_output(&outs, "DP-1", 1).expect("HDMI-A-1 is right of DP-1");
        assert_eq!(right.name, "HDMI-A-1");
        assert!(find_adjacent_output(&outs, "DP-1", -1).is_none());

        let left = find_adjacent_output(&outs, "HDMI-A-1", -1).expect("DP-1 is left of HDMI-A-1");
        assert_eq!(left.name, "DP-1");

        let mapped = map_y_between_outputs(330.0, 420.0, 1080.0, 1440.0);
        assert!((mapped - 510.0).abs() < 1e-3);
    }

    #[test]
    fn parses_hyprland_socket2_workspace_events() {
        assert_eq!(parse_hyprland_workspace_event("workspacev2>>4,4"), Some(4));
        assert_eq!(parse_hyprland_workspace_event("focusedmonv2>>DP-1,2"), Some(2));
        assert_eq!(parse_hyprland_workspace_event("workspace>>3"), Some(3));
        assert_eq!(parse_hyprland_workspace_event("activewindow>>kitty,agy"), None);
    }
}
