<template>
  <div class="subtitle-container">
    <el-card class="subtitle-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-title">🎬 {{ t('subtitle.pageTitle') }}</span>
        </div>
      </template>

      <p class="page-subtitle">{{ t('subtitle.pageSubtitle') }}</p>

      <!-- ============== 依赖状态检查 ============== -->
      <el-alert
        v-if="!statusLoading && !statusInfo.ready"
        type="warning"
        show-icon
        :closable="false"
        class="status-alert"
      >
        <template #title>
          <div v-if="!statusInfo.ffmpeg.available">⚠️ {{ statusInfo.ffmpeg.message || t('subtitle.statusFfmpegMissing') }}</div>
          <div v-if="!statusInfo.qwen3_asr.available">⚠️ {{ statusInfo.qwen3_asr.message || t('subtitle.statusQwenMissing') }}</div>
        </template>
        <el-button size="small" text @click="checkStatus" :loading="statusLoading">
          🔄 {{ t('subtitle.statusRecheck') }}
        </el-button>
      </el-alert>
      <el-alert
        v-else-if="!statusLoading && statusInfo.ready"
        type="success"
        show-icon
        :closable="false"
        class="status-alert"
      >
        <template #title>✓ {{ t('subtitle.statusReady') }}</template>
      </el-alert>
      <el-alert v-else type="info" show-icon :closable="false" class="status-alert">
        <template #title>{{ t('subtitle.statusChecking') }}</template>
      </el-alert>

      <!-- ============== 上传区 ============== -->
      <div class="section-block">
        <div class="section-heading">📁 {{ t('subtitle.uploadTitle') }}</div>

        <el-upload
          v-if="!mediaInfo"
          drag
          action="#"
          :auto-upload="false"
          :limit="1"
          :disabled="uploading"
          :on-change="handleFileSelect"
          accept="video/*,audio/*,.mp4,.mkv,.mov,.avi,.webm,.flv,.wmv,.ts,.m4v,.wav,.mp3,.flac,.m4a,.aac,.ogg,.wma,.opus"
          class="media-upload"
        >
          <el-icon class="upload-icon"><UploadFilled /></el-icon>
          <div class="el-upload__text">{{ t('subtitle.uploadHint') }}</div>
        </el-upload>

        <div v-if="uploading" class="upload-progress">
          <el-progress :percentage="100" :indeterminate="true" :duration="1.5" />
          <span>{{ t('subtitle.uploading') }}</span>
        </div>

        <div v-if="mediaInfo" class="media-info-card">
          <div class="media-info-row">
            <span class="media-icon">{{ mediaInfo.is_video ? '🎞️' : '🎵' }}</span>
            <span class="media-name" :title="mediaInfo.filename">{{ mediaInfo.filename }}</span>
            <el-tag size="small" :type="mediaInfo.is_video ? 'primary' : 'success'">
              {{ mediaInfo.is_video ? t('subtitle.fileTypeVideo') : t('subtitle.fileTypeAudio') }}
            </el-tag>
            <span v-if="mediaInfo.duration" class="media-duration">
              {{ t('subtitle.fileDuration') }}: {{ formatDuration(mediaInfo.duration) }}
            </span>
          </div>
          <el-button size="small" :disabled="recognizing" @click="resetMedia">
            🔁 {{ t('subtitle.uploadReplace') }}
          </el-button>
        </div>
      </div>

      <!-- ============== 识别设置 ============== -->
      <div v-if="mediaInfo" class="section-block">
        <div class="section-heading">⚙️ {{ t('subtitle.settingsTitle') }}</div>

        <el-form label-width="140px" class="settings-form">
          <el-form-item :label="t('subtitle.language')">
            <el-select v-model="recognizeSettings.language" style="width: 240px">
              <el-option :label="t('subtitle.languageAuto')" value="auto" />
              <el-option
                v-for="opt in LANGUAGE_OPTIONS"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
          </el-form-item>

          <el-form-item :label="t('subtitle.device')">
            <el-radio-group v-model="recognizeSettings.device">
              <el-radio value="auto">{{ t('subtitle.deviceAuto') }}</el-radio>
              <el-radio value="cpu">{{ t('subtitle.deviceCpu') }}</el-radio>
              <el-radio value="cuda">{{ t('subtitle.deviceCuda') }}</el-radio>
            </el-radio-group>
          </el-form-item>

          <el-form-item :label="t('subtitle.batchSize')">
            <el-input-number v-model="recognizeSettings.batchSize" :min="1" :max="64" :step="1" />
            <el-tooltip :content="t('subtitle.batchSizeHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>

          <el-form-item :label="t('subtitle.maxChars')">
            <el-input-number v-model="recognizeSettings.maxChars" :min="8" :max="500" :step="2" />
            <el-tooltip :content="t('subtitle.maxCharsHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>

          <el-form-item :label="t('subtitle.splitAtSentenceEnd')">
            <el-switch v-model="recognizeSettings.splitAtSentenceEnd" />
            <el-tooltip :content="t('subtitle.splitAtSentenceEndHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>

          <el-form-item v-if="recognizeSettings.splitAtSentenceEnd" :label="t('subtitle.allowCommaSplit')">
            <el-switch v-model="recognizeSettings.allowCommaSplit" />
            <el-tooltip :content="t('subtitle.allowCommaSplitHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>

          <el-form-item :label="t('subtitle.removePunctuation')">
            <el-switch v-model="recognizeSettings.removePunctuation" />
            <el-tooltip :content="t('subtitle.removePunctuationHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>

          <el-form-item :label="t('subtitle.closeVadGaps')">
            <el-switch v-model="recognizeSettings.closeVadGaps" />
            <el-tooltip :content="t('subtitle.closeVadGapsHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>

          <el-form-item v-if="recognizeSettings.closeVadGaps" :label="t('subtitle.vadGapThreshold')">
            <el-input-number
              v-model="recognizeSettings.vadGapThresholdSec"
              :min="0.05"
              :max="5"
              :step="0.1"
              :precision="2"
            />
            <el-tooltip :content="t('subtitle.vadGapThresholdHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>
        </el-form>

        <el-button
          type="primary"
          size="large"
          :loading="recognizing"
          :disabled="!statusInfo.ready"
          @click="startRecognize"
        >
          {{ recognizing ? t('subtitle.recognizing') : `▶️ ${t('subtitle.startRecognize')}` }}
        </el-button>

        <div v-if="recognizing" class="recognize-progress">
          <el-progress :percentage="recognizeProgressPercent" :status="recognizeProgressPercent >= 100 ? 'success' : undefined" />
          <span class="progress-label">{{ recognizeStageLabel }}</span>
        </div>

        <el-alert v-if="recognizeError" type="error" show-icon :closable="true" @close="recognizeError = ''" class="status-alert">
          <template #title>{{ recognizeError }}</template>
        </el-alert>
      </div>

      <!-- ============== 预览播放器 + 字幕列表 ============== -->
      <div v-if="entries.length || recognizing" class="section-block">
        <div class="section-heading">🖥️ {{ t('subtitle.playerTitle') }}</div>

        <div class="player-layout">
          <div class="player-wrap">
            <video
              v-if="mediaInfo && mediaInfo.is_video"
              ref="videoRef"
              :src="mediaInfo.play_url"
              controls
              class="media-player"
              @timeupdate="onTimeUpdate"
            />
            <audio
              v-else-if="mediaInfo"
              ref="audioRef"
              :src="mediaInfo.play_url"
              controls
              class="media-player audio-player"
              @timeupdate="onTimeUpdate"
            />
            <div v-if="currentEntry" class="subtitle-overlay">{{ currentEntry.text }}</div>
          </div>
        </div>

        <div class="section-heading subtitle-list-heading">
          <span>📝 {{ t('subtitle.subtitleListTitle') }}</span>
          <div class="list-actions">
            <el-button size="small" type="danger" plain :disabled="!entries.length" @click="clearAll">
              🗑️ {{ t('subtitle.clearAll') }}
            </el-button>
          </div>
        </div>

        <p v-if="!entries.length" class="empty-hint">{{ t('subtitle.subtitleListEmpty') }}</p>

        <el-table v-else :data="entries" size="small" max-height="420" class="subtitle-table" row-key="_uid">
          <el-table-column :label="t('subtitle.columnIndex')" width="50">
            <template #default="{ $index }">{{ $index + 1 }}</template>
          </el-table-column>
          <el-table-column :label="t('subtitle.columnStart')" width="130">
            <template #default="{ row }">
              <el-input
                v-model="row._startText"
                size="small"
                @change="onTimeEdit(row, 'start')"
              />
            </template>
          </el-table-column>
          <el-table-column :label="t('subtitle.columnEnd')" width="130">
            <template #default="{ row }">
              <el-input
                v-model="row._endText"
                size="small"
                @change="onTimeEdit(row, 'end')"
              />
            </template>
          </el-table-column>
          <el-table-column :label="t('subtitle.columnText')">
            <template #default="{ row }">
              <el-input
                v-model="row.text"
                size="small"
                type="textarea"
                :autosize="{ minRows: 1, maxRows: 3 }"
              />
            </template>
          </el-table-column>
          <el-table-column :label="t('subtitle.columnAction')" width="230">
            <template #default="{ row, $index }">
              <el-tooltip :content="t('subtitle.jumpToTime')" placement="top">
                <el-button size="small" circle @click="jumpToEntry(row)">▶</el-button>
              </el-tooltip>
              <el-tooltip :content="t('subtitle.splitEntry')" placement="top">
                <el-button size="small" circle :loading="row._splitting" @click="splitEntry($index)">✂️</el-button>
              </el-tooltip>
              <el-tooltip :content="t('subtitle.addAfter')" placement="top">
                <el-button size="small" circle @click="insertAfter($index)">➕</el-button>
              </el-tooltip>
              <el-tooltip v-if="$index < entries.length - 1" :content="t('subtitle.mergeNext')" placement="top">
                <el-button size="small" circle @click="mergeWithNext($index)">🔗</el-button>
              </el-tooltip>
              <el-tooltip :content="t('subtitle.deleteEntry')" placement="top">
                <el-button size="small" circle type="danger" @click="deleteEntry($index)">🗑️</el-button>
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- ============== 导出 ============== -->
      <div v-if="entries.length" class="section-block">
        <div class="section-heading">📤 {{ t('subtitle.exportTitle') }}</div>
        <div class="export-buttons">
          <el-button @click="exportSubtitle('srt')">📥 {{ t('subtitle.exportSrt') }}</el-button>
          <el-button @click="exportSubtitle('lrc')">📥 {{ t('subtitle.exportLrc') }}</el-button>
          <el-button @click="exportSubtitle('txt')">📥 {{ t('subtitle.exportTxt') }}</el-button>
          <el-tooltip v-if="mediaInfo && !mediaInfo.is_video" :content="t('subtitle.embedAudioHint')" placement="top">
            <el-button type="primary" :loading="embedding === 'soft'" @click="embedSubtitleIntoMedia('soft')">
              🎵 {{ t('subtitle.embedIntoAudio') }}
            </el-button>
          </el-tooltip>
          <el-button v-else type="primary" :loading="embedding === 'soft'" @click="embedSubtitleIntoMedia('soft')">
            🎬 {{ t('subtitle.embedIntoVideo') }}
          </el-button>
          <el-tooltip v-if="mediaInfo && !mediaInfo.is_video" :content="t('subtitle.embedVideoHint')" placement="top">
            <el-button type="success" :loading="embedding === 'burn'" @click="embedSubtitleIntoMedia('burn')">
              🔥 {{ t('subtitle.embedBurnVideo') }}
            </el-button>
          </el-tooltip>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { useAppLocale } from '../i18n'

