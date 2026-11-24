#!/usr/bin/env python3
"""
Stage 3 Helper: Slice reference audio & rendered score audio into short synchronized MP3 clips
for direct human/agent ear listening via read_file.

Modes:
1. Chunk mode (default): slices the entire reference & score audio into fixed chunks (e.g. 25s
   with 2s overlap) so every phrase can be audited cleanly.
2. Suspect mode (--suspect-only with --audit-json): automatically groups contiguous SUSPECT /
   MISMATCH bars from audit_metrics.json and slices synchronized (ref, score) clips for each.
3. Custom bar ranges (--bar-ranges "1-8,17-24" with --audit-json): slices exact bar windows.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def probe_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def cut_mp3(src: Path, dst: Path, start_sec: float, end_sec: float) -> None:
    start_sec = max(0.0, start_sec)
    dur = max(0.5, end_sec - start_sec)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start_sec:.2f}", "-t", f"{dur:.2f}",
        "-i", str(src), "-ac", "1", "-ar", "22050", "-b:a", "96k", str(dst),
    ]
    subprocess.run(cmd, check=True)


def fmt_tag(sec: float) -> str:
    m = int(sec // 60)
    s = int(sec % 60)
    return f"{m:02d}m{s:02d}s"


def group_bar_numbers(nums: list[int], max_gap: int = 2) -> list[tuple[int, int]]:
    if not nums:
        return []
    nums = sorted(set(nums))
    groups = [[nums[0], nums[0]]]
    for n in nums[1:]:
        if n - groups[-1][1] <= max_gap:
            groups[-1][1] = n
        else:
            groups.append([n, n])
    return [(a, b) for a, b in groups]


def main() -> None:
    parser = argparse.ArgumentParser(description="Slice reference & score audio into short MP3s for ear review.")
    parser.add_argument("--ref-audio", required=True, help="Path to reference audio (WAV/MP3)")
    parser.add_argument("--score-audio", help="Optional path to rendered score audio (_試聽.mp3)")
    parser.add_argument("--audit-json", help="Optional path to audit_metrics.json from Stage 2")
    parser.add_argument("--out-dir", required=True, help="Directory to save sliced MP3 clips")
    parser.add_argument("--chunk-sec", type=float, default=25.0, help="Chunk duration in seconds (default 25)")
    parser.add_argument("--overlap-sec", type=float, default=2.0, help="Overlap between chunks in seconds (default 2)")
    parser.add_argument("--suspect-only", action="store_true", help="Slice only contiguous SUSPECT/MISMATCH bar groups")
    parser.add_argument("--bar-ranges", help="Comma-separated bar ranges, e.g. '1-8,17-24' (requires --audit-json)")
    args = parser.parse_args()

    ref_path = Path(args.ref_audio).resolve()
    score_path = Path(args.score_audio).resolve() if args.score_audio else None
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = ["=== REVIEW AUDIO CLIPS MANIFEST ===", ""]

    if args.bar_ranges or args.suspect_only:
        if not args.audit_json:
            print("ERROR: --bar-ranges and --suspect-only require --audit-json.", file=sys.stderr)
            sys.exit(2)
        audit = json.loads(Path(args.audit_json).resolve().read_text(encoding="utf-8"))
        bars_map = {b["bar"]: b for b in audit["bars"]}

        if args.bar_ranges:
            ranges = []
            for part in args.bar_ranges.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    ranges.append((int(a), int(b)))
                elif part:
                    v = int(part)
                    ranges.append((v, v))
        else:
            flagged = audit["summary_metrics"]["flagged_bar_numbers"]
            ranges = group_bar_numbers(flagged, max_gap=2)

        for idx, (b0, b1) in enumerate(ranges, 1):
            valid_bars = [bars_map[k] for k in range(b0, b1 + 1) if k in bars_map]
            if not valid_bars:
                continue
            rt0 = max(0.0, min(b["ref_start"] for b in valid_bars) - 1.5)
            rt1 = max(b["ref_end"] for b in valid_bars) + 1.5
            st0 = max(0.0, min(b["score_start"] for b in valid_bars) - 1.0)
            st1 = max(b["score_end"] for b in valid_bars) + 1.0

            ref_clip = out_dir / f"clip{idx:02d}_bars{b0:02d}-{b1:02d}_ref_{fmt_tag(rt0)}-{fmt_tag(rt1)}.mp3"
            cut_mp3(ref_path, ref_clip, rt0, rt1)
            manifest.append(f"[Group {idx:02d}] Bars m.{b0:02d}-m.{b1:02d}")
            manifest.append(f"  REF   ({rt0:5.1f}s - {rt1:5.1f}s): {ref_clip}")
            if score_path and score_path.exists():
                sc_clip = out_dir / f"clip{idx:02d}_bars{b0:02d}-{b1:02d}_score_{fmt_tag(st0)}-{fmt_tag(st1)}.mp3"
                cut_mp3(score_path, sc_clip, st0, st1)
                manifest.append(f"  SCORE ({st0:5.1f}s - {st1:5.1f}s): {sc_clip}")
    else:
        ref_dur = probe_duration(ref_path)
        step = max(5.0, args.chunk_sec - args.overlap_sec)
        t = 0.0
        idx = 1
        manifest.append("--- Reference Audio Chunks ---")
        while t < ref_dur - 1.0:
            t_end = min(t + args.chunk_sec, ref_dur)
            dst = out_dir / f"ref_{idx:02d}_{fmt_tag(t)}-{fmt_tag(t_end)}.mp3"
            cut_mp3(ref_path, dst, t, t_end)
            manifest.append(f"  [{idx:02d}] {t:5.1f}s - {t_end:5.1f}s : {dst}")
            if t_end >= ref_dur:
                break
            t += step
            idx += 1

        if score_path and score_path.exists():
            sc_dur = probe_duration(score_path)
            t = 0.0
            idx = 1
            manifest.append("\n--- Rendered Score Audio Chunks ---")
            while t < sc_dur - 1.0:
                t_end = min(t + args.chunk_sec, sc_dur)
                dst = out_dir / f"score_{idx:02d}_{fmt_tag(t)}-{fmt_tag(t_end)}.mp3"
                cut_mp3(score_path, dst, t, t_end)
                manifest.append(f"  [{idx:02d}] {t:5.1f}s - {t_end:5.1f}s : {dst}")
                if t_end >= sc_dur:
                    break
                t += step
                idx += 1

    man_path = out_dir / "clips_manifest.txt"
    man_path.write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(f"OK: Generated review clips and manifest at {man_path}")


if __name__ == "__main__":
    main()
