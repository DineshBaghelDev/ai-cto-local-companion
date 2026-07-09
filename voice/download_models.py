#!/usr/bin/env python3
"""One-time model download for the fully-local voice stack.

Downloads (free, no API keys; everything runs offline afterwards):
  - Kokoro TTS ONNX model + voices  -> voice/models/   (~330 MB, GitHub releases)

Also generates a default NeuTTS reference voice clip (British male, from Kokoro) so
the optional NeuTTS engine (AI_CTO_TTS_ENGINE=neutts) works out of the box. Replace
voice/models/neutts/ref_default.wav + .txt with a real 3-15s recording to clone any
voice.

NOT downloaded here (auto-fetched to local caches on first run, then offline):
  - faster-whisper STT weights      (HuggingFace -> %LOCALAPPDATA% cache; large-v3-turbo on GPU)
  - Parakeet TDT v3 ONNX            (HuggingFace -> cache, when AI_CTO_STT_ENGINE=parakeet)
  - NeuTTS nano backbone + neucodec (HuggingFace -> cache, when AI_CTO_TTS_ENGINE=neutts)
  - openWakeWord feature models     (when AI_CTO_WAKE_ENGINE=openwakeword)
  - Silero VAD                      (bundled with pipecat's silero extra)
  - Smart Turn v3 ONNX              (HuggingFace -> cache, via pipecat)

Run:  python download_models.py
"""
import pathlib
import urllib.request

MODELS = pathlib.Path(__file__).parent / "models"
NEUTTS_REF_TEXT = ("Good evening. I am your local assistant, ready to help you build, "
                   "review, and ship your projects whenever you need me.")
BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = {
    "kokoro-v1.0.onnx": f"{BASE}/kokoro-v1.0.onnx",     # ~310 MB
    "voices-v1.0.bin": f"{BASE}/voices-v1.0.bin",       # ~27 MB
}


def main() -> None:
    MODELS.mkdir(exist_ok=True)
    for name, url in FILES.items():
        dest = MODELS / name
        if dest.exists() and dest.stat().st_size > 1_000_000:
            print(f"already present: {dest}")
            continue
        print(f"downloading {name} ...")
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done / total:6.1%} of {total >> 20} MB", end="", flush=True)
        print()
        tmp.rename(dest)
        print(f"saved: {dest}")
    print("Kokoro models ready. Whisper/Silero/Smart-Turn download to local caches on first run.")
    generate_neutts_reference()


def generate_neutts_reference() -> None:
    """Create the default NeuTTS reference voice clip (British male, from Kokoro).

    Only needed if you enable AI_CTO_TTS_ENGINE=neutts. Replace the resulting
    ref_default.wav / .txt with a real 3-15s recording to clone a specific voice.
    """
    ref_dir = MODELS / "neutts"
    ref_wav = ref_dir / "ref_default.wav"
    if ref_wav.exists() and ref_wav.stat().st_size > 100_000:
        print(f"NeuTTS reference already present: {ref_wav}")
        return
    try:
        import numpy as np
        import soundfile as sf
        from kokoro_onnx import Kokoro
    except ImportError:
        print("(skipping NeuTTS reference: soundfile/kokoro not installed)")
        return
    ref_dir.mkdir(parents=True, exist_ok=True)
    k = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
    samples, sr = k.create(NEUTTS_REF_TEXT, voice="bm_george", speed=1.0)
    sf.write(str(ref_wav), np.clip(samples, -1, 1).astype("float32"), sr, subtype="PCM_16")
    (ref_dir / "ref_default.txt").write_text(NEUTTS_REF_TEXT, encoding="utf-8")
    print(f"NeuTTS reference voice written: {ref_wav}")


if __name__ == "__main__":
    main()
