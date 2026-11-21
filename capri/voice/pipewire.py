"""
CAPRI // PipeWire Audio, Acoustic Echo Cancellation (AEC) & Mic Calibration (v3.0 — Wave 5).

Provides a clean, zero-Qt PipeWire control layer reused by both:
- Stage 2: PipeWire `module-echo-cancel` (WebRTC AEC), input device selection, and
  guided 3-second microphone calibration for Hands-Free Barge-In.
- Stage 3: Virtual PipeWire sink/source routing for Join Voice.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

PIPEWIRE_AEC_SOURCE_NAME = "capri_aec_source"
PIPEWIRE_AEC_SINK_NAME = "capri_aec_sink"
PIPEWIRE_CONF_DIR = Path.home() / ".config" / "pipewire" / "pipewire.conf.d"
PIPEWIRE_AEC_CONF_FILE = PIPEWIRE_CONF_DIR / "99-capri-aec.conf"

# Tracks exact PulseAudio/PipeWire module IDs loaded by Capri during this process lifetime
_CAPRI_OWNED_AEC_MODULE_IDS: set[str] = set()


@dataclass(frozen=True)
class PipeWireAudioSource:
    index: str
    name: str
    driver: str = ""
    spec: str = ""
    state: str = ""
    is_monitor: bool = False
    is_aec: bool = False


@dataclass
class PipeWireStatus:
    available: bool = False
    is_pipewire: bool = False
    server_name: str = ""
    default_sink: str = ""
    default_source: str = ""
    aec_active: bool = False
    aec_source_name: str = ""
    sources: list[PipeWireAudioSource] = field(default_factory=list)


@dataclass(frozen=True)
class CalibrationResult:
    noise_rms: float
    speech_rms: float
    recommended_threshold: float
    sample_count: int = 0
    note: str = ""


def parse_pactl_info(raw: str) -> tuple[str, str, str, bool]:
    """Parse `pactl info` output into `(server_name, default_sink, default_source, is_pipewire)`."""
    server_name = ""
    default_sink = ""
    default_source = ""
    for line in (raw or "").splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        k = key.strip().lower()
        v = val.strip()
        if k == "server name":
            server_name = v
        elif k == "default sink":
            default_sink = v
        elif k == "default source":
            default_source = v
    is_pw = "pipewire" in server_name.lower()
    return server_name, default_sink, default_source, is_pw


def parse_pactl_sources(raw: str) -> list[PipeWireAudioSource]:
    """Parse `pactl list short sources` tab-separated output."""
    sources: list[PipeWireAudioSource] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split()
        if len(parts) < 2:
            continue
        idx = parts[0].strip()
        name = parts[1].strip()
        driver = parts[2].strip() if len(parts) > 2 else ""
        spec = parts[3].strip() if len(parts) > 3 else ""
        state = parts[4].strip() if len(parts) > 4 else ""
        name_l = name.lower()
        is_mon = name_l.endswith(".monitor")
        is_aec = (not is_mon) and any(
            tok in name_l for tok in ("echo-cancel", "echo_cancel", "capri_aec", "aec_source", ".aec")
        )
        sources.append(
            PipeWireAudioSource(
                index=idx,
                name=name,
                driver=driver,
                spec=spec,
                state=state,
                is_monitor=is_mon,
                is_aec=is_aec,
            )
        )
    return sources


def _run_pactl(args: list[str], timeout: float = 1.8) -> tuple[int, str, str]:
    if not shutil.which("pactl"):
        return -1, "", "pactl not found"
    try:
        proc = subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return -1, "", str(e)


def get_pipewire_status() -> PipeWireStatus:
    """Inspect active PipeWire / PulseAudio server, sources, and AEC status. Never raises."""
    code_info, out_info, _ = _run_pactl(["info"])
    if code_info != 0:
        return PipeWireStatus(available=False)

    server_name, default_sink, default_source, is_pw = parse_pactl_info(out_info)
    code_src, out_src, _ = _run_pactl(["list", "short", "sources"])
    sources = parse_pactl_sources(out_src) if code_src == 0 else []

    aec_src = next((s.name for s in sources if s.is_aec), "")
    return PipeWireStatus(
        available=True,
        is_pipewire=is_pw,
        server_name=server_name,
        default_sink=default_sink,
        default_source=default_source,
        aec_active=bool(aec_src),
        aec_source_name=aec_src,
        sources=sources,
    )


def generate_pipewire_aec_conf() -> str:
    """Return persistent PipeWire native WebRTC echo-cancel config snippet."""
    return (
        "# CAPRI // PipeWire WebRTC Acoustic Echo Cancellation (AEC)\n"
        "# Place in ~/.config/pipewire/pipewire.conf.d/99-capri-aec.conf and restart pipewire.\n"
        "context.modules = [\n"
        "  { name = libpipewire-module-echo-cancel\n"
        "    args = {\n"
        "      library.name  = aec/libspa-aec-webrtc\n"
        "      node.latency  = 1024/48000\n"
        "      aec.args = {\n"
        "        webrtc.gain_control = true\n"
        "        webrtc.noise_suppression = true\n"
        "        webrtc.voice_detection = true\n"
        "      }\n"
        "      capture.props = {\n"
        "        node.name = \"capri_aec_capture\"\n"
        "      }\n"
        "      source.props = {\n"
        f"        node.name = \"{PIPEWIRE_AEC_SOURCE_NAME}\"\n"
        "        node.description = \"Capri Echo-Cancelled Mic\"\n"
        "      }\n"
        "      sink.props = {\n"
        f"        node.name = \"{PIPEWIRE_AEC_SINK_NAME}\"\n"
        "        node.description = \"Capri Echo-Cancelled Output\"\n"
        "      }\n"
        "      playback.props = {\n"
        "        node.name = \"capri_aec_playback\"\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "]\n"
    )


def enable_pipewire_aec(
    source_master: str = "",
    sink_master: str = "",
    set_as_default: bool = False,
) -> tuple[bool, str]:
    """
    Enable runtime WebRTC Acoustic Echo Cancellation via PipeWire/Pulse `module-echo-cancel`.
    Idempotent: if an AEC source is already present, reuses it without duplicating modules.
    """
    status = get_pipewire_status()
    if not status.available:
        return False, "Сервер PipeWire / PulseAudio (`pactl`) недоступен."
    if status.aec_active:
        if set_as_default and status.aec_source_name:
            _run_pactl(["set-default-source", status.aec_source_name])
        return True, f"PipeWire AEC уже активен (`{status.aec_source_name}`)."

    args = [
        "load-module",
        "module-echo-cancel",
        "use_master_format=1",
        "aec_method=webrtc",
        f"source_name={PIPEWIRE_AEC_SOURCE_NAME}",
        f"sink_name={PIPEWIRE_AEC_SINK_NAME}",
    ]
    if source_master.strip():
        args.append(f"source_master={source_master.strip()}")
    if sink_master.strip():
        args.append(f"sink_master={sink_master.strip()}")

    code, out, err = _run_pactl(args, timeout=2.5)
    if code != 0:
        detail = (err or out).strip() or "module-echo-cancel не поддерживается текущей сборкой"
        return (
            False,
            f"Не удалось загрузить `module-echo-cancel` через pactl ({detail}). "
            f"Для постоянного AEC можно сохранить конфиг `{PIPEWIRE_AEC_CONF_FILE}`.",
        )

    loaded_id = (out or "").strip()
    if loaded_id.isdigit():
        _CAPRI_OWNED_AEC_MODULE_IDS.add(loaded_id)

    refreshed = get_pipewire_status()
    aec_name = refreshed.aec_source_name or PIPEWIRE_AEC_SOURCE_NAME
    if set_as_default and aec_name:
        _run_pactl(["set-default-source", aec_name])
    return True, f"PipeWire WebRTC AEC активирован: источник `{aec_name}`."


def disable_pipewire_aec() -> tuple[bool, str]:
    """
    Unload ONLY `module-echo-cancel` modules strictly owned by Capri
    (matching tracked module IDs or explicit `capri_aec_source`/`capri_aec_sink` args).
    Never touches foreign AEC modules loaded by other applications.
    """
    code, out, err = _run_pactl(["list", "short", "modules"])
    if code != 0:
        return False, f"Не удалось прочитать модули PipeWire: {(err or out).strip()}"

    unloaded: list[str] = []
    foreign_aec_found = False
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        mod_id = parts[0].strip()
        mod_name = parts[1].strip()
        mod_args = parts[2].strip() if len(parts) > 2 else ""
        if mod_name != "module-echo-cancel":
            continue
        is_capri_owned = (
            mod_id in _CAPRI_OWNED_AEC_MODULE_IDS
            or PIPEWIRE_AEC_SOURCE_NAME in mod_args
            or PIPEWIRE_AEC_SINK_NAME in mod_args
        )
        if is_capri_owned:
            ucode, _, _ = _run_pactl(["unload-module", mod_id])
            if ucode == 0:
                unloaded.append(mod_id)
                _CAPRI_OWNED_AEC_MODULE_IDS.discard(mod_id)
        else:
            foreign_aec_found = True

    if unloaded:
        return True, f"PipeWire AEC Capri выгружен (модулей отключено: {len(unloaded)})."
    if foreign_aec_found:
        return (
            False,
            "Обнаружен сторонний `module-echo-cancel` (создан не Capri) — выгрузка пропущена, чтобы не затронуть чужую обработку звука.",
        )
    return True, "Активных модулей PipeWire AEC Capri не обнаружено."


def list_input_devices() -> list[tuple[str, str]]:
    """
    Return `(device_key, display_label)` options for UI microphone selection.
    Always includes `("", "Системный микрофон по умолчанию")`.
    """
    items: list[tuple[str, str]] = [("", "Системный микрофон по умолчанию (Auto)")]
    seen: set[str] = {""}

    pw = get_pipewire_status()
    for src in pw.sources:
        if src.is_monitor:
            continue
        if src.name not in seen:
            tag = " [AEC]" if src.is_aec else ""
            items.append((src.name, f"{src.name}{tag}"))
            seen.add(src.name)

    try:
        import sounddevice as sd

        for dev in sd.query_devices():
            if int(dev.get("max_input_channels", 0)) <= 0:
                continue
            dname = str(dev.get("name") or "").strip()
            if dname and dname not in seen:
                items.append((dname, dname))
                seen.add(dname)
    except Exception:
        pass

    return items


def resolve_sounddevice_input(preferred: str = "") -> Optional[int]:
    """
    Resolve a configured microphone name (or PipeWire AEC name) to a valid `sounddevice` input index.
    Returns `None` to use the system default when empty or unmatched (never raises).
    """
    pref = (preferred or "").strip()
    if not pref or pref.lower() in ("default", "auto"):
        return None
    try:
        import sounddevice as sd

        devices = sd.query_devices()
    except Exception:
        return None

    pref_l = pref.lower()
    # Exact match first
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        name = str(dev.get("name") or "").strip()
        if name.lower() == pref_l:
            return idx

    # Substring match (e.g. `capri_aec_source`, `easyeffects_source`, `FIFINE`)
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        name = str(dev.get("name") or "").strip()
        if pref_l in name.lower() or name.lower() in pref_l:
            return idx

    return None


def compute_chunk_rms(chunk_float: np.ndarray) -> float:
    """Compute float32 RMS level in [0.0, 1.0]."""
    if chunk_float.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(chunk_float, dtype=np.float32))))


def rms_to_meter_percent(rms: float) -> int:
    """Convert linear RMS (0..1) into a responsive 0..100 perceptual VU-meter percentage."""
    val = max(0.0, min(1.0, float(rms)))
    if val < 0.0015:
        return 0
    # Smooth perceptual dB mapping: -56 dB -> 0%, -6 dB -> 100%
    db = 20.0 * math.log10(val)
    pct = ((db + 56.0) / 50.0) * 100.0
    return max(0, min(100, int(round(pct))))


def recommend_barge_in_threshold(noise_rms: float, speech_rms: float) -> CalibrationResult:
    """
    Calculate a calibrated `barge_in_rms_threshold` from measured ambient noise RMS
    and normal speaking speech RMS.
    """
    n_rms = max(0.0, float(noise_rms))
    s_rms = max(0.0, float(speech_rms))

    floor_guard = max(0.015, n_rms * 2.5 + 0.008)
    if s_rms > floor_guard * 1.15:
        rec = floor_guard + (s_rms - floor_guard) * 0.35
        note = "Оптимальный баланс между фоновым шумом и твоим голосом."
    elif s_rms > n_rms * 1.3 and s_rms >= 0.015:
        rec = max(0.018, (n_rms + s_rms) * 0.55)
        note = "Тихий голос относительно фона — порог выставлен осторожно; для колонок лучше включить PipeWire AEC."
    else:
        rec = max(0.028, floor_guard)
        note = "Речь во время теста почти не отличалась от фона — выставлен защитный порог по уровню шума."

    rec = round(max(0.015, min(0.200, rec)), 3)
    return CalibrationResult(
        noise_rms=round(n_rms, 4),
        speech_rms=round(s_rms, 4),
        recommended_threshold=rec,
        note=note,
    )
