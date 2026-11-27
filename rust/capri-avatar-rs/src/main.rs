//! `capri-avatar-rs` — Experimental native Rust Wayland 2D Avatar Daemon for Capri.
//!
//! Talks over the exact same versioned (`v: 1`) per-user Unix socket IPC contract as
//! `capri/ui/overlay.py` (`$XDG_RUNTIME_DIR/capri/avatar.sock`, `0700`/`0600`, `flock`, `SO_PEERCRED`).

mod ipc;
mod menu;
mod physics;
mod protocol;
mod renderer;
mod sprites;
mod wayland;

use std::env;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use ipc::{
    ensure_avatar_runtime_dir, get_avatar_lock_path, get_avatar_pid_path, get_avatar_socket_path,
    safe_unlink_owned_path, write_owned_pid_file, AvatarIpcServer, AvatarSingletonLock,
};
use menu::{MenuAction, QuickMenu};
use physics::{query_window_shelves, AvatarPhysics2D, LandingSurface, MonitorWorkArea, PhysicsState};
use renderer::{AvatarRenderer, AvatarVisualState, LocomotionRequest, CANVAS_H, CANVAS_W};
use sprites::{resolve_bundled_sprites_dir, SpriteStore};
use wayland::{
    find_adjacent_output, map_y_between_outputs, send_cyberdeck_command, StageEvent, WaylandStage,
    POINTER_BTN_LEFT, POINTER_BTN_RIGHT,
};

static SHUTDOWN_FLAG: AtomicBool = AtomicBool::new(false);

extern "C" fn handle_signal(_sig: libc::c_int) {
    SHUTDOWN_FLAG.store(true, Ordering::SeqCst);
}

struct CliArgs {
    parent_pid: Option<u32>,
    systemd: bool,
    wait_wayland: Option<f64>,
    sprites_dir: Option<PathBuf>,
    headless: bool,
    bench_first_frame: bool,
}

fn parse_args() -> CliArgs {
    let mut args = CliArgs {
        parent_pid: None,
        systemd: false,
        wait_wayland: None,
        sprites_dir: None,
        headless: false,
        bench_first_frame: false,
    };
    let mut it = env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--version" | "-v" => {
                println!("capri-avatar-rs v{}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            "--help" | "-h" => {
                println!(
                    "capri-avatar-rs v{} — Native Rust Wayland zwlr_layer_shell_v1 2D Avatar\n\
                     Options:\n\
                       --mode <2d>            Ignored (2D only, for capri-avatar CLI parity)\n\
                       --parent-pid <PID>     Exit automatically when parent process exits\n\
                       --systemd              Wait up to 15s for Wayland socket on startup\n\
                       --wait-wayland <SEC>   Seconds to wait for Wayland socket\n\
                       --sprites-dir <PATH>   Override bundled sprites_v2 directory\n\
                       --headless             Run IPC + offscreen software renderer (no Wayland)\n\
                       --bench-first-frame    Print FIRST_FRAME_MS=<ms> after first frame commit",
                    env!("CARGO_PKG_VERSION")
                );
                std::process::exit(0);
            }
            "--mode" => {
                let _ = it.next();
            }
            "--parent-pid" => {
                if let Some(v) = it.next() {
                    args.parent_pid = v.parse::<u32>().ok();
                }
            }
            "--systemd" => args.systemd = true,
            "--wait-wayland" => {
                if let Some(v) = it.next() {
                    args.wait_wayland = v.parse::<f64>().ok();
                }
            }
            "--sprites-dir" => {
                if let Some(v) = it.next() {
                    args.sprites_dir = Some(PathBuf::from(v));
                }
            }
            "--headless" => args.headless = true,
            "--bench-first-frame" => args.bench_first_frame = true,
            _ => {}
        }
    }
    args
}