const { t } = useAppLocale()

// ─────────────────────────────────────────────────────────────────
// 语言选项：与 qwen3_server / subtitle_processor.py 里的语言代码保持一致
// ─────────────────────────────────────────────────────────────────
const LANGUAGE_OPTIONS = [
  { value: 'zh', label: '中文 (Chinese)' },
  { value: 'yue', label: '粤语 (Cantonese)' },
  { value: 'en', label: 'English' },
  { value: 'ja', label: '日本語 (Japanese)' },
  { value: 'ko', label: '한국어 (Korean)' },
  { value: 'ar', label: 'العربية (Arabic)' },
  { value: 'de', label: 'Deutsch (German)' },
  { value: 'fr', label: 'Français (French)' },
  { value: 'es', label: 'Español (Spanish)' },
  { value: 'pt', label: 'Português (Portuguese)' },
  { value: 'id', label: 'Indonesia (Indonesian)' },
  { value: 'it', label: 'Italiano (Italian)' },
  { value: 'ru', label: 'Русский (Russian)' },
  { value: 'th', label: 'ไทย (Thai)' },
  { value: 'vi', label: 'Tiếng Việt (Vietnamese)' },
  { value: 'tr', label: 'Türkçe (Turkish)' },
  { value: 'hi', label: 'हिन्दी (Hindi)' },
  { value: 'ms', label: 'Melayu (Malay)' },
  { value: 'nl', label: 'Nederlands (Dutch)' },
  { value: 'sv', label: 'Svenska (Swedish)' },
  { value: 'da', label: 'Dansk (Danish)' },
  { value: 'fi', label: 'Suomi (Finnish)' },
  { value: 'pl', label: 'Polski (Polish)' },
  { value: 'cs', label: 'Čeština (Czech)' },
  { value: 'fil', label: 'Filipino' },
  { value: 'fa', label: 'فارسی (Persian)' },
  { value: 'el', label: 'Ελληνικά (Greek)' },
  { value: 'hu', label: 'Magyar (Hungarian)' },
  { value: 'mk', label: 'Македонски (Macedonian)' },
  { value: 'ro', label: 'Română (Romanian)' },
]

