# SVS Lab Aligner

**Singing Voice Synthesis Lab Aligner**

一個完全獨立的 Web 應用程序，面向歌聲合成（SVS）工作流程，用於將「輸入音訊 + 文字」轉換為可在歌聲合成軟體中使用的工程檔案。

它的目標不是做 TTS，也不是替代歌聲合成引擎本身，而是作為一座橋樑，幫助歌姬「假裝擅長說話」：

- 先用 **[MFA](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner)** 將音訊與文字自動對齊，產生 `.lab`
- 再用 **[PyWORLD](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)** 擷取 F0 音高
- 最後輸出歌聲合成工程文件，方便在軟體中繼續編輯播放

---

## 語言（Language）

[**简体中文**](../../README.md) | [**繁體中文**](./README_ZH_TW.md) | [**English**](./README_EN.md) | [**日本語**](./README_JP.md) | [**한국어**](./README_KR.md) 

---

## ✨ 主要功能

* **僅標註（快速）**
 只執行 MFA 自動對齊，快速產生 `.lab`。

* **完整處理（標註 + F0 + 工程文件）**
 自動完成標註、音高提取，並輸出歌聲合成工程檔。

* **僅產生工程（WAV + LAB）**
 基於已有的 `WAV + LAB` 直接產生工程文件，無需重新標註。

* **支援多種音高擷取演算法**
 內建 **PyWORLD、CREPE、RMVPE** 三種 F0 擷取方式，可依不同需求選擇不同演算法。

* **支援連續音高寫入**
 可選擇僅產生純淨音符，也可將連續 F0 曲線寫入工程，保留更真實的音高變化。

* **支援長音頻處理**
 能夠處理長時間音頻，適用於完整歌曲、長篇語音等場景。

* **支援多種文字轉換方式**
 在「僅產生工程（WAV + LAB）」模式下，可選擇：

 * 不轉換
 * 合併輔音
 * 平假名
 * 片假名

* **支援多種音訊格式**
 `WAV / MP3 / FLAC / M4A / AAC`

* **支援多語文字對齊**

* **支援 LAB 檔案匯出**

* **支援多種對其演算法**
 內建 **MFA、WhisperX、Qwen3-ASR、Qwen3-FA** 四種對其方式，可依不同需求選擇不同演算法。

* **提供簡潔易用的 Web 介面**

---

## 📌 適用場景

- 已經有歌聲合成或 TTS 音頻，希望快速產生標註
- 想把語音工程導入歌聲合成軟體繼續編輯
- 想批量產生 `.lab`、F0 和工程文件
- 想在「說話」和「唱歌」之間建立可編輯的中間層

---

## 🛠️ 運作原理

1. 輸入音訊檔案與文字
2. 使用 **[MFA](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner)** 進行自動時間對齊
3. 產生 `.lab` 標註文件
4. 使用 **[PyWORLD](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)** 擷取 F0 音高
5. 根據處理模式輸出工程文件
6. 將結果導入歌聲合成軟體繼續使用

---

## 🚀 快速開始

### Windows

```batch
setup.bat
run.bat
```

### Linux / Mac

```Linux / Mac
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

啟動後訪問：
```
http://localhost:5000
```
## 🔧 處理模式
### 1. 僅標註（快速）

只進行 MFA 自動標註，產生 .lab 檔。

### 2. 完整處理（標註 + F0 + 工程文件）

執行 MFA 對齊、F0 提取，並產生工程文件。

### 3. 僅產生工程（WAV + LAB）

使用現有的 WAV + LAB 產生工程文件，不再重複做完整標註流程。

### 📋 系統需求
Python 3.8+

Node.js 16+

4GB RAM

1GB 可用磁碟空間

### 🔧 安裝 MFA
```
pip install montreal-forced-aligner
```

# 下載語言模型
```
mfa model download acoustic cmn # 中文
mfa model download acoustic eng # 英語
mfa model download acoustic jpn # 日語
mfa model download acoustic kor # 韓語
mfa model download acoustic yue # 粵語
```

## 📖 使用流程
### 僅標註（快速）

1. 開啟本應用

2. 上傳音訊文件

3. 輸入文字

4. 選擇語言

5. 點選開始標註

6. 下載產生的 .lab

### 完整處理（標註 + F0 + 工程文件）

1. 上傳音訊文件

2. 輸入文字

3. 選擇語言

4. 選擇處理模式為“完整處理”

5. 點選開始處理

6. 下載產生的工程文件

### 僅產生工程（WAV + LAB）

1. 準備好 WAV 和對應的 LAB

2. 選擇“僅生成工程”

3. 上傳 WAV + LAB

4. 點選生成

5. 下載工程文件

### 📝 輸出內容
根據處理模式，應用可輸出：

- .lab 時間對齊文件
- F0 音高訊息
- 歌聲合成工程文件

### 📁 目錄結構
```
backend/ 後端服務
frontend/ 前端介面
setup.bat Windows 環境安裝腳本
run.bat Windows 啟動腳本
setup.sh Linux / Mac 環境安裝腳本
run.sh Linux / Mac 啟動腳本
```

## ⚠️ 免責聲明

本項目用於以下用途：

- 音訊自動對齊（MFA）
- 音高擷取（PyWORLD / F0）
- 歌聲合成工程文件生成

---

### 📌 功能限制

本工具僅作為“SVS 工作流程輔助工具”，不具備以下能力：

- 不進行語音合成（TTS / SVS）
- 不訓練或修改聲庫模型
- 不取代任何歌聲合成軟體

---

### 📁 使用責任

使用本工具產生的結果（包括 `.lab`、F0 資料及工程文件）：

- 由使用者自行負責其合法性與用途
- 使用前請確認所使用音訊及聲庫的授權範圍
- 若涉及第三方聲庫，請遵守其官方使用條款

---

### 🎭 聲庫與標註說明

如聲庫或合成系統要求註明來源，請根據對應協議標註（是否要標明演技指導取決於轉換成工程文件後的聲庫的聲音是否被識別有原樣本特徵，如果被識別有原樣本特徵，請根據對應協議標註），例如：

- “使用聲庫：XXX”
- “演技指導：XXX”

---

### 📌 版權說明

本項目不聲明對使用者輸入資料或產生內容的所有權。

資料版權歸使用者所有，取決於所使用原始樣本轉換成工程文件後的聲庫的聲音，是否被識別有原始樣本特徵。如果使用第三方原始樣本被識別有原始樣本特徵，則可能被視為第三方的衍生作品。

---

## 📝 許可證
MIT License

包括非「軟體」部分的文件（僅原創部分有效）
