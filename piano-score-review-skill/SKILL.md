---
name: piano-score-review-skill
description: >-
  獨立稽核與檢驗鋼琴譜（PDF / PNG / score.json / MIDI / MusicXML / 試聽音檔）與原始參考音訊
  （YouTube 連結或本機音檔）之間的旋律、調性、轉調、曲式結構、節奏拍位與和聲正確性。
  透過「獨立 Subagent 盲測審查＋CQT/DTW 客觀 DSP 比對＋分段音訊實聽＋譜面視覺交叉驗證」，
  輸出逐段逐小節的錯誤清單、嚴重度分級、根因診斷與修正建議。
  當使用者要求「檢查鋼琴譜旋律對不對」、「稽核抓歌正確性」、「比對原曲與樂譜差異」、
  或「驗收 piano-score-skill 產出的鋼琴譜品質」時使用此技能。
---

# 🔍 Piano Score Review Skill（鋼琴譜旋律與正確性獨立稽核）

本技能提供一套**「獨立審稿代理人（Independent Auditor Subagent）＋客觀音訊 DSP/DTW 比對＋原曲／試聽分段實聽＋譜面視覺核對」**的四重交叉驗證流程，專門揪出自動採譜或 AI 編曲中常見的**旋律幻覺、調性／轉調錯誤、曲式漏段、小節線偏移、拍子誤寫與和聲衝突**。

> **核心紀律（Anti-Confirmation-Bias Rule）**
> 稽核者**不得**沿用寫譜者（Author Agent）的既有對話脈絡，也不得盲信 `piano-score-skill` 產出的 `analysis_summary.txt` 或 `verify_score.py` 的 `PASS`（`verify_score.py` 只檢查拍數與指距，**完全不驗證旋律是否符合原曲**）。
> 若目前對話剛產出過樂譜，請務必透過 `invoke_subagent` 派出獨立的審稿 Subagent，以「假設樂譜有錯、逐句舉證」的態度執行完整稽核。

---

## 🏗️ 五階段多模態稽核流程（5-Stage Audit Pipeline）

```text
[Stage 0] 派駐獨立審稿 Subagent（隔離對話脈絡、禁止改動目標樂譜檔）
                │
                ▼
[Stage 1] 樂譜解析與譜面核對 (scripts/extract_score_melody.py + read_file PDF/PNG)
   score.json / .mid / .pdf ──> 逐小節主旋律音高、簡譜級數、拍位、和弦、歌詞總表
                │
                ▼
[Stage 2] 客觀 DSP/MIR 交叉稽核 (scripts/audit_audio_vs_score.py)
   原曲音訊 vs 樂譜 MIDI/試聽檔 ──> 音準偏移 / 全域與滑動視窗調性 / 轉調偵測
                              ──> CQT Chroma DTW 時間對齊（每小節對應原曲精確秒數）
                              ──> 逐小節和聲相似度 + 右手主旋律頻譜命中率 + 可疑小節標記
                │
                ▼
[Stage 3] 分段同步切片與耳朵實聽 (scripts/slice_review_clips.py + read_file 音檔)
   依曲式段落或可疑區間切出 15~30 秒 MP3（原曲 + 樂譜試聽對照）
   ──> 稽核 Agent 以 read_file 直接聆聽確認真實旋律、八度、切分與樂句收尾
                │
                ▼
[Stage 4] 逐段逐小節多模態彙整報告 (references/audit-checklist.md + report-template.md)
   六大維度評核 + 錯誤嚴重度分級（致命 / 嚴重 / 中等 / 輕微）+ 根本原因診斷
                │
                ▼
[Stage 5] 品質閘門裁決（Quality Gate Verdict）與可執行修正清單
   PASS / MINOR_REVISION / MAJOR_REVISION / REJECT_REWRITE
```

---

## ⚙️ 環境與前置需求

預設使用專案 Python 虛擬環境與系統工具：
- **Python**：`/Users/mcgradymac/Gemini/write-piano-score/.venv/bin/python`
  （套件：`librosa`, `numpy`, `scipy`, `soundfile`, `pretty_midi`, `mido`, `yt-dlp`）
- **音訊處理**：`ffmpeg`
- **技能路徑**：`REVIEW_SKILL_DIR=/Users/mcgradymac/Gemini/write-piano-score/piano-score-review-skill`
- **暫存規則**：所有稽核過程產生的中間檔（`score_melody_table.txt`、`audit_metrics.json`、切片 MP3）一律輸出到獨立暫存目錄（如 `<artifactDir>/scratch/review_<song>/`），**嚴禁修改或覆蓋受檢的原始樂譜檔案**。

---

## 📋 逐步執行指南（Step-by-Step Runbook）

### Stage 0：派駐獨立審稿 Subagent（強烈建議）

當使用者要求「派獨立 agent 檢查」或「剛寫完譜要驗收」時，使用 `invoke_subagent`（`TypeName: "self"`, `Role: "Independent Piano Score Auditor"`），在 Prompt 中明確提供：
1. 原始參考音訊來源（YouTube URL 或本機 `audio.wav` / `audio.mp3` 路徑；若僅有 YouTube URL，先以 `piano-score-skill/scripts/fetch_audio.py` 下載並確認 `metadata.json` 中的真實版本與長度）。
2. 受檢鋼琴譜路徑（`score.json`、`.pdf`、逐頁 `-page*.png`、`.mid`、`_試聽.mp3`、`_簡譜與和弦.md`）。
3. 要求 Subagent 閱讀本 `SKILL.md`、`references/audit-checklist.md` 與 `references/report-template.md`，嚴格依 Stage 1 ~ Stage 5 執行並回報完整稽核報告。

---

### Stage 1：樂譜旋律萃取與譜面視覺核對