fn wait_for_wayland_socket(timeout_s: f64) -> bool {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_s.max(0.0));
    let rdir = env::var("XDG_RUNTIME_DIR")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("/run/user/{}", unsafe { libc::getuid() })));

    loop {
        if let Ok(wd) = env::var("WAYLAND_DISPLAY") {
            let trimmed = wd.trim();
            if !trimmed.is_empty() {
                let cand = if Path::new(trimmed).is_absolute() {
                    PathBuf::from(trimmed)
                } else {
                    rdir.join(trimmed)
                };
                if std::os::unix::net::UnixStream::connect(&cand).is_ok() {
                    return true;
                }
            }
        }
        if let Ok(entries) = std::fs::read_dir(&rdir) {
            for ent in entries.flatten() {
                let name = ent.file_name();
                let name_str = name.to_string_lossy();
                if name_str.starts_with("wayland-")
                    && !name_str.ends_with(".lock")
                    && std::os::unix::net::UnixStream::connect(ent.path()).is_ok()
                {
                    env::set_var("WAYLAND_DISPLAY", name_str.as_ref());
                    return true;
                }
            }
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn parent_is_alive(pid: u32) -> bool {
    // SAFETY: kill(pid, 0) only probes process existence.
    let rc = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if rc == 0 {
        return true;
    }
    let err = std::io::Error::last_os_error();
    err.raw_os_error() == Some(libc::EPERM)
}

fn install_signal_handlers() {
    unsafe {
        libc::signal(libc::SIGTERM, handle_signal as *const () as usize);
        libc::signal(libc::SIGINT, handle_signal as *const () as usize);
    }
}

fn main() {
    let start_instant = Instant::now();
    let args = parse_args();
    install_signal_handlers();

    if args.parent_pid.is_some() {
        unsafe {
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
        }
    }

    let wait_sec = args
        .wait_wayland
        .unwrap_or(if args.systemd { 15.0 } else { 0.0 });
    if !args.headless && wait_sec > 0.0 && !wait_for_wayland_socket(wait_sec) {
        eprintln!("capri-avatar-rs: Wayland socket not ready after {wait_sec:.1}s");
        std::process::exit(1);
    }

    let sock_path = get_avatar_socket_path();
    let pid_path = get_avatar_pid_path();
    if let Some(p) = sock_path.parent() {
        if let Err(e) = ensure_avatar_runtime_dir(p) {
            eprintln!("capri-avatar-rs: runtime dir error: {e}");
            std::process::exit(1);
        }
    }
    if let Some(p) = pid_path.parent() {
        if let Err(e) = ensure_avatar_runtime_dir(p) {
            eprintln!("capri-avatar-rs: pid dir error: {e}");
            std::process::exit(1);
        }
    }

    let lock_path = get_avatar_lock_path(&pid_path);
    let _singleton_lock = match AvatarSingletonLock::acquire(&lock_path) {
        Ok(Some(lock)) => lock,
        Ok(None) => std::process::exit(0),
        Err(e) => {
            eprintln!("capri-avatar-rs: failed to acquire lock: {e}");
            std::process::exit(1);
        }
    };

    if let Err(e) = write_owned_pid_file(&pid_path, std::process::id()) {
        eprintln!("capri-avatar-rs: failed to write pid file: {e}");
        std::process::exit(1);
    }

    let mut ipc_server = match AvatarIpcServer::bind(sock_path.clone()) {
        Ok(srv) => srv,
        Err(e) => {
            eprintln!("capri-avatar-rs: failed to bind IPC socket {}: {e}", sock_path.display());
            safe_unlink_owned_path(&pid_path);
            std::process::exit(1);
        }
    };

    let sprites_dir = resolve_bundled_sprites_dir(args.sprites_dir.as_deref());
    let mut sprites = SpriteStore::new(sprites_dir, None);
    let mut renderer = AvatarRenderer::new();
    let mut vis = AvatarVisualState::default();
    let mut menu = QuickMenu::new();

    let mut stage = if args.headless {
        None
    } else {
        match WaylandStage::connect("capri-avatar") {
            Ok(s) => Some(s),
            Err(e) => {
                eprintln!("capri-avatar-rs: {e}");
                safe_unlink_owned_path(&pid_path);
                std::process::exit(1);
            }
        }
    };

    let (init_w, init_h) = stage.as_ref().map(WaylandStage::size).unwrap_or((1920, 1080));
    let mut monitor = MonitorWorkArea::from_size(init_w.max(300), init_h.max(420));
    let init_x = (monitor.right() - CANVAS_W as f64 - 24.0).max(0.0);
    let init_y = (monitor.bottom() - CANVAS_H as f64).max(0.0);
    let mut physics = AvatarPhysics2D::new(CANVAS_W as f64, CANVAS_H as f64, init_x, init_y);
    let mut shelves: Vec<LandingSurface> = Vec::new();

    // Drag gesture tracking
    let mut dragging = false;
    let mut drag_grab_offset = (0.0f64, 0.0f64);
    let mut drag_start_pos = (init_x, init_y);
    let mut drag_start_at = Instant::now();
    let mut drag_distance = 0.0f64;
    let mut poke_count: u32 = 0;
    let mut last_poke_at: Option<Instant> = None;

    let mut last_frame_at = Instant::now();
    let mut last_parent_check = Instant::now();
    let mut tick_accum: f64 = 0.0;
    let mut first_frame_reported = false;
    let mut force_redraw = false;
    let mut compositor_disconnected = false;

    // Render and commit first frame immediately at startup!
    {
        let pm = renderer.render(&vis, &physics, &mut sprites);
        if let Some(ref mut st) = stage {
            let ax = physics.x.round() as i32;
            let ay = physics.y.round() as i32;
            st.update_input_region(false, ax, ay);
            if st.present_frame(pm, ax, ay, None) && args.bench_first_frame && !first_frame_reported
            {
                first_frame_reported = true;
                println!("FIRST_FRAME_MS={:.2}", start_instant.elapsed().as_secs_f64() * 1000.0);
            }
        } else if args.bench_first_frame && !first_frame_reported {
            first_frame_reported = true;
            println!("FIRST_FRAME_MS={:.2}", start_instant.elapsed().as_secs_f64() * 1000.0);
        }
    }

    while !SHUTDOWN_FLAG.load(Ordering::SeqCst) {
        if let Some(ppid) = args.parent_pid {
            if last_parent_check.elapsed() >= Duration::from_millis(800) {
                last_parent_check = Instant::now();
                if !parent_is_alive(ppid) {
                    break;
                }
            }
        }

        if let Some(ref st) = stage {
            if st.is_closed() || st.flush().is_err() {
                compositor_disconnected = true;
                break;
            }
        }

        // Compute adaptive poll timeout until next scheduled animation frame
        let interval_ms = vis.target_frame_interval_ms(&physics);
        let timeout_ms: i32 = if force_redraw {
            0
        } else if let Some(iv) = interval_ms {
            let elapsed_ms = last_frame_at.elapsed().as_millis() as u64;
            iv.saturating_sub(elapsed_ms).min(500) as i32
        } else {
            500
        };

        let mut pfds: Vec<libc::pollfd> = Vec::with_capacity(8);
        let wl_fd = stage.as_ref().map(WaylandStage::raw_fd);
        if let Some(fd) = wl_fd {
            pfds.push(libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            });
        }
        let listener_fd = ipc_server.listener_fd();
        pfds.push(libc::pollfd {
            fd: listener_fd,
            events: libc::POLLIN,
            revents: 0,
        });
        let client_fds = ipc_server.client_fds();
        for &cfd in &client_fds {
            pfds.push(libc::pollfd {
                fd: cfd,
                events: libc::POLLIN,
                revents: 0,
            });
        }

        let poll_rc = unsafe { libc::poll(pfds.as_mut_ptr(), pfds.len() as libc::nfds_t, timeout_ms) };
        if poll_rc < 0 {
            let err = std::io::Error::last_os_error();
            if err.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            break;
        }

        // 1. Dispatch Wayland events
        let mut stage_events: Vec<StageEvent> = Vec::new();
        if let Some(ref mut st) = stage {
            stage_events.extend(st.drain_events());
            if let Some(wfd) = wl_fd {
                if pfds.iter().any(|p| p.fd == wfd && (p.revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR)) != 0) {
                    match st.read_and_dispatch() {
                        Ok(evs) => stage_events.extend(evs),
                        Err(_) => {
                            compositor_disconnected = true;
                            break;
                        }
                    }
                }
            }
        }

        for ev in stage_events {
            match ev {
                StageEvent::Closed => {
                    compositor_disconnected = true;
                    SHUTDOWN_FLAG.store(true, Ordering::SeqCst);
                }
                StageEvent::HotplugFallbackNeeded => {
                    menu.close();
                    dragging = false;
                    physics.cancel_grab();
                    if let Some(ref mut st) = stage {
                        if st.recreate_on_fallback_output() {
                            let (nw, nh) = st.size();
                            monitor = MonitorWorkArea::from_size(nw, nh);
                            physics.clamp_to_monitor(monitor);
                            shelves.clear();
                            force_redraw = true;
                        } else {
                            compositor_disconnected = true;
                            SHUTDOWN_FLAG.store(true, Ordering::SeqCst);
                        }
                    }
                }
                StageEvent::OutputSwitched => {
                    if let Some(ref st) = stage {
                        let (nw, nh) = st.size();
                        if nw > 0 && nh > 0 {
                            monitor = MonitorWorkArea::from_size(nw, nh);
                            physics.clamp_to_monitor(monitor);
                        }
                    }
                    force_redraw = true;
                }
                StageEvent::Configured { width, height } => {
                    monitor = MonitorWorkArea::from_size(width, height);
                    physics.clamp_to_monitor(monitor);
                    force_redraw = true;
                }
                StageEvent::PointerPress { x, y, button } => {
                    if menu.visible {
                        if button == POINTER_BTN_LEFT {
                            if let Some(action) = menu.on_click(x, y) {
                                handle_menu_action(
                                    action,
                                    &mut vis,
                                    &mut physics,
                                    &mut sprites,
                                    &mut stage,
                                    &mut monitor,
                                    &mut shelves,
                                );
                            }
                            if let Some(ref mut st) = stage {
                                st.update_input_region(false, physics.x.round() as i32, physics.y.round() as i32);
                            }
                            force_redraw = true;
                            continue;
                        } else if button == POINTER_BTN_RIGHT {
                            menu.close();
                            if let Some(ref mut st) = stage {
                                st.update_input_region(false, physics.x.round() as i32, physics.y.round() as i32);
                            }
                            force_redraw = true;
                            continue;
                        }
                    }

                    if button == POINTER_BTN_LEFT {
                        let now_sec = start_instant.elapsed().as_secs_f64();
                        dragging = true;
                        drag_grab_offset = (x - physics.x, y - physics.y);
                        drag_start_pos = (physics.x, physics.y);
                        drag_start_at = Instant::now();
                        drag_distance = 0.0;
                        physics.begin_grab(now_sec);
                        vis.wake_up(&mut sprites);
                        if let Some(ref mut st) = stage {
                            st.update_input_region(true, physics.x.round() as i32, physics.y.round() as i32);
                        }
                        force_redraw = true;
                    } else if button == POINTER_BTN_RIGHT {
                        let (sw, sh) = stage.as_ref().map(WaylandStage::size).unwrap_or((1920, 1080));
                        let outs = stage
                            .as_ref()
                            .map(WaylandStage::available_output_names)
                            .unwrap_or_default();
                        let active_out = stage
                            .as_ref()
                            .map(WaylandStage::active_output_name)
                            .unwrap_or("")
                            .to_string();
                        menu.open(
                            x,
                            y,
                            sw,
                            sh,
                            &vis.pose,
                            vis.dance_active,
                            physics.physics_enabled,
                            &outs,
                            &active_out,
                        );
                        if let Some(ref mut st) = stage {
                            st.update_input_region(true, physics.x.round() as i32, physics.y.round() as i32);
                        }
                        force_redraw = true;
                    }
                }
                StageEvent::PointerMove { x, y, at } => {
                    if menu.visible {
                        if menu.on_pointer_move(x, y) {
                            force_redraw = true;
                        }
                        continue;
                    }
                    if dragging {
                        let target_x = x - drag_grab_offset.0;
                        let target_y = y - drag_grab_offset.1;
                        let prev_x = physics.x;
                        let prev_y = physics.y;
                        let ts = at.duration_since(start_instant).as_secs_f64();
                        physics.observe_drag(target_x, target_y, ts, monitor);
                        drag_distance += (physics.x - prev_x).hypot(physics.y - prev_y);
                        if drag_distance >= 8.0 && vis.pose != "normal" {
                            vis.pose = "normal".to_string();
                        }
                        force_redraw = true;
                    }
                }
                StageEvent::PointerRelease { button, shift } => {
                    if button == POINTER_BTN_LEFT && dragging {
                        dragging = false;
                        let elapsed = drag_start_at.elapsed().as_secs_f64();
                        let moved = drag_distance
                            .max((physics.x - drag_start_pos.0).hypot(physics.y - drag_start_pos.1));
                        if moved < 6.0 && elapsed < 0.38 {
                            physics.vx = 0.0;
                            physics.vy = 0.0;
                            physics.state = if physics.pinned {
                                PhysicsState::Pinned
                            } else {
                                PhysicsState::Idle
                            };
                            if vis.reactions_enabled && !vis.power_saver_active {
                                let now = Instant::now();
                                if last_poke_at
                                    .map(|t| now.duration_since(t) <= Duration::from_secs(5))
                                    .unwrap_or(false)
                                {
                                    poke_count += 1;
                                } else {
                                    poke_count = 1;
                                }
                                last_poke_at = Some(now);
                                if poke_count >= 3 {
                                    vis.trigger_glitch(&mut sprites);
                                    vis.show_ephemeral_caption("Эй, хватит тыкать в энграмму!", 210);
                                } else {
                                    vis.show_ephemeral_caption("Хм? Я на связи.", 180);
                                }
                            }
                        } else {
                            shelves = query_window_shelves(monitor);
                            let now_sec = start_instant.elapsed().as_secs_f64();
                            let evs = physics.end_grab(shift, now_sec, monitor, &shelves);
                            if evs.perched {
                                vis.pose = "perch".to_string();
                            }
                        }
                        if let Some(ref mut st) = stage {
                            st.update_input_region(menu.visible, physics.x.round() as i32, physics.y.round() as i32);
                        }
                        force_redraw = true;
                    }
                }
            }
        }

        // 2. Dispatch Unix IPC events
        if pfds.iter().any(|p| p.fd == listener_fd && (p.revents & libc::POLLIN) != 0) {
            ipc_server.accept_pending();
        }
        let mut quit_requested = false;
        for &cfd in &client_fds {
            if pfds.iter().any(|p| p.fd == cfd && (p.revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR)) != 0) {
                for cmd in ipc_server.read_client(cfd) {
                    let is_audio_tick = matches!(cmd, protocol::IpcCommand::AudioLevel { .. });
                    if vis.apply_ipc_command(cmd, &mut sprites, &mut physics) {
                        quit_requested = true;
                    }
                    if !is_audio_tick {
                        force_redraw = true;
                    }
                }
            }
        }
        if quit_requested {
            break;
        }

        // Resolve any pending high-level locomotion command (`come`, `home`) with current monitor/pointer
        if let Some(req) = vis.pending_locomotion.take() {
            match req {
                LocomotionRequest::Home => {
                    physics.x = (monitor.right() - CANVAS_W as f64 - 24.0).max(monitor.left);
                    physics.y = (monitor.bottom() - CANVAS_H as f64).max(monitor.top);
                    physics.vx = 0.0;
                    physics.vy = 0.0;
                    physics.state = PhysicsState::Idle;
                    force_redraw = true;
                }
                LocomotionRequest::Come { target_x, target_y } => {
                    let (cx, cy) = match (target_x, target_y) {
                        (Some(tx), Some(ty)) => (tx, ty),
                        _ => stage
                            .as_ref()
                            .and_then(WaylandStage::last_pointer_pos)
                            .map(|(px, py)| (px - CANVAS_W as f64 * 0.5, py - CANVAS_H as f64 * 0.5))
                            .unwrap_or((
                                monitor.left + (monitor.width - CANVAS_W as f64) * 0.5,
                                monitor.bottom() - CANVAS_H as f64,
                            )),
                    };
                    physics.x = cx;
                    physics.y = cy;
                    physics.clamp_to_monitor(monitor);
                    physics.vx = 0.0;
                    physics.vy = 0.0;
                    physics.state = PhysicsState::Idle;
                    force_redraw = true;
                }
            }
        }

        // 3. Advance physics & animation and commit Wayland frame on adaptive schedule
        let now = Instant::now();
        let dt_secs = now.duration_since(last_frame_at).as_secs_f64();
        let due = match interval_ms {
            Some(iv) => dt_secs * 1000.0 >= (iv as f64) * 0.9,
            None => false,
        };
        if force_redraw || due {
            last_frame_at = now;
            tick_accum = (tick_accum + dt_secs * 60.0).min(16.0);
            let whole_ticks = tick_accum.floor() as u64;
            if whole_ticks > 0 {
                tick_accum -= whole_ticks as f64;
            }

            if !dragging {
                let (adj_left, adj_right) = if let Some(ref st) = stage {
                    let outs = st.available_outputs();
                    let cur_name = st.active_output_name();
                    (
                        find_adjacent_output(&outs, cur_name, -1).is_some(),
                        find_adjacent_output(&outs, cur_name, 1).is_some(),
                    )
                } else {
                    (false, false)
                };

                let evs = physics.step(dt_secs, monitor, &shelves, adj_left, adj_right);
                if evs.crossed_monitor_side != 0 {
                    if let Some(ref mut st) = stage {
                        let outs = st.available_outputs();
                        let cur_name = st.active_output_name().to_string();
                        if let Some(neighbor) =
                            find_adjacent_output(&outs, &cur_name, evs.crossed_monitor_side)
                        {
                            let old_h = monitor.height;
                            if st.switch_output(&neighbor.name) {
                                let (nw, nh) = st.size();
                                monitor = MonitorWorkArea::from_size(nw, nh);
                                physics.y = map_y_between_outputs(
                                    physics.y,
                                    CANVAS_H as f64,
                                    old_h,
                                    monitor.height,
                                );
                                physics.x = if evs.crossed_monitor_side < 0 {
                                    (monitor.right() - CANVAS_W as f64 - 8.0).max(monitor.left)
                                } else {
                                    (monitor.left + 8.0).min(monitor.right() - CANVAS_W as f64)
                                };
                                shelves = query_window_shelves(monitor);
                            }
                        }
                    }
                }
                if evs.wall_hit && vis.reactions_enabled && !vis.power_saver_active {
                    vis.trigger_glitch(&mut sprites);
                }
                if evs.perched {
                    vis.pose = "perch".to_string();
                }
            }
            if whole_ticks > 0 {
                let phys_active = matches!(
                    physics.state,
                    PhysicsState::Grabbed
                        | PhysicsState::Falling
                        | PhysicsState::WallSplat
                        | PhysicsState::Landing
                );
                vis.advance(whole_ticks, phys_active, &mut sprites);
            }

            let menu_overlay = menu.render(renderer.ui_font());
            // Re-render avatar canvas and blit both avatar + optional menu onto persistent wl_shm slot
            let menu_owned = menu_overlay.map(|(pm, mx, my)| (pm.clone(), mx, my));
            let pm = renderer.render(&vis, &physics, &mut sprites);
            if let Some(ref mut st) = stage {
                let ax = physics.x.round() as i32;
                let ay = physics.y.round() as i32;
                st.update_input_region(dragging || menu.visible, ax, ay);
                let ov_ref = menu_owned.as_ref().map(|(p, mx, my)| (p, *mx, *my));
                let committed = st.present_frame(pm, ax, ay, ov_ref);
                if committed && args.bench_first_frame && !first_frame_reported {
                    first_frame_reported = true;
                    println!("FIRST_FRAME_MS={:.2}", start_instant.elapsed().as_secs_f64() * 1000.0);
                }
            }
            force_redraw = false;
        }
    }

    drop(ipc_server);
    safe_unlink_owned_path(&pid_path);
    if compositor_disconnected && args.systemd {
        std::process::exit(1);
    }
}

