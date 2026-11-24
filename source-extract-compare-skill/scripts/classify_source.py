#!/usr/bin/env python3
"""
Stage 1: Independent Source Audio Classification & Musical Parameter Profiling.

Classifies whether the audio is:
- solo_piano            (best transcribed with ByteDance neural piano AMT)
- vocal_pop_band        (requires stem separation / vocal-aware transcription)
- instrumental_ensemble (polyphonic harmonic STFT + careful ear verification)

Also independently measures tuning offset, global key, sliding-window modulations,
and multi-candidate BPM (explicitly flagging ballad half/4:3 tempo ambiguities).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import librosa
import numpy as np

SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
FLAT_MAJOR_KEYS = {"F", "Bb", "Eb", "Ab", "Db", "Gb", "Cb"}
FLAT_MINOR_KEYS = {"D", "G", "C", "F", "Bb", "Eb", "Ab"}
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
KS_MAJOR /= np.linalg.norm(KS_MAJOR)
KS_MINOR /= np.linalg.norm(KS_MINOR)


def detect_key(chroma_vec: np.ndarray) -> dict:
    v = np.asarray(chroma_vec, dtype=float)
    n = np.linalg.norm(v)
    if n < 1e-8:
        return {"label": "C major", "score": 0.0, "alt": "-"}
    v = v / n
    cands = []
    for root in range(12):
        ms = float(np.dot(v, np.roll(KS_MAJOR, root)))
        ns = float(np.dot(v, np.roll(KS_MINOR, root)))
        mn = FLAT_NAMES[root] if FLAT_NAMES[root] in FLAT_MAJOR_KEYS else SHARP_NAMES[root]
        nn = FLAT_NAMES[root] if FLAT_NAMES[root] in FLAT_MINOR_KEYS else SHARP_NAMES[root]
        cands.append((ms, f"{mn} major"))
        cands.append((ns, f"{nn} minor"))
    cands.sort(key=lambda x: x[0], reverse=True)
    return {"label": cands[0][1], "score": round(cands[0][0], 4), "alt": f"{cands[1][1]} ({cands[1][0]:.3f})"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify source audio type and profile key/tuning/BPM.")
    parser.add_argument("--audio", required=True, help="Path to WAV/MP3 source audio")
    parser.add_argument("--metadata", help="Optional metadata.json from fetch_audio.py")
    parser.add_argument("--out-dir", required=True, help="Output directory for source_profile.json")
    args = parser.parse_args()

    audio_path = Path(args.audio).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {}
    if args.metadata and Path(args.metadata).exists():
        meta = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
    title_lower = str(meta.get("title", audio_path.stem)).lower()

    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    dur = float(len(y) / sr)
    tuning = float(librosa.estimate_tuning(y=y, sr=sr))
    y_harm, y_perc = librosa.effects.hpss(y, margin=2.0)

    # Acoustic descriptors for source classification
    rms_total = float(np.sqrt(np.mean(y ** 2)) + 1e-8)
    rms_harm = float(np.sqrt(np.mean(y_harm ** 2)))
    rms_perc = float(np.sqrt(np.mean(y_perc ** 2)))
    perc_ratio = rms_perc / rms_total

    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    hf_mask = freqs >= 5000.0
    sub_mask = (freqs >= 35.0) & (freqs <= 95.0)
    hf_energy_ratio = float(np.mean(S[hf_mask, :] ** 2) / (np.mean(S ** 2) + 1e-8))
    sub_energy_ratio = float(np.mean(S[sub_mask, :] ** 2) / (np.mean(S ** 2) + 1e-8))
    flatness_med = float(np.median(librosa.feature.spectral_flatness(S=S)))
    rolloff_med = float(np.median(librosa.feature.spectral_rolloff(S=S, sr=sr, roll_percent=0.85)))

    # Heuristic scoring combining DSP + metadata hints
    piano_title_hint = any(k in title_lower for k in ("piano", "鋼琴", "钢琴", "彈奏", "弹奏", "純鋼琴", "独奏"))
    vocal_title_hint = any(k in title_lower for k in ("official mv", "官方mv", "原唱", "live", "演唱會", "演唱会"))

    piano_score = 0.0
    if piano_title_hint:
        piano_score += 2.0
    if vocal_title_hint:
        piano_score -= 1.5
    if flatness_med < 0.0025:
        piano_score += 1.5  # Pure tonal instrument, very low broadband noise/cymbals
    elif flatness_med > 0.008:
        piano_score -= 2.0  # Drums/hi-hats/full pop mix
    if hf_energy_ratio < 0.015:
        piano_score += 1.0  # Piano energy rolls off cleanly above 5kHz
    elif hf_energy_ratio > 0.05:
        piano_score -= 1.5
    if rolloff_med < 2600.0:
        piano_score += 1.0
    elif rolloff_med > 4200.0:
        piano_score -= 1.0
    if perc_ratio < 0.28:
        piano_score += 0.5
    elif perc_ratio > 0.42:
        piano_score -= 1.5

    if piano_score >= 2.5:
        source_type = "solo_piano"
        rec_extractor = "neural_piano (ByteDance piano_transcription_inference)"
    elif flatness_med > 0.006 or perc_ratio > 0.36 or vocal_title_hint:
        source_type = "vocal_pop_band"
        rec_extractor = "stem_separation + stft_poly (or vocal-isolated transcription)"
    else:
        source_type = "instrumental_ensemble"
        rec_extractor = "stft_poly + neural_piano cross-check"

    # Key & sliding-window modulation scan
    hop = 512
    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr, hop_length=hop, tuning=tuning, bins_per_octave=36, n_octaves=6)
    global_key = detect_key(chroma.mean(axis=1))
    key_windows = []
    t = 0.0
    while t < dur - 6.0:
        t_end = min(t + 20.0, dur)
        f0 = librosa.time_to_frames(t, sr=sr, hop_length=hop)
        f1 = max(f0 + 1, librosa.time_to_frames(t_end, sr=sr, hop_length=hop))
        kw = detect_key(chroma[:, f0:f1].mean(axis=1))
        key_windows.append({"start_sec": round(t, 1), "end_sec": round(t_end, 1), **kw})
        t += 10.0

    # Multi-candidate BPM estimation (catches 4:3 and 2:1 ballad ambiguities!)
    onset_env = librosa.onset.onset_strength(y=y_perc, sr=sr)
    tg = librosa.feature.tempogram(onset_envelope=onset_env, sr=sr)
    tg_freqs = librosa.tempo_frequencies(tg.shape[0], sr=sr)
    tg_mean = tg.mean(axis=1)
    valid_bpm = (tg_freqs >= 50.0) & (tg_freqs <= 160.0)
    cand_idxs = np.where(valid_bpm)[0]
    peaks = []
    for idx in cand_idxs:
        if 0 < idx < len(tg_mean) - 1 and tg_mean[idx] >= tg_mean[idx - 1] and tg_mean[idx] >= tg_mean[idx + 1]:
            peaks.append((float(tg_mean[idx]), float(tg_freqs[idx])))
    peaks.sort(reverse=True)
    bpm_candidates = [{"bpm": round(b, 1), "strength": round(s, 3)} for s, b in peaks[:5]]

    profile = {
        "audio_file": str(audio_path),
        "title": meta.get("title", audio_path.stem),
        "duration_sec": round(dur, 2),
        "source_type": source_type,
        "piano_confidence_score": round(piano_score, 2),
        "recommended_extractor": rec_extractor,
        "acoustic_features": {
            "perc_ratio": round(perc_ratio, 4),
            "spectral_flatness_median": round(flatness_med, 6),
            "hf_5khz_energy_ratio": round(hf_energy_ratio, 5),
            "sub_bass_energy_ratio": round(sub_energy_ratio, 5),
            "rolloff_85_hz": round(rolloff_med, 1),
        },
        "tuning_semitones": round(tuning, 3),
        "global_key": global_key,
        "bpm_candidates": bpm_candidates,
        "key_windows": key_windows,
    }

    out_path = out_dir / "source_profile.json"
    out_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=== SOURCE AUDIO PROFILE ===")
    print(f"Title             : {profile['title']} ({dur:.1f}s)")
    print(f"Detected Type     : {source_type} (piano_score={piano_score:+.2f})")
    print(f"Recommended Engine: {rec_extractor}")
    print(f"Tuning Offset     : {tuning:+.3f} semitones")
    print(f"Global Key        : {global_key['label']} (score={global_key['score']}, alt={global_key['alt']})")
    print(f"Top BPM Candidates: " + ", ".join(f"{c['bpm']} ({c['strength']})" for c in bpm_candidates[:4]))
    print(f"OK: Saved {out_path}")


if __name__ == "__main__":
    main()
