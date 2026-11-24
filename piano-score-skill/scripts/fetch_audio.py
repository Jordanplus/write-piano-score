#!/usr/bin/env python3
"""
Stage 1: Fetch & normalize audio from a YouTube URL or local audio file.
Outputs a clean 44.1kHz WAV file and metadata JSON for downstream MIR analysis.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd: list[str], desc: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{desc} failed (exit {proc.returncode}):\n"
            f"CMD: {' '.join(cmd)}\n"
            f"STDERR:\n{proc.stderr.strip()}"
        )
    return proc


def find_yt_dlp() -> str:
    # Prefer yt-dlp from the same Python environment as this script
    venv_yt = Path(sys.executable).parent / "yt-dlp"
    if venv_yt.exists():
        return str(venv_yt)
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise FileNotFoundError("yt-dlp not found. Install it with `uv pip install yt-dlp`.")


def fetch_youtube(url: str, out_dir: Path, max_duration: int | None = None) -> tuple[Path, dict]:
    yt_dlp = find_yt_dlp()
    raw_template = str(out_dir / "source_raw.%(ext)s")

    # Fetch metadata first (without downloading playlist)
    meta_cmd = [
        yt_dlp,
        "--no-playlist",
        "--dump-json",
        "--no-warnings",
        url,
    ]
    meta_proc = run_cmd(meta_cmd, "yt-dlp metadata fetch")
    meta_raw = json.loads(meta_proc.stdout.splitlines()[0])

    # Download best audio
    dl_cmd = [
        yt_dlp,
        "--no-playlist",
        "-f",
        "bestaudio/best",
        "-o",
        raw_template,
        "--no-warnings",
        url,
    ]
    run_cmd(dl_cmd, "yt-dlp audio download")

    downloaded = sorted(out_dir.glob("source_raw.*"))
    if not downloaded:
        raise FileNotFoundError(f"No downloaded file found matching {raw_template}")
    raw_audio = downloaded[0]

    wav_path = out_dir / "audio.wav"
    ff_cmd = ["ffmpeg", "-y", "-i", str(raw_audio)]
    if max_duration:
        ff_cmd += ["-t", str(max_duration)]
    ff_cmd += ["-ac", "2", "-ar", "44100", "-sample_fmt", "s16", str(wav_path)]
    run_cmd(ff_cmd, "ffmpeg WAV conversion")

    metadata = {
        "source_type": "youtube",
        "source_url": url,
        "video_id": meta_raw.get("id"),
        "title": meta_raw.get("title"),
        "uploader": meta_raw.get("uploader") or meta_raw.get("channel"),
        "duration_sec": meta_raw.get("duration"),
        "audio_wav": str(wav_path.resolve()),
    }
    return wav_path, metadata


def prepare_local(input_path: Path, out_dir: Path, max_duration: int | None = None) -> tuple[Path, dict]:
    if not input_path.exists():
        raise FileNotFoundError(f"Local audio file not found: {input_path}")

    wav_path = out_dir / "audio.wav"
    ff_cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    if max_duration:
        ff_cmd += ["-t", str(max_duration)]
    ff_cmd += ["-ac", "2", "-ar", "44100", "-sample_fmt", "s16", str(wav_path)]
    run_cmd(ff_cmd, "ffmpeg local WAV conversion")

    # Probe duration via ffprobe
    probe = run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(wav_path),
        ],
        "ffprobe duration check",
    )
    duration = float(probe.stdout.strip()) if probe.stdout.strip() else None

    metadata = {
        "source_type": "local",
        "source_path": str(input_path.resolve()),
        "title": input_path.stem,
        "duration_sec": duration,
        "audio_wav": str(wav_path.resolve()),
    }
    return wav_path, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and normalize song audio for piano transcription.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="YouTube (or yt-dlp supported) URL")
    group.add_argument("--file", help="Path to local audio/video file (mp3/wav/m4a/flac/mp4)")
    parser.add_argument("--out-dir", required=True, help="Output working directory")
    parser.add_argument("--max-duration", type=int, default=None, help="Optional max seconds to trim")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.url:
        wav_path, metadata = fetch_youtube(args.url, out_dir, args.max_duration)
    else:
        wav_path, metadata = prepare_local(Path(args.file), out_dir, args.max_duration)

    meta_path = out_dir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: Audio ready at {wav_path}")
    print(f"OK: Metadata saved to {meta_path}")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
