---
name: piano-score-skill
description: >-
  依指定歌曲（YouTube 連結或本機音訊檔）全自動分析音高、調性、轉調、BPM、和弦進行與主旋律，
  並編寫出版級雙手鋼琴譜（五線譜 PDF、高解析 PNG 預覽、LilyPond 原始碼、可編輯 MusicXML、
  表情 MIDI、FluidSynth 真實平台鋼琴試聽 MP3、以及繁體中文簡譜與和弦對照表）。
  當使用者要求「把某首歌寫成鋼琴譜」、「抓歌編鋼琴譜」、「把 YouTube 音樂轉成五線譜／簡譜」時使用此技能。
---

# 🎹 Piano Score Skill（依曲編寫出版級鋼琴譜全流程）

本技能提供一套**「音訊實測分析（Audio-Grounded MIR）＋專業鋼琴編曲（Idiomatic Arranging）＋多格式出版級排版（Multi-Format Engraving）」**的標準化端到端流程，將任何 YouTube 連結或本機音訊轉換為真正可直接彈奏、經過樂理與視覺雙重驗證的雙手鋼琴譜。

---

## 🏗️ 核心五階段工作流（5-Stage Pipeline）

```text
[Stage 1] 音訊擷取與標準化 (scripts/fetch_audio.py)
   YouTube URL / 本機音檔 ──> 44.1kHz WAV + metadata.json
                │
                ▼
[Stage 2] 來源判別、高精度 MIDI 抽取與忠實度比對 (呼叫 source-extract-compare-skill)
   ├─ 1. classify_source.py：判別純鋼琴／原唱樂團、真實調性／轉調、音準與 BPM 候選
   ├─ 2. extract_source_midi.py：純鋼琴以 ByteDance Neural Piano AMT 抽出 88 鍵精準 MIDI
   ├─ 3. compare_extracted_vs_source.py：1:1 時間軸原音比對＋漏抓頂音／滾奏掃描（必達 EXTRACT_PASS）
   └─ 4. 搭配 analyze_audio.py 輔助產出 analysis_summary.txt 供小節和弦與結構參照
                │
                ▼
[Stage 3] 雙手鋼琴編曲定稿 (Agent 撰寫 score.json)
   對照 analysis_summary.txt + references/piano-arrangement-guide.md
   撰寫結構化 score.json（含段落標記、轉調、和弦代號、力度、踏板、歌詞）
                │
                ▼
[Stage 4] 樂理驗證與多格式渲染 (verify_score.py + render_score.py)
   ├─ 1. verify_score.py：嚴格檢查每小節拍數、單手跨度(<=10度)、低音渾濁度、連結線
   └─ 2. render_score.py 一鍵產出 5 大成品：
        • 出版級五線譜 PDF + 逐頁高解析 PNG + LilyPond (.ly)
        • 可編輯 MusicXML (.musicxml，支援 MuseScore / Sibelius / Finale / Dorico)
        • 表情化標準 MIDI (.mid，含主旋律聲部平衡與 CC64 延音踏板)
        • 真實平台鋼琴試聽音檔 (_試聽.mp3，FluidSynth + MuseScore SoundFont)
        • 首調簡譜與和弦歌詞對照表 (_簡譜與和弦.md)
                │
                ▼
[Stage 5] 視覺與獨立旋律品質驗收 (Visual QA + piano-score-review-skill 必過閘門)
   ├─ 1. 視覺檢視逐頁 PNG，確認無文字重疊、無跨拍休止符違規、中文字體清晰
   └─ 2. 強制呼叫 piano-score-review-skill（派出獨立審稿 Subagent＋CQT/DTW＋分段實聽）
         未達 PASS（或使用者核准之 MINOR_REVISION）不可交付，必須回修 Stage 3 重編！
```

---

## ⚙️ 環境與工具前置需求

預設使用工作區內的 Python 3.12 虛擬環境（`/Users/mcgradymac/Gemini/write-piano-score/.venv/bin/python`）以及系統工具：

- **Python 套件**：`yt-dlp`, `librosa`, `soundfile`, `music21`, `mido`, `pretty_midi`, `scipy`, `numpy`
- **樂譜排版引擎**：`lilypond`（`brew install lilypond`）
- **音訊處理與鋼琴合成**：`ffmpeg`、`fluidsynth`（搭配 SoundFont，如 `/Users/mcgradymac/Gemini/music-gen/soundfonts/MuseScore_General.sf3`）

