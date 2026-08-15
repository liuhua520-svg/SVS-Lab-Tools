<template>
  <div class="about-container">
    <el-card class="about-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-title">ℹ️ {{ t('about.pageTitle') }}</span>
        </div>
      </template>

      <p class="page-subtitle">{{ t('about.pageSubtitle') }}</p>

      <!-- ============== 项目介绍 ============== -->
      <div class="section-heading">
        <span>📖 {{ t('about.introTitle') }}</span>
      </div>
      <p class="intro-text">{{ t('about.introBody') }}</p>

      <div class="link-row">
        <el-button tag="a" href="https://github.com/liuhua520-svg/SVS-Lab-Tools" target="_blank">
          📚 GitHub
        </el-button>
        <el-button tag="a" href="https://github.com/liuhua520-svg/SVS-Lab-Tools/issues" target="_blank">
          🐛 {{ t('about.reportIssue') }}
        </el-button>
      </div>

      <el-descriptions :column="1" border class="meta-descriptions">
        <el-descriptions-item :label="t('about.version')">{{ appVersion }}</el-descriptions-item>
        <el-descriptions-item :label="t('about.license')">MIT License</el-descriptions-item>
      </el-descriptions>

      <el-divider />

      <!-- ============== MIT 许可证 ============== -->
      <div class="section-heading">
        <span>📜 {{ t('about.licenseTitle') }}</span>
      </div>
      <p class="section-subtitle">{{ t('about.licenseSubtitle') }}</p>
      <pre class="license-block">{{ mitLicenseText }}</pre>

      <el-divider />

      <!-- ============== 鸣谢名单 ============== -->
      <div class="section-heading">
        <span>🙏 {{ t('about.acknowledgmentTitle') }}</span>
      </div>
      <p class="section-subtitle">{{ t('about.acknowledgmentSubtitle') }}</p>

      <el-collapse v-model="activeGroups" class="ack-collapse">
        <el-collapse-item
          v-for="group in ackGroups"
          :key="group.key"
          :name="group.key"
          :title="`${group.title} (${group.items.length})`"
        >
          <el-table :data="group.items" size="small" style="width: 100%">
            <el-table-column prop="name" :label="t('about.ackColName')" min-width="150" />
            <el-table-column prop="version" :label="t('about.ackColVersion')" width="140" />
            <el-table-column :label="t('about.ackColLink')" min-width="160">
              <template #default="{ row }">
                <a :href="row.link" target="_blank" rel="noopener" class="ack-link">{{ row.link }}</a>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="ack-footnote"
        :title="t('about.acknowledgmentFootnote')"
      />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useAppLocale } from '../i18n'

const { t } = useAppLocale()

const appVersion = '1.0.0'

const mitLicenseText = `MIT License

Copyright (c) 2026 liuhua520-svg (https://github.com/liuhua520-svg/SVS-Lab-Tools)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.`

// 鸣谢数据源自仓库根目录的 ACKNOWLEDGMENT.md / THIRD-PARTY-NOTICES.txt，
// 按「运行环境 -> 许可证类型」分组，与后端四个 requirements 文件
// （requirements.txt / requirements-qwen3.txt / requirements-nemo.txt /
// requirements-qwen3tts.txt）保持一一对应。更新依赖时请同步维护三处：
// 这里、仓库根目录 ACKNOWLEDGMENT.md、THIRD-PARTY-NOTICES.txt。
interface AckItem {
  name: string
  version: string
  link: string
}
interface AckGroup {
  key: string
  title: string
  items: AckItem[]
}