// ─────────────────────────────────────────────────────────────────
// 依赖状态检查（ffmpeg + Qwen3-ASR 独立服务）
// ─────────────────────────────────────────────────────────────────
interface DepStatus { available: boolean; message: string }
const statusLoading = ref(true)
const statusInfo = reactive<{ ffmpeg: DepStatus; qwen3_asr: DepStatus; ready: boolean }>({
  ffmpeg: { available: false, message: '' },
  qwen3_asr: { available: false, message: '' },
  ready: false,
})

const checkStatus = async () => {
  statusLoading.value = true
  try {
    const res = await fetch('/api/subtitle/status')
    const data = await res.json()
    if (data.success) {
      statusInfo.ffmpeg = data.ffmpeg
      statusInfo.qwen3_asr = data.qwen3_asr
      statusInfo.ready = data.ready
    }
  } catch (e) {
    // 静默失败：保持"未就绪"提示状态，避免掩盖真实问题
  } finally {
    statusLoading.value = false
  }
}
checkStatus()

// ─────────────────────────────────────────────────────────────────
// 媒体上传
// ─────────────────────────────────────────────────────────────────
interface MediaInfo {
  media_id: string
  filename: string
  is_video: boolean
  duration: number | null
  play_url: string
}

const uploading = ref(false)
const mediaInfo = ref<MediaInfo | null>(null)