---

## 📋 逐步執行指南（Step-by-Step Runbook）

### Stage 1：擷取與正規化音訊

```bash
PY=/Users/mcgradymac/Gemini/write-piano-score/.venv/bin/python
SKILL_DIR=/Users/mcgradymac/Gemini/write-piano-score/piano-score-skill

# 從 YouTube 連結擷取（支援 --file <本機音檔> 替代 --url）
$PY $SKILL_DIR/scripts/fetch_audio.py \
  --url "<YouTube_URL>" \
  --out-dir "<工作輸出資料夾>"
```

> **驗證檢查**：確認產出 `audio.wav` 與 `metadata.json`，並閱讀 `metadata.json` 中的真實標題與長度（切勿未經確認就假設版本，同一首歌可能有原唱版、鋼琴獨奏版、Live 降調版等不同版本）。

---

### Stage 2：來源判別、高精度 MIDI 抽取與忠實度比對（必過上游閘門！）

> **強制規則（Upstream Extraction Gate）**
> 切勿直接盲信單音 `pYIN`（面對複音鋼琴或滾奏雙音極易漏抓頂音）。在撰寫 `score.json` 之前，**必須先呼叫 [source-extract-compare-skill](../source-extract-compare-skill/SKILL.md)** 完成來源判別、高精度複音 MIDI 抽取與 1:1 原音忠實度比對（達 `EXTRACT_PASS` 或附完整漏音修正表）：

```bash
SEC_DIR=/Users/mcgradymac/Gemini/write-piano-score/source-extract-compare-skill

# 2a. 來源判別 + 神經網路高精度 MIDI 抽取 + 1:1 原音比對
$PY $SEC_DIR/scripts/classify_source.py --audio "<工作輸出資料夾>/audio.wav" --metadata "<工作輸出資料夾>/metadata.json" --out-dir "<工作輸出資料夾>/source_check"
$PY $SEC_DIR/scripts/extract_source_midi.py --audio "<工作輸出資料夾>/audio.wav" --out-dir "<工作輸出資料夾>/source_check" --backend auto
$PY $SEC_DIR/scripts/compare_extracted_vs_source.py \
  --ref-audio "<工作輸出資料夾>/audio.wav" \
  --extracted-midi "<工作輸出資料夾>/source_check/extracted_raw.mid" \
  --extracted-audio "<工作輸出資料夾>/source_check/extracted_preview.mp3" \
  --out-dir "<工作輸出資料夾>/source_check/compare" --slice-suspects

# 2b. 輔助小節和弦網格分析（可選）
$PY $SKILL_DIR/scripts/analyze_audio.py \
  --audio "<工作輸出資料夾>/audio.wav" \
  --out-dir "<工作輸出資料夾>" \
  --beats-per-bar 4
```

- 若已知歌曲大概速度（例如慢板抒情約 70 BPM），可加上 `--bpm-hint 70` 避免倍速誤判。
- 腳本會同時產出三份分析檔：
  1. `analysis.json`：完整機器可讀之拍點、和弦、低音與旋律資料。
  2. `analysis_summary.txt`：全域調性、滑動視窗轉調掃描、以及逐小節和弦／低音／旋律總表。
  3. `onsets.txt`：高解析度音符起音（Onset）之高音部主旋律與低音部根音清單（對於純鋼琴獨奏、前奏鉤子與複雜過門極度精準）。
- **進階聽覺對照**：若需確認細微節奏切分、裝飾音或原曲織體，可用 `ffmpeg -y -i audio.wav -b:a 96k audio.mp3` 壓出輕量音檔後，直接以 `read_file` 聆聽確認！

> **關鍵原則**：**永遠以實測音訊的真實調性與和弦為最高準則**，不可盲信網路吉他譜的調性或簡易和弦。若原曲後半段有升半音或升全音轉調，必須在 `score.json` 中透過 `key_change` 忠實還原！

---

### Stage 3：編寫結構化鋼琴譜 (`score.json`)

在撰寫 `score.json` 之前，請先閱讀以下兩份核心參考文件：
1. [score-schema.md](./references/score-schema.md)：完整的 JSON 結構、時值、踏板、轉調、逐字歌詞對齊規格。
2. [piano-arrangement-guide.md](./references/piano-arrangement-guide.md)：鋼琴人體工學跨度限制、華語抒情與流行伴奏織體（1-5-8-9-10 分解和弦、順階低音）、難度分級規範（`beginner` / `intermediate` / `advanced`）。

