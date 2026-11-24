#!/usr/bin/env python3
"""
Stage 2: Music Information Retrieval (MIR) Audio Analysis for Piano Arranging.

Analyzes a WAV recording and extracts:
1. True tempo (BPM) with octave-error correction for ballads/pop songs.
2. Beat and bar (measure) grid timestamps.
3. Global key + sliding-window key/modulation detection (Krumhansl-Schmuckler).
4. Beat- & measure-aligned chord progression (HMM/Viterbi-smoothed CQT chroma matching,
   plus bass-note estimation for slash chords).
5. Vocal/lead melody pitch contour (pYIN on harmonic mid-high band), quantized to beats.
6. Energy envelope & structural segment boundaries (Intro, Verse, Chorus, Interlude, Outro).

Outputs:
- analysis.json        (full machine-readable musical data)
- analysis_summary.txt (concise bar-by-bar report for arranging the piano score)
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import librosa
import numpy as np
from scipy.ndimage import median_filter

PITCH_CLASSES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
PITCH_CLASSES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# Flat keys prefer flat spelling; sharp keys prefer sharp spelling
FLAT_MAJOR_KEYS = {"F", "Bb", "Eb", "Ab", "Db", "Gb", "Cb"}
FLAT_MINOR_KEYS = {"D", "G", "C", "F", "Bb", "Eb", "Ab"}

# Krumhansl-Schmuckler key profiles
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
KS_MAJOR /= np.linalg.norm(KS_MAJOR)
KS_MINOR /= np.linalg.norm(KS_MINOR)


def pc_name(pc: int, use_flats: bool) -> str:
    return (PITCH_CLASSES_FLAT if use_flats else PITCH_CLASSES_SHARP)[pc % 12]


def midi_to_note_name(midi_num: int, use_flats: bool) -> str:
    octave = (midi_num // 12) - 1
    return f"{pc_name(midi_num % 12, use_flats)}{octave}"


def detect_key_from_chroma(chroma_vec: np.ndarray) -> dict:
    """Detect best major/minor key using Krumhansl-Schmuckler correlation."""
    vec = np.asarray(chroma_vec, dtype=float)
    norm = np.linalg.norm(vec)
    if norm < 1e-8:
        return {"root": "C", "mode": "major", "label": "C major", "score": 0.0, "use_flats": False}
    vec = vec / norm

    candidates = []
    for root in range(12):
        maj_prof = np.roll(KS_MAJOR, root)
        min_prof = np.roll(KS_MINOR, root)
        maj_score = float(np.dot(vec, maj_prof))
        min_score = float(np.dot(vec, min_prof))

        maj_name = PITCH_CLASSES_FLAT[root] if PITCH_CLASSES_FLAT[root] in FLAT_MAJOR_KEYS else PITCH_CLASSES_SHARP[root]
        min_name = PITCH_CLASSES_FLAT[root] if PITCH_CLASSES_FLAT[root] in FLAT_MINOR_KEYS else PITCH_CLASSES_SHARP[root]

        candidates.append((maj_score, maj_name, "major", root, maj_name in FLAT_MAJOR_KEYS))
        candidates.append((min_score, min_name, "minor", root, min_name in FLAT_MINOR_KEYS))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top_score, top_root, top_mode, top_pc, use_flats = candidates[0]
    runner_up = candidates[1]
    return {
        "root": top_root,
        "root_pc": top_pc,
        "mode": top_mode,
        "label": f"{top_root} {top_mode}",
        "score": round(top_score, 4),
        "use_flats": use_flats,
        "relative_candidate": f"{runner_up[1]} {runner_up[2]} ({runner_up[0]:.4f})",
    }


def build_chord_templates(use_flats: bool) -> tuple[list[str], np.ndarray]:
    """Build normalized chroma templates for Major, Minor, 7, maj7, m7, sus4, dim."""
    qualities = [
        ("", [0, 4, 7], [1.0, 0.85, 0.9]),
        ("m", [0, 3, 7], [1.0, 0.85, 0.9]),
        ("7", [0, 4, 7, 10], [1.0, 0.78, 0.82, 0.65]),
        ("m7", [0, 3, 7, 10], [1.0, 0.78, 0.82, 0.62]),
        ("maj7", [0, 4, 7, 11], [1.0, 0.78, 0.82, 0.60]),
        ("sus4", [0, 5, 7], [1.0, 0.82, 0.88]),
        ("dim", [0, 3, 6], [1.0, 0.85, 0.85]),
    ]
    names = []
    templates = []
    for root in range(12):
        rname = pc_name(root, use_flats)
        for q_suffix, intervals, weights in qualities:
            vec = np.zeros(12, dtype=float)
            for iv, w in zip(intervals, weights):
                vec[(root + iv) % 12] = w
            vec /= np.linalg.norm(vec)
            names.append(f"{rname}{q_suffix}")
            templates.append(vec)
    return names, np.vstack(templates)


def viterbi_chords(beat_chroma: np.ndarray, templates: np.ndarray, stay_bonus: float = 0.14) -> list[int]:
    """Smooth chord sequence across beats using Viterbi decoding."""
    n_beats = beat_chroma.shape[1]
    n_chords = templates.shape[0]
    if n_beats == 0:
        return []

    # Normalize columns
    norms = np.linalg.norm(beat_chroma, axis=0, keepdims=True)
    norms = np.where(norms < 1e-8, 1.0, norms)
    X = beat_chroma / norms

    # Similarity matrix (n_chords x n_beats)
    scores = templates @ X

    # Slight preference for plain triads over 7ths/sus/dim unless clearly supported
    triad_bonus = np.zeros(n_chords)
    for idx in range(n_chords):
        q = idx % 7
        if q in (0, 1):  # major / minor triad
            triad_bonus[idx] = 0.045
        elif q in (2, 3):  # 7 / m7
            triad_bonus[idx] = 0.015
    scores = scores + triad_bonus[:, None]

    dp = np.zeros((n_chords, n_beats))
    back = np.zeros((n_chords, n_beats), dtype=int)
    dp[:, 0] = scores[:, 0]

    for t in range(1, n_beats):
        prev = dp[:, t - 1]
        best_prev_idx = int(np.argmax(prev))
        best_prev_val = prev[best_prev_idx]
        for c in range(n_chords):
            stay_val = prev[c] + stay_bonus
            switch_val = best_prev_val
            if stay_val >= switch_val:
                dp[c, t] = stay_val + scores[c, t]
                back[c, t] = c
            else:
                dp[c, t] = switch_val + scores[c, t]
                back[c, t] = best_prev_idx

    path = [0] * n_beats
    path[-1] = int(np.argmax(dp[:, -1]))
    for t in range(n_beats - 2, -1, -1):
        path[t] = int(back[path[t + 1], t + 1])
    return path


def estimate_tempo_and_beats(y_perc: np.ndarray, sr: int, bpm_hint: float | None = None) -> tuple[float, np.ndarray]:
    """Estimate tempo and beat times, resolving common half/double-time ambiguity."""
    onset_env = librosa.onset.onset_strength(y=y_perc, sr=sr)
    start_bpm = bpm_hint if bpm_hint else 90.0
    tempo_raw, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env,
        sr=sr,
        start_bpm=start_bpm,
        trim=False,
    )
    bpm = float(np.atleast_1d(tempo_raw)[0])

    # If user gave no hint and detected BPM is > 125 while ballad-like, check half-tempo option
    if bpm_hint is None and bpm > 125.0:
        # Compare onset autocorrelation at half tempo vs detected tempo
        tempogram = librosa.feature.tempogram(onset_envelope=onset_env, sr=sr)
        freqs = librosa.tempo_frequencies(tempogram.shape[0], sr=sr)
        mean_tg = tempogram.mean(axis=1)
        idx_full = int(np.argmin(np.abs(freqs - bpm)))
        idx_half = int(np.argmin(np.abs(freqs - bpm / 2.0)))
        if mean_tg[idx_half] >= 0.72 * mean_tg[idx_full]:
            bpm = bpm / 2.0
            _, beat_frames = librosa.beat.beat_track(
                onset_envelope=onset_env, sr=sr, start_bpm=bpm, trim=False
            )
            bpm = float(np.atleast_1d(bpm)[0])

    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    return round(bpm, 2), beat_times


def extract_bass_notes(y_harm: np.ndarray, sr: int, beat_times: np.ndarray, use_flats: bool) -> list[str | None]:
    """Estimate dominant bass pitch class per beat from C1–B2 CQT range."""
    cqt_bass = np.abs(
        librosa.cqt(
            y=y_harm,
            sr=sr,
            fmin=librosa.note_to_hz("C1"),
            n_bins=24,  # 2 octaves: C1..B2
            bins_per_octave=12,
            hop_length=512,
        )
    )
    # Fold 2 octaves into 12 pitch classes
    bass_pc = cqt_bass[:12, :] + cqt_bass[12:24, :]
    beat_frames = librosa.time_to_frames(beat_times, sr=sr, hop_length=512)
    beat_frames = np.clip(beat_frames, 0, bass_pc.shape[1] - 1)
    sync_bass = librosa.util.sync(bass_pc, beat_frames, aggregate=np.median)
    results = []
    for t in range(sync_bass.shape[1]):
        col = sync_bass[:, t]
        if np.max(col) < 1e-4:
            results.append(None)
        else:
            results.append(pc_name(int(np.argmax(col)), use_flats))
    return results


def extract_melody_by_measure(
    y_harm: np.ndarray,
    sr: int,
    beat_times: np.ndarray,
    beats_per_bar: int,
    use_flats: bool,
) -> list[list[dict]]:
    """Extract lead/vocal melody using pYIN on harmonic audio, quantized to 8th-note grid per bar."""
    # Gentle high-pass emphasis to focus on vocal/lead range (F3 ~ C6)
    f0, voiced_flag, voiced_prob = librosa.pyin(
        y_harm,
        fmin=librosa.note_to_hz("D3"),
        fmax=librosa.note_to_hz("A5"),
        sr=sr,
        frame_length=2048,
        hop_length=512,
    )
    times = librosa.times_like(f0, sr=sr, hop_length=512)

    # Convert to MIDI and median-smooth voiced regions
    midi_f0 = np.full_like(f0, np.nan)
    valid = (~np.isnan(f0)) & (voiced_prob > 0.35)
    midi_f0[valid] = np.round(librosa.hz_to_midi(f0[valid]))

    # Smooth small 1-2 frame glitches
    valid_idx = np.where(valid)[0]
    if len(valid_idx) > 5:
        midi_smooth = midi_f0.copy()
        midi_smooth[valid_idx] = median_filter(midi_f0[valid_idx], size=5)
        midi_f0 = midi_smooth

    n_bars = len(beat_times) // beats_per_bar
    bars_melody: list[list[dict]] = []

    for bar_idx in range(n_bars):
        b0 = bar_idx * beats_per_bar
        t_start = float(beat_times[b0])
        if b0 + beats_per_bar < len(beat_times):
            t_end = float(beat_times[b0 + beats_per_bar])
        else:
            avg_beat = float(np.median(np.diff(beat_times))) if len(beat_times) > 1 else 0.85
            t_end = t_start + beats_per_bar * avg_beat

        # Quantize into 8th-note slots (2 slots per beat)
        n_slots = beats_per_bar * 2
        slot_edges = np.linspace(t_start, t_end, n_slots + 1)
        slot_notes: list[dict] = []
        for s in range(n_slots):
            mask = (times >= slot_edges[s]) & (times < slot_edges[s + 1]) & (~np.isnan(midi_f0))
            if np.count_nonzero(mask) >= 2:
                vals, counts = np.unique(midi_f0[mask], return_counts=True)
                dom_midi = int(vals[np.argmax(counts)])
                slot_notes.append(
                    {
                        "beat": round(1.0 + s * 0.5, 2),
                        "midi": dom_midi,
                        "note": midi_to_note_name(dom_midi, use_flats),
                    }
                )
            else:
                slot_notes.append({"beat": round(1.0 + s * 0.5, 2), "midi": None, "note": "-"})
        bars_melody.append(slot_notes)
    return bars_melody


def detect_modulations(
    chroma: np.ndarray, sr: int, hop_length: int, duration: float, window_sec: float = 20.0, step_sec: float = 10.0
) -> list[dict]:
    """Sliding-window key estimation to catch mid-song / final-chorus key changes."""
    windows = []
    t = 0.0
    while t < duration - 6.0:
        t_end = min(t + window_sec, duration)
        f0 = librosa.time_to_frames(t, sr=sr, hop_length=hop_length)
        f1 = librosa.time_to_frames(t_end, sr=sr, hop_length=hop_length)
        seg_chroma = chroma[:, f0:max(f0 + 1, f1)].mean(axis=1)
        key_info = detect_key_from_chroma(seg_chroma)
        windows.append(
            {
                "start_sec": round(t, 1),
                "end_sec": round(t_end, 1),
                "key": key_info["label"],
                "score": key_info["score"],
                "relative": key_info["relative_candidate"],
            }
        )
        t += step_sec
    return windows


def format_time(sec: float) -> str:
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:04.1f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze song audio for piano score arrangement.")
    parser.add_argument("--audio", required=True, help="Path to WAV/MP3 audio file")
    parser.add_argument("--out-dir", required=True, help="Directory to write analysis.json and summary")
    parser.add_argument("--beats-per-bar", type=int, default=4, help="Time signature numerator (default 4)")
    parser.add_argument("--bpm-hint", type=float, default=None, help="Optional approximate BPM hint")
    parser.add_argument("--skip-melody", action="store_true", help="Skip pYIN melody extraction for speed")
    args = parser.parse_args()

    audio_path = Path(args.audio).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading audio: {audio_path}")
    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))

    # Tuning offset estimation (e.g. slightly sharp/flat 90s recordings!)
    tuning = float(librosa.estimate_tuning(y=y, sr=sr))
    print(f"Duration: {duration:.1f}s | Tuning offset: {tuning:+.3f} semitones")

    # Separate harmonic and percussive components
    y_harm, y_perc = librosa.effects.hpss(y, margin=2.0)

    # Tempo and beat grid
    bpm, beat_times = estimate_tempo_and_beats(y_perc, sr, args.bpm_hint)
    print(f"Estimated BPM: {bpm} | Total beats: {len(beat_times)}")

    # CQT Chromagram (tuning-compensated)
    hop_length = 512
    chroma = librosa.feature.chroma_cqt(
        y=y_harm,
        sr=sr,
        hop_length=hop_length,
        tuning=tuning,
        bins_per_octave=36,
        n_octaves=6,
    )

    # Global key + sliding-window key/modulation scan
    global_key = detect_key_from_chroma(chroma.mean(axis=1))
    key_windows = detect_modulations(chroma, sr, hop_length, duration)
    use_flats = global_key["use_flats"]
    print(f"Global Key: {global_key['label']} (score {global_key['score']}, runner-up: {global_key['relative_candidate']})")

    # Beat-synchronous chroma & Viterbi chord decoding
    beat_frames = librosa.time_to_frames(beat_times, sr=sr, hop_length=hop_length)
    beat_frames = np.clip(beat_frames, 0, chroma.shape[1] - 1)
    beat_chroma = librosa.util.sync(chroma, beat_frames, aggregate=np.median)
    chord_names, chord_templates = build_chord_templates(use_flats)
    chord_path = viterbi_chords(beat_chroma, chord_templates)
    beat_chords = [chord_names[i] for i in chord_path]

    # Bass note per beat
    bass_notes = extract_bass_notes(y_harm, sr, beat_times, use_flats)

    # Energy per beat (RMS) to distinguish soft verses vs loud choruses / intro / outro
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    beat_rms = librosa.util.sync(rms[None, :], beat_frames, aggregate=np.mean)[0]
    max_rms = float(np.max(beat_rms)) if len(beat_rms) else 1.0
    norm_rms = beat_rms / max(max_rms, 1e-8)

    # Vocal / lead melody per measure
    if args.skip_melody:
        bars_melody = []
    else:
        print("Extracting harmonic melody contour (pYIN)...")
        bars_melody = extract_melody_by_measure(y_harm, sr, beat_times, args.beats_per_bar, use_flats)

    # Assemble measure-by-measure table
    bpb = args.beats_per_bar
    n_bars = len(beat_times) // bpb
    measures = []
    for bar in range(n_bars):
        b0 = bar * bpb
        b1 = min(b0 + bpb, len(beat_chords))
        t_start = float(beat_times[b0])
        t_end = float(beat_times[b0 + bpb]) if (b0 + bpb) < len(beat_times) else duration
        chords_in_bar = beat_chords[b0:b1]
        bass_in_bar = bass_notes[b0:b1]
        energy = float(np.mean(norm_rms[b0:b1])) if b1 > b0 else 0.0

        # Simplify chords in bar: first half (beats 1-2) and second half (beats 3-4)
        first_half = chords_in_bar[: max(1, bpb // 2)]
        second_half = chords_in_bar[max(1, bpb // 2) :]
        c1 = max(set(first_half), key=first_half.count)
        c2 = max(set(second_half), key=second_half.count) if second_half else c1
        chord_summary = c1 if c1 == c2 else f"{c1} -> {c2}"

        # Key window at midpoint
        mid_t = 0.5 * (t_start + t_end)
        local_key = global_key["label"]
        for w in key_windows:
            if w["start_sec"] <= mid_t <= w["end_sec"]:
                local_key = w["key"]
                break

        melody_slots = bars_melody[bar] if bar < len(bars_melody) else []
        melody_str = " ".join(s["note"] for s in melody_slots)

        measures.append(
            {
                "bar": bar + 1,
                "start_sec": round(t_start, 2),
                "end_sec": round(t_end, 2),
                "time_str": format_time(t_start),
                "energy": round(energy, 3),
                "local_key": local_key,
                "chord_summary": chord_summary,
                "beat_chords": chords_in_bar,
                "beat_bass": bass_in_bar,
                "melody_8th_grid": melody_slots,
                "melody_str": melody_str,
            }
        )

    result = {
        "audio_file": str(audio_path),
        "duration_sec": round(duration, 2),
        "tuning_semitones": round(tuning, 3),
        "bpm": bpm,
        "time_signature": f"{bpb}/4",
        "global_key": global_key,
        "key_windows": key_windows,
        "total_measures": len(measures),
        "measures": measures,
    }

    json_path = out_dir / "analysis.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # Write concise human/LLM-readable summary
    lines = [
        f"=== AUDIO MIR ANALYSIS SUMMARY ===",
        f"Audio: {audio_path.name} | Duration: {format_time(duration)} ({duration:.1f}s)",
        f"Tuning offset: {tuning:+.3f} semitones",
        f"Estimated BPM: {bpm} | Time Signature: {bpb}/4 | Measures: {len(measures)}",
        f"Global Key: {global_key['label']} (score={global_key['score']}, runner-up={global_key['relative_candidate']})",
        "",
        "--- Sliding-Window Key / Modulation Scan ---",
    ]
    for w in key_windows:
        lines.append(
            f"  {format_time(w['start_sec'])} - {format_time(w['end_sec'])}: "
            f"{w['key']:<12s} (score={w['score']:.3f}, alt={w['relative']})"
        )

    lines += [
        "",
        "--- Measure-by-Measure Chord, Bass, Energy & Melody Grid ---",
        "Bar | Time  | Dyn  | Chords            | Bass          | Melody (8th-note grid)",
        "-" * 96,
    ]
    for m in measures:
        dyn = "pp" if m["energy"] < 0.22 else "mp" if m["energy"] < 0.42 else "mf" if m["energy"] < 0.65 else "f "
        bass_str = " ".join((b or "-") for b in m["beat_bass"])
        lines.append(
            f"{m['bar']:3d} | {m['time_str']} | {dyn}   | {m['chord_summary']:<17s} | {bass_str:<13s} | {m['melody_str']}"
        )

    summary_path = out_dir / "analysis_summary.txt"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Also extract high-resolution onset-level treble/bass pitch transcription (ideal for solo piano/lead)
    C_full = np.abs(
        librosa.cqt(
            y_harm,
            sr=sr,
            fmin=librosa.note_to_hz("C2"),
            n_bins=60,
            bins_per_octave=12,
            hop_length=256,
            tuning=tuning,
        )
    )
    onset_frames = librosa.onset.onset_detect(y=y, sr=sr, hop_length=256, backtrack=False, delta=0.08, wait=8)
    onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=256)
    onset_lines = [
        "=== ONSET-LEVEL TOP-VOICE & BASS PITCH TRANSCRIPTION ===",
        "Time(s) | Top  | Treble Peaks                 | Bass",
        "-" * 60,
    ]
    c2_midi = int(librosa.note_to_midi("C2"))
    for f, t in zip(onset_frames, onset_times):
        win = C_full[:, f : min(f + 6, C_full.shape[1])].mean(axis=1)
        treble = win[24:]
        bass = win[:24]
        t_max = float(treble.max())
        t_peaks = [
            i
            for i in range(1, len(treble) - 1)
            if treble[i] >= treble[i - 1]
            and treble[i] >= treble[i + 1]
            and treble[i] >= 0.38 * max(t_max, 1e-6)
        ]
        clean_t = []
        for p in t_peaks:
            is_harm = any(
                q < p and (p - q) in (12, 19) and treble[q] > 1.2 * treble[p] for q in t_peaks
            )
            if not is_harm:
                clean_t.append(midi_to_note_name(c2_midi + 24 + p, use_flats))
        b_idx = int(np.argmax(bass))
        b_note = midi_to_note_name(c2_midi + b_idx, use_flats) if bass[b_idx] > 0.25 * max(float(win.max()), 1e-6) else "-"
        top_mel = clean_t[-1] if clean_t else "-"
        onset_lines.append(f"{t:7.2f} | {top_mel:<4s} | {str(clean_t):<28s} | {b_note}")

    onsets_path = out_dir / "onsets.txt"
    onsets_path.write_text("\n".join(onset_lines) + "\n", encoding="utf-8")
    print(f"OK: Saved {json_path}")
    print(f"OK: Saved {summary_path}")
    print(f"OK: Saved {onsets_path}")


if __name__ == "__main__":
    main()