const handleFileSelect = async (file: any) => {
  const raw: File | null = file?.raw || null
  if (!raw) return

  if (mediaInfo.value) {
    try {
      await ElMessageBox.confirm(t('subtitle.reuploadWarning'), '', { type: 'warning' })
    } catch {
      return
    }
  }

  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', raw)
    const res = await fetch('/api/subtitle/upload', { method: 'POST', body: fd })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitle.uploadFailed'))

    // 新媒体上传成功后，清空旧的识别结果，避免时间轴与新媒体错位
    entries.value = []
    mediaInfo.value = {
      media_id: data.media_id,
      filename: data.filename,
      is_video: data.is_video,
      duration: data.duration,
      play_url: data.play_url,
    }
    ElMessage.success(`✅ ${t('subtitle.uploadSuccess')}`)
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  } finally {
    uploading.value = false
  }
}

const resetMedia = async () => {
  if (mediaInfo.value) {
    try {
      await fetch('/api/subtitle/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ media_id: mediaInfo.value.media_id }),
      })
    } catch {
      // 清理失败不影响前端状态重置
    }
  }
  mediaInfo.value = null
  entries.value = []
  recognizeError.value = ''
}

// ─────────────────────────────────────────────────────────────────
// 识别设置 + 启动识别 + 进度轮询
// ─────────────────────────────────────────────────────────────────
const recognizeSettings = reactive({
  language: 'auto',
  device: 'auto',
  batchSize: 8,
  maxChars: 250,
  splitAtSentenceEnd: false,
  allowCommaSplit: false,
  removePunctuation: false,
  closeVadGaps: false,
  vadGapThresholdSec: 0.1,
})