```bash
PY=/Users/mcgradymac/Gemini/write-piano-score/.venv/bin/python
REVIEW_DIR=/Users/mcgradymac/Gemini/write-piano-score/piano-score-review-skill

# 支援輸入 --score score.json 或 --midi <file.mid>
$PY $REVIEW_DIR/scripts/extract_score_melody.py \
  --score "<樂譜資料夾>/score.json" \
  --out "<暫存目錄>/score_melody_table.txt"
```

接著執行兩項檢查：
1. 閱讀 `<暫存目錄>/score_melody_table.txt`，掌握整首譜的總小節數、段落劃分、調性／轉調、每小節右手最高聲部（主旋律）音高與首調簡譜級數、拍位與歌詞。
2. 使用 `read_file` 檢視受檢的 `.pdf`（或逐頁 `-page1.png` ~ `-pageN.png`），確認 **PDF 實際印出的音符、調號、臨時記號、拍號與小節線**與 `score.json` 一致，並記錄任何記譜／排版誤導演奏者的問題。

---

### Stage 2：客觀 DSP/MIR 交叉稽核與 DTW 對齊

```bash
$PY $REVIEW_DIR/scripts/audit_audio_vs_score.py \
  --ref-audio "<原曲 audio.wav 或 audio.mp3>" \
  --score "<樂譜資料夾>/score.json" \
  --score-audio "<樂譜資料夾>/<曲名>_鋼琴譜_試聽.mp3" \
  --out-dir "<暫存目錄>/dsp_audit"
```
> 若沒有 `_試聽.mp3` 或 `score.json`，可改傳 `--midi "<樂譜>.mid"`，腳本會自動合成參考 chroma 進行比對。

檢視產出的 `<暫存目錄>/dsp_audit/audit_summary.txt` 與 `audit_metrics.json`，重點判讀：
- **音準偏移（Tuning cents）與調性／轉調對照**：原曲實際調性與轉調時間點是否與樂譜吻合？
- **DTW 對齊路徑斜率與跳躍（Structural Warping Anomalies）**：是否有某幾小節在原曲中完全找不到對應（多編／幻覺段落），或原曲大幅跳躍（漏段／少彈反覆）？
- **逐小節和聲 Chroma 相似度（`chroma_sim`）與主旋律頻譜命中率（`melody_hit`）**：所有被標記為 `[SUSPECT]` 或 `[MISMATCH]` 的小節，皆為 Stage 3 必聽重點！

---

### Stage 3：同步音訊切片與耳朵實聽驗證（不可省略！）

DSP 指標可能受踏板延音、泛音或原曲多聲部織體影響，因此**絕不可只看數據下結論**。請切出同步對照片段並直接聆聽：

```bash
# 依整曲分段（每 25 秒一段，或依 --segments 指定精確秒數）切出原曲與試聽對照 MP3
$PY $REVIEW_DIR/scripts/slice_review_clips.py \
  --ref-audio "<原曲 audio.wav>" \
  --score-audio "<樂譜資料夾>/<曲名>_鋼琴譜_試聽.mp3" \
  --audit-json "<暫存目錄>/dsp_audit/audit_metrics.json" \
  --out-dir "<暫存目錄>/clips" \
  --chunk-sec 25
```

接著使用 `read_file` **逐段聆聽** `<暫存目錄>/clips/ref_*.mp3` 與對應的 `score_*.mp3`（或對關鍵可疑小節使用 `--bar-ranges "1-8,17-24"` 精細切片），逐樂句耳朵核對：
1. 主旋律的真實音高輪廓、起始音與句尾落音（Cadence tone）。
2. 是否出現高／低八度錯置、臨時記號錯、或憑記憶硬填非原曲旋律的幻覺。
3. 弱起拍（Anacrusis）、切分音（Syncopation）、附點節奏與休止符是否對齊原曲律動。
4. 若為純鋼琴演奏版原曲（例如 YouTube 鋼琴獨奏版），需特別核對前奏、間奏、過門與尾奏是否忠於該影片演奏版本。

---

### Stage 4：撰寫標準化稽核報告

閱讀兩份參考文件以確保審查完整且格式統一：
1. [audit-checklist.md](./references/audit-checklist.md)：六大檢查維度、嚴重度分級定義、常見 AI 採譜失敗模式清單。
2. [report-template.md](./references/report-template.md)：標準繁體中文稽核報告格式。

每一個列出的錯誤**必須同時具備四要素**：
1. **精確位置**：PDF 頁碼、小節號、拍位、對應歌詞（若有）。
2. **現行譜面內容**：實際寫出的音名＋首調簡譜級數（例如 `F4–G4–A4 (5–6–7)`）。
3. **原曲實測證據**：原曲時間戳記（如 `00:24.5`）＋實際演奏音名與級數（經 CQT 與實聽雙重確認）。
4. **錯誤分類與嚴重度**：`致命` / `嚴重` / `中等` / `輕微`。

---

### Stage 5：品質閘門裁決（Quality Gate Verdict）

依據 `references/audit-checklist.md` 的門檻給出明確裁決，並附上「可直接交回 `piano-score-skill` 修正的具體修改清單」：
- **`PASS`**：估計旋律正確率 >= 95%、零「致命」錯誤、調性／轉調／曲式完全正確。
- **`MINOR_REVISION`**：旋律正確率 85%–94%、零「致命」錯誤、僅有局部裝飾音／尾音／小節細節需微調。
- **`MAJOR_REVISION`**：旋律正確率 65%–84%、或存在關鍵副歌／主歌錯音、局部漏段或小節線偏移。
- **`REJECT_REWRITE`**：旋律正確率 < 65%、或調性／轉調錯誤、多段主旋律為幻覺捏造、曲式與原曲嚴重脫節，建議以重新高精度採譜方式重編 `score.json`。
