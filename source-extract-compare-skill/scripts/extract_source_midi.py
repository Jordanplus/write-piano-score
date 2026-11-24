#!/usr/bin/env python3
"""
Stage 2: High-Resolution Polyphonic MIDI Extraction from Source Audio.

Backends:
- neural_piano (default for solo piano): ByteDance `piano_transcription_inference`
  (Kong et al., 88-key onset/offset/velocity + sustain pedal CC64).
- stft_poly (lightweight fallback): 8192-pt STFT harmonic-verified multi-pitch extractor.

Outputs:
- extracted_raw.mid       (exact millisecond-aligned polyphonic MIDI)
- extracted_notes.json    (structured notes + top-voice melody contour)
- extracted_preview.mp3   (FluidSynth grand piano synthesis for 1:1 audio comparison)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import librosa
import numpy as np
import pretty_midi

FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
SOUNDFONT_CANDIDATES = [
    Path("/Users/mcgradymac/Gemini/music-gen/soundfonts/MuseScore_General.sf3"),
    Path("/opt/homebrew/share/soundfonts/default.sf2"),
    Path("/usr/share/sounds/sf2/FluidR3_GM.sf2"),
]


def midi_to_name(m: int) -> str:
    return f"{FLAT_NAMES[m % 12]}{(m // 12) - 1}"


def ensure_bytedance_checkpoint() -> Path:
    ckpt = Path.home() / "piano_transcription_inference_data" / "note_F1=0.9677_pedal_F1=0.9186.pth"
    if not ckpt.exists() or ckpt.stat().st_size < 160_000_000:
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        url = "https://zenodo.org/record/4034264/files/CRNN_note_F1%3D0.9677_pedal_F1%3D0.9186.pth?download=1"
        print(f"[neural_piano] Downloading ByteDance piano checkpoint (~165MB) -> {ckpt}")
        subprocess.run(["curl", "-L", "--fail", "--progress-bar", "-o", str(ckpt), url], check=True)
    return ckpt


def run_neural_piano(audio_path: Path, midi_out: Path, device: str = "cpu") -> str:
    import torch

    ckpt_path = ensure_bytedance_checkpoint()

    # PyTorch >=2.6 defaults torch.load weights_only=True; old checkpoints need weights_only=False
    orig_load = torch.load

    def _compat_load(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        return orig_load(*args, **kwargs)

    torch.load = _compat_load  # type: ignore[assignment]
    try:
        from piano_transcription_inference import PianoTranscription, sample_rate

        chosen_device = device
        if chosen_device == "auto":
            # CPU is rock-solid with torchlibrosa STFT on macOS arm64 (~15-25s for 3min)
            chosen_device = "cpu"
        print(f"[neural_piano] Loading audio at {sample_rate}Hz via librosa (device={chosen_device})...")
        audio, _ = librosa.load(str(audio_path), sr=sample_rate, mono=True)
        transcriptor = PianoTranscription(device=chosen_device, checkpoint_path=str(ckpt_path))
        print("[neural_piano] Running ByteDance 88-key polyphonic transcription...")
        transcriptor.transcribe(audio, str(midi_out))
        return f"neural_piano ({chosen_device})"
    finally:
        torch.load = orig_load  # type: ignore[assignment]


def run_stft_poly(audio_path: Path, midi_out: Path) -> str:
    """Fallback harmonic-verified STFT multi-pitch extractor (requires f0 + 2*f0)."""
    print("[stft_poly] Running 8192-pt STFT harmonic-verified polyphonic extractor...")
    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    n_fft = 8192
    hop = 512
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    frame_sec = hop / sr

    midi_range = list(range(36, 97))  # C2..C7
    k1 = [int(np.argmin(np.abs(freqs - librosa.midi_to_hz(m)))) for m in midi_range]
    k2 = [int(np.argmin(np.abs(freqs - 2.0 * librosa.midi_to_hz(m)))) for m in midi_range]

    # Build note activation matrix (n_notes x n_frames)
    act = np.zeros((len(midi_range), S.shape[1]), dtype=float)
    for i, (b1, b2) in enumerate(zip(k1, k2)):
        e1 = S[max(0, b1 - 1) : b1 + 2, :].max(axis=0)
        e2 = S[max(0, b2 - 1) : b2 + 2, :].max(axis=0) if b2 < len(freqs) - 1 else e1
        act[i] = np.sqrt(e1 * e2)

    # Onset delta detection per note
    diff = np.maximum(0.0, np.diff(act, axis=1, prepend=0.0))
    frame_max = np.maximum(act.max(axis=0, keepdims=True), 1e-6)
    rel_act = act / frame_max

    pm = pretty_midi.PrettyMIDI(initial_tempo=68.0)
    inst = pretty_midi.Instrument(program=0, name="STFT Polyphonic Piano")

    for i, m in enumerate(midi_range):
        thr = max(float(np.percentile(act[i], 92)) * 0.65, 1.5)
        f = 1
        while f < S.shape[1] - 2:
            if act[i, f] > thr and rel_act[i, f] > 0.22 and diff[i, f] > 0.30 * act[i, f]:
                start_f = f
                end_f = f + 1
                while end_f < S.shape[1] - 1 and act[i, end_f] > 0.35 * act[i, start_f] and (end_f - start_f) < int(3.0 / frame_sec):
                    if end_f > start_f + 3 and diff[i, end_f] > 0.45 * act[i, end_f]:
                        break
                    end_f += 1
                if end_f - start_f >= 2:
                    vel = int(np.clip(45 + 65 * rel_act[i, start_f], 40, 115))
                    inst.notes.append(pretty_midi.Note(vel, m, start_f * frame_sec, end_f * frame_sec))
                f = end_f
            else:
                f += 1

    pm.instruments.append(inst)
    pm.write(str(midi_out))
    return "stft_poly"


def export_notes_json(midi_path: Path, json_out: Path, backend_used: str, audio_path: Path) -> dict:
    pm = pretty_midi.PrettyMIDI(str(midi_path))
    notes = []
    for inst in pm.instruments:
        for n in inst.notes:
            notes.append({
                "start": round(float(n.start), 3),
                "end": round(float(n.end), 3),
                "dur": round(float(n.end - n.start), 3),
                "midi": int(n.pitch),
                "pitch": midi_to_name(int(n.pitch)),
                "velocity": int(n.velocity),
            })
    notes.sort(key=lambda x: (x["start"], -x["midi"]))

    # Extract clean top-voice timeline (grouping onsets within 60ms)
    top_voice = []
    for n in notes:
        if n["midi"] < 55:  # below G3 is usually bass
            continue
        if top_voice and abs(n["start"] - top_voice[-1]["start"]) <= 0.065:
            if n["midi"] > top_voice[-1]["midi"]:
                top_voice[-1] = n
        else:
            top_voice.append(n)

    payload = {
        "source_audio": str(audio_path),
        "backend": backend_used,
        "midi_file": str(midi_path),
        "duration_sec": round(float(pm.get_end_time()), 2),
        "total_notes": len(notes),
        "top_voice_count": len(top_voice),
        "notes": notes,
        "top_voice": top_voice,
    }
    json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def render_preview_mp3(midi_path: Path, mp3_out: Path) -> bool:
    sf = next((p for p in SOUNDFONT_CANDIDATES if p.exists()), None)
    if not sf or not shutil.which("fluidsynth") or not shutil.which("ffmpeg"):
        print("WARN: FluidSynth/SoundFont/ffmpeg unavailable; skipping preview MP3.", file=sys.stderr)
        return False
    wav_tmp = mp3_out.with_suffix(".wav")
    subprocess.run(
        ["fluidsynth", "-ni", "-g", "0.85", "-R", "1", "-C", "0", "-r", "44100", "-F", str(wav_tmp), str(sf), str(midi_path)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_tmp), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-b:a", "192k", str(mp3_out)],
        check=True, capture_output=True, text=True,
    )
    wav_tmp.unlink(missing_ok=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract high-resolution polyphonic MIDI from source audio.")
    parser.add_argument("--audio", required=True, help="Path to source audio WAV/MP3")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument("--backend", choices=("auto", "neural_piano", "stft_poly"), default="auto")
    parser.add_argument("--device", default="auto", help="Device for neural_piano (auto/cpu/mps)")
    parser.add_argument("--skip-audio", action="store_true", help="Skip FluidSynth MP3 preview rendering")
    args = parser.parse_args()

    audio_path = Path(args.audio).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    midi_out = out_dir / "extracted_raw.mid"
    json_out = out_dir / "extracted_notes.json"
    mp3_out = out_dir / "extracted_preview.mp3"

    backend_used = ""
    if args.backend in ("auto", "neural_piano"):
        try:
            backend_used = run_neural_piano(audio_path, midi_out, device=args.device)
        except Exception as exc:
            if args.backend == "neural_piano":
                raise
            print(f"WARN: neural_piano failed ({exc}); falling back to stft_poly.", file=sys.stderr)
            backend_used = run_stft_poly(audio_path, midi_out)
    else:
        backend_used = run_stft_poly(audio_path, midi_out)

    info = export_notes_json(midi_out, json_out, backend_used, audio_path)
    print(f"OK: Extracted {info['total_notes']} notes ({info['top_voice_count']} top-voice) via {backend_used} -> {midi_out}")

    if not args.skip_audio:
        if render_preview_mp3(midi_out, mp3_out):
            print(f"OK: Rendered extracted MIDI preview -> {mp3_out}")


if __name__ == "__main__":
    main()