// "允许按句末切分"关闭时，"允许逗号切分"没有意义（逗号切分是在句末切分
// 基础上的进一步细分），跟随强制关闭，避免出现"句末切分已关闭，但
// 逗号切分仍勾选"这种界面上看不到、但仍会生效的矛盾状态。
watch(
  () => recognizeSettings.splitAtSentenceEnd,
  (enabled) => {
    if (!enabled && recognizeSettings.allowCommaSplit) {
      recognizeSettings.allowCommaSplit = false
    }
  },
)

const recognizing = ref(false)
const recognizeError = ref('')
const recognizeProgress = reactive({ done: 0, total: 0, stage: 'extract' as 'extract' | 'recognize' })
let jobPollTimer: number | null = null

const recognizeProgressPercent = computed(() => {
  if (recognizeProgress.stage === 'extract') return 5
  if (!recognizeProgress.total) return 10
  return Math.min(100, Math.round((recognizeProgress.done / recognizeProgress.total) * 100))
})

const recognizeStageLabel = computed(() => {
  if (recognizeProgress.stage === 'extract') return t('subtitle.recognizeStageExtract')
  return t('subtitle.recognizeStageRecognize', { done: recognizeProgress.done, total: recognizeProgress.total })
})

const clearJobPolling = () => {
  if (jobPollTimer !== null) {
    window.clearTimeout(jobPollTimer)
    jobPollTimer = null
  }
}

const startRecognize = async () => {
  if (!mediaInfo.value) {
    ElMessage.warning(t('subtitle.needUploadFirst'))
    return
  }
  if (!statusInfo.ready) {
    ElMessage.warning(t('subtitle.needReadyFirst'))
    return
  }

  recognizeError.value = ''
  entries.value = []
  recognizing.value = true
  recognizeProgress.done = 0
  recognizeProgress.total = 0
  recognizeProgress.stage = 'extract'

  try {
    const res = await fetch('/api/subtitle/recognize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        media_id: mediaInfo.value.media_id,
        language: recognizeSettings.language,
        device: recognizeSettings.device,
        batch_size: recognizeSettings.batchSize,
        max_chars: recognizeSettings.maxChars,
        split_at_sentence_end: recognizeSettings.splitAtSentenceEnd,
        allow_comma_split: recognizeSettings.allowCommaSplit,
        remove_punctuation: recognizeSettings.removePunctuation,
        close_vad_gaps: recognizeSettings.closeVadGaps,
        vad_gap_threshold_sec: recognizeSettings.vadGapThresholdSec,
      }),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitle.recognizeFailed'))

    await pollRecognizeJob(data.job_id)
  } catch (e: any) {
    recognizeError.value = e?.message || String(e)
    ElMessage.error(`❌ ${recognizeError.value}`)
  } finally {
    recognizing.value = false
    clearJobPolling()
  }
}

const pollRecognizeJob = (jobId: string): Promise<void> => {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const res = await fetch(`/api/subtitle/job/${jobId}`)
        const data = await res.json()
        if (!res.ok || !data.success) throw new Error(data.error || t('subtitle.recognizeFailed'))

        const job = data.job || {}
        if (job.progress) {
          recognizeProgress.done = job.progress.done ?? recognizeProgress.done
          recognizeProgress.total = job.progress.total ?? recognizeProgress.total
          recognizeProgress.stage = job.progress.stage ?? recognizeProgress.stage
        }

        if (job.status === 'done') {
          const result = job.result
          const rawEntries = (result?.entries || []) as Array<{ start: number; end: number; text: string }>
          entries.value = rawEntries.map(toEditableEntry)
          if (!entries.value.length) {
            ElMessage.warning(t('subtitle.recognizeEmptyResult'))
          } else {
            ElMessage.success(t('subtitle.recognizeSuccess', { count: entries.value.length }))
          }
          resolve()
          return
        }

        if (job.status === 'failed') {
          reject(new Error(job.error || t('subtitle.recognizeFailed')))
          return
        }

        jobPollTimer = window.setTimeout(tick, 1200)
      } catch (e) {
        reject(e)
      }
    }
    tick()
  })
}

// ─────────────────────────────────────────────────────────────────
// 字幕条目：编辑态数据结构（额外维护可编辑的时间文本 + 唯一 key）
// ─────────────────────────────────────────────────────────────────
interface SubtitleEntry {
  _uid: number
  start: number
  end: number
  text: string
  _startText: string
  _endText: string
  _splitting?: boolean
}

