#!/usr/bin/env python3
"""
Stage 3: Strict 1:1 Source-vs-Extracted MIDI Fidelity Comparison & Missed-Top-Voice Gate.

Compares `extracted_raw.mid` (+ its FluidSynth audio render `extracted_preview.mp3`) directly
against the original reference audio `audio.wav` on an EXACT 1:1 timestamp grid (no DTW slack!).

Checks:
1. Per-2s-window 1:1 CQT Chroma Cosine Similarity (harmonic fidelity)
2. Per-2s-window Treble (C4..C7) log-CQT Spectral Correlation
3. Missed Higher Top-Voice Detector (8192-pt STFT f0 + 2*f0 harmonic verification):
   specifically catches rolled chords / softer melody top notes (like D5 above Bb4!)
   that naive extractors drop!
4. Spurious/Hallucinated Note Detector (notes in extracted MIDI with weak ref spectral support)
5. Optional synchronized MP3 clipping of every flagged window (`--slice-suspects`)
6. Gate Verdict: EXTRACT_PASS / EXTRACT_WARN / EXTRACT_FAIL
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import librosa
import numpy as np
import pretty_midi

FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def midi_to_name(m: int) -> str:
    return f"{FLAT_NAMES[m % 12]}{(m // 12) - 1}"


def fmt_time(sec: float) -> str:
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:04.1f}"


def cut_mp3(src: Path, dst: Path, start_sec: float, end_sec: float) -> None:
    s0 = max(0.0, start_sec)
    dur = max(1.0, end_sec - s0)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s0:.2f}", "-t", f"{dur:.2f}",
         "-i", str(src), "-ac", "1", "-ar", "22050", "-b:a", "128k", str(dst)],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Strict 1:1 comparison between source audio and extracted MIDI.")
    parser.add_argument("--ref-audio", required=True, help="Original source audio WAV/MP3")
    parser.add_argument("--extracted-midi", required=True, help="Raw extracted MIDI file")
    parser.add_argument("--extracted-audio", help="Optional FluidSynth preview MP3/WAV of extracted MIDI")
    parser.add_argument("--out-dir", required=True, help="Output directory for comparison report")
    parser.add_argument("--window-sec", type=float, default=2.0, help="Comparison window length (default 2.0s)")
    parser.add_argument("--slice-suspects", action="store_true", help="Slice orig vs ext MP3 pairs for flagged windows")
    args = parser.parse_args()

    ref_path = Path(args.ref_audio).resolve()
    midi_path = Path(args.extracted_midi).resolve()
    ext_audio_path = Path(args.extracted_audio).resolve() if args.extracted_audio else None
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] Loading reference audio: {ref_path}")
    y_ref, sr = librosa.load(str(ref_path), sr=22050, mono=True)
    dur = float(len(y_ref) / sr)
    tuning = float(librosa.estimate_tuning(y=y_ref, sr=sr))
    y_ref_harm, _ = librosa.effects.hpss(y_ref, margin=1.5)

    hop = 512
    ref_chroma = librosa.feature.chroma_cqt(y=y_ref_harm, sr=sr, hop_length=hop, tuning=tuning, bins_per_octave=36, n_octaves=6)
    ref_cqt = np.abs(librosa.cqt(y=y_ref_harm, sr=sr, fmin=librosa.note_to_hz("C2"), n_bins=60, bins_per_octave=12, hop_length=hop, tuning=tuning))
    ref_rms = librosa.feature.rms(y=y_ref, hop_length=hop)[0]
    ref_rms_thr = float(np.percentile(ref_rms, 15)) * 1.5 + 1e-4

    # High-res 8192-pt STFT for f0 + 2*f0 missed-top-voice verification
    n_fft = 8192
    S_ref = np.abs(librosa.stft(y_ref, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    print(f"[2/4] Loading extracted MIDI ({midi_path.name}) & building 1:1 comparison features...")
    pm = pretty_midi.PrettyMIDI(str(midi_path))
    all_notes = [n for inst in pm.instruments for n in inst.notes]
    all_notes.sort(key=lambda n: (n.start, -n.pitch))

    n_frames = ref_chroma.shape[1]
    if ext_audio_path and ext_audio_path.exists():
        y_ext, _ = librosa.load(str(ext_audio_path), sr=sr, mono=True)
        # Pad or trim to exact ref length for strict 1:1 alignment
        if len(y_ext) < len(y_ref):
            y_ext = np.pad(y_ext, (0, len(y_ref) - len(y_ext)))
        else:
            y_ext = y_ext[: len(y_ref)]
        y_ext_harm, _ = librosa.effects.hpss(y_ext, margin=1.5)
        ext_chroma = librosa.feature.chroma_cqt(y=y_ext_harm, sr=sr, hop_length=hop, tuning=0.0, bins_per_octave=36, n_octaves=6)[:, :n_frames]
        ext_cqt = np.abs(librosa.cqt(y=y_ext_harm, sr=sr, fmin=librosa.note_to_hz("C2"), n_bins=60, bins_per_octave=12, hop_length=hop))[:, :n_frames]
        compare_mode = "audio_to_audio_1to1"
    else:
        # Synthesize symbolic chroma & CQT mask directly on the exact same frame grid
        ext_chroma = np.zeros_like(ref_chroma)
        ext_cqt = np.zeros_like(ref_cqt)
        for n in all_notes:
            f0 = int(np.clip(librosa.time_to_frames(n.start, sr=sr, hop_length=hop), 0, n_frames - 1))
            f1 = int(np.clip(librosa.time_to_frames(n.end, sr=sr, hop_length=hop), f0 + 1, n_frames))
            w = n.velocity / 127.0
            ext_chroma[n.pitch % 12, f0:f1] += w
            b = n.pitch - 36
            if 0 <= b < 60:
                ext_cqt[b, f0:f1] += w
        compare_mode = "symbolic_to_audio_1to1"

    print("[3/4] Auditing 2-second windows + scanning for missed higher top-voice notes...")
    windows = []
    chroma_sims = []
    treble_corrs = []
    missed_top_events = []

    t = 0.0
    while t < dur - 0.5:
        t_end = min(t + args.window_sec, dur)
        f0 = int(np.clip(librosa.time_to_frames(t, sr=sr, hop_length=hop), 0, n_frames - 1))
        f1 = int(np.clip(librosa.time_to_frames(t_end, sr=sr, hop_length=hop), f0 + 1, n_frames))
        if float(ref_rms[f0:f1].mean()) < ref_rms_thr:
            t += args.window_sec
            continue

        rc = ref_chroma[:, f0:f1].mean(axis=1)
        ec = ext_chroma[:, f0:f1].mean(axis=1)
        denom = float(np.linalg.norm(rc) * np.linalg.norm(ec))
        csim = float(np.dot(rc, ec) / denom) if denom > 1e-8 else 0.0
        chroma_sims.append(csim)

        # Log-CQT treble (C4..C7 = bins 24..60) Pearson correlation
        rt = np.log1p(ref_cqt[24:, f0:f1] * 10.0).ravel()
        et = np.log1p(ext_cqt[24:, f0:f1] * 10.0).ravel()
        if np.std(rt) > 1e-6 and np.std(et) > 1e-6:
            tcorr = float(np.corrcoef(rt, et)[0, 1])
        else:
            tcorr = 0.0
        treble_corrs.append(tcorr)

        # Check active extracted notes in this window
        win_notes = [n for n in all_notes if n.end > t and n.start < t_end]
        ext_max_midi = max((n.pitch for n in win_notes), default=55)

        # Scan high-res STFT for any note STRICTLY higher than ext_max_midi (1..7 semitones)
        # that has BOTH strong f0 AND strong 2*f0 harmonic confirmation!
        lo_idx = np.searchsorted(freqs, 260.0)
        hi_idx = np.searchsorted(freqs, 1400.0)
        win_max_e = float(S_ref[lo_idx:hi_idx, f0:f1].max()) if f1 > f0 else 1.0
        missed_here = []
        for cand_m in range(max(60, ext_max_midi + 1), min(92, ext_max_midi + 8)):
            hz1 = librosa.midi_to_hz(cand_m)
            hz2 = hz1 * 2.0
            k1 = int(np.argmin(np.abs(freqs - hz1)))
            k2 = int(np.argmin(np.abs(freqs - hz2)))
            e1 = float(S_ref[max(0, k1 - 1) : k1 + 2, f0:f1].max())
            e2 = float(S_ref[max(0, k2 - 1) : k2 + 2, f0:f1].max()) if k2 < len(freqs) - 1 else 0.0
            # Avoid octave-harmonic false alarms from an existing note 12st below
            sub_m = cand_m - 12
            has_loud_sub = any(n.pitch == sub_m for n in win_notes)
            # Also avoid 19st (3rd harmonic) false alarm
            has_loud_19 = any(n.pitch == cand_m - 19 for n in win_notes)
            if has_loud_sub or has_loud_19:
                continue
            if e1 >= 0.22 * win_max_e and e2 >= 0.09 * win_max_e:
                t_pk = float(librosa.frames_to_time(f0 + int(np.argmax(S_ref[k1, f0:f1])), sr=sr, hop_length=hop))
                missed_here.append({"pitch": midi_to_name(cand_m), "midi": cand_m, "time": round(t_pk, 2), "rel_f0": round(e1 / max(win_max_e, 1e-6), 2)})

        flags = []
        if csim < 0.80:
            flags.append("LOW_CHROMA")
        elif csim < 0.88:
            flags.append("WEAK_CHROMA")
        if tcorr < 0.65:
            flags.append("LOW_TREBLE_CORR")
        if missed_here:
            flags.append("MISSED_TOP_VOICE")
            missed_top_events.extend(missed_here)

        status = "FAIL" if ("LOW_CHROMA" in flags or "LOW_TREBLE_CORR" in flags) else ("WARN" if flags else "OK")
        windows.append({
            "start_sec": round(t, 1),
            "end_sec": round(t_end, 1),
            "chroma_sim": round(csim, 3),
            "treble_corr": round(tcorr, 3),
            "ext_note_count": len(win_notes),
            "ext_max_pitch": midi_to_name(ext_max_midi) if win_notes else "-",
            "missed_top_candidates": missed_here,
            "status": status,
            "flags": flags,
        })
        t += args.window_sec

    print("[4/4] Checking for spurious/hallucinated notes in extracted MIDI...")
    spurious_notes = []
    checked_notes = 0
    for n in all_notes:
        if n.start > dur - 0.5 or n.pitch < 36 or n.pitch >= 96:
            continue
        checked_notes += 1
        f0 = int(np.clip(librosa.time_to_frames(n.start, sr=sr, hop_length=hop), 0, n_frames - 1))
        f1 = int(np.clip(librosa.time_to_frames(min(n.end, n.start + 0.35), sr=sr, hop_length=hop), f0 + 1, n_frames))
        b = n.pitch - 36
        seg = ref_cqt[:, f0:f1].mean(axis=1)
        band = seg[24:] if b >= 24 else seg[:24]
        bmax = float(band.max()) if band.size else 1e-6
        rel = float(seg[b]) / max(bmax, 1e-6)
        if rel < 0.14:
            spurious_notes.append({"time": round(n.start, 2), "pitch": midi_to_name(n.pitch), "vel": n.velocity, "rel_e": round(rel, 3)})

    avg_chroma = round(float(np.mean(chroma_sims)), 3) if chroma_sims else 0.0
    avg_treble = round(float(np.mean(treble_corrs)), 3) if treble_corrs else 0.0
    missed_win_ratio = sum(1 for w in windows if "MISSED_TOP_VOICE" in w["flags"]) / max(len(windows), 1)
    spurious_ratio = len(spurious_notes) / max(checked_notes, 1)

    if avg_chroma >= 0.90 and avg_treble >= 0.78 and missed_win_ratio < 0.08 and spurious_ratio < 0.10:
        verdict = "EXTRACT_PASS"
    elif avg_chroma >= 0.80 and avg_treble >= 0.65 and missed_win_ratio <= 0.20 and spurious_ratio <= 0.18:
        verdict = "EXTRACT_WARN"
    else:
        verdict = "EXTRACT_FAIL"

    # Optional suspect clip slicing
    sliced_clips = []
    if args.slice_suspects:
        clips_dir = out_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        flagged = [w for w in windows if w["status"] != "OK"]
        # Merge contiguous flagged windows (gap <= 2s)
        groups = []
        for w in flagged:
            if groups and w["start_sec"] <= groups[-1][1] + 2.0:
                groups[-1][1] = w["end_sec"]
                groups[-1][2].update(w["flags"])
            else:
                groups.append([w["start_sec"], w["end_sec"], set(w["flags"])])
        for idx, (s0, s1, fl) in enumerate(groups[:10], 1):
            c0, c1 = max(0.0, s0 - 1.5), min(dur, s1 + 1.5)
            o_mp3 = clips_dir / f"suspect{idx:02d}_orig_{int(c0):03d}-{int(c1):03d}s.mp3"
            cut_mp3(ref_path, o_mp3, c0, c1)
            entry = {"group": idx, "range": f"{c0:.1f}s-{c1:.1f}s", "flags": sorted(fl), "orig_clip": str(o_mp3)}
            if ext_audio_path and ext_audio_path.exists():
                e_mp3 = clips_dir / f"suspect{idx:02d}_ext_{int(c0):03d}-{int(c1):03d}s.mp3"
                cut_mp3(ext_audio_path, e_mp3, c0, c1)
                entry["ext_clip"] = str(e_mp3)
            sliced_clips.append(entry)

    result = {
        "verdict": verdict,
        "compare_mode": compare_mode,
        "ref_audio": str(ref_path),
        "extracted_midi": str(midi_path),
        "metrics": {
            "avg_chroma_sim_1to1": avg_chroma,
            "avg_treble_spectral_corr": avg_treble,
            "total_active_windows": len(windows),
            "ok_windows": sum(1 for w in windows if w["status"] == "OK"),
            "warn_windows": sum(1 for w in windows if w["status"] == "WARN"),
            "fail_windows": sum(1 for w in windows if w["status"] == "FAIL"),
            "missed_top_voice_window_ratio": round(missed_win_ratio, 3),
            "checked_notes": checked_notes,
            "spurious_notes_count": len(spurious_notes),
            "spurious_notes_ratio": round(spurious_ratio, 3),
        },
        "missed_top_voice_events": missed_top_events,
        "spurious_notes_sample": spurious_notes[:30],
        "sliced_clips": sliced_clips,
        "windows": windows,
    }

    json_path = out_dir / "extraction_compare.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "=== SOURCE vs EXTRACTED MIDI 1:1 FIDELITY COMPARISON ===",
        f"Gate Verdict                  : {verdict}",
        f"Compare Mode                  : {compare_mode}",
        f"1:1 Avg Chroma Similarity     : {avg_chroma:.3f}",
        f"1:1 Treble Spectral Corr      : {avg_treble:.3f}",
        f"Active 2s Windows             : OK={result['metrics']['ok_windows']} | WARN={result['metrics']['warn_windows']} | FAIL={result['metrics']['fail_windows']}",
        f"Missed Top-Voice Window Ratio : {missed_win_ratio*100:.1f}% ({len(missed_top_events)} candidate events)",
        f"Spurious Notes Ratio          : {spurious_ratio*100:.1f}% ({len(spurious_notes)}/{checked_notes})",
        "",
        "--- Flagged Windows (WARN / FAIL) ---",
        "Time          | Chrm  | TCorr | ExtMax | Status | Flags / Missed Top Candidates",
        "-" * 95,
    ]
    for w in windows:
        if w["status"] != "OK":
            mstr = ", ".join(f"{m['pitch']}@{m['time']}s({m['rel_f0']})" for m in w["missed_top_candidates"])
            fl = ",".join(w["flags"])
            lines.append(
                f"{fmt_time(w['start_sec'])}-{fmt_time(w['end_sec'])} | {w['chroma_sim']:5.3f} | {w['treble_corr']:5.3f} | "
                f"{w['ext_max_pitch']:<6s} | {w['status']:<6s} | {fl}" + (f" -> {mstr}" if mstr else "")
            )
    if sliced_clips:
        lines += ["", "--- Auto-Sliced Side-by-Side Review Clips ---"]
        for c in sliced_clips:
            lines.append(f"  [{c['group']:02d}] {c['range']} ({','.join(c['flags'])})")
            lines.append(f"       ORIG: {c['orig_clip']}")
            if "ext_clip" in c:
                lines.append(f"       EXT : {c['ext_clip']}")

    sum_path = out_dir / "extraction_compare_summary.txt"
    sum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"OK: Verdict = {verdict} | Saved {sum_path}")


if __name__ == "__main__":
    main()
