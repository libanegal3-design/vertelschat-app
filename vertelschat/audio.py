"""Audio inspection and conversion with ffprobe/ffmpeg. Originals are never modified."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class AudioError(RuntimeError):
    pass


def _bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AudioError(f"{name} niet gevonden; installeer ffmpeg")
    return path


def probe(path: Path) -> dict:
    """Return {'duration', 'codec', 'channels', 'sample_rate'} or raise AudioError for corrupt/non-audio files."""
    cmd = [_bin("ffprobe"), "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,codec_name,channels,sample_rate", "-of", "json", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise AudioError(f"ffprobe: {res.stderr.strip()[:300]}")
    data = json.loads(res.stdout or "{}")
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise AudioError("geen audiospoor gevonden")
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0.2:
        raise AudioError("audio is leeg of onleesbaar")
    # decode test: a truncated/corrupt file often probes fine but cannot be decoded
    dec = subprocess.run([_bin("ffmpeg"), "-v", "error", "-i", str(path), "-f", "null", "-"],
                         capture_output=True, text=True, timeout=300)
    if dec.returncode != 0 or "Invalid data" in dec.stderr:
        raise AudioError(f"audio kan niet worden gedecodeerd: {dec.stderr.strip()[:300]}")
    s = audio[0]
    return {"duration": duration, "codec": s.get("codec_name", ""), "channels": s.get("channels"),
            "sample_rate": s.get("sample_rate")}


def to_mp3(src: Path, dest: Path, bitrate: str = "64k") -> None:
    """Playback/archive copy that plays everywhere (older iPhones, car stereos, Windows Media Player)."""
    cmd = [_bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "44100",
           "-c:a", "libmp3lame", "-b:a", bitrate, str(dest)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if res.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise AudioError(f"omzetten naar mp3 mislukt: {res.stderr.strip()[:300]}")


def split_for_transcription(src: Path, workdir: Path, seconds: int = 600) -> list[Path]:
    """Split long recordings into mono 16 kHz Opus chunks for providers with per-request limits."""
    workdir.mkdir(parents=True, exist_ok=True)
    pattern = workdir / "deel%03d.ogg"
    cmd = [_bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "libopus", "-b:a", "24k", "-f", "segment", "-segment_time", str(seconds), "-reset_timestamps", "1",
           str(pattern)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if res.returncode != 0:
        raise AudioError(f"opsplitsen mislukt: {res.stderr.strip()[:300]}")
    return sorted(workdir.glob("deel*.ogg"))


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "0:00"
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def spoken_duration(seconds: float | None) -> str:
    """Human wording for lists and the book: '39 seconden', 'ongeveer 4 minuten'."""
    total = int(round(seconds or 0))
    if total < 60:
        return "1 seconde" if total == 1 else f"{total} seconden"
    minutes = int(round(total / 60))
    return "ongeveer 1 minuut" if minutes == 1 else f"ongeveer {minutes} minuten"