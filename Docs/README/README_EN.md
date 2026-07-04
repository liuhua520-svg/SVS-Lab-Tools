# SVS Lab Aligner

**Singing Voice Synthesis Lab Aligner**

A completely standalone web application for Singing Voice Synthesis (SVS) workflows, used to convert input audio + text into project files usable in singing voice synthesis software.

Its goal isn't to create TTS (Text-to-Speech) or replace the vocal synthesis engine itself, but rather to act as a bridge, helping singers "pretend to be good at speaking":

- First, use **[MFA](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner)** to automatically align audio and text, generating a `.lab` file.

- Then, use **[PyWORLD](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)** to extract the F0 pitch.

- Finally, output the vocal synthesis project file for further editing and playback within the software.

---

## Language

[**简体中文**](../../README.md) | [**繁體中文**](./README_ZH_TW.md) | [**English**](./README_EN.md) | [**日本語**](./README_JP.md) | [**한국어**](./README_KR.md) 

---

## ✨ Main Functions

* **Annotation Only (Fast)**
Performs only MFA automatic alignment, quickly generating `.lab` files.

* **Complete Processing (Annotation + F0 + Project File)**
Automatically completes annotation, pitch extraction, and outputs a vocal synthesis project file.

* **Project Generation Only (WAV + LAB)**
Directly generates project files based on existing `WAV + LAB` files, eliminating the need for re-annotation.

* **Supports Multiple Pitch Extraction Algorithms**
Built-in **PyWORLD, CREPE, RMVPE** three F0 extraction methods, allowing selection of different algorithms based on specific needs.

* **Supports Continuous Pitch Writing**
Allows the option to generate only pure notes or write continuous F0 curves into the project, preserving more realistic pitch variations.

* **Supports Long Audio Processing**
Capable of processing long audio files, suitable for complete songs, long speech sequences, and other scenarios.

* **Supports multiple text conversion methods**
In "Generate Project Only (WAV + LAB)" mode, you can choose:

* No conversion

* Merge consonants

* Hiragana

* Katakana

* **Supports multiple audio formats**
`WAV / MP3 / FLAC / M4A / AAC`

* **Supports multilingual text alignment**

* **Supports LAB file export**

* **Supports Multiple Alignment Algorithms**
It includes four built-in alignment methods: MFA, WhisperX, Qwen3-ASR, and Qwen3-FA, allowing users to choose the appropriate algorithm based on their specific needs.

* **Provides a simple and easy-to-use web interface**

---

## 📌 Applicable Scenarios

- You already have vocal synthesis or TTS audio and want to quickly generate annotations.

- You want to import your speech project into vocal synthesis software for further editing.

- You want to batch generate `.lab`, F0, and project files.

- You want to create an editable intermediate layer between "speaking" and "singing".

---

## 🛠️ How It Works

1. Input audio file and text.

2. Use **[MFA](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner)** for automatic time alignment.

3. Generate `.lab` files. Annotation Files

4. Extract F0 pitch using **[PyWORLD](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder)**

5. Output project file according to processing mode

6. Import the result into vocal synthesis software for further use

---

## 🚀 Quick Start

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

Access after startup:

```
http://localhost:5000

```
## 🔧 Processing Modes

### 1. Annotation Only (Quick)

Performs only automatic MFA annotation, generating .lab files.

### 2. Complete Processing (Annotations + F0 + Project File)

Perform MFA alignment, F0 extraction, and generate the project file.

### 3. Generate Project Only (WAV + LAB)

Use existing WAV + LAB files to generate the project file; the complete annotation process is not repeated.

### 📋 System Requirements

Python 3.8+

Node.js 16+

4GB RAM

1GB Available Disk Space

### 🔧 Install MFA

```
pip install montreal-forced-aligner

```
# Download Language Models

```
mfa model download acoustic cmn # Chinese
mfa model download acoustic eng # English
mfa model download acoustic jpn # Japanese
mfa model download acoustic kor # Korean
mfa model download acoustic yue # Cantonese

```

## 📖 Usage Flow

### Annotation Only (Quick)

1. Open this application

2. Upload audio file

3. Enter text

4. Select language

5. Click Start Annotation

6. Download the generated .lab file

### Complete Processing (Annotation + F0 + Project Files)

1. Upload audio file

2. 1. Input Text

2. Select Language

3. Select Processing Mode as "Full Processing"

4. Click Start Processing

5. Download the Generated Project File

### Generate Project Only (WAV + LAB)

1. Prepare WAV and the corresponding LAB file

2. Select "Generate Project Only"

3. Upload WAV + LAB

4. Click Generate

5. Download Project File

### 📝 Output Content
Depending on the processing mode, the application can output:

- .lab Time-aligned file

- F0 Pitch Information

- Vocal Synthesis Project File

### 📁 Directory Structure

```
backend/    Backend services
frontend/   Front-end interface
setup.bat   Windows environment installation script
run.bat     Windows startup scripts
setup.sh    Linux / Mac environment installation script
run.sh      Linux / Mac startup scripts

```

## ⚠️ Disclaimer

This project is intended for the following purposes:

- Automatic Audio Alignment (MFA)

- Pitch Extraction (PyWORLD / F0)

- Generation of Vocal Synthesis Project Files

---

### 📌 Functional Limitations

This tool is only intended as an "SVS Workflow Auxiliary Tool" and does not have the following capabilities:

- It does not perform speech synthesis (TTS / SVS)

- It does not train or modify voicebank models

- It does not replace any vocal synthesis software

---

### 📁 Responsibility for Use

The results generated using this tool (including `.lab`, F0 data, and project files):

- The user is solely responsible for the legality and intended use of the results.

- Please confirm the license scope of the audio and voicebank used before use.

- If third-party voicebanks are involved, please comply with their official terms of use.

---

### 🎭 Voicebank and Annotation Notes

If the voicebank or synthesis system requires attribution, please annotate according to the corresponding license (whether to indicate acting guidance depends on whether the sound in the converted voicebank file is identified as having the characteristics of the original sample; if it is, please annotate according to the corresponding license). For example:

- "Voicebank used: XXX"

- "Acting guidance: XXX"

---

### 📌 Copyright Notice

This project does not claim ownership of user-input data or generated content.

Data copyright belongs to the user, depending on whether the original sample used (referring to the sound in the converted voicebank file) is identified as having the characteristics of the original sample. If a third-party original sample is identified as having the characteristics of the original sample, it may be considered a derivative work of that third party.

---

## 📝 License
MIT License

Including files that are not "software" (only the original parts are valid).