let uidCounter = 0
const nextUid = () => ++uidCounter

const formatTimeInput = (sec: number): string => {
  // 先把总毫秒数四舍五入到整数，再统一从毫秒往上进位拆分时/分/秒/毫秒，
  // 避免"秒的小数部分单独四舍五入到 1000ms"时不进位到秒的问题
  // （例如 1.9996 秒之前会被格式化成非法的 00:00:01.1000，而不是 00:00:02.000）。
  let totalMs = Math.round(Math.max(0, sec) * 1000)
  const ms = totalMs % 1000
  totalMs = Math.floor(totalMs / 1000)
  const ss = totalMs % 60
  totalMs = Math.floor(totalMs / 60)
  const m = totalMs % 60
  const h = Math.floor(totalMs / 60)
  const pad = (n: number, len = 2) => String(n).padStart(len, '0')
  return `${pad(h)}:${pad(m)}:${pad(ss)}.${pad(ms, 3)}`
}

const parseTimeInput = (text: string): number | null => {
  const m = text.trim().match(/^(\d+):(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?$/)
  if (!m) return null
  const [, hh, mm, ss, ms] = m
  const total = Number(hh) * 3600 + Number(mm) * 60 + Number(ss) + Number((ms || '0').padEnd(3, '0')) / 1000
  return Number.isFinite(total) ? total : null
}

const toEditableEntry = (e: { start: number; end: number; text: string }): SubtitleEntry => ({
  _uid: nextUid(),
  start: e.start,
  end: e.end,
  text: e.text,
  _startText: formatTimeInput(e.start),
  _endText: formatTimeInput(e.end),
})

const entries = ref<SubtitleEntry[]>([])

const onTimeEdit = (row: SubtitleEntry, field: 'start' | 'end') => {
  const raw = field === 'start' ? row._startText : row._endText
  const parsed = parseTimeInput(raw)
  if (parsed === null) {
    ElMessage.error(t('subtitle.invalidTimeFormat'))
    // 恢复为原值的格式化文本，避免残留非法输入
    if (field === 'start') row._startText = formatTimeInput(row.start)
    else row._endText = formatTimeInput(row.end)
    return
  }
  if (field === 'start') {
    if (parsed >= row.end) {
      ElMessage.error(t('subtitle.timeOverlapWarning'))
      row._startText = formatTimeInput(row.start)
      return
    }
    row.start = parsed
  } else {
    if (parsed <= row.start) {
      ElMessage.error(t('subtitle.timeOverlapWarning'))
      row._endText = formatTimeInput(row.end)
      return
    }
    row.end = parsed
  }
}

const insertAfter = (index: number) => {
  const cur = entries.value[index]
  const next = entries.value[index + 1]
  const start = cur.end
  const end = next ? Math.min(next.start, cur.end + 2) : cur.end + 2
  entries.value.splice(index + 1, 0, toEditableEntry({ start, end: Math.max(end, start + 0.3), text: '' }))
}

const deleteEntry = async (index: number) => {
  try {
    await ElMessageBox.confirm(t('subtitle.deleteConfirm'), '', { type: 'warning' })
  } catch {
    return
  }
  entries.value.splice(index, 1)
}

const mergeWithNext = (index: number) => {
  const cur = entries.value[index]
  const next = entries.value[index + 1]
  if (!next) return
  cur.end = next.end
  cur.text = `${cur.text}${next.text}`
  cur._endText = formatTimeInput(cur.end)
  entries.value.splice(index + 1, 1)
}

// 手动"拆分"某一行字幕为两行：交给后端按标点/文本长度比例算出拆分点
// （这一行此时可能是识别结果，也可能已被用户编辑/合并过，早就没有
// 逐字时间戳了，所以拆分点只能靠文本本身重新估算，与 /api/subtitle/export
// 一样是无状态接口，不依赖 job）。
const splitEntry = async (index: number) => {
  const cur = entries.value[index]
  if (!cur || cur._splitting) return
  if (cur.end - cur.start < 0.05) {
    ElMessage.warning(t('subtitle.splitTooShort'))
    return
  }

  cur._splitting = true
  try {
    const res = await fetch('/api/subtitle/split_entry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start: cur.start, end: cur.end, text: cur.text }),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitle.splitFailed'))

    const left = toEditableEntry(data.left)
    const right = toEditableEntry(data.right)
    entries.value.splice(index, 1, left, right)
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  } finally {
    // cur 对应的行可能已经被 splice 替换掉了，这里的 cur._splitting 只是
    // 让原引用在 splice 之前那一刻的 loading 状态能正确复位，不影响新行
    cur._splitting = false
  }
}

