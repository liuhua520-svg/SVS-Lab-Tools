# SVS Lab Tools

**Singing Voice Synthesis Lab Tools**

A standalone web application for singing voice synthesis (SVS) workflows. It converts “input audio + text” into project files that can be used in singing voice synthesis software.

The goal is not to perform SVS or replace the singing voice synthesis engine itself. Instead, the app acts as a bridge that helps singing voicebanks “pretend to be good at speaking”:

- First, use **[Qwen3-FA](https://github.com/QwenLM/Qwen3-ASR)** to automatically align the audio and text and generate a `.lab` file.
- Next, use **[PyWORLD](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)** to extract F0 pitch data.
- Finally, export a singing voice synthesis project file so it can be further edited and played in the target software.

---

## Languages

[**简体中文**](../../README.md) | [**繁體中文**](./README_ZH_TW.md) | [**English**](./README_EN.md) | [**日本語**](./README_JP.md) | [**한국어**](./README_KR.md)

📐 Want to learn how the code is organized internally, including the microservices architecture, module responsibilities, core algorithms, and known technical debt?
See [**ARCHITECTURE.md — Technical Architecture and Code Analysis Report**](../ARCHITECTURE.md).

---

## ✨ Key Features

* **Annotation Only (Fast)**  
  Runs only Qwen3-FA automatic alignment to quickly generate `.lab` files.

* **Full Processing (Annotation + F0 + Project File)**  
  Automatically performs annotation and pitch extraction, then outputs a singing voice synthesis project file.

* **Generate Project Only (WAV + LAB)**  
  Generates a project file directly from existing `WAV + LAB/MIDI` files without repeating the annotation process.

* **Multiple Pitch Extraction Algorithms**  
  Includes three F0 extraction methods: **PyWORLD, CREPE, and RMVPE**, allowing you to choose an algorithm for your needs.

* **Continuous Pitch Data Support**  
  Choose to generate clean, discrete notes only, or write a continuous F0 curve into the project to preserve more natural pitch variation.

* **Long Audio Support**  
  Processes long audio files, making it suitable for full songs, long-form speech, and similar use cases.

* **Multiple Text Conversion Options**  
  In **Generate Project Only (WAV + LAB/MIDI)** mode, you can choose:

  * No conversion
  * Merge consonants
  * Hiragana
  * Katakana

* **Multiple Audio Formats**  
  `WAV / MP3 / FLAC / M4A / AAC / OGG`

* **Multilingual Text Alignment**

* **LAB File Export**

* **Multiple Alignment Algorithms**  
  Includes five alignment options: **Montreal Forced Aligner, WhisperX, Qwen3-ASR-1.7B, Qwen3-ForcedAligner-0.6B, and NeMo Forced Aligner**. Choose the method that best fits your needs.

* **A Simple, Easy-to-Use Web Interface**

* **Batch Project Generation**  
  Convert multiple LAB / MIDI / text + WAV inputs into project files in batches.

---

## 📌 Use Cases

- You already have singing voice synthesis or TTS audio and want to generate annotations quickly.
- You want to import a speech project into singing voice synthesis software for further editing.
- You want to batch-generate `.lab` files, F0 data, and project files.
- You want to create an editable intermediate layer between “speaking” and “singing.”

---

## 🛠️ How It Works

1. Provide an audio file and its text.
2. Use **[Qwen3-FA](https://github.com/QwenLM/Qwen3-ASR)** for automatic time alignment.
3. Generate a `.lab` annotation file.
4. Use **[PyWORLD](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)** to extract F0 pitch data.
5. Output a project file according to the selected processing mode.
6. Import the result into singing voice synthesis software for further use.

---

## 🚀 Quick Start

### Windows

```batch
setup.bat
run.bat
```

### Linux / Mac

```bash
chmod +x setup.sh run.sh
./setup.sh
./run.sh
```

After launching, open:

```text
http://127.0.0.1:5850
```

## 🔧 Processing Modes

### 1. Annotation Only (Fast)

Runs Qwen3-FA automatic annotation only and generates a `.lab` file.

### 2. Full Processing (Annotation + F0 + Project File)

Runs Qwen3-FA alignment and F0 extraction, then generates a project file.

### 3. Generate Project Only (WAV + LAB)

Generates a project file from existing WAV + LAB files without repeating the full annotation process.

### 📋 System Requirements

- Python 3.8+
- Node.js 16+
- 4 GB RAM
- 1 GB of available disk space

## 📖 Workflow

### Annotation Only (Fast)

1. Open the application.
2. Upload an audio file.
3. Enter the text.
4. Select the language.
5. Click **Start Annotation**.
6. Download the generated `.lab` file.

### Full Processing (Annotation + F0 + Project File)

1. Upload an audio file.
2. Enter the text.
3. Select the language.
4. Set the processing mode to **Full Processing**.
5. Click **Start Processing**.
6. Download the generated project file.

### Generate Project Only (WAV + LAB)

1. Prepare a WAV file and its corresponding LAB file.
2. Select **Generate Project Only**.
3. Upload the WAV + LAB files.
4. Click **Generate**.
5. Download the project file.

### 📝 Output

Depending on the processing mode, the application can output:

- `.lab` time-alignment files
- F0 pitch data
- Singing voice synthesis project files

### 📁 Directory Structure

```text
backend/                 Backend: four Flask processes (main service app.py :5850,
                         qwen3_server.py :5851, nemo_server.py :5852,
                         qwen3tts_server.py :5853). Each runs in its own virtual
                         environment and communicates over local HTTP to avoid
                         dependency conflicts.
frontend/                Frontend UI (Vue 3 + Element Plus + TypeScript)
launcher.py              Process orchestration, system tray, and native window
                         for packaged releases
setup.bat / run.bat       Windows environment setup / launch scripts
setup.sh / run.sh         Linux / Mac environment setup / launch scripts
Docs/ARCHITECTURE.md      Detailed technical architecture and code analysis report
```

> For a more detailed module-by-module directory guide, including what each file does and how the files interact, see
> [ARCHITECTURE.md](../ARCHITECTURE.md#3-目录结构详解).

## ⚠️ Disclaimer

This project is intended for:

- Automatic audio alignment (Qwen3-FA)
- Pitch extraction (PyWORLD / F0)
- Singing voice synthesis project-file generation

---

### 📌 Limitations

This tool is only an **SVS workflow assistant**. It does not:

- Perform singing voice synthesis (SVS)
- Train or modify voicebank models
- Replace any singing voice synthesis software

---

### 📁 User Responsibility

Users are responsible for the results generated by this tool, including `.lab` files, F0 data, and project files:

- Users are responsible for ensuring their use of the results is lawful and appropriate.
- Before use, confirm the licensing scope of the audio and voicebanks involved.
- If third-party voicebanks are involved, follow their official terms of use.

---

### 🎭 Voicebank and Attribution Notes

If a voicebank or synthesis system requires attribution, provide it according to the applicable terms. Whether credit for voice direction is required depends on whether the voice in the voicebank used in the generated project file is recognized as retaining characteristics of the original sample. If those characteristics are identifiable, provide attribution as required by the applicable terms. Examples:

- “Voicebank used: XXX”
- “Voice direction: XXX”

---

### 📌 Copyright

This project does not claim ownership of user-provided input data or generated content.

Copyright in the data belongs to the user, subject to whether the voice in the voicebank used after converting the original sample into a project file is recognized as retaining characteristics of that original sample. If a third-party original sample is identifiable in the result, the result may be considered a derivative work of that third party.

---

## 📝 License

The software portion of this project, including source code and other files that form part of the software, is licensed under the **MIT License**.

Unless otherwise stated, author-created non-software content in this project—such as original documentation, examples, explanatory text, and other non-software works—is dedicated to the public domain under **CC0 1.0 Universal**.

Third-party models, dictionaries, voicebanks, datasets, third-party software, and derivative files remain subject to their respective licenses or authorization terms. The MIT License and CC0 1.0 Universal used by this project do not change the original licensing terms of such third-party content.

**License scope:**

* Software and source code: MIT License
* Author-created non-software content: CC0 1.0 Universal
* Third-party content: governed by its own license or authorization terms
