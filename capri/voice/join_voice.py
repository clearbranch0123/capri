"""
CAPRI // Join Voice — PipeWire Virtual Audio Bridge for Discord / Telegram / Web Calls (v3.0 — Wave 6).

Creates a dedicated Capri-owned PipeWire virtual audio bus:
  - `capri_voice_bus` (`module-null-sink`) — receives Capri's TTS speech (+ optional user mic loopback)
  - `capri_virtual_mic` (`module-remap-source` from `capri_voice_bus.monitor`) — selectable in Discord,
    Telegram, Zoom, or browser calls as "Capri Virtual Mic (Discord/Call)".

Safety guarantee:
  - Strictly tracks exact loaded PipeWire module IDs AND explicit Capri sink/source names
    (`capri_voice_bus` / `capri_virtual_mic`) so `leave_voice()` NEVER touches any other
    application's virtual sinks, sources, or loopbacks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .pipewire import _run_pactl, get_pipewire_status

logger = logging.getLogger(__name__)

CAPRI_VOICE_BUS_SINK = "capri_voice_bus"
CAPRI_VIRTUAL_MIC_SOURCE = "capri_virtual_mic"

# Strictly tracks module IDs created by Capri during this process lifetime
_CAPRI_VOICE_MODULE_IDS: set[str] = set()


@dataclass(frozen=True)
class JoinVoiceStatus:
    active: bool = False
    pipewire_available: bool = False
    sink_name: str = CAPRI_VOICE_BUS_SINK
    virtual_mic_name: str = CAPRI_VIRTUAL_MIC_SOURCE
    mic_mixed: bool = False
    owned_module_ids: tuple[str, ...] = field(default_factory=tuple)


def _list_loaded_modules() -> list[tuple[str, str, str]]:
    code, out, _ = _run_pactl(["list", "short", "modules"])
    if code != 0:
        return []
    rows: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        mid = parts[0].strip()
        mname = parts[1].strip()
        margs = parts[2].strip() if len(parts) > 2 else ""
        rows.append((mid, mname, margs))
    return rows


def _is_capri_voice_module(mod_id: str, mod_name: str, mod_args: str) -> bool:
    if mod_id in _CAPRI_VOICE_MODULE_IDS:
        return True
    if mod_name not in ("module-null-sink", "module-remap-source", "module-loopback"):
        return False
    return (CAPRI_VOICE_BUS_SINK in mod_args) or (CAPRI_VIRTUAL_MIC_SOURCE in mod_args)


def get_join_voice_status() -> JoinVoiceStatus:
    """Inspect whether Capri's PipeWire virtual call bridge is currently active."""
    pw = get_pipewire_status()
    if not pw.available:
        return JoinVoiceStatus(active=False, pipewire_available=False)

    modules = _list_loaded_modules()
    owned = [mid for mid, mname, margs in modules if _is_capri_voice_module(mid, mname, margs)]
    has_bus = any(
        mname == "module-null-sink" and CAPRI_VOICE_BUS_SINK in margs
        for _mid, mname, margs in modules
    )
    has_mic = any(
        (mname == "module-remap-source" and CAPRI_VIRTUAL_MIC_SOURCE in margs)
        for _mid, mname, margs in modules
    ) or any(s.name == CAPRI_VIRTUAL_MIC_SOURCE for s in pw.sources)
    has_loopback = any(
        mname == "module-loopback" and CAPRI_VOICE_BUS_SINK in margs
        for _mid, mname, margs in modules
    )
    return JoinVoiceStatus(
        active=bool(has_bus and has_mic),
        pipewire_available=True,
        mic_mixed=has_loopback,
        owned_module_ids=tuple(owned),
    )


def is_join_voice_active() -> bool:
    """Fast check used by TTS playback to decide whether to mirror speech into `capri_voice_bus`."""
    try:
        return get_join_voice_status().active
    except Exception:
        return False


