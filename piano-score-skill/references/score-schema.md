# Structured Piano Score JSON Schema (`score.json`)

本技能採用結構化 JSON 作為鋼琴譜的單一真實來源（Single Source of Truth），再由 `scripts/render_score.py` 一次編譯為五線譜 PDF、PNG、LilyPond (`.ly`)、MusicXML、MIDI、鋼琴試聽音檔與簡譜和弦 Markdown。

## 頂層欄位（Top-Level Metadata）

```json
{
  "title": "一千個傷心的理由",
  "subtitle": "鋼琴獨奏改編版（原調）",
  "artist": "張學友",
  "composer": "李偲菘",
  "lyricist": "邢增華",
  "arranger": "McGrady-AI",
  "difficulty": "intermediate",
  "key": "Bb major",
  "time_signature": "4/4",
  "tempo_bpm": 70,
  "tempo_text": "Andante espressivo（深情的行板）",
  "jianpu_tonic": "Bb",
  "measures": [ ... ]
}
```

## 小節結構（Measure Object）

每一個小節描述一個完整的拍號時值（例如 `4/4` 拍中，右手與左手各自所有事件時值總和必須嚴格等於 `4.0` 拍）：

```json
{
  "number": 1,
  "section": "Intro 前奏",
  "key_change": null,
  "tempo_change": null,
  "dynamic": "mp",
  "pedal": "bar",
  "chords": [
    {"beat": 1.0, "symbol": "Gm"},
    {"beat": 3.0, "symbol": "Eb"}
  ],
  "lyrics": "愛 過 的 人",
  "rh": [
    {"pitches": ["G4"], "dur": 1.0, "slur": "start"},
    {"pitches": ["Bb4"], "dur": 0.5},
    {"pitches": ["D5"], "dur": 0.5},
    {"pitches": ["F5"], "dur": 1.5, "arpeggio": false},
    {"pitches": ["D5"], "dur": 0.5, "slur": "stop"}
  ],
  "lh": [
    {"pitches": ["G2"], "dur": 0.5},
    {"pitches": ["D3"], "dur": 0.5},
    {"pitches": ["G3"], "dur": 0.5},
    {"pitches": ["Bb3"], "dur": 0.5},
    {"pitches": ["Eb2"], "dur": 0.5},
    {"pitches": ["Bb2"], "dur": 0.5},
    {"pitches": ["Eb3"], "dur": 0.5},
    {"pitches": ["G3"], "dur": 0.5}
  ]
}
```

## 欄位規格說明

1. **`pitches`（音高陣列）**：
   - 使用標準科學音高記法（Scientific Pitch Notation）：`C4` 為中央 C，升降記號用 `#` 或 `b`（例如 `Bb4`, `F#3`, `Eb2`）。
   - 休止符使用空陣列 `[]` 或 `["R"]`。
   - 同時發聲的和弦使用多元素陣列，例如 `["D4", "F4", "Bb4"]`（由低至高排列）。
2. **`dur`（時值，以四分音符 = 1.0 拍計）**：
   - 支援值：`4.0`（全音符）、`3.0`（附點二分）、`2.0`（二分）、`1.5`（附點四分）、`1.0`（四分）、`0.75`（附點八分）、`0.5`（八分）、`0.25`（十六分）。
   - 若需要跨拍連結（例如 `2.5` 拍），請拆成兩個事件並加上 `"tie": true`（如 `2.0` + `"tie": true` 後接 `0.5`），確保五線譜切分記譜清晰易讀！
3. **`tie` / `slur` / `arpeggio` / `articulation`**：
   - `"tie": true`：連結至下一個相同音高的音符（延音線）。
   - `"slur": "start"` / `"stop"`：圓滑線起訖點。
   - `"arpeggio": true`：在和弦前加上琶音波浪線（`\arpeggio`）。
   - `"articulation": "accent" | "tenuto" | "staccato"`：演奏法記號。
4. **`pedal`（延音踏板策略）**：
   - `"bar"`：整小節踩下一次踏板（第 1 拍踩下、第 4 拍末抬起）。
   - `"half"`：每半小節換一次踏板（第 1 拍與第 3 拍各換一次，適合一小節兩個和弦！）。
   - `"none"`：不標示踏板。
   - `"chord"`：自動依 `chords` 切換點換踏板（最推薦！避免不同和弦混濁）。
5. **`key_change`（轉調）**：
   - 可選字串，如 `"C major"` 或 `"B major"`。當歌曲在最後一段副歌升半音或升全音時，在轉調首小節指定此欄位，系統會自動切換五線譜調號、雙白線小節線（`\bar "||"`）與簡譜調性標註！
6. **`dynamic`（力度記號）**：
   - 支援 `"pp"`, `"p"`, `"mp"`, `"mf"`, `"f"`, `"ff"`, `"cresc"`, `"dim"`。
7. **`chords`（和弦代號）**：
   - 例：`Bb`, `Gm`, `Eb`, `F`, `F/A`, `Bb/D`, `Cm7`, `F7`, `Dm7`, `Gsus4`。
   - 渲染器會將和弦名稱整齊顯示在大譜表上方。
8. **`lyrics`（歌詞）**：
   - 繁體中文字串（可加空格對齊樂句，會同時顯示於五線譜兩譜表之間或上方參考，以及簡譜 Markdown 對照表）。