const ackGroups: AckGroup[] = [
  // ── 主后端 backend/requirements.txt ──────────────────────────────
  {
    key: 'main-mit',
    title: 'MIT License — 主后端',
    items: [
      { name: 'setuptools', version: '81.0.0', link: 'https://github.com/pypa/setuptools' },
      { name: 'flask-cors', version: '4.0.0', link: 'https://github.com/corydolphin/flask-cors' },
      { name: 'montreal-forced-aligner', version: '3.3.9', link: 'https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner' },
      { name: 'pgvector', version: '0.3.6', link: 'https://github.com/pgvector/pgvector-python' },
      { name: 'textgrid', version: '1.5', link: 'https://github.com/kylebgorman/textgrid' },
      { name: 'pypinyin', version: '0.53.0', link: 'https://github.com/mozillazg/python-pinyin' },
      { name: 'pycantonese', version: '5.0.0', link: 'https://github.com/pycantonese/pycantonese' },
      { name: 'jamo', version: '0.4.1', link: 'https://github.com/JDongian/python-jamo' },
      { name: 'spacy-pkuseg', version: '1.0.1', link: 'https://github.com/explosion/spacy-pkuseg' },
      { name: 'dragonmapper', version: '0.3.0', link: 'https://github.com/tsroten/dragonmapper' },
      { name: 'pyworld', version: '0.3.5', link: 'https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder' },
      { name: 'torchcrepe', version: '0.0.24', link: 'https://github.com/maxrmorrison/torchcrepe' },
      { name: 'ctranslate2', version: '4.4.0', link: 'https://github.com/OpenNMT/CTranslate2' },
      { name: 'ruamel.yaml', version: '0.19.1', link: 'https://pypi.org/project/ruamel.yaml/' },
      { name: 'mido', version: '1.3.3', link: 'https://github.com/mido/mido' },
    ],
  },
  {
    key: 'main-bsd3',
    title: 'BSD 3-Clause — 主后端',
    items: [
      { name: 'numpy', version: '1.26.4', link: 'https://github.com/numpy/numpy' },
      { name: 'torch', version: '2.3.1+cpu', link: 'https://github.com/pytorch/pytorch' },
      { name: 'flask', version: '2.3.3', link: 'https://github.com/pallets/flask' },
      { name: 'soundfile', version: '0.12.1', link: 'https://github.com/bastibe/python-soundfile' },
      { name: 'python-mecab-ko', version: '1.3.7', link: 'https://github.com/jonghwanhyeon/python-mecab-ko' },
    ],
  },
  {
    key: 'main-bsd2',
    title: 'BSD 2-Clause — 主后端',
    items: [
      { name: 'torchaudio', version: '2.3.1+cpu', link: 'https://github.com/pytorch/audio' },
      { name: 'whisperx', version: '3.2.0', link: 'https://github.com/m-bain/whisperX' },
    ],
  },
  {
    key: 'main-apache',
    title: 'Apache License 2.0 — 主后端',
    items: [
      { name: 'requests', version: '2.34.2', link: 'https://github.com/psf/requests' },
      { name: 'sudachipy', version: '0.6.8', link: 'https://github.com/WorksApplications/sudachi' },
      { name: 'sudachidict-core', version: '20240409', link: 'https://github.com/WorksApplications/SudachiDict' },
      { name: 'nltk', version: '3.10.2', link: 'https://github.com/nltk/nltk' },
      { name: 'g2p_en', version: '2.1.0', link: 'https://github.com/Kyubyong/g2p' },
      { name: 'hanziconv', version: '0.3.2', link: 'https://github.com/berniey/hanziconv' },
      { name: 'opencc-python-reimplemented', version: '0.1.7', link: 'https://pypi.org/project/opencc-python-reimplemented/' },
      { name: 'transformers', version: '4.39.3', link: 'https://github.com/huggingface/transformers' },
      { name: 'tokenizers', version: '0.15.2', link: 'https://github.com/huggingface/tokenizers' },
      { name: 'huggingface-hub', version: '0.36.2', link: 'https://github.com/huggingface/huggingface_hub' },
    ],
  },
  {
    key: 'main-lgpl21',
    title: 'LGPL 2.1 License — 主后端',
    items: [
      { name: 'num2words', version: '0.5.14', link: 'https://github.com/savoirfairelinux/num2words' },
    ],
  },
  {
    key: 'main-lgpl3',
    title: 'LGPL 3.0 License — 主后端',
    items: [
      { name: 'edge-tts', version: '7.2.8', link: 'https://github.com/rany2/edge-tts' },
    ],
  },
  {
    key: 'main-isc',
    title: 'ISC License — 主后端',
    items: [
      { name: 'librosa', version: '0.11.0', link: 'https://github.com/librosa/librosa' },
      { name: 'resampy', version: '0.4.3', link: 'https://github.com/bmcfee/resampy' },
    ],
  },
  {
    key: 'main-psf',
    title: 'PSF 2.0 License — 主后端',
    items: [
      { name: 'pywin32', version: '312 (Windows only)', link: 'https://github.com/mhammond/pywin32' },
    ],
  },
  {
    key: 'main-multi',
    title: '多重许可证 (MIT AND MPL-2.0) — 主后端',
    items: [
      { name: 'tqdm', version: '4.70.0', link: 'https://github.com/tqdm/tqdm' },
    ],
  },
  // ── Qwen3-ASR / Qwen3-ForcedAligner backend/requirements-qwen3.txt ──
  {
    key: 'qwen3-apache',
    title: 'Apache License 2.0 — Qwen3-ASR/ForcedAligner',
    items: [
      { name: 'qwen-asr', version: '0.0.6', link: 'https://github.com/QwenLM/Qwen3-ASR' },
      { name: 'accelerate', version: '1.12.0', link: 'https://github.com/huggingface/accelerate' },
    ],
  },
  // ── NVIDIA NeMo 强制对齐 backend/requirements-nemo.txt ──────────────
  {
    key: 'nemo-apache',
    title: 'Apache License 2.0 — NVIDIA NeMo',
    items: [
      { name: 'nemo_toolkit[asr]', version: '2.7.3', link: 'https://github.com/NVIDIA-NeMo/Speech' },
    ],
  },
  // ── Qwen3-TTS backend/requirements-qwen3tts.txt ─────────────────────
  {
    key: 'qwen3tts-bsd3',
    title: 'BSD 3-Clause — Qwen3-TTS',
    items: [
      { name: 'torchvision', version: '0.18.1+cpu', link: 'https://github.com/pytorch/vision' },
    ],
  },
  {
    key: 'qwen3tts-apache',
    title: 'Apache License 2.0 — Qwen3-TTS',
    items: [
      { name: 'qwen-tts', version: '0.1.1', link: 'https://github.com/QwenLM/Qwen3-TTS' },
    ],
  },
  // ── 前端 frontend/package.json ───────────────────────────────────
  {
    key: 'mit-frontend',
    title: 'MIT License — Frontend',
    items: [
      { name: 'vue', version: '^3.3.4', link: 'https://github.com/vuejs/core' },
      { name: 'element-plus', version: '^2.4.1', link: 'https://github.com/element-plus/element-plus' },
      { name: '@element-plus/icons-vue', version: '^2.1.0', link: 'https://github.com/element-plus/element-plus' },
      { name: 'axios', version: '^1.5.0', link: 'https://github.com/axios/axios' },
      { name: 'vue-i18n', version: '^11.4.6', link: 'https://github.com/intlify/vue-i18n' },
      { name: '@vitejs/plugin-vue', version: '^4.3.4', link: 'https://github.com/vitejs/vite-plugin-vue' },
      { name: '@vue/tsconfig', version: '^0.4.0', link: 'https://github.com/vuejs/tsconfig' },
      { name: 'vite', version: '^4.4.9', link: 'https://github.com/vitejs/vite' },
      { name: 'vue-tsc', version: '^1.8.13', link: 'https://github.com/vuejs/language-tools' },
    ],
  },
  {
    key: 'bsd2-frontend',
    title: 'BSD 2-Clause — Frontend',
    items: [
      { name: 'terser', version: '^5.29.1', link: 'https://github.com/terser/terser' },
    ],
  },
  {
    key: 'apache-frontend',
    title: 'Apache License 2.0 — Frontend',
    items: [
      { name: 'typescript', version: '^5.1.6', link: 'https://github.com/microsoft/TypeScript' },
    ],
  },
]

