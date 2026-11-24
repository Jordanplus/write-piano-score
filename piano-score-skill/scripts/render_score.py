#!/usr/bin/env python3
"""
Stage 4b: Multi-Format Piano Score Compiler & Renderer.

Compiles a verified `score.json` into 5 publication-grade outputs:
1. LilyPond source (.ly) + Engraved PDF (.pdf) + High-Res Page PNGs (.png)
2. MusicXML (.musicxml) via music21 (editable in MuseScore / Finale / Sibelius / Dorico)
3. Expressive Standard MIDI (.mid) via pretty_midi (with melody voicing & CC64 sustain pedal)
4. Realistic Acoustic Grand Piano Audio Preview (.wav & .mp3) via FluidSynth + ffmpeg
5. Dual-Format Jianpu (簡譜) & Chord Sheet Markdown (.md)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import music21 as m21
import pretty_midi

NOTE_TO_PC = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4, "E#": 5, "F": 5, "F#": 6, "Gb": 6,
    "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11, "B#": 0,
}

PITCH_RE = re.compile(r"^([A-G])([#b]?)(-?\d)$")

DUR_TO_LY = {
    4.0: "1",
    3.0: "2.",
    2.0: "2",
    1.5: "4.",
    1.0: "4",
    0.75: "8.",
    0.5: "8",
    0.25: "16",
    0.125: "32",
}

DYNAMIC_VELOCITY = {
    "pp": 42,
    "p": 54,
    "mp": 66,
    "mf": 78,
    "f": 92,
    "ff": 106,
}

# Major scale semitone intervals -> Jianpu degree
MAJOR_SEMITONE_TO_JIANPU = {
    0: "1",
    1: "#1",
    2: "2",
    3: "b3",
    4: "3",
    5: "4",
    6: "#4",
    7: "5",
    8: "#5",
    9: "6",
    10: "b7",
    11: "7",
}

DEFAULT_SOUNDFONT_CANDIDATES = [
    Path("/Users/mcgradymac/Gemini/music-gen/soundfonts/MuseScore_General.sf3"),
    Path("/opt/homebrew/share/soundfonts/default.sf2"),
    Path("/usr/share/sounds/sf2/FluidR3_GM.sf2"),
]


def pitch_to_midi(p: str) -> int:
    m = PITCH_RE.match(p.strip())
    if not m:
        raise ValueError(f"Invalid pitch: {p}")
    letter, acc, oct_s = m.group(1), m.group(2), int(m.group(3))
    return (oct_s + 1) * 12 + NOTE_TO_PC[letter + acc]


def pitch_to_ly(p: str) -> str:
    """Convert scientific pitch (e.g. 'Bb4', 'F#3') to LilyPond absolute pitch ('bes\\'', 'fis')."""
    m = PITCH_RE.match(p.strip())
    if not m:
        raise ValueError(f"Invalid pitch: {p}")
    letter = m.group(1).lower()
    acc = {"#": "is", "b": "es", "": ""}[m.group(2)]
    octave = int(m.group(3))
    if octave > 3:
        oct_mark = "'" * (octave - 3)
    elif octave < 3:
        oct_mark = "," * (3 - octave)
    else:
        oct_mark = ""
    return f"{letter}{acc}{oct_mark}"


def key_to_ly(key_str: str) -> str:
    """Convert 'Bb major' or 'G minor' -> '\\key bes \\major'."""
    parts = key_str.strip().split()
    root = parts[0]
    mode = parts[1].lower() if len(parts) > 1 else "major"
    letter = root[0].lower()
    acc = ""
    if len(root) > 1:
        acc = {"#": "is", "b": "es"}.get(root[1], "")
    return f"\\key {letter}{acc} \\{mode}"


def key_to_major_tonic_pc(key_str: str) -> tuple[str, int]:
    """Return relative major tonic name and pitch class for movable-do Jianpu."""
    parts = key_str.strip().split()
    root = parts[0]
    mode = parts[1].lower() if len(parts) > 1 else "major"
    pc = NOTE_TO_PC[root]
    if mode == "minor":
        # Relative major is +3 semitones
        pc = (pc + 3) % 12
        pc_names = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
        return pc_names[pc], pc
    return root, pc


def chord_to_ly(symbol: str, dur_ly: str) -> str:
    """Convert chord symbol like 'Bb', 'Gm7', 'F/A', 'Ebmaj7', 'Gsus4' to LilyPond chordmode."""
    m = re.match(
        r"^([A-G])([#b]?)(maj7|m7b5|m7|m6|maj9|m9|add9|sus4|sus2|dim7|dim|aug|m|7|6|9)?(?:/([A-G])([#b]?))?$",
        symbol.strip(),
    )
    if not m:
        return f"s{dur_ly}"
    r_let, r_acc, qual, b_let, b_acc = m.groups()
    root_ly = r_let.lower() + {"#": "is", "b": "es", "": ""}[r_acc]
    qual_map = {
        None: "",
        "": "",
        "m": ":m",
        "7": ":7",
        "m7": ":m7",
        "maj7": ":maj7",
        "m6": ":m6",
        "6": ":6",
        "9": ":9",
        "m9": ":m9",
        "maj9": ":maj9",
        "add9": ":5.9",
        "sus4": ":sus4",
        "sus2": ":sus2",
        "dim": ":dim",
        "dim7": ":dim7",
        "m7b5": ":m7.5-",
        "aug": ":aug",
    }
    q_ly = qual_map.get(qual, "")
    bass_ly = ""
    if b_let:
        bass_ly = "/" + b_let.lower() + {"#": "is", "b": "es", "": ""}[b_acc or ""]
    return f"{root_ly}{dur_ly}{q_ly}{bass_ly}"


def split_duration_to_ly(beats: float) -> list[str]:
    """Break an arbitrary beat length (e.g., 2.5 or 3.5) into standard LilyPond duration tokens."""
    rem = round(beats * 4) / 4.0
    out = []
    for d in (4.0, 3.0, 2.0, 1.5, 1.0, 0.75, 0.5, 0.25):
        while rem >= d - 1e-6:
            out.append(DUR_TO_LY[d])
            rem = round((rem - d) * 4) / 4.0
    return out


def parse_time_sig(ts: str) -> float:
    num, den = ts.split("/")
    return float(num) * (4.0 / float(den))


def escape_ly_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def format_event_ly(ev: dict) -> str:
    """Format a single RH or LH note/chord/rest event in LilyPond."""
    pitches = ev.get("pitches", [])
    if pitches == ["R"]:
        pitches = []
    dur_ly = DUR_TO_LY[float(ev["dur"])]

    if not pitches:
        return f"r{dur_ly}"

    if len(pitches) == 1:
        core = f"{pitch_to_ly(pitches[0])}{dur_ly}"
    else:
        inner = " ".join(pitch_to_ly(p) for p in pitches)
        core = f"<{inner}>{dur_ly}"

    suffixes = []
    if ev.get("arpeggio"):
        suffixes.append("\\arpeggio")
    art = ev.get("articulation")
    if art == "accent":
        suffixes.append("->")
    elif art == "tenuto":
        suffixes.append("--")
    elif art == "staccato":
        suffixes.append("-.")
    if ev.get("slur") == "start":
        suffixes.append("(")
    elif ev.get("slur") == "stop":
        suffixes.append(")")
    if ev.get("tie"):
        suffixes.append("~")

    return core + "".join(suffixes)


def build_lilypond(score: dict) -> str:
    """Generate complete LilyPond (.ly) source with Grand Staff, Chords, Dynamics, Pedal & Lyrics."""
    title = escape_ly_str(score.get("title", "Untitled"))
    subtitle = escape_ly_str(score.get("subtitle", ""))
    artist = escape_ly_str(score.get("artist", ""))
    composer = escape_ly_str(score.get("composer", ""))
    lyricist = escape_ly_str(score.get("lyricist", ""))
    arranger = escape_ly_str(score.get("arranger", "McGrady-AI"))
    time_sig = score.get("time_signature", "4/4")
    beats_per_bar = parse_time_sig(time_sig)
    tempo_bpm = int(round(score.get("tempo_bpm", 72)))
    tempo_text = escape_ly_str(score.get("tempo_text", "Moderato"))
    init_key_ly = key_to_ly(score.get("key", "C major"))

    composer_line = f"曲：{composer}" if composer else ""
    if artist:
        composer_line = f"原唱：{artist}   {composer_line}".strip()
    poet_line = f"詞：{lyricist}" if lyricist else ""

    # Detect whether any RH event has per-note syllable lyrics
    has_syllable_lyrics = any(
        ev.get("lyric") for bar in score["measures"] for ev in bar.get("rh", [])
    )

    chord_lines: list[str] = []
    rh_lines: list[str] = []
    lh_lines: list[str] = []
    dyn_lines: list[str] = []
    lyric_tokens: list[str] = []

    rh_lines.append(f'  \\clef treble {init_key_ly} \\time {time_sig}')
    rh_lines.append(f'  \\tempo "{tempo_text}" 4 = {tempo_bpm}')
    lh_lines.append(f'  \\clef bass {init_key_ly} \\time {time_sig}')
    lh_lines.append('  \\set Staff.pedalSustainStyle = #\'bracket')

    prev_section = None
    for bar_idx, bar in enumerate(score["measures"]):
        bnum = bar.get("number", bar_idx + 1)
        is_last = bar_idx == len(score["measures"]) - 1

        # Key change (modulation!)
        if bar.get("key_change"):
            kc_ly = key_to_ly(bar["key_change"])
            rh_lines.append(f'  \\bar "||" {kc_ly}')
            lh_lines.append(f'  {kc_ly}')

        # Section rehearsal mark
        sec = bar.get("section")
        if sec and sec != prev_section:
            rh_lines.append(
                f'  \\mark \\markup {{ \\box \\bold \\fontsize #-1 "{escape_ly_str(sec)}" }}'
            )
            prev_section = sec

        # Tempo change
        if bar.get("tempo_change"):
            tc = bar["tempo_change"]
            if isinstance(tc, dict):
                t_txt = escape_ly_str(tc.get("text", ""))
                t_bpm = int(tc.get("bpm", tempo_bpm))
                rh_lines.append(f'  \\tempo "{t_txt}" 4 = {t_bpm}')
            else:
                rh_lines.append(f'  \\tempo "{escape_ly_str(str(tc))}"')

        # --- Chords track ---
        chords = sorted(bar.get("chords", []), key=lambda c: c.get("beat", 1.0))
        bar_chord_tokens = []
        if not chords:
            bar_chord_tokens.append(f"s{DUR_TO_LY.get(beats_per_bar, '1')}")
        else:
            cursor = 1.0
            for ci, ch in enumerate(chords):
                cb = float(ch.get("beat", 1.0))
                if cb > cursor + 1e-4:
                    for d_ly in split_duration_to_ly(cb - cursor):
                        bar_chord_tokens.append(f"s{d_ly}")
                next_b = float(chords[ci + 1]["beat"]) if ci + 1 < len(chords) else (beats_per_bar + 1.0)
                span = max(0.25, next_b - cb)
                durs = split_duration_to_ly(span)
                bar_chord_tokens.append(chord_to_ly(ch["symbol"], durs[0]))
                for extra_d in durs[1:]:
                    bar_chord_tokens.append(f"s{extra_d}")
                cursor = next_b
        chord_lines.append(f"  {' '.join(bar_chord_tokens)} | % bar {bnum}")

        # --- RH track ---
        rh_tokens = []
        bar_lyrics_text = bar.get("lyrics", "").strip()
        placed_bar_lyric = False
        for ev in bar.get("rh", []):
            tok = format_event_ly(ev)
            # If no per-note syllable lyrics, attach bar-level lyric phrase once above/below first note
            if not has_syllable_lyrics and bar_lyrics_text and not placed_bar_lyric:
                pitches = ev.get("pitches", [])
                if pitches and pitches != ["R"]:
                    tok += f' _\\markup {{ \\fontsize #-1.5 "{escape_ly_str(bar_lyrics_text)}" }}'
                    placed_bar_lyric = True
            rh_tokens.append(tok)

            # Collect syllable lyrics if used (LilyPond aligns one lyric per non-rest, non-tied-continuation note)
            if has_syllable_lyrics:
                pitches = ev.get("pitches", [])
                if pitches and pitches != ["R"]:
                    syl = ev.get("lyric")
                    if syl:
                        lyric_tokens.append(f'"{escape_ly_str(syl)}"')
                    else:
                        # Only consume a lyric slot if previous event wasn't tied into this one
                        lyric_tokens.append("_")

        if is_last:
            rh_tokens.append('\\bar "|."')
        rh_lines.append(f"  {' '.join(rh_tokens)} | % bar {bnum}")

        # --- LH track (with sustain pedal) ---
        pedal_mode = bar.get("pedal", "chord")
        lh_events = bar.get("lh", [])
        # Determine pedal-change beat positions (1-indexed beats)
        pedal_starts: set[float] = set()
        if pedal_mode == "bar":
            pedal_starts.add(1.0)
        elif pedal_mode == "half":
            pedal_starts.add(1.0)
            if beats_per_bar >= 4.0:
                pedal_starts.add(3.0)
        elif pedal_mode == "chord":
            if chords:
                for ch in chords:
                    pedal_starts.add(round(float(ch.get("beat", 1.0)) * 4) / 4.0)
            else:
                pedal_starts.add(1.0)

        lh_tokens = []
        if len(lh_events) == 1 and pedal_starts:
            tok = format_event_ly(lh_events[0])
            lh_tokens.append(f"<< {{ {tok} }} {{ s2.\\sustainOn s4\\sustainOff }} >>")
        else:
            beat_pos = 1.0
            pedal_down = False
            for e_idx, ev in enumerate(lh_events):
                tok = format_event_ly(ev)
                cur_b = round(beat_pos * 4) / 4.0
                is_last_ev = e_idx == len(lh_events) - 1
                if cur_b in pedal_starts:
                    if pedal_down:
                        tok += "\\sustainOff\\sustainOn"
                    else:
                        tok += "\\sustainOn"
                        pedal_down = True
                if is_last_ev and pedal_down and cur_b not in pedal_starts:
                    tok += "\\sustainOff"
                    pedal_down = False
                lh_tokens.append(tok)
                beat_pos += float(ev["dur"])

        if is_last:
            lh_tokens.append('\\bar "|."')
        lh_lines.append(f"  {' '.join(lh_tokens)} | % bar {bnum}")

        # --- Dynamics track ---
        dyn = bar.get("dynamic")
        dur_bar = DUR_TO_LY.get(beats_per_bar, "1")
        if dyn in ("pp", "p", "mp", "mf", "f", "ff"):
            dyn_lines.append(f"  s{dur_bar}\\{dyn} | % bar {bnum}")
        elif dyn == "cresc":
            dyn_lines.append(f"  s2\\< s2\\! | % bar {bnum}")
        elif dyn == "dim":
            dyn_lines.append(f"  s2\\> s2\\! | % bar {bnum}")
        else:
            dyn_lines.append(f"  s{dur_bar} | % bar {bnum}")

    lyrics_block = ""
    lyrics_staff_line = ""
    if has_syllable_lyrics:
        # Remove tie-continuation dummy slots cleanly by using \new Lyrics \lyricsto "melody"
        # Wait: in LilyPond, \lyricsto skips tied continuations automatically!
        # So we should NOT emit a token for events immediately preceded by tie=true!
        clean_lyrics = []
        prev_tied = False
        for bar in score["measures"]:
            for ev in bar.get("rh", []):
                pitches = ev.get("pitches", [])
                is_note = bool(pitches) and pitches != ["R"]
                if is_note:
                    if not prev_tied:
                        syl = ev.get("lyric")
                        clean_lyrics.append(f'"{escape_ly_str(syl)}"' if syl else "_")
                    prev_tied = bool(ev.get("tie"))
                else:
                    prev_tied = False

        # Format nicely in chunks of 16 tokens
        chunks = [
            "  " + " ".join(clean_lyrics[i : i + 16])
            for i in range(0, len(clean_lyrics), 16)
        ]
        lyrics_block = "vocalLyrics = \\lyricmode {\n" + "\n".join(chunks) + "\n}\n"
        lyrics_staff_line = '      \\new Lyrics \\with { \\override LyricText.font-size = #-0.5 } \\lyricsto "melody" \\vocalLyrics\n'

    ly = f'''\\version "2.26.0"

\\paper {{
  #(set-paper-size "a4")
  top-margin = 14\\mm
  bottom-margin = 14\\mm
  left-margin = 14\\mm
  right-margin = 14\\mm
  system-system-spacing.basic-distance = #13
  system-system-spacing.minimum-distance = #9
  markup-system-spacing.basic-distance = #12
  property-defaults.fonts.serif = "PingFang TC"
  property-defaults.fonts.sans = "PingFang TC"
  property-defaults.fonts.typewriter = "Menlo"
}}

\\header {{
  title = \\markup \\bold \\fontsize #3 "{title}"
  subtitle = \\markup \\fontsize #0.5 "{subtitle}"
  composer = "{composer_line}"
  poet = "{poet_line}"
  arranger = "撰譜:{arranger}"
  tagline = \\markup \\fontsize #-2 "Generated by Antigravity Piano Score Skill (LilyPond Engraving)"
}}

chordTrack = \\chordmode {{
  \\set chordChanges = ##t
{chr(10).join(chord_lines)}
}}

rightHand = {{
  \\override Score.RehearsalMark.self-alignment-X = #LEFT
  \\override Score.RehearsalMark.padding = #2.2
  \\set melismaBusyProperties = #'(tieMelismaBusy)
{chr(10).join(rh_lines)}
}}

leftHand = {{
{chr(10).join(lh_lines)}
}}

dynamicsTrack = {{
{chr(10).join(dyn_lines)}
}}

{lyrics_block}
\\score {{
  <<
    \\new ChordNames \\with {{
      \\override ChordName.font-size = #0
      \\override ChordName.font-series = #'bold
    }} \\chordTrack
    \\new PianoStaff \\with {{
      instrumentName = "Piano"
      shortInstrumentName = "Pno."
    }} <<
      \\new Staff = "right" <<
        \\new Voice = "melody" \\rightHand
      >>
{lyrics_staff_line}      \\new Dynamics \\dynamicsTrack
      \\new Staff = "left" \\leftHand
    >>
  >>
  \\layout {{
    #(layout-set-staff-size 17.5)
    \\context {{
      \\Score
      \\override BarNumber.font-size = #-2
    }}
  }}
}}
'''
    return ly


def build_musicxml(score: dict, out_path: Path) -> None:
    """Export editable Piano Grand Staff MusicXML via music21."""
    s = m21.stream.Score()
    s.metadata = m21.metadata.Metadata()
    s.metadata.title = score.get("title", "Untitled")
    s.metadata.composer = score.get("composer", "")
    s.metadata.arranger = score.get("arranger", "")

    rh_part = m21.stream.Part(id="RH")
    rh_part.partName = "Piano"
    lh_part = m21.stream.Part(id="LH")
    lh_part.partName = "Piano (LH)"

    init_key = score.get("key", "C major").split()
    k_tonic = init_key[0].replace("b", "-")
    k_mode = init_key[1].lower() if len(init_key) > 1 else "major"
    ts_str = score.get("time_signature", "4/4")
    bpm = score.get("tempo_bpm", 72)

    prev_tie_rh = False
    prev_tie_lh = False

    for idx, bar in enumerate(score["measures"]):
        bnum = bar.get("number", idx + 1)
        m_rh = m21.stream.Measure(number=bnum)
        m_lh = m21.stream.Measure(number=bnum)

        if idx == 0:
            m_rh.append(m21.clef.TrebleClef())
            m_rh.append(m21.key.Key(k_tonic, k_mode))
            m_rh.append(m21.meter.TimeSignature(ts_str))
            m_rh.append(m21.tempo.MetronomeMark(text=score.get("tempo_text", ""), number=bpm))

            m_lh.append(m21.clef.BassClef())
            m_lh.append(m21.key.Key(k_tonic, k_mode))
            m_lh.append(m21.meter.TimeSignature(ts_str))

        if bar.get("key_change"):
            kc = bar["key_change"].split()
            kt = kc[0].replace("b", "-")
            km = kc[1].lower() if len(kc) > 1 else "major"
            m_rh.append(m21.key.Key(kt, km))
            m_lh.append(m21.key.Key(kt, km))

        if bar.get("section"):
            m_rh.insert(0.0, m21.expressions.RehearsalMark(bar["section"]))

        if bar.get("dynamic") in ("pp", "p", "mp", "mf", "f", "ff"):
            m_rh.insert(0.0, m21.dynamics.Dynamic(bar["dynamic"]))

        for ch in bar.get("chords", []):
            offset = float(ch.get("beat", 1.0)) - 1.0
            try:
                cs = m21.harmony.ChordSymbol(ch["symbol"].replace("b", "-"))
                m_rh.insert(offset, cs)
            except Exception:
                pass

        for hand_key, m_target, prev_tied in (("rh", m_rh, prev_tie_rh), ("lh", m_lh, prev_tie_lh)):
            cur_tied = prev_tied
            for ev in bar.get(hand_key, []):
                pitches = ev.get("pitches", [])
                if pitches == ["R"]:
                    pitches = []
                dur = float(ev["dur"])
                if not pitches:
                    obj = m21.note.Rest(quarterLength=dur)
                    cur_tied = False
                elif len(pitches) == 1:
                    obj = m21.note.Note(pitches[0].replace("b", "-"), quarterLength=dur)
                else:
                    obj = m21.chord.Chord([p.replace("b", "-") for p in pitches], quarterLength=dur)

                if pitches:
                    if ev.get("lyric") and hand_key == "rh":
                        obj.lyric = ev["lyric"]
                    if cur_tied and ev.get("tie"):
                        obj.tie = m21.tie.Tie("continue")
                    elif cur_tied:
                        obj.tie = m21.tie.Tie("stop")
                    elif ev.get("tie"):
                        obj.tie = m21.tie.Tie("start")
                    cur_tied = bool(ev.get("tie"))
                m_target.append(obj)
            if hand_key == "rh":
                prev_tie_rh = cur_tied
            else:
                prev_tie_lh = cur_tied

        rh_part.append(m_rh)
        lh_part.append(m_lh)

    s.append(rh_part)
    s.append(lh_part)
    # Group as PianoStaff
    staff_group = m21.layout.StaffGroup([rh_part, lh_part], name="Piano", symbol="brace")
    s.insert(0, staff_group)
    s.write("musicxml", fp=str(out_path))


def build_midi(score: dict, out_path: Path) -> None:
    """Generate expressive Piano MIDI with melody voicing balance, tempo changes & CC64 sustain pedal."""
    bpm = float(score.get("tempo_bpm", 72))
    sec_per_beat = 60.0 / bpm
    beats_per_bar = parse_time_sig(score.get("time_signature", "4/4"))

    pm = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    piano = pretty_midi.Instrument(program=0, name="Acoustic Grand Piano")

    cur_vel = 74
    bar_start_sec = 0.0

    # Track tied notes across events/bars per hand: dict[(hand, midi)] -> (start_sec, vel)
    active_ties: dict[tuple[str, int], tuple[float, int]] = {}

    for bar in score["measures"]:
        tc = bar.get("tempo_change")
        if tc:
            if isinstance(tc, dict) and tc.get("bpm") is not None:
                sec_per_beat = 60.0 / float(tc["bpm"])
            elif isinstance(tc, (int, float)):
                sec_per_beat = 60.0 / float(tc)

        dyn = bar.get("dynamic")
        if dyn in DYNAMIC_VELOCITY:
            cur_vel = DYNAMIC_VELOCITY[dyn]
        elif dyn == "cresc":
            cur_vel = min(104, cur_vel + 10)
        elif dyn == "dim":
            cur_vel = max(46, cur_vel - 10)

        # Sustain pedal CC64
        pedal_mode = bar.get("pedal", "chord")
        chords = sorted(bar.get("chords", []), key=lambda c: c.get("beat", 1.0))
        pedal_beats: list[float] = []
        if pedal_mode == "bar":
            pedal_beats = [0.0]
        elif pedal_mode == "half":
            pedal_beats = [0.0, 2.0] if beats_per_bar >= 4.0 else [0.0]
        elif pedal_mode == "chord":
            pedal_beats = sorted({float(c.get("beat", 1.0)) - 1.0 for c in chords}) or [0.0]

        for pi, pb in enumerate(pedal_beats):
            t_on = bar_start_sec + pb * sec_per_beat
            next_pb = pedal_beats[pi + 1] if pi + 1 < len(pedal_beats) else beats_per_bar
            t_off = bar_start_sec + next_pb * sec_per_beat - 0.035
            piano.control_changes.append(pretty_midi.ControlChange(64, 0, max(0.0, t_on - 0.015)))
            piano.control_changes.append(pretty_midi.ControlChange(64, 118, t_on + 0.01))
            piano.control_changes.append(pretty_midi.ControlChange(64, 0, max(t_on + 0.02, t_off)))

        for hand in ("rh", "lh"):
            pos_beat = 0.0
            for ev in bar.get(hand, []):
                dur = float(ev["dur"])
                pitches = ev.get("pitches", [])
                if pitches == ["R"]:
                    pitches = []
                t0 = bar_start_sec + pos_beat * sec_per_beat
                t1 = bar_start_sec + (pos_beat + dur) * sec_per_beat
                is_tie = bool(ev.get("tie"))
                is_arp = bool(ev.get("arpeggio"))

                if pitches:
                    midis = sorted(pitch_to_midi(p) for p in pitches)
                    top_m = max(midis)
                    for m_idx, m in enumerate(midis):
                        key = (hand, m)
                        # Voice balance: RH top melody note sings louder; LH softer warm accompaniment
                        if hand == "rh":
                            vel = min(118, cur_vel + (12 if m == top_m else 0))
                        else:
                            # Bass root slightly warmer than inner arpeggio notes
                            vel = max(34, cur_vel - (10 if m == midis[0] else 17))
                        if ev.get("articulation") == "accent":
                            vel = min(124, vel + 10)

                        roll_offset = m_idx * 0.035 if is_arp else 0.0
                        note_start = t0 + roll_offset
                        if key in active_ties:
                            start_s, saved_vel = active_ties.pop(key)
                            vel = saved_vel
                        else:
                            start_s = note_start

                        if is_tie:
                            active_ties[key] = (start_s, vel)
                        else:
                            end_s = t1 - (0.06 if ev.get("articulation") == "staccato" else 0.015)
                            end_s = max(start_s + 0.04, end_s)
                            piano.notes.append(
                                pretty_midi.Note(velocity=vel, pitch=m, start=start_s, end=end_s)
                            )
                pos_beat += dur

        bar_start_sec += beats_per_bar * sec_per_beat

    # Flush any lingering ties
    end_time = bar_start_sec
    for (_, m), (start_s, vel) in active_ties.items():
        piano.notes.append(pretty_midi.Note(velocity=vel, pitch=m, start=start_s, end=max(start_s + 0.1, end_time)))

    pm.instruments.append(piano)
    pm.write(str(out_path))


def midi_to_jianpu(midi_num: int, tonic_pc: int, center_octave_midi: int = 60) -> str:
    """Convert MIDI pitch to movable-do Jianpu token relative to major tonic (C4..Bb4 as central octave)."""
    ref_tonic = 60 + tonic_pc
    if tonic_pc >= 11:  # Only B places tonic at B3=59; A4/Bb4 stay in central vocal octave
        ref_tonic -= 12
    diff = midi_num - ref_tonic
    oct_shift = diff // 12
    semitone = diff % 12
    deg = MAJOR_SEMITONE_TO_JIANPU[semitone]
    if oct_shift > 0:
        deg = deg + ("'" * oct_shift)
    elif oct_shift < 0:
        deg = deg + ("," * (-oct_shift))
    return deg


def build_jianpu_markdown(score: dict, out_path: Path) -> None:
    """Generate clean Traditional Chinese Jianpu (簡譜) & Chord Sheet Markdown."""
    title = score.get("title", "Untitled")
    artist = score.get("artist", "")
    composer = score.get("composer", "")
    lyricist = score.get("lyricist", "")
    init_key = score.get("key", "C major")
    tonic_name, tonic_pc = key_to_major_tonic_pc(init_key)
    bpm = score.get("tempo_bpm", 72)
    ts = score.get("time_signature", "4/4")

    lines = [
        f"# 🎹 {title} — 鋼琴簡譜與和弦對照表",
        "",
        f"- **原唱**：{artist}　｜　**曲**：{composer}　｜　**詞**：{lyricist}",
        f"- **原調**：`{init_key}`（首調簡譜 `1 = {tonic_name}`）　｜　**拍號**：`{ts}`　｜　**速度**：`♩ = {bpm} BPM`",
        f"- **難度**：`{score.get('difficulty', 'intermediate')}`",
        "- **簡譜符號說明**：`'` = 高八度（如 `1'`）、`,` = 低八度（如 `6,`）、`0` = 休止符、`-` = 延長拍、`()` = 八分音符群組",
        "",
        "---",
        "",
    ]

    cur_tonic_pc = tonic_pc
    cur_section = None

    for bar in score["measures"]:
        if bar.get("key_change"):
            new_t_name, cur_tonic_pc = key_to_major_tonic_pc(bar["key_change"])
            lines.append(f"\n> 🎼 **轉調（Modulation）**：切換至 `{bar['key_change']}`（首調簡譜 `1 = {new_t_name}`）\n")

        sec = bar.get("section")
        if sec and sec != cur_section:
            if cur_section is not None:
                lines.append("")
            lines.append(f"## {sec}")
            lines.append("")
            lines.append("| 小節 | 力度 | 和弦 | 右手主旋律簡譜（1 = 首調） | 歌詞 |")
            lines.append("| :---: | :---: | :--- | :--- | :--- |")
            cur_section = sec

        chords_str = " / ".join(c["symbol"] for c in bar.get("chords", [])) or "—"
        dyn = bar.get("dynamic") or ""

        # Convert RH melody (top note of each event) to Jianpu tokens
        jp_tokens = []
        bar_lyr_parts = []
        for ev in bar.get("rh", []):
            pitches = ev.get("pitches", [])
            if pitches == ["R"]:
                pitches = []
            dur = float(ev["dur"])
            if ev.get("lyric"):
                bar_lyr_parts.append(ev["lyric"])
            if not pitches:
                tok = "0"
            else:
                top_m = max(pitch_to_midi(p) for p in pitches)
                tok = midi_to_jianpu(top_m, cur_tonic_pc)

            if dur >= 4.0:
                jp_tokens.append(f"{tok} - - -")
            elif dur >= 3.0:
                jp_tokens.append(f"{tok} - -")
            elif dur >= 2.0:
                jp_tokens.append(f"{tok} -")
            elif dur >= 1.5:
                jp_tokens.append(f"{tok}.")
            elif dur >= 1.0:
                jp_tokens.append(tok)
            elif dur >= 0.75:
                jp_tokens.append(f"{tok}.")
            else:
                jp_tokens.append(f"_{tok}_")

        lyric_str = "".join(bar_lyr_parts) if bar_lyr_parts else bar.get("lyrics", "")
        lines.append(
            f"| {bar['number']} | {dyn} | `{chords_str}` | `{' '.join(jp_tokens)}` | {lyric_str} |"
        )

    lines += [
        "",
        "---",
        "",
        "### 演奏提示",
        "1. 每個和弦切換點務必重新踩放延音踏板（Syncopated Pedaling），避免前後和聲混濁。",
        "2. 右手最高音為歌唱主旋律，指尖保持飽滿集中；左手分解和弦控制在右手音量的 60%～70%。",
        "3. 可搭配同資料夾的 `.pdf` 五線譜、`.musicxml`（MuseScore 可編輯檔）與 `_試聽.mp3` 一起練習。",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def find_soundfont(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.exists() else None
    for cand in DEFAULT_SOUNDFONT_CANDIDATES:
        if cand.exists():
            return cand
    return None


def render_audio_preview(midi_path: Path, wav_path: Path, mp3_path: Path, sf_path: Path) -> None:
    """Render MIDI to realistic piano WAV + MP3 using FluidSynth + ffmpeg."""
    cmd_fs = [
        "fluidsynth",
        "-ni",
        "-g", "0.85",
        "-R", "1",
        "-C", "0",
        "-r", "44100",
        "-F", str(wav_path),
        str(sf_path),
        str(midi_path),
    ]
    proc = subprocess.run(cmd_fs, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FluidSynth failed:\n{proc.stderr}")

    cmd_ff = [
        "ffmpeg", "-y", "-i", str(wav_path),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-b:a", "192k",
        str(mp3_path),
    ]
    proc2 = subprocess.run(cmd_ff, capture_output=True, text=True)
    if proc2.returncode != 0:
        raise RuntimeError(f"ffmpeg MP3 encode failed:\n{proc2.stderr}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render structured score.json into PDF, PNG, MusicXML, MIDI, MP3, and Jianpu.")
    parser.add_argument("--score", required=True, help="Path to verified score.json")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument("--prefix", default=None, help="Output filename prefix (defaults to song title)")
    parser.add_argument("--soundfont", default=None, help="Optional .sf2/.sf3 SoundFont path for audio preview")
    parser.add_argument("--skip-audio", action="store_true", help="Skip FluidSynth audio rendering")
    args = parser.parse_args()

    score_path = Path(args.score).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    score = json.loads(score_path.read_text(encoding="utf-8"))
    prefix = args.prefix or f"{score.get('title', 'piano_score')}_鋼琴譜"

    ly_path = out_dir / f"{prefix}.ly"
    pdf_base = out_dir / prefix
    xml_path = out_dir / f"{prefix}.musicxml"
    mid_path = out_dir / f"{prefix}.mid"
    wav_path = out_dir / f"{prefix}_試聽.wav"
    mp3_path = out_dir / f"{prefix}_試聽.mp3"
    jp_path = out_dir / f"{prefix.replace('_鋼琴譜', '')}_簡譜與和弦.md"

    # 1. LilyPond source
    ly_src = build_lilypond(score)
    ly_path.write_text(ly_src, encoding="utf-8")
    print(f"OK: Wrote LilyPond source -> {ly_path}")

    # 2. LilyPond PDF + PNG engraving
    lilypond_bin = shutil.which("lilypond")
    if lilypond_bin:
        print("Compiling PDF via LilyPond...")
        p_pdf = subprocess.run(
            [lilypond_bin, "--pdf", "-o", str(pdf_base), str(ly_path)],
            capture_output=True,
            text=True,
        )
        if p_pdf.returncode != 0:
            print(p_pdf.stdout)
            print(p_pdf.stderr, file=sys.stderr)
            raise RuntimeError("LilyPond PDF compilation failed.")
        print(f"OK: Engraved PDF -> {pdf_base}.pdf")

        print("Rendering high-res PNG previews via LilyPond...")
        p_png = subprocess.run(
            [lilypond_bin, "--png", "-dresolution=180", "-o", str(pdf_base), str(ly_path)],
            capture_output=True,
            text=True,
        )
        if p_png.returncode != 0:
            print(p_png.stderr, file=sys.stderr)
            raise RuntimeError("LilyPond PNG rendering failed.")
        pngs = sorted(out_dir.glob(f"{prefix}*.png"))
        print(f"OK: Rendered {len(pngs)} PNG page(s): {[p.name for p in pngs]}")
    else:
        print("WARN: lilypond not found on PATH; skipped PDF/PNG engraving.", file=sys.stderr)

    # 3. MusicXML
    build_musicxml(score, xml_path)
    print(f"OK: Exported MusicXML -> {xml_path}")

    # 4. Expressive MIDI
    build_midi(score, mid_path)
    print(f"OK: Exported MIDI -> {mid_path}")

    # 5. Jianpu & Chord Markdown
    build_jianpu_markdown(score, jp_path)
    print(f"OK: Exported Jianpu & Chords -> {jp_path}")

    # 6. Piano Audio Preview (FluidSynth + ffmpeg)
    if not args.skip_audio:
        sf = find_soundfont(args.soundfont)
        if sf and shutil.which("fluidsynth") and shutil.which("ffmpeg"):
            print(f"Rendering Grand Piano preview audio using {sf.name}...")
            render_audio_preview(mid_path, wav_path, mp3_path, sf)
            wav_path.unlink(missing_ok=True)
            print(f"OK: Rendered Piano Audio Preview -> {mp3_path}")
        else:
            print("WARN: SoundFont or FluidSynth/ffmpeg missing; skipped MP3 preview.", file=sys.stderr)


if __name__ == "__main__":
    main()
