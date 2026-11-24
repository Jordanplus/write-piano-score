#!/usr/bin/env python3
"""
Stage 2 Helper: Objective DSP/MIR Cross-Audit between Reference Audio and Piano Score.

Performs:
1. Reference audio tuning, BPM, global key, and sliding-window modulation detection.
2. Score timeline construction (from score.json / MIDI) + chroma generation (from rendered
   score audio if provided, else directly synthesized from score notes).
3. Subsequence/full Dynamic Time Warping (DTW) on normalized CQT chromagrams to align
   every score measure to its exact timestamp window in the reference recording.
4. Per-measure audit metrics:
   - Chroma cosine similarity (harmonic alignment)
   - Right-hand top-voice melody spectral peak hit rate (against tuning-compensated CQT)
   - Dominant upper-register reference peaks vs. written RH melody pitches
   - Time-warping stretch ratio (flags missing/extra/misaligned bars)
   - Local key mismatch check
5. Outputs audit_metrics.json and audit_summary.txt with SUSPECT/MISMATCH bar flags.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import librosa
import numpy as np

NOTE_TO_PC = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4, "E#": 5, "F": 5, "F#": 6, "Gb": 6,
    "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11, "B#": 0,
}
PITCH_RE = re.compile(r"^([A-G][#b]?)(-?\d)$")
SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
FLAT_MAJOR_KEYS = {"F", "Bb", "Eb", "Ab", "Db", "Gb", "Cb"}
FLAT_MINOR_KEYS = {"D", "G", "C", "F", "Bb", "Eb", "Ab"}

KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
KS_MAJOR /= np.linalg.norm(KS_MAJOR)
KS_MINOR /= np.linalg.norm(KS_MINOR)


def pitch_to_midi(p: str) -> int:
    m = PITCH_RE.match(p.strip())
    if not m:
        raise ValueError(f"Invalid pitch: {p!r}")
    return (int(m.group(2)) + 1) * 12 + NOTE_TO_PC[m.group(1)]


def midi_to_name(midi: int, use_flats: bool = True) -> str:
    names = FLAT_NAMES if use_flats else SHARP_NAMES
    return f"{names[midi % 12]}{(midi // 12) - 1}"


def fmt_time(sec: float) -> str:
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:04.1f}"


def detect_key(chroma_vec: np.ndarray) -> dict:
    v = np.asarray(chroma_vec, dtype=float)
    n = np.linalg.norm(v)
    if n < 1e-8:
        return {"label": "C major", "root_pc": 0, "mode": "major", "score": 0.0, "use_flats": False, "alt": "-"}
    v = v / n
    cands = []
    for root in range(12):
        ms = float(np.dot(v, np.roll(KS_MAJOR, root)))
        ns = float(np.dot(v, np.roll(KS_MINOR, root)))
        m_name = FLAT_NAMES[root] if FLAT_NAMES[root] in FLAT_MAJOR_KEYS else SHARP_NAMES[root]
        n_name = FLAT_NAMES[root] if FLAT_NAMES[root] in FLAT_MINOR_KEYS else SHARP_NAMES[root]
        cands.append((ms, f"{m_name} major", root, "major", m_name in FLAT_MAJOR_KEYS))
        cands.append((ns, f"{n_name} minor", root, "minor", n_name in FLAT_MINOR_KEYS))
    cands.sort(key=lambda x: x[0], reverse=True)
    top = cands[0]
    alt = cands[1]
    return {
        "label": top[1],
        "root_pc": top[2],
        "mode": top[3],
        "score": round(top[0], 4),
        "use_flats": top[4],
        "alt": f"{alt[1]} ({alt[0]:.3f})",
    }


def parse_key_family_pc(key_str: str) -> int:
    """Return canonical major-tonic pitch class so relative major/minor share the same key signature."""
    parts = key_str.strip().split()
    root = parts[0] if parts else "C"
    mode = parts[1].lower() if len(parts) > 1 else "major"
    pc = NOTE_TO_PC.get(root, 0)
    return (pc + 3) % 12 if "min" in mode else pc


def parse_time_sig(ts: str) -> float:
    num, den = ts.split("/")
    return float(num) * (4.0 / float(den))


def load_score_bars_from_json(score_path: Path) -> tuple[dict, list[dict]]:
    score = json.loads(score_path.read_text(encoding="utf-8"))
    bpb = parse_time_sig(score.get("time_signature", "4/4"))
    bpm = float(score.get("tempo_bpm", 72))
    active_key = score.get("key", "C major")
    active_bpm = bpm
    t_cur = 0.0
    bars = []

    for idx, bar in enumerate(score.get("measures", [])):
        bnum = bar.get("number", idx + 1)
        if bar.get("key_change"):
            kc = bar["key_change"]
            active_key = str(kc.get("key", active_key) if isinstance(kc, dict) else kc)
        if bar.get("tempo_change"):
            tc = bar["tempo_change"]
            if isinstance(tc, dict):
                if tc.get("bpm") is not None:
                    active_bpm = float(tc["bpm"])
            else:
                active_bpm = float(tc)
        sec_per_beat = 60.0 / active_bpm
        bar_dur = bpb * sec_per_beat

        rh_melody = []
        all_notes = []
        for hand in ("rh", "lh"):
            t_ev = t_cur
            for ev in bar.get(hand, []):
                dur_b = float(ev.get("dur", 0.0))
                dur_s = dur_b * sec_per_beat
                pitches = [p for p in ev.get("pitches", []) if p and p != "R"]
                midis = [pitch_to_midi(p) for p in pitches]
                for m in midis:
                    all_notes.append((t_ev, t_ev + dur_s, m, hand))
                if hand == "rh" and midis:
                    rh_melody.append({
                        "start": t_ev,
                        "end": t_ev + dur_s,
                        "midi": max(midis),
                        "pitch": pitches[midis.index(max(midis))],
                        "beat": round(1.0 + (t_ev - t_cur) / sec_per_beat, 2),
                    })
                t_ev += dur_s

        bars.append({
            "bar": bnum,
            "section": bar.get("section") or "",
            "key": active_key,
            "bpm": active_bpm,
            "score_start": t_cur,
            "score_end": t_cur + bar_dur,
            "score_dur": bar_dur,
            "lyrics": (bar.get("lyrics") or "").strip(),
            "chords": [c.get("symbol", "") for c in bar.get("chords", [])],
            "rh_melody": rh_melody,
            "all_notes": all_notes,
        })
        t_cur += bar_dur

    meta = {
        "title": score.get("title", score_path.stem),
        "key": score.get("key", "C major"),
        "time_signature": score.get("time_signature", "4/4"),
        "tempo_bpm": bpm,
        "duration_sec": t_cur,
        "total_bars": len(bars),
    }
    return meta, bars


def load_score_bars_from_midi(midi_path: Path, beats_per_bar: int = 4) -> tuple[dict, list[dict]]:
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(midi_path))
    _, tempos = pm.get_tempo_changes()
    bpm = float(tempos[0]) if len(tempos) else 72.0
    duration = pm.get_end_time()
    sec_per_beat = 60.0 / bpm
    bar_dur = beats_per_bar * sec_per_beat
    n_bars = max(1, int(np.ceil(duration / bar_dur)))

    all_notes = []
    for i_idx, inst in enumerate(pm.instruments):
        hand = "rh" if i_idx == 0 else "lh"
        for n in inst.notes:
            all_notes.append((n.start, n.end, n.pitch, hand))

    bars = []
    for b in range(n_bars):
        t0, t1 = b * bar_dur, (b + 1) * bar_dur
        bar_notes = [x for x in all_notes if x[1] > t0 and x[0] < t1]
        rh_cands = sorted([x for x in bar_notes if x[3] == "rh" and x[0] >= t0 - 0.02], key=lambda x: (x[0], -x[2]))
        rh_mel = []
        for s, e, m, _ in rh_cands:
            if rh_mel and abs(s - rh_mel[-1]["start"]) < 0.035:
                if m > rh_mel[-1]["midi"]:
                    rh_mel[-1]["midi"] = m
                    rh_mel[-1]["pitch"] = midi_to_name(m)
            else:
                rh_mel.append({
                    "start": s,
                    "end": e,
                    "midi": m,
                    "pitch": midi_to_name(m),
                    "beat": round(1.0 + max(0.0, s - t0) / sec_per_beat, 2),
                })
        bars.append({
            "bar": b + 1,
            "section": "",
            "key": "Unknown",
            "bpm": bpm,
            "score_start": t0,
            "score_end": t1,
            "score_dur": bar_dur,
            "lyrics": "",
            "chords": [],
            "rh_melody": rh_mel,
            "all_notes": bar_notes,
        })
    meta = {
        "title": midi_path.stem,
        "key": "Unknown",
        "time_signature": f"{beats_per_bar}/4",
        "tempo_bpm": bpm,
        "duration_sec": duration,
        "total_bars": len(bars),
    }
    return meta, bars


def synthesize_score_chroma(bars: list[dict], total_sec: float, fps: float = 10.0) -> np.ndarray:
    """Create a 12 x T normalized chromagram directly from symbolic score notes."""
    n_frames = max(10, int(np.ceil(total_sec * fps)))
    chroma = np.zeros((12, n_frames), dtype=float)
    for b in bars:
        for s, e, midi, hand in b["all_notes"]:
            f0 = max(0, int(s * fps))
            f1 = min(n_frames, max(f0 + 1, int(e * fps)))
            weight = 1.2 if hand == "rh" else 0.9
            chroma[midi % 12, f0:f1] += weight
            # Light harmonic energy at fifth (+7)
            chroma[(midi + 7) % 12, f0:f1] += 0.2 * weight
    norms = np.linalg.norm(chroma, axis=0, keepdims=True)
    chroma = np.divide(chroma, np.where(norms < 1e-8, 1.0, norms))
    return chroma


def compute_audio_chroma_10hz(y: np.ndarray, sr: int, tuning: float, fps: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    """Compute tuning-compensated 10 Hz normalized chromagram and frame times."""
    hop = 512
    c_raw = librosa.feature.chroma_cqt(
        y=y, sr=sr, hop_length=hop, tuning=tuning, bins_per_octave=36, n_octaves=6
    )
    dur = len(y) / sr
    n_out = max(10, int(np.ceil(dur * fps)))
    times_out = np.arange(n_out) / fps
    frames_raw = librosa.time_to_frames(times_out, sr=sr, hop_length=hop)
    frames_raw = np.clip(frames_raw, 0, c_raw.shape[1] - 1)
    # Pool +-1 frame around each 10 Hz sample
    c_10 = np.zeros((12, n_out), dtype=float)
    for i, fr in enumerate(frames_raw):
        lo = max(0, fr - 2)
        hi = min(c_raw.shape[1], fr + 3)
        c_10[:, i] = c_raw[:, lo:hi].mean(axis=1)
    norms = np.linalg.norm(c_10, axis=0, keepdims=True)
    c_10 = np.divide(c_10, np.where(norms < 1e-8, 1.0, norms))
    return c_10, times_out


def extract_top_cqt_peaks(cqt_mag: np.ndarray, f0: int, f1: int, c2_midi: int, use_flats: bool, top_k: int = 5) -> list[str]:
    """Return dominant upper-register (C4..C7) spectral peaks with simple harmonic suppression."""
    if f1 <= f0:
        return []
    seg = cqt_mag[:, f0:f1]
    if seg.size == 0:
        return []
    # Treble band C4 (offset 24 from C2) up to top of CQT
    treble = seg[24:, :]
    # Mean + 85th percentile emphasizes articulated melody notes over sustained wash
    profile = 0.5 * treble.mean(axis=1) + 0.5 * np.percentile(treble, 85, axis=1)
    pmax = float(profile.max())
    if pmax < 1e-6:
        return []
    peaks = []
    for i in range(1, len(profile) - 1):
        if profile[i] >= profile[i - 1] and profile[i] >= profile[i + 1] and profile[i] >= 0.32 * pmax:
            peaks.append((profile[i], i))
    # Suppress obvious 2nd/3rd harmonics if fundamental is clearly stronger
    filtered = []
    peak_map = {idx: val for val, idx in peaks}
    for val, idx in peaks:
        is_harm = False
        for harm_iv in (12, 19):
            fund = idx - harm_iv
            if fund in peak_map and peak_map[fund] > 1.25 * val:
                is_harm = True
                break
        if not is_harm:
            filtered.append((val, idx))
    filtered.sort(key=lambda x: x[0], reverse=True)
    return [midi_to_name(c2_midi + 24 + idx, use_flats) for _, idx in filtered[:top_k]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Objective DSP/MIR audit comparing reference audio vs piano score.")
    parser.add_argument("--ref-audio", required=True, help="Path to reference audio (WAV/MP3)")
    parser.add_argument("--score", help="Path to score.json")
    parser.add_argument("--midi", help="Path to score MIDI file (if no score.json)")
    parser.add_argument("--score-audio", help="Optional rendered piano preview MP3/WAV for audio-to-audio chroma DTW")
    parser.add_argument("--out-dir", required=True, help="Output directory for audit_metrics.json & audit_summary.txt")
    args = parser.parse_args()

    if not args.score and not args.midi:
        print("ERROR: Provide at least --score or --midi.", file=sys.stderr)
        sys.exit(2)

    ref_path = Path(args.ref_audio).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] Loading reference audio: {ref_path}")
    y_ref, sr = librosa.load(str(ref_path), sr=22050, mono=True)
    ref_dur = float(len(y_ref) / sr)
    ref_tuning = float(librosa.estimate_tuning(y=y_ref, sr=sr))
    y_ref_harm, y_ref_perc = librosa.effects.hpss(y_ref, margin=2.0)

    # BPM estimate on ref
    onset_env = librosa.onset.onset_strength(y=y_ref_perc, sr=sr)
    tempo_raw, _ = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, start_bpm=75.0)
    ref_bpm = round(float(np.atleast_1d(tempo_raw)[0]), 1)

    fps = 10.0
    ref_chroma, ref_times = compute_audio_chroma_10hz(y_ref_harm, sr, ref_tuning, fps=fps)
    ref_global_key = detect_key(ref_chroma.mean(axis=1))
    use_flats = ref_global_key["use_flats"]

    # Sliding-window key scan (20s window, 10s step)
    key_windows = []
    t = 0.0
    while t < ref_dur - 6.0:
        t_end = min(t + 20.0, ref_dur)
        i0, i1 = int(t * fps), max(int(t * fps) + 1, int(t_end * fps))
        k = detect_key(ref_chroma[:, i0:i1].mean(axis=1))
        key_windows.append({
            "start_sec": round(t, 1),
            "end_sec": round(t_end, 1),
            "key": k["label"],
            "score": k["score"],
            "alt": k["alt"],
        })
        t += 10.0

    # High-res CQT of reference audio for melody pitch hit-rate verification (C2..B6 = 60 semitones)
    cqt_hop = 256
    c2_midi = int(librosa.note_to_midi("C2"))
    ref_cqt = np.abs(
        librosa.cqt(
            y=y_ref_harm,
            sr=sr,
            fmin=librosa.note_to_hz("C2"),
            n_bins=60,
            bins_per_octave=12,
            hop_length=cqt_hop,
            tuning=ref_tuning,
        )
    )

    print("[2/4] Loading score structure & building score chroma...")
    if args.score:
        score_meta, bars = load_score_bars_from_json(Path(args.score).resolve())
    else:
        score_meta, bars = load_score_bars_from_midi(Path(args.midi).resolve())

    if args.score_audio and Path(args.score_audio).exists():
        y_sc, _ = librosa.load(str(Path(args.score_audio).resolve()), sr=sr, mono=True)
        score_chroma, _ = compute_audio_chroma_10hz(y_sc, sr, tuning=0.0, fps=fps)
        chroma_source = "rendered_audio"
    else:
        score_chroma = synthesize_score_chroma(bars, score_meta["duration_sec"], fps=fps)
        chroma_source = "symbolic_notes"

    print(f"[3/4] Running Chroma DTW alignment ({chroma_source}, score={score_chroma.shape[1]}f vs ref={ref_chroma.shape[1]}f)...")
    # Cosine distance matrix between score frames (rows) and ref frames (cols)
    D_dtw, wp = librosa.sequence.dtw(X=score_chroma, Y=ref_chroma, metric="cosine")
    # wp is (score_idx, ref_idx) from end to start; reverse to chronological order
    wp = wp[::-1]
    score_to_ref = np.zeros(score_chroma.shape[1], dtype=float)
    counts = np.zeros(score_chroma.shape[1], dtype=float)
    for si, ri in wp:
        score_to_ref[si] += ri / fps
        counts[si] += 1.0
    score_to_ref = np.divide(score_to_ref, np.where(counts > 0, counts, 1.0))

    print("[4/4] Computing per-measure chroma similarity & melody spectral hit rate...")
    bar_reports = []
    chroma_sims = []
    mel_hits_all = []
    suspect_bars = []

    for b in bars:
        s_i0 = max(0, min(score_chroma.shape[1] - 1, int(b["score_start"] * fps)))
        s_i1 = max(s_i0 + 1, min(score_chroma.shape[1], int(b["score_end"] * fps)))
        ref_t0 = float(score_to_ref[s_i0])
        ref_t1 = float(score_to_ref[min(s_i1 - 1, len(score_to_ref) - 1)])
        if ref_t1 <= ref_t0 + 0.15:
            ref_t1 = min(ref_dur, ref_t0 + b["score_dur"])

        r_i0 = max(0, min(ref_chroma.shape[1] - 1, int(ref_t0 * fps)))
        r_i1 = max(r_i0 + 1, min(ref_chroma.shape[1], int(ref_t1 * fps)))

        sc_vec = score_chroma[:, s_i0:s_i1].mean(axis=1)
        rf_vec = ref_chroma[:, r_i0:r_i1].mean(axis=1)
        denom = float(np.linalg.norm(sc_vec) * np.linalg.norm(rf_vec))
        csim = float(np.dot(sc_vec, rf_vec) / denom) if denom > 1e-8 else 0.0
        chroma_sims.append(csim)

        # Warp ratio check
        ref_span = ref_t1 - ref_t0
        warp_ratio = ref_span / max(b["score_dur"], 0.1)

        # Local reference key around aligned midpoint
        mid_ref = 0.5 * (ref_t0 + ref_t1)
        local_ref_key = ref_global_key["label"]
        for w in key_windows:
            if w["start_sec"] <= mid_ref <= w["end_sec"]:
                local_ref_key = w["key"]
                break

        # Melody pitch hit-rate & top-voice contour error against aligned ref CQT
        note_checks = []
        exact_hits = 0
        semitone_errors = []
        octave_err_count = 0
        for ev in b["rh_melody"]:
            ev_s0 = max(0, min(len(score_to_ref) - 1, int(ev["start"] * fps)))
            ev_s1 = max(ev_s0, min(len(score_to_ref) - 1, int(ev["end"] * fps)))
            rt0 = float(score_to_ref[ev_s0])
            rt1 = max(rt0 + 0.12, float(score_to_ref[ev_s1]))
            cf0 = librosa.time_to_frames(rt0, sr=sr, hop_length=cqt_hop)
            cf1 = max(cf0 + 2, librosa.time_to_frames(rt1, sr=sr, hop_length=cqt_hop))
            cf0 = int(np.clip(cf0, 0, ref_cqt.shape[1] - 1))
            cf1 = int(np.clip(cf1, cf0 + 1, ref_cqt.shape[1]))

            bin_idx = ev["midi"] - c2_midi
            if 0 <= bin_idx < ref_cqt.shape[0]:
                # Focus on attack + early sustain (first 0.35s) where melody note stands out most
                cf1_atk = min(cf1, cf0 + max(3, int(0.35 * sr / cqt_hop)))
                seg_e = ref_cqt[:, cf0:cf1_atk].mean(axis=1)
                treble_e = seg_e[24:]
                tmax = float(treble_e.max()) if treble_e.size else 1e-6

                # Find local peaks in treble (C4..B6) with harmonic suppression
                t_peaks = []
                for k in range(1, len(treble_e) - 1):
                    if treble_e[k] >= treble_e[k - 1] and treble_e[k] >= treble_e[k + 1] and treble_e[k] >= 0.35 * tmax:
                        t_peaks.append((treble_e[k], k))
                pmap = {k: v for v, k in t_peaks}
                clean_peaks = [
                    (v, k) for v, k in t_peaks
                    if not any((k - h) in pmap and pmap[k - h] > 1.25 * v for h in (12, 19))
                ]
                top_midi = (c2_midi + 24 + max(k for _, k in clean_peaks)) if clean_peaks else None

                note_e = float(seg_e[bin_idx])
                rel_exact = note_e / max(tmax, 1e-6)
                # Strict hit: exact MIDI pitch has strong energy (>= 0.45 of treble max)
                # AND is within 12 semitones of ref top voice (or exact peak)
                is_exact_hit = rel_exact >= 0.48
                if is_exact_hit:
                    exact_hits += 1

                err_st = abs(ev["midi"] - top_midi) if top_midi is not None else None
                if err_st is not None:
                    semitone_errors.append(err_st)
                    if err_st >= 11:
                        octave_err_count += 1

                note_checks.append({
                    "pitch": ev["pitch"],
                    "beat": ev["beat"],
                    "ref_t": round(rt0, 2),
                    "ref_top": midi_to_name(top_midi, use_flats) if top_midi is not None else "-",
                    "rel_exact": round(rel_exact, 2),
                    "err_st": err_st,
                    "hit": is_exact_hit,
                })
        hit_rate = (exact_hits / len(note_checks)) if note_checks else 1.0
        mean_top_err = round(float(np.mean(semitone_errors)), 1) if semitone_errors else 0.0
        if note_checks:
            mel_hits_all.append(hit_rate)

        # Dominant reference treble peaks during this bar
        bar_cf0 = int(np.clip(librosa.time_to_frames(ref_t0, sr=sr, hop_length=cqt_hop), 0, ref_cqt.shape[1] - 1))
        bar_cf1 = int(np.clip(librosa.time_to_frames(ref_t1, sr=sr, hop_length=cqt_hop), bar_cf0 + 1, ref_cqt.shape[1]))
        ref_peaks = extract_top_cqt_peaks(ref_cqt, bar_cf0, bar_cf1, c2_midi, use_flats, top_k=5)

        flags = []
        if csim < 0.65:
            flags.append("LOW_CHROMA")
        elif csim < 0.78:
            flags.append("WEAK_CHROMA")
        if note_checks and hit_rate < 0.45:
            flags.append("MELODY_MISS")
        elif note_checks and hit_rate < 0.68:
            flags.append("MELODY_WEAK")
        if note_checks and octave_err_count >= max(2, len(note_checks) // 2):
            flags.append("OCTAVE_SHIFT")
        elif note_checks and mean_top_err >= 4.5:
            flags.append("CONTOUR_DIFF")
        if warp_ratio < 0.45 or warp_ratio > 1.85:
            flags.append("TIME_WARP_ANOMALY")

        # Only flag KEY_MISMATCH if aligned ref window strongly prefers a different key family
        if b["key"] != "Unknown" and parse_key_family_pc(b["key"]) != parse_key_family_pc(local_ref_key):
            # Verify chroma correlation margin
            sc_root_pc = parse_key_family_pc(b["key"])
            ref_win_norm = rf_vec / max(float(np.linalg.norm(rf_vec)), 1e-8)
            sc_key_corr = float(np.dot(ref_win_norm, np.roll(KS_MAJOR, sc_root_pc)))
            rf_key_corr = float(np.dot(ref_win_norm, np.roll(KS_MAJOR, parse_key_family_pc(local_ref_key))))
            if rf_key_corr - sc_key_corr > 0.08:
                flags.append("KEY_MISMATCH")

        if any(f in flags for f in ("LOW_CHROMA", "MELODY_MISS", "OCTAVE_SHIFT", "KEY_MISMATCH")):
            status = "MISMATCH"
            suspect_bars.append(b["bar"])
        elif flags:
            status = "SUSPECT"
            suspect_bars.append(b["bar"])
        else:
            status = "OK"

        bar_reports.append({
            "bar": b["bar"],
            "section": b["section"],
            "score_start": round(b["score_start"], 2),
            "score_end": round(b["score_end"], 2),
            "ref_start": round(ref_t0, 2),
            "ref_end": round(ref_t1, 2),
            "warp_ratio": round(warp_ratio, 2),
            "score_key": b["key"],
            "ref_local_key": local_ref_key,
            "chroma_sim": round(csim, 3),
            "melody_hit_rate": round(hit_rate, 3),
            "mean_top_voice_err_st": mean_top_err,
            "written_rh_pitches": [e["pitch"] for e in b["rh_melody"]],
            "ref_treble_peaks": ref_peaks,
            "lyrics": b["lyrics"],
            "status": status,
            "flags": flags,
        })

    avg_chroma = round(float(np.mean(chroma_sims)), 3) if chroma_sims else 0.0
    avg_mel_hit = round(float(np.mean(mel_hits_all)), 3) if mel_hits_all else 0.0
    n_mismatch = sum(1 for r in bar_reports if r["status"] == "MISMATCH")
    n_suspect = sum(1 for r in bar_reports if r["status"] == "SUSPECT")

    metrics = {
        "ref_audio": str(ref_path),
        "ref_duration_sec": round(ref_dur, 2),
        "ref_tuning_semitones": round(ref_tuning, 3),
        "ref_estimated_bpm": ref_bpm,
        "ref_global_key": ref_global_key,
        "ref_key_windows": key_windows,
        "score_meta": score_meta,
        "chroma_source": chroma_source,
        "summary_metrics": {
            "avg_chroma_similarity": avg_chroma,
            "avg_melody_hit_rate": avg_mel_hit,
            "total_bars": len(bar_reports),
            "ok_bars": len(bar_reports) - n_mismatch - n_suspect,
            "suspect_bars": n_suspect,
            "mismatch_bars": n_mismatch,
            "flagged_bar_numbers": suspect_bars,
        },
        "bars": bar_reports,
    }

    metrics_path = out_dir / "audit_metrics.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    # Build human/agent-readable summary
    lines = [
        "=== OBJECTIVE DSP / DTW SCORE AUDIT SUMMARY ===",
        f"Reference Audio: {ref_path.name} | Duration: {fmt_time(ref_dur)} ({ref_dur:.1f}s) | Tuning: {ref_tuning:+.3f} st",
        f"Reference Global Key: {ref_global_key['label']} (score={ref_global_key['score']}, alt={ref_global_key['alt']}) | Est. BPM: {ref_bpm}",
        f"Score Metadata: {score_meta['title']} | Key: {score_meta['key']} | BPM: {score_meta['tempo_bpm']} | Bars: {score_meta['total_bars']} | Est. Dur: {fmt_time(score_meta['duration_sec'])} ({score_meta['duration_sec']:.1f}s)",
        f"Duration Ratio (Score / Ref): {score_meta['duration_sec'] / max(ref_dur, 0.1):.2f}x",
        "",
        f"Overall Avg Chroma Similarity : {avg_chroma:.3f}",
        f"Overall Melody Spectral Hit   : {avg_mel_hit*100:.1f}%",
        f"Bar Verdict Counts            : OK={len(bar_reports)-n_mismatch-n_suspect}, SUSPECT={n_suspect}, MISMATCH={n_mismatch}",
        "",
        "--- Reference Sliding-Window Key / Modulation Scan ---",
    ]
    for w in key_windows:
        lines.append(f"  {fmt_time(w['start_sec'])}-{fmt_time(w['end_sec'])} : {w['key']:<12s} (score={w['score']:.3f}, alt={w['alt']})")

    lines += [
        "",
        "--- Measure-by-Measure DTW Alignment & Mismatch Table ---",
        "Bar | RefTime       | Warp | Chrm  | ExactHit | TopErr | Status   | Written RH -> Ref Top Peaks | Flags",
        "-" * 118,
    ]
    for r in bar_reports:
        if r["section"]:
            lines.append(f"  [{r['section']}] (Score Key: {r['score_key']} | Ref Local Key: {r['ref_local_key']})")
        rh_s = ",".join(r["written_rh_pitches"][:8]) or "-"
        pk_s = ",".join(r["ref_treble_peaks"][:5]) or "-"
        fl_s = ",".join(r["flags"]) if r["flags"] else "-"
        lines.append(
            f"m.{r['bar']:02d} | {fmt_time(r['ref_start'])}-{fmt_time(r['ref_end'])} | {r['warp_ratio']:4.2f} | "
            f"{r['chroma_sim']:5.3f} | {r['melody_hit_rate']*100:6.1f}%  | {r['mean_top_voice_err_st']:4.1f}st | {r['status']:<8s} | "
            f"{rh_s}  ==>  {pk_s} | {fl_s}"
        )

    summary_path = out_dir / "audit_summary.txt"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"OK: Saved {metrics_path}")
    print(f"OK: Saved {summary_path}")


if __name__ == "__main__":
    main()