// 默认展开第一组，其余折叠，避免长列表默认铺满整页
const activeGroups = ref<string[]>(['main-mit'])
</script>

<style scoped>
.about-container {
  width: 100%;
}

.about-card {
  background: white;
  border-radius: 8px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}

.card-title {
  font-size: 16px;
  font-weight: bold;
  color: #333;
}

.page-subtitle {
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
  margin: 4px 0 20px;
}

.section-heading {
  font-size: 15px;
  font-weight: bold;
  color: #333;
  margin: 4px 0 8px;
}

.section-subtitle {
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
  margin: 0 0 14px;
}

.intro-text {
  color: #606266;
  font-size: 13px;
  line-height: 1.8;
  margin: 0 0 16px;
  white-space: pre-line;
}

.link-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}

.meta-descriptions {
  max-width: 480px;
}

.license-block {
  background: #f6f8fa;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 16px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12px;
  line-height: 1.7;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 360px;
  overflow-y: auto;
}

.ack-collapse {
  border-top: none;
}

.ack-link {
  color: #4f46e5;
  text-decoration: none;
  word-break: break-all;
}

.ack-link:hover {
  text-decoration: underline;
}

.ack-footnote {
  margin-top: 16px;
}

@media (max-width: 768px) {
  .meta-descriptions {
    max-width: 100%;
  }
}
</style>