const clearAll = async () => {
  try {
    await ElMessageBox.confirm(t('subtitle.clearAllConfirm'), '', { type: 'warning' })
  } catch {
    return
  }
  entries.value = []
  if (mediaInfo.value) {
    try {
      await fetch('/api/subtitle/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ media_id: mediaInfo.value.media_id }),
      })
    } catch {
      // 忽略清理失败
    }
  }
  mediaInfo.value = null
}

// ─────────────────────────────────────────────────────────────────
// 播放器联动：当前时间对应的字幕高亮 + 点击跳转
// ─────────────────────────────────────────────────────────────────
const videoRef = ref<HTMLVideoElement | null>(null)
const audioRef = ref<HTMLAudioElement | null>(null)
const currentTime = ref(0)

const onTimeUpdate = (evt: Event) => {
  const target = evt.target as HTMLMediaElement
  currentTime.value = target.currentTime
}

const currentEntry = computed(() => {
  const t = currentTime.value
  return entries.value.find((e) => t >= e.start && t <= e.end) || null
})

const jumpToEntry = (row: SubtitleEntry) => {
  const el = videoRef.value || audioRef.value
  if (!el) return
  el.currentTime = row.start
  el.play().catch(() => {
    // 部分浏览器要求用户手势才能自动播放，静默忽略
  })
}

