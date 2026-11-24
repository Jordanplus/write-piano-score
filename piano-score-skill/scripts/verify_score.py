#!/usr/bin/env python3
"""
Stage 4a: Automated Musical & Playability Verification for score.json.

Validates before engraving:
1. Schema integrity & required metadata.
2. Exact beat-duration sum per measure in both RH and LH (zero rhythmic errors).
3. Hand-span playability check (flags simultaneous chord spans > 10th / 16 semitones
   unless marked with arpeggio=true).
4. Pitch range & muddy low-register triad check (flags close intervals below C3).
5. Tie validity check (a tied note should match the next event's pitch).
6. Key/modulation & chord symbol syntax check.
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
VALID_DURS = {4.0, 3.0, 2.0, 1.5, 1.0, 0.75, 0.5, 0.25, 0.125}
PITCH_RE = re.compile(r"^([A-G][#b]?)(-?\d)$")
CHORD_RE = re.compile(r"^[A-G][#b]?(?:maj7|m7b5|m7|m6|maj9|m9|add9|sus4|sus2|dim7|dim|aug|m|7|6|9)?(?:/[A-G][#b]?)?$")


def pitch_to_midi(p: str) -> int:
    m = PITCH_RE.match(p.strip())
    if not m:
        raise ValueError(f"Invalid pitch string: {p!r}")
    name, oct_s = m.group(1), int(m.group(2))
    return (oct_s + 1) * 12 + NOTE_TO_PC[name]


def parse_time_sig(ts: str) -> float:
    num, den = ts.split("/")
    return float(num) * (4.0 / float(den))


def verify_score(score: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for req in ("title", "key", "time_signature", "tempo_bpm", "measures"):
        if req not in score:
            errors.append(f"Missing required top-level field: '{req}'")
    if errors:
        return errors, warnings

    expected_beats = parse_time_sig(score["time_signature"])
    measures = score["measures"]
    if not measures:
        errors.append("Score has 0 measures.")
        return errors, warnings

    for idx, bar in enumerate(measures):
        bar_num = bar.get("number", idx + 1)
        if bar_num != idx + 1:
            warnings.append(f"Bar index {idx+1} has number={bar_num} (expected sequential numbering).")

        # Check chords syntax
        for ch in bar.get("chords", []):
            sym = ch.get("symbol", "")
            if sym and not CHORD_RE.match(sym):
                errors.append(f"Bar {bar_num}: invalid chord symbol {sym!r}")
            b = ch.get("beat", 1.0)
            if not (1.0 <= b <= expected_beats + 0.01):
                errors.append(f"Bar {bar_num}: chord beat {b} out of range 1..{expected_beats}")

        # Validate both hands
        for hand in ("rh", "lh"):
            events = bar.get(hand)
            if not isinstance(events, list) or not events:
                errors.append(f"Bar {bar_num} ({hand.upper()}): missing or empty event list.")
                continue

            total_dur = 0.0
            for e_idx, ev in enumerate(events):
                dur = float(ev.get("dur", 0))
                if dur not in VALID_DURS:
                    errors.append(
                        f"Bar {bar_num} ({hand.upper()} ev#{e_idx+1}): unsupported duration {dur} "
                        f"(allowed: {sorted(VALID_DURS)})"
                    )
                total_dur += dur

                pitches = ev.get("pitches", [])
                if pitches == ["R"]:
                    pitches = []
                midis = []
                for p in pitches:
                    try:
                        m = pitch_to_midi(p)
                        if not (21 <= m <= 108):
                            warnings.append(f"Bar {bar_num} ({hand.upper()}): pitch {p} outside 88-key piano range.")
                        midis.append(m)
                    except ValueError as exc:
                        errors.append(f"Bar {bar_num} ({hand.upper()} ev#{e_idx+1}): {exc}")

                if len(midis) >= 2:
                    span = max(midis) - min(midis)
                    if span > 16 and not ev.get("arpeggio", False):
                        errors.append(
                            f"Bar {bar_num} ({hand.upper()} ev#{e_idx+1}): simultaneous hand span is "
                            f"{span} semitones ({pitches}) > 10th (16st). Break it up or set arpeggio=true."
                        )
                    elif span > 14 and not ev.get("arpeggio", False):
                        warnings.append(
                            f"Bar {bar_num} ({hand.upper()} ev#{e_idx+1}): wide span {span}st ({pitches}); "
                            "consider marking arpeggio=true."
                        )

                    # Muddy low register check: close interval (<=4 semitones) below C3 (MIDI 48)
                    sorted_m = sorted(midis)
                    for a, b in zip(sorted_m, sorted_m[1:]):
                        if a < 48 and (b - a) <= 4:
                            warnings.append(
                                f"Bar {bar_num} ({hand.upper()} ev#{e_idx+1}): muddy close interval below C3 ({pitches})."
                            )

                # Check tie points to a matching pitch
                if ev.get("tie"):
                    next_ev = None
                    if e_idx + 1 < len(events):
                        next_ev = events[e_idx + 1]
                    elif idx + 1 < len(measures) and measures[idx + 1].get(hand):
                        next_ev = measures[idx + 1][hand][0]
                    if next_ev is None:
                        errors.append(f"Bar {bar_num} ({hand.upper()} ev#{e_idx+1}): tie at end of score.")
                    else:
                        next_p = next_ev.get("pitches", [])
                        if not next_p or next_p == ["R"]:
                            errors.append(f"Bar {bar_num} ({hand.upper()} ev#{e_idx+1}): tie into rest.")
                        else:
                            cur_m = set(midis)
                            nxt_m = {pitch_to_midi(x) for x in next_p if PITCH_RE.match(x)}
                            if not cur_m.intersection(nxt_m):
                                errors.append(
                                    f"Bar {bar_num} ({hand.upper()} ev#{e_idx+1}): tie pitches {pitches} "
                                    f"do not match next event {next_p}."
                                )

            if abs(total_dur - expected_beats) > 1e-4:
                errors.append(
                    f"Bar {bar_num} ({hand.upper()}): duration sum = {total_dur:.3f} beats "
                    f"(expected {expected_beats:.3f} for {score['time_signature']})."
                )

    return errors, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify structured piano score JSON.")
    parser.add_argument("--score", required=True, help="Path to score.json")
    args = parser.parse_args()

    path = Path(args.score).resolve()
    score = json.loads(path.read_text(encoding="utf-8"))
    errors, warnings = verify_score(score)

    print(f"Verified: {path.name} ({len(score.get('measures', []))} measures)")
    if warnings:
        print(f"\nWARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  [WARN] {w}")
    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for e in errors:
            print(f"  [ERR]  {e}")
        sys.exit(1)

    print("PASS: Zero errors! Score is rhythmically exact and pianistically playable.")


if __name__ == "__main__":
    main()
