#!/usr/bin/env python3
"""
Stage 1 Helper: Extract measure-by-measure melody, scale degrees (jianpu), chords,
sections, key changes, and lyrics from score.json or a standard MIDI file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NOTE_TO_PC = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4, "E#": 5, "F": 5, "F#": 6, "Gb": 6,
    "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11, "B#": 0,
}
PITCH_RE = re.compile(r"^([A-G][#b]?)(-?\d)$")
SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
JIANPU_MAJOR = ["1", "#1", "2", "b3", "3", "4", "#4", "5", "#5", "6", "b7", "7"]


def pitch_to_midi(p: str) -> int:
    m = PITCH_RE.match(p.strip())
    if not m:
        raise ValueError(f"Invalid pitch: {p!r}")
    return (int(m.group(2)) + 1) * 12 + NOTE_TO_PC[m.group(1)]


def midi_to_name(midi: int, use_flats: bool = True) -> str:
    names = FLAT_NAMES if use_flats else SHARP_NAMES
    return f"{names[midi % 12]}{(midi // 12) - 1}"


def parse_key_tonic_pc(key_str: str) -> tuple[int, bool]:
    """Return (major_tonic_pc, is_minor) for movable-do jianpu calculation."""
    parts = key_str.strip().split()
    root = parts[0] if parts else "C"
    mode = parts[1].lower() if len(parts) > 1 else "major"
    pc = NOTE_TO_PC.get(root, 0)
    if "min" in mode:
        # Relative major tonic for standard Chinese jianpu (6 = minor tonic)
        return (pc + 3) % 12, True
    return pc, False


def midi_to_jianpu(midi: int, tonic_pc: int, ref_octave_midi: int = 60) -> str:
    """Convert MIDI note to movable-do jianpu token with octave dots (., ')."""
    # Find tonic closest to C4 (60)
    base_tonic = 60 + tonic_pc
    if base_tonic > 66:
        base_tonic -= 12
    rel = midi - base_tonic
    oct_shift = rel // 12
    deg = JIANPU_MAJOR[rel % 12]
    if oct_shift > 0:
        return deg + ("'" * oct_shift)
    if oct_shift < 0:
        return deg + ("," * (-oct_shift))
    return deg


def parse_time_sig(ts: str) -> float:
    num, den = ts.split("/")
    return float(num) * (4.0 / float(den))


def extract_from_score_json(score_path: Path) -> str:
    score = json.loads(score_path.read_text(encoding="utf-8"))
    title = score.get("title", score_path.stem)
    key_str = score.get("key", "C major")
    ts = score.get("time_signature", "4/4")
    bpm = float(score.get("tempo_bpm", 72))
    bpb = parse_time_sig(ts)
    measures = score.get("measures", [])

    active_key = key_str
    active_bpm = bpm
    tonic_pc, _ = parse_key_tonic_pc(score.get("jianpu_tonic") or active_key)

    total_sec = 0.0
    sections: list[tuple[int, str, str, float]] = []
    rows: list[str] = []

    for idx, bar in enumerate(measures):
        bar_num = bar.get("number", idx + 1)
        if bar.get("key_change"):
            kc = bar["key_change"]
            active_key = str(kc.get("key", active_key) if isinstance(kc, dict) else kc)
            tonic_pc, _ = parse_key_tonic_pc(active_key)
        if bar.get("tempo_change"):
            tc = bar["tempo_change"]
            if isinstance(tc, dict):
                if tc.get("bpm") is not None:
                    active_bpm = float(tc["bpm"])
            else:
                active_bpm = float(tc)

        bar_start_sec = total_sec
        bar_dur_sec = bpb * (60.0 / active_bpm)
        total_sec += bar_dur_sec

        sec_label = bar.get("section") or ""
        if sec_label:
            sections.append((bar_num, sec_label, active_key, bar_start_sec))

        chords = ", ".join(
            f"@{c.get('beat', 1.0):g}:{c.get('symbol', '')}" for c in bar.get("chords", [])
        ) or "-"
        lyrics = (bar.get("lyrics") or "").strip()

        rh_events = bar.get("rh", [])
        beat = 1.0
        mel_tokens: list[str] = []
        jp_tokens: list[str] = []
        for ev in rh_events:
            dur = float(ev.get("dur", 0.0))
            pitches = [p for p in ev.get("pitches", []) if p and p != "R"]
            tie_mark = "~" if ev.get("tie") else ""
            if not pitches:
                mel_tokens.append(f"@{beat:g}:R({dur:g})")
                jp_tokens.append("0")
            else:
                midis = [(pitch_to_midi(p), p) for p in pitches]
                midis.sort(key=lambda x: x[0])
                top_m, top_p = midis[-1]
                poly_tag = f"[{len(midis)}v]" if len(midis) > 1 else ""
                mel_tokens.append(f"@{beat:g}:{top_p}{poly_tag}({dur:g}){tie_mark}")
                jp_tokens.append(f"{midi_to_jianpu(top_m, tonic_pc)}{tie_mark}")
            beat += dur

        mm = int(bar_start_sec // 60)
        ss = bar_start_sec - mm * 60
        key_tag = f" [KEY->{active_key}]" if bar.get("key_change") else ""
        sec_tag = f" === {sec_label} ===" if sec_label else ""
        if sec_tag or key_tag:
            rows.append(f"\n>>> Bar {bar_num:02d} ({mm:02d}:{ss:04.1f}){sec_tag}{key_tag}")

        rows.append(
            f"m.{bar_num:02d} ({mm:02d}:{ss:04.1f}) | Chords: {chords:<22s} | Jianpu: {' '.join(jp_tokens)}"
        )
        rows.append(f"     RH Top: {'  '.join(mel_tokens)}")
        if lyrics:
            rows.append(f"     Lyrics: {lyrics}")

    header = [
        "=== SCORE MELODY & STRUCTURE EXTRACTION ===",
        f"Source: {score_path}",
        f"Title: {title} | Key: {key_str} | Time: {ts} | Initial BPM: {bpm:g}",
        f"Total Measures: {len(measures)} | Estimated Score Duration: {int(total_sec//60):02d}:{total_sec%60:04.1f} ({total_sec:.1f}s)",
        "",
        "--- Section Outline ---",
    ]
    for bnum, slabel, skey, stime in sections:
        header.append(f"  m.{bnum:02d} ({int(stime//60):02d}:{stime%60:04.1f}) | Key: {skey:<10s} | {slabel}")
    header.append("\n--- Measure-by-Measure RH Top-Voice Melody ---")
    return "\n".join(header + rows) + "\n"


def extract_from_midi(midi_path: Path, key_str: str = "C major", beats_per_bar: int = 4) -> str:
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(midi_path))
    duration = pm.get_end_time()
    tempo_times, tempos = pm.get_tempo_changes()
    bpm = float(tempos[0]) if len(tempos) else 72.0
    bar_sec = beats_per_bar * (60.0 / bpm)
    n_bars = max(1, int(round(duration / bar_sec)))
    tonic_pc, _ = parse_key_tonic_pc(key_str)

    # Collect notes from first track (RH) or all notes >= C4
    all_notes = []
    if pm.instruments:
        src_notes = pm.instruments[0].notes
        if not src_notes:
            for inst in pm.instruments:
                src_notes.extend(inst.notes)
        all_notes = sorted(src_notes, key=lambda n: (n.start, -n.pitch))

    lines = [
        "=== MIDI MELODY EXTRACTION ===",
        f"Source: {midi_path}",
        f"Estimated Initial BPM: {bpm:.1f} | Duration: {duration:.1f}s | Approx Bars ({beats_per_bar}/4): {n_bars}",
        f"Reference Jianpu Key: {key_str}",
        "",
    ]
    for b in range(n_bars):
        t0 = b * bar_sec
        t1 = (b + 1) * bar_sec
        in_bar = [n for n in all_notes if t0 - 0.03 <= n.start < t1 - 0.03]
        # Group simultaneous onsets (within 35ms) and keep top voice
        top_events = []
        for n in in_bar:
            if top_events and abs(n.start - top_events[-1].start) < 0.035:
                if n.pitch > top_events[-1].pitch:
                    top_events[-1] = n
            else:
                top_events.append(n)
        toks = []
        jps = []
        for n in top_events:
            beat = 1.0 + (n.start - t0) / (60.0 / bpm)
            dur_b = (n.end - n.start) / (60.0 / bpm)
            toks.append(f"@{beat:.2g}:{midi_to_name(n.pitch)}({dur_b:.2g})")
            jps.append(midi_to_jianpu(n.pitch, tonic_pc))
        mm, ss = int(t0 // 60), t0 % 60
        lines.append(f"m.{b+1:02d} ({mm:02d}:{ss:04.1f}) | Jianpu: {' '.join(jps) or '-'}")
        lines.append(f"     Top: {'  '.join(toks) or '-'}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract bar-by-bar melody & structure from score.json or MIDI.")
    parser.add_argument("--score", help="Path to score.json")
    parser.add_argument("--midi", help="Path to MIDI file (fallback if no score.json)")
    parser.add_argument("--key", default="C major", help="Reference key for MIDI jianpu conversion")
    parser.add_argument("--beats-per-bar", type=int, default=4, help="Beats per bar for MIDI fallback")
    parser.add_argument("--out", help="Optional output text file path")
    args = parser.parse_args()

    if not args.score and not args.midi:
        print("ERROR: Provide either --score or --midi.", file=sys.stderr)
        sys.exit(2)

    if args.score:
        text = extract_from_score_json(Path(args.score).resolve())
    else:
        text = extract_from_midi(Path(args.midi).resolve(), key_str=args.key, beats_per_bar=args.beats_per_bar)

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"OK: Wrote {out_path}")
    else:
        print(text)


if __name__ == "__main__":
    main()