編曲原則：
- **忠於原曲結構**：完整涵蓋 Intro 前奏、Verse 主歌、Pre-Chorus 導歌、Chorus 副歌、Interlude 間奏、終極副歌（含轉調）與 Outro 尾奏。
- **可彈奏性優先**：右手旋律清晰突出，副歌才加入適度三度/六度/八度豐厚織體；左手低音避開 `C3` 以下密集三度，單手同時跨度不超過十度。
- **情緒動態弧線**：從主歌 `p`/`mp` 到副歌 `mf`/`f` 再到尾奏 `p`/`pp`，搭配和弦同步踏板（`"pedal": "chord"`）。

---

### Stage 4：樂理自動驗證與五合一成品渲染

```bash
# 4a. 嚴格驗證拍數、跨度、連結線與和弦語法（必須 PASS 才可進入排版！）
$PY $SKILL_DIR/scripts/verify_score.py --score "<輸出資料夾>/score.json"

# 4b. 一鍵編譯產出 PDF / PNG / .ly / MusicXML / MIDI / 鋼琴試聽 MP3 / 簡譜 Markdown
$PY $SKILL_DIR/scripts/render_score.py \
  --score "<輸出資料夾>/score.json" \
  --out-dir "<輸出資料夾>" \
  --prefix "<曲名>_鋼琴譜"
```

---

### Stage 5：視覺驗收與 `piano-score-review-skill` 獨立品質閘門（必過！）

> **強制規則（Mandatory Independent Audit Gate）**
> `verify_score.py` 的 `PASS` **僅代表拍數與跨度合法，完全不代表旋律正確**。
> 每次產出或大幅修訂鋼琴譜後，**必須**執行以下兩步驟，否則不得向使用者交付成品：

1. **視覺驗收**：使用 `read_file` 檢視產出的 `<曲名>_鋼琴譜-page1.png`、`page2.png`…，確認：
   - 繁體中文曲名、作曲/作詞資訊與逐字歌詞完整無亂碼、無撞疊。
   - 和弦名稱與小節段落框（Rehearsal Marks）位置乾淨。
   - 無違規跨拍休止符（如第 3.5 拍不可寫單一 `r8.`，須拆為 `r8 r16`）。
   - 生成的 `_試聽.mp3` 音檔大小正常、長度符合原曲總小節數與 BPM。
2. **強制獨立旋律稽核（呼叫 `piano-score-review-skill`）**：
   - 嚴禁由寫譜對話自我宣布「旋律正確」；請依 [piano-score-review-skill](../piano-score-review-skill/SKILL.md) 流程，**派出獨立審稿 Subagent** 執行：
     1. `extract_score_melody.py` 萃取逐小節旋律與簡譜；
     2. `audit_audio_vs_score.py` 進行原曲 vs 樂譜 CQT/DTW 客觀比對；
     3. `slice_review_clips.py` 同步切片原曲與 `_試聽.mp3`，並以 `read_file` 分段實聽交叉驗證；
     4. 輸出逐段逐小節稽核報告與品質閘門裁決（`PASS` / `MINOR_REVISION` / `MAJOR_REVISION` / `REJECT_REWRITE`）。
   - **通過門檻**：必須達到 **`PASS`**（估計主旋律正確率 >= 95%、零致命錯誤、調性／轉調／曲式完全正確）始可正式交付；若為 `MAJOR_REVISION` 或 `REJECT_REWRITE`，必須依稽核報告回返 Stage 3 修正 `score.json`、重新渲染並再次通過獨立複審。

---

## 📁 參考完整範例（Reference Example）

本專案附帶完整端到端範例：**張學友《一千個傷心的理由》（53 小節精修版）**，位於專案根目錄：
- [../examples/jacky-cheung-1000-reasons/](../examples/jacky-cheung-1000-reasons/)
  - `score.json`（標準抒情獨奏譜結構範本）
  - `一千個傷心的理由_鋼琴譜.pdf` / `.ly` / `.musicxml` / `.mid` / `_試聽.mp3`
  - `一千個傷心的理由_簡譜與和弦.md`