// ─────────────────────────────────────────────────────────────────
// 导出（前端持有完整字幕数据，请求后端仅做格式转换，返回文本后
// 用 Blob 方式触发浏览器下载，不落盘到工作目录）
// ─────────────────────────────────────────────────────────────────
const exportSubtitle = async (format: 'srt' | 'lrc' | 'txt') => {
  if (!entries.value.length) {
    ElMessage.warning(t('subtitle.exportEmpty'))
    return
  }
  try {
    const payload = {
      format,
      entries: entries.value.map((e) => ({ start: e.start, end: e.end, text: e.text })),
    }
    const res = await fetch('/api/subtitle/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitle.exportFailed'))

    const baseName = mediaInfo.value ? mediaInfo.value.filename.replace(/\.[^.]+$/, '') : 'subtitle'
    const blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${baseName}.${format}`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)

    ElMessage.success(`✅ ${t('subtitle.exportSuccess')}`)
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  }
}

// ─────────────────────────────────────────────────────────────────
// 字幕嵌入：把当前编辑区字幕用 ffmpeg 封装进原始视频/音频，生成新文件
// 后触发浏览器下载。与 exportSubtitle 共用同一份 entries，但落盘/耗时
// 更长，走独立的异步 job 轮询（不复用 pollRecognizeJob，避免和识别
// 进度条的状态绑在一起）。两种模式：
//   - 'soft' : 软字幕封装（/api/subtitle/embed）。视频走原容器格式，
//              音频因容器限制统一封装成 .mka——多数播放器没问题，但
//              VLC 等在"纯音频文件"上不一定渲染字幕轨（没有画面可
//              叠加），仅推荐给熟悉播放器字幕轨切换的用户。
//   - 'burn' : 硬字幕烧录（/api/subtitle/embed-video），仅音频文件可
//              用。生成一个纯色背景 + 烧录字幕的 mp4，字幕不可关闭，
//              但保证任何播放器打开都能直接看到。
// ─────────────────────────────────────────────────────────────────
const embedding = ref<'soft' | 'burn' | false>(false)

const embedSubtitleIntoMedia = async (mode: 'soft' | 'burn') => {
  if (!mediaInfo.value) {
    ElMessage.warning(t('subtitle.needUploadFirst'))
    return
  }
  if (!entries.value.length) {
    ElMessage.warning(t('subtitle.exportEmpty'))
    return
  }

  embedding.value = mode
  try {
    const endpoint = mode === 'burn' ? '/api/subtitle/embed-video' : '/api/subtitle/embed'
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        media_id: mediaInfo.value.media_id,
        entries: entries.value.map((e) => ({ start: e.start, end: e.end, text: e.text })),
      }),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitle.embedFailed'))

    const downloadUrl = await new Promise<string>((resolve, reject) => {
      const tick = async () => {
        try {
          const jobRes = await fetch(`/api/subtitle/job/${data.job_id}`)
          const jobData = await jobRes.json()
          if (!jobRes.ok || !jobData.success) throw new Error(jobData.error || t('subtitle.embedFailed'))

          const job = jobData.job || {}
          if (job.status === 'done') {
            resolve(job.result?.download_url)
            return
          }
          if (job.status === 'failed') {
            reject(new Error(job.error || t('subtitle.embedFailed')))
            return
          }
          window.setTimeout(tick, 1200)
        } catch (e) {
          reject(e)
        }
      }
      tick()
    })

    // 直接用 <a download> 触发浏览器另存为；文件已经在服务端生成好，
    // 不需要像 exportSubtitle 那样先取文本再拼 Blob。
    const link = document.createElement('a')
    link.href = downloadUrl
    link.click()

    ElMessage.success(`✅ ${t('subtitle.embedSuccess')}`)
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  } finally {
    embedding.value = false
  }
}

// ─────────────────────────────────────────────────────────────────
// 工具函数
// ─────────────────────────────────────────────────────────────────
const formatDuration = (sec: number): string => {
  const s = Math.floor(sec % 60)
  const m = Math.floor((sec / 60) % 60)
  const h = Math.floor(sec / 3600)
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

onBeforeUnmount(() => {
  clearJobPolling()
})
</script>

<style scoped>
.subtitle-container {
  width: 100%;
}

.subtitle-card {
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
  margin: 4px 0 16px;
}

.status-alert {
  margin-bottom: 16px;
}

.section-block {
  margin-bottom: 28px;
  padding-bottom: 24px;
  border-bottom: 1px solid #f0f0f0;
}

.section-block:last-child {
  border-bottom: none;
}

.section-heading {
  font-size: 15px;
  font-weight: bold;
  color: #333;
  margin: 0 0 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.subtitle-list-heading {
  margin-top: 20px;
}

.list-actions {
  display: flex;
  gap: 8px;
}

.media-upload {
  width: 100%;
}

.media-upload :deep(.el-upload) {
  width: 100%;
}

.media-upload :deep(.el-upload-dragger) {
  width: 100%;
  padding: 32px 20px;
}

.upload-icon {
  font-size: 40px;
  color: #94a3b8;
  margin-bottom: 8px;
}

.upload-progress {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
  color: #606266;
  font-size: 13px;
}

.upload-progress .el-progress {
  flex: 1;
}

.media-info-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  background: #f8f9fc;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 12px 16px;
}

.media-info-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  min-width: 0;
}

.media-icon {
  font-size: 20px;
}

.media-name {
  font-weight: 600;
  color: #303133;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.media-duration {
  color: #909399;
  font-size: 13px;
}

.settings-form {
  max-width: 560px;
}

.option-hint-icon {
  margin-left: 8px;
  cursor: help;
  opacity: 0.7;
}

.recognize-progress {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
}

.recognize-progress .el-progress {
  flex: 1;
  max-width: 400px;
}

.progress-label {
  color: #606266;
  font-size: 13px;
  white-space: nowrap;
}

.player-layout {
  display: flex;
  justify-content: center;
  margin-bottom: 8px;
}

.player-wrap {
  position: relative;
  width: 100%;
  max-width: 720px;
}

.media-player {
  width: 100%;
  border-radius: 8px;
  background: #000;
  display: block;
}

.audio-player {
  background: transparent;
}

.subtitle-overlay {
  position: absolute;
  bottom: 46px;
  left: 50%;
  transform: translateX(-50%);
  background: rgba(0, 0, 0, 0.65);
  color: #fff;
  padding: 6px 16px;
  border-radius: 6px;
  font-size: 15px;
  max-width: 90%;
  text-align: center;
  pointer-events: none;
}

.empty-hint {
  color: #909399;
  font-size: 13px;
  text-align: center;
  padding: 24px 0;
}

.subtitle-table :deep(.el-table__cell) {
  vertical-align: top;
  padding-top: 8px;
  padding-bottom: 8px;
}

.export-buttons {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

@media (max-width: 768px) {
  .media-name {
    max-width: 200px;
  }
}
</style>
