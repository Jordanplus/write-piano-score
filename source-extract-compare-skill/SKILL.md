---
name: source-extract-compare-skill
description: >-
  獨立進行音訊來源型態判別（純鋼琴獨奏 / 原唱樂團編曲 / 多樂器演奏）、高精度複音 MIDI 抽取
  （ByteDance Neural Piano Transcription / 多音頻譜抽取），並在進入編譜前執行嚴格的
  「抽取 MIDI vs 原始來源音訊」1:1 時間軸忠實度比對（Chroma 重建相似度、漏抓最高聲部／滾奏頂音偵測、
  幻覺雜音偵測與同步切片實聽），確保從 YouTube 或本地音檔抽出的 MIDI 本身就正確無誤。
  當使用者要求「檢查從 YT 抽取的 MIDI 對不對」、「先做來源判斷與抽取正確性比對」、
  或作為 piano-score-skill Stage 2 前置閘門時使用此技能。
---

# 🎯 Source Extract & Compare Skill（來源判別、高精度 MIDI 抽取與忠實度獨立比對）

本技能專門解決自動扒譜管線最上游的致命痛點：**「如果從 YouTube / 音檔抽取的原始 MIDI 本身就漏音、錯拍或把滾奏頂音濾掉，後續編曲與排版再用心也會繼承錯誤！」**

透過**四階段獨立上游閘門（Upstream Extraction Gate）**，先確認「來源是什麼」→「用對的模型抽出高解析複音 MIDI」→「把抽出的 MIDI 直接合成音檔，與原曲做 1:1 毫秒級頻譜＋漏音＋聽覺比對」，通過閘門後才交給 `piano-score-skill` 編寫鋼琴譜。

---

## 🏗️ 四階段上游工作流（4-Stage Upstream Pipeline）

```text
[Stage 1] 來源型態與音樂參數獨立判別 (scripts/classify_source.py)
   YouTube URL / 本地音檔 ──> 判斷來源型態：solo_piano / vocal_pop_band / instrumental_ensemble
                         ──> 獨立實測：音準偏移(cents)、真實調性與轉調、真實 BPM 候選
                         ──> 決定最佳抽取引擎（避免單音 pYIN 誤用於複音鋼琴！）
                │
                ▼
[Stage 2] 高精度複音 MIDI 抽取 (scripts/extract_source_midi.py)
   solo_piano      ──> ByteDance Neural Piano Transcription (Kong et al., 88-key onset/offset/vel/CC64)
   vocal_pop_band  ──> 提示分軌／多音頻譜抽取（搭配高精度 STFT 雙倍頻頂音追蹤）
                   ──> 產出：extracted_raw.mid + extracted_notes.json + extracted_preview.mp3
                │
                ▼
[Stage 3] 抽取 MIDI vs 原始音訊 1:1 忠實度比對 (scripts/compare_extracted_vs_source.py)
   extracted_raw.mid + extracted_preview.mp3 vs 原始 audio.wav（無 DTW 放水、嚴格同時間軸比對！）
   ├─ 1. 逐 2 秒視窗 Chroma 相似度 & 高音域 (C4–C7) 頻譜重建相關係數
   ├─ 2. 漏抓高聲部／滾奏頂音掃描器（8192-pt STFT 同時驗證基頻 f0 + 2*f0，專抓內聲部大聲蓋過頂音之案例！）
   ├─ 3. 幻覺／多抓音符掃描器（檢核 MIDI 中存在但原音頻譜無能量之假音）
   └─ 4. 自動針對所有異常時間點切出「原曲 vs 抽取 MIDI」同步對照 MP3 片段
                │
                ▼
[Stage 4] 獨立實聽確認與閘門裁決 (references/extraction-rubric.md)
   以 read_file 實聽可疑區段原音 vs 抽取試聽檔，輸出：
   EXTRACT_PASS / EXTRACT_WARN / EXTRACT_FAIL + 精確漏音／修正時間點清單
   ──> 達 EXTRACT_PASS（或修正完漏音清單）後，才交付 piano-score-skill 進入 Stage 3 編譜！
```

---

## ⚙️ 環境與工具需求

- **Python 虛擬環境**：`/Users/mcgradymac/Gemini/write-piano-score/.venv/bin/python`
  - 核心神經轉錄引擎：`torch`, `torchlibrosa`, `piano-transcription-inference`（Apple Silicon MPS / CPU 支援）
  - 訊號處理與 MIDI：`librosa`, `scipy`, `numpy`, `pretty_midi`, `mido`, `soundfile`
