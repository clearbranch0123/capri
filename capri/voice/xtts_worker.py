import os
import sys
import json
import time
from pathlib import Path

# Automatically agree to Coqui terms of service
os.environ["COQUI_TOS_AGREED"] = "1"

# PyTorch 2.6+ compatibility fix for XTTS legacy checkpoints
import torch
_orig_torch_load = torch.load
def _compat_torch_load(*args, **kwargs):
    if "weights_only" not in kwargs:
        kwargs["weights_only"] = False
    return _orig_torch_load(*args, **kwargs)
torch.load = _compat_torch_load

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
from capri.core.resources import get_asset_path

DEFAULT_SPEAKER = get_asset_path("capri_voice_reference.wav")


def load_model():
    from TTS.api import TTS

    device = "cuda" if torch.cuda.is_available() else "cpu"
    sys.stderr.write(f"[XTTS Worker] Загрузка модели на {device}...\n")
    sys.stderr.flush()
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
    sys.stderr.write("[XTTS Worker] Модель готова к генерации!\n")
    sys.stderr.flush()
    return tts, device


def synthesize_file(tts, text: str, output_path: str, speaker_path: str = None, language: str = "ru"):
    speaker = speaker_path or str(DEFAULT_SPEAKER)
    if not os.path.exists(speaker):
        raise FileNotFoundError(f"Speaker reference not found: {speaker}")

    t0 = time.time()
    tts.tts_to_file(
        text=text,
        speaker_wav=speaker,
        language=language,
        file_path=output_path,
        split_sentences=True,
    )
    duration = time.time() - t0
    return duration


def main():
    if len(sys.argv) > 2 and sys.argv[1] != "--daemon":
        # CLI mode
        text = sys.argv[1]
        output_path = sys.argv[2]
        speaker_path = sys.argv[3] if len(sys.argv) > 3 else None

        tts, _ = load_model()
        dur = synthesize_file(tts, text, output_path, speaker_path)
        print(f"[XTTS Worker] Сгенерировано за {dur:.2f} сек: {output_path}")
        return

    # Daemon mode: separate IPC channel from noisy library stdout
    # Duplicate original stdout descriptor
    ipc_fd = os.dup(sys.stdout.fileno())
    ipc_out = os.fdopen(ipc_fd, "w", buffering=1)

    # Redirect standard stdout to stderr so Coqui logs don't pollute IPC
    sys.stdout = sys.stderr

    tts, device = load_model()

    # Send READY signal exclusively on IPC channel
    ipc_out.write("READY\n")
    ipc_out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "PING":
            ipc_out.write(json.dumps({"status": "PONG"}) + "\n")
            ipc_out.flush()
            continue
        if line == "EXIT":
            break

        try:
            req = json.loads(line)
            text = req.get("text", "")
            out_file = req.get("output", "/tmp/xtts_out.wav")
            speaker = req.get("speaker") or str(DEFAULT_SPEAKER)
            lang = req.get("language", "ru")

            dur = synthesize_file(tts, text, out_file, speaker, lang)
            ipc_out.write(json.dumps({"status": "ok", "output": out_file, "duration": round(dur, 3)}) + "\n")
            ipc_out.flush()
        except Exception as e:
            ipc_out.write(json.dumps({"status": "error", "message": str(e)}) + "\n")
            ipc_out.flush()


if __name__ == "__main__":
    main()
