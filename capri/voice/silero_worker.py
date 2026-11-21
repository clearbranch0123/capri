import json
import os
import sys
import time
from pathlib import Path
import torch
import torchaudio

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def load_silero():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    sys.stderr.write(f"[Silero Worker] Загрузка модели на {device}...\n")
    sys.stderr.flush()

    model, _ = torch.hub.load(
        repo_or_dir="snakers4/silero-models",
        model="silero_tts",
        language="ru",
        speaker="v4_ru",
        trust_repo=True,
    )
    model.to(device)

    # Warmup inference
    try:
        model.apply_tts(text="т+ест", speaker="baya", sample_rate=24000)
    except Exception:
        pass

    sys.stderr.write("[Silero Worker] Модель готова к генерации!\n")
    sys.stderr.flush()
    return model, device


def synthesize_silero(model, text: str, output_path: str, speaker: str = "baya", sample_rate: int = 24000):
    t0 = time.time()
    audio = model.apply_tts(text=text, speaker=speaker, sample_rate=sample_rate)
    torchaudio.save(output_path, audio.unsqueeze(0).cpu(), sample_rate)
    return time.time() - t0


def main():
    if len(sys.argv) > 2 and sys.argv[1] != "--daemon":
        # Direct CLI mode
        text = sys.argv[1]
        out_file = sys.argv[2]
        speaker = sys.argv[3] if len(sys.argv) > 3 else "baya"
        model, _ = load_silero()
        dur = synthesize_silero(model, text, out_file, speaker)
        print(f"[Silero Worker] Сгенерировано за {dur:.3f} сек: {out_file}")
        return

    # Daemon mode: use clean duplicate file descriptor for IPC
    ipc_fd = os.dup(sys.stdout.fileno())
    ipc_out = os.fdopen(ipc_fd, "w", buffering=1)
    sys.stdout = sys.stderr

    model, _ = load_silero()

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
            out_file = req.get("output", "/tmp/silero_out.wav")
            speaker = req.get("speaker", "baya")
            sr = int(req.get("sample_rate", 24000))

            dur = synthesize_silero(model, text, out_file, speaker, sr)
            ipc_out.write(json.dumps({"status": "ok", "output": out_file, "duration": round(dur, 3)}) + "\n")
            ipc_out.flush()
        except Exception as e:
            ipc_out.write(json.dumps({"status": "error", "message": str(e)}) + "\n")
            ipc_out.flush()


if __name__ == "__main__":
    main()