def join_voice(mix_user_mic: bool = True, user_mic_source: str = "") -> tuple[bool, str]:
    """
    Create Capri's virtual PipeWire audio bus & virtual microphone for Discord/Telegram calls.
    Idempotent: if already active, reports current status without duplicating modules.
    """
    pw = get_pipewire_status()
    if not pw.available:
        return False, "PipeWire / PulseAudio (`pactl`) недоступен — Join Voice требует PipeWire."

    cur = get_join_voice_status()
    if cur.active:
        return (
            True,
            f"Join Voice уже активен! Выбери в Discord/Telegram микрофон **`Capri Virtual Mic`** (`{CAPRI_VIRTUAL_MIC_SOURCE}`).",
        )

    created_ids: list[str] = []

    def _rollback():
        for mid in reversed(created_ids):
            _run_pactl(["unload-module", mid])
            _CAPRI_VOICE_MODULE_IDS.discard(mid)

    # 1. Create virtual null sink where Capri's TTS (+ optional user mic) is mixed
    code1, out1, err1 = _run_pactl(
        [
            "load-module",
            "module-null-sink",
            f"sink_name={CAPRI_VOICE_BUS_SINK}",
            'sink_properties=device.description="Capri_Voice_Bus"',
        ],
        timeout=2.5,
    )
    if code1 != 0:
        return False, f"Не удалось создать виртуальную шину PipeWire: {(err1 or out1).strip()}"
    id1 = (out1 or "").strip()
    if id1.isdigit():
        created_ids.append(id1)
        _CAPRI_VOICE_MODULE_IDS.add(id1)

    # 2. Expose bus monitor as a selectable virtual microphone source
    code2, out2, err2 = _run_pactl(
        [
            "load-module",
            "module-remap-source",
            f"master={CAPRI_VOICE_BUS_SINK}.monitor",
            f"source_name={CAPRI_VIRTUAL_MIC_SOURCE}",
            'source_properties=device.description="Capri_Virtual_Mic"',
        ],
        timeout=2.5,
    )
    if code2 != 0:
        _rollback()
        return False, f"Не удалось создать виртуальный микрофон `{CAPRI_VIRTUAL_MIC_SOURCE}`: {(err2 or out2).strip()}"
    id2 = (out2 or "").strip()
    if id2.isdigit():
        created_ids.append(id2)
        _CAPRI_VOICE_MODULE_IDS.add(id2)

    # 3. Optional loopback of user's real microphone into the virtual bus (so call hears BOTH user + Capri)
    mic_note = "только голос Капри"
    if mix_user_mic:
        src_name = (user_mic_source or "").strip() or (pw.aec_source_name if pw.aec_active else pw.default_source)
        if src_name and src_name != CAPRI_VIRTUAL_MIC_SOURCE and not src_name.endswith(".monitor"):
            code3, out3, _err3 = _run_pactl(
                [
                    "load-module",
                    "module-loopback",
                    f"source={src_name}",
                    f"sink={CAPRI_VOICE_BUS_SINK}",
                    "latency_msec=30",
                ],
                timeout=2.5,
            )
            if code3 == 0:
                id3 = (out3 or "").strip()
                if id3.isdigit():
                    created_ids.append(id3)
                    _CAPRI_VOICE_MODULE_IDS.add(id3)
                mic_note = f"твой микрофон (`{src_name}`) + голос Капри"

    return (
        True,
        f"Join Voice **активирован** ({mic_note}).\n"
        f"• Выбери в Discord / Telegram / браузере устройство ввода: **`Capri_Virtual_Mic`** (`{CAPRI_VIRTUAL_MIC_SOURCE}`).\n"
        f"• Речь Капри автоматически дублируется в звонок. Для выхода введи `/call leave`.",
    )


def leave_voice() -> tuple[bool, str]:
    """
    Cleanly unload ONLY Capri-owned Join Voice PipeWire modules (`capri_voice_bus`, `capri_virtual_mic`,
    and its loopback). Never touches any foreign PipeWire sinks, sources, or loopbacks.
    """
    modules = _list_loaded_modules()
    to_unload = [mid for mid, mname, margs in modules if _is_capri_voice_module(mid, mname, margs)]
    if not to_unload:
        _CAPRI_VOICE_MODULE_IDS.clear()
        return True, "Join Voice не был активен (виртуальные модули Capri отсутствуют)."

    unloaded = 0
    # Unload in reverse order (loopback -> remap-source -> null-sink)
    for mid in reversed(to_unload):
        ucode, _, _ = _run_pactl(["unload-module", mid])
        if ucode == 0:
            unloaded += 1
            _CAPRI_VOICE_MODULE_IDS.discard(mid)

    return True, f"Join Voice **отключён** (выгружено модулей Capri: {unloaded}; системный звук не затронут)."