fn handle_menu_action(
    action: MenuAction,
    vis: &mut AvatarVisualState,
    physics: &mut AvatarPhysics2D,
    sprites: &mut SpriteStore,
    stage: &mut Option<WaylandStage>,
    monitor: &mut MonitorWorkArea,
    shelves: &mut Vec<LandingSurface>,
) {
    match action {
        MenuAction::SetPose(pose) => {
            vis.dance_active = false;
            if vis.pose == "sleep" && pose != "sleep" {
                vis.wake_up(sprites);
            }
            vis.pose = pose;
            vis.idle_ticks = 0;
        }
        MenuAction::ToggleDance => {
            vis.wake_up(sprites);
            vis.dance_active = !vis.dance_active;
        }
        MenuAction::TogglePhysics => {
            physics.set_physics_enabled(!physics.physics_enabled);
        }
        MenuAction::SwitchOutput(name) => {
            if let Some(ref mut st) = stage {
                if st.switch_output(&name) {
                    let (nw, nh) = st.size();
                    *monitor = MonitorWorkArea::from_size(nw, nh);
                    physics.clamp_to_monitor(*monitor);
                    shelves.clear();
                }
            }
        }
        MenuAction::QuickAsk => {
            if !send_cyberdeck_command("ask") {
                vis.show_ephemeral_caption("Запусти Cyberdeck для быстрого вопроса.", 180);
            }
        }
        MenuAction::ToggleCyberdeck => {
            if !send_cyberdeck_command("toggle") {
                vis.show_ephemeral_caption("Cyberdeck не запущен (capri-deck).", 180);
            }
        }
        MenuAction::ToggleVoice => {
            let cmd = if matches!(vis.state.as_str(), "listening" | "speaking") {
                "voice_stop"
            } else {
                "voice_toggle"
            };
            if !send_cyberdeck_command(cmd) {
                vis.show_ephemeral_caption("Запусти Cyberdeck для голосового ввода.", 180);
            }
        }
        MenuAction::Quit => {
            SHUTDOWN_FLAG.store(true, Ordering::SeqCst);
        }
    }
}
