#!/usr/bin/env python3
"""Transcribe an audio file using faster-whisper. Prints the transcript to stdout.

Usage: .venv/bin/python transcribe.py <audio_path> [model_size]
Models cached under ~/.cache/huggingface (lazy-downloaded on first use).
"""
import sys
from faster_whisper import WhisperModel

_model = None

def transcribe(path: str, model_size: str = "base") -> str:
    global _model
    if _model is None:
        _model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = _model.transcribe(path, beam_size=1, vad_filter=True)
    parts = [seg.text for seg in segments]
    text = "".join(parts).strip()
    return f"[lang={info.language}] {text}" if text else "[no speech detected]"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: transcribe.py <audio_path> [model_size]", file=sys.stderr)
        sys.exit(1)
    size = sys.argv[2] if len(sys.argv) > 2 else "base"
    print(transcribe(sys.argv[1], size))