- **系統工具**：`fluidsynth`（搭配 `MuseScore_General.sf3`）、`ffmpeg`、`yt-dlp`
- **技能路徑**：`SEC_DIR=/Users/mcgradymac/Gemini/write-piano-score/source-extract-compare-skill`

---

## 📋 逐步執行指南（Step-by-Step Runbook）

### Stage 1：來源型態與音訊參數判別

```bash
PY=/Users/mcgradymac/Gemini/write-piano-score/.venv/bin/python
SEC_DIR=/Users/mcgradymac/Gemini/write-piano-score/source-extract-compare-skill

$PY $SEC_DIR/scripts/classify_source.py \
  --audio "<工作資料夾>/audio.wav" \
  --metadata "<工作資料夾>/metadata.json" \
  --out-dir "<工作資料夾>/source_check"
```

檢視產出的 `source_profile.json` 與終端摘要：
- 確認 `source_type`（`solo_piano` / `vocal_pop_band` / `instrumental_ensemble`）與建議的 `recommended_extractor`。
- 確認 `tuning_semitones`、`global_key`、`key_windows`（有無轉調）及 `bpm_candidates`（是否存有 4:3 或 2:1 拍速歧義）。

---

### Stage 2：高精度複音 MIDI 抽取與試聽合成

```bash
$PY $SEC_DIR/scripts/extract_source_midi.py \
  --audio "<工作資料夾>/audio.wav" \
  --out-dir "<工作資料夾>/source_check" \
  --backend auto
```

- `--backend auto` 會優先啟用 **ByteDance `piano_transcription_inference`**（高精度 88 鍵神經網路鋼琴轉錄，精準捕捉滾奏雙音、延音踏板與力道）；若環境無模型權重或非鋼琴來源，可降級或搭配 `--backend stft_poly`（高解析 8192-pt 雙倍頻多音抽取器）。
- 產出：`extracted_raw.mid`、`extracted_notes.json`、`extracted_preview.mp3`。

---

### Stage 3：抽取 MIDI vs 原音 1:1 忠實度比對（核心防錯關卡）

```bash
$PY $SEC_DIR/scripts/compare_extracted_vs_source.py \
  --ref-audio "<工作資料夾>/audio.wav" \
  --extracted-midi "<工作資料夾>/source_check/extracted_raw.mid" \
  --extracted-audio "<工作資料夾>/source_check/extracted_preview.mp3" \
  --out-dir "<工作資料夾>/source_check/compare" \
  --slice-suspects
```

重點判讀產出的 `compare/extraction_compare_summary.txt` 與 `extraction_compare.json`：
1. **全域 1:1 時間軸 Chroma 相似度（`avg_chroma_sim`）**與 **高音域頻譜重建相關係數（`treble_corr`）**。
2. **漏抓最高聲部警報（`MISSED_TOP_VOICE`）**：專抓類似「滾奏雙音 `<Bb4, D5>` 中內聲部較大聲、頂音 `D5` 被漏抓」的致命旋律盲點！
3. **幻覺雜音警報（`SPURIOUS_NOTE`）**：抓出抽取 MIDI 多寫但原曲無能量的雜音（常見於延音踏板倍頻或雜訊）。
4. **自動切片對照檔**：腳本會在 `compare/clips/` 自動切出異常區間的 `orig_*.mp3` 與 `ext_*.mp3`。

---

### Stage 4：獨立實聽抽核與閘門放行

1. 使用 `read_file` 實聽 `compare/clips/` 中標記為 `WARN` / `FAIL` 的原曲與抽取試聽片段（以及任選 2 段關鍵主歌／副歌正常片段抽檢）。
2. 對照 [extraction-rubric.md](./references/extraction-rubric.md) 給出閘門裁決：
   - **`EXTRACT_PASS`**：可直接以 `extracted_raw.mid` 與 `extracted_notes.json` 作為精確 Ground Truth，交由 `piano-score-skill` 編寫出版級 `score.json`。
   - **`EXTRACT_WARN`**：整體準確但存在局部漏抓頂音／雜音清單，交付 `piano-score-skill` 時必須附上「必修正時間點清單」。
   - **`EXTRACT_FAIL`**：來源判別錯誤或轉錄引擎嚴重失真（例如對含人聲與鼓組的完整樂團版直接套用純鋼琴模型），須切換分軌或調整引擎重抽，**嚴禁帶著錯誤的抽取結果進入寫譜階段**！
