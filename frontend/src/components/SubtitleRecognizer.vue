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

        <div v-if="!mediaInfo" class="audio-upload-row">
          <el-upload
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
          <!-- 录音只产出音频，不涉及视频；识别流程本身对音频/视频一视同仁，
               因此这里直接复用 handleFileSelect，与手动选择文件走同一条
               上传路径。上传前尚无 mediaInfo，预览按钮退化为播放"刚录制
               但还没点击上传/尚在上传中"的本地录音（见 AudioRecordPreview
               内部 justRecordedBlob 兜底逻辑）。 -->
          <AudioRecordPreview
            :current-file="null"
            :disabled="uploading"
            @recorded="(f: File) => handleFileSelect({ raw: f })"
          />
        </div>

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
            <!-- 上传成功后，本地 File 引用已经不在了（handleFileSelect 只
                 保留服务端返回的 mediaInfo），因此这里改用 sourceUrl 模式
                 预览/下载，而不是像上传前那样传 currentFile。视频文件的
                 play_url 同样可以用 <audio> 播放（浏览器只关心资源是否
                 可解码，不关心标签本身），因此不区分 is_video。录音按钮
                 在这个状态下隐藏（showRecordButton=false）——重新录音应该
                 走下面的"重新选择文件"按钮触发完整的替换+重新上传流程，
                 而不是在已上传状态下静默录一段新音频、却不触发重新上传，
                 导致预览的是新录音、但实际参与识别的仍是服务端旧文件。 -->
            <AudioRecordPreview
              :current-file="null"
              :source-url="mediaInfo.play_url"
              :download-file-name="mediaInfo.filename"
              :show-record-button="false"
              :disabled="recognizing"
            />
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

        <div v-if="mediaInfo && entries.length" class="section-heading waveform-heading">
          <span>🌊 {{ t('subtitle.waveformTitle') }}</span>
        </div>
        <SubtitleWaveform
          v-if="mediaInfo && entries.length"
          :entries="entries"
          :media-url="mediaInfo.waveform_url || mediaInfo.play_url"
          :duration="mediaInfo.duration || 0"
          :current-time="currentTime"
          :active-uid="activeUid"
          class="waveform-block"
          @seek="onWaveformSeek"
          @update-entry="onWaveformUpdateEntry"
          @add-entry="onWaveformAddEntry"
          @select="onWaveformSelect"
          @select-multi="onWaveformSelectMulti"
          @edit-text="onWaveformEditText"
          @delete-entry="onWaveformDeleteEntry"
          @delete-entries="onWaveformDeleteEntries"
          @split-entry="onWaveformSplitEntry"
          @split-entries="onWaveformSplitEntries"
          @drag-start="history.beginGesture()"
          @drag-end="history.commitGesture()"
        />

        <div class="section-heading subtitle-list-heading">
          <span>📝 {{ t('subtitle.subtitleListTitle') }}</span>
          <div class="list-actions">
            <el-tooltip :content="t('subtitle.undoHint')" placement="top">
              <el-button size="small" :disabled="!canUndo" @click="onUndo">↩️ {{ t('subtitle.undo') }}</el-button>
            </el-tooltip>
            <el-tooltip :content="t('subtitle.redoHint')" placement="top">
              <el-button size="small" :disabled="!canRedo" @click="onRedo">↪️ {{ t('subtitle.redo') }}</el-button>
            </el-tooltip>
            <el-button size="small" type="danger" plain :disabled="!entries.length" @click="clearAll">
              🗑️ {{ t('subtitle.clearAll') }}
            </el-button>
          </div>
        </div>

        <p v-if="!entries.length" class="empty-hint">{{ t('subtitle.subtitleListEmpty') }}</p>

        <el-table
          v-else
          :data="entries"
          size="small"
          max-height="420"
          class="subtitle-table"
          row-key="_uid"
          :row-class-name="rowClassName"
        >
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
                @focus="history.beginGesture()"
                @blur="history.commitGesture()"
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
          <el-button @click="exportSubtitle('lab')">📥 {{ t('subtitle.exportLab') }}</el-button>
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
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { useAppLocale } from '../i18n'
import SubtitleWaveform from './SubtitleWaveform.vue'
import { useSubtitleHistory } from './useSubtitleHistory'
import AudioRecordPreview from './AudioRecordPreview.vue'

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
  waveform_url: string | null
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
    history.resetHistory()
    mediaInfo.value = {
      media_id: data.media_id,
      filename: data.filename,
      is_video: data.is_video,
      duration: data.duration,
      play_url: data.play_url,
      waveform_url: data.waveform_url ?? null,
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
  history.resetHistory()
  recognizeError.value = ''
}

// ─────────────────────────────────────────────────────────────────
// 识别设置 + 启动识别 + 进度轮询
// ─────────────────────────────────────────────────────────────────
const recognizeSettings = reactive({
  language: 'auto',
  device: 'auto',
  batchSize: 8,
  maxChars: 34,
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
  history.resetHistory()
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
    history.recordBeforeChange()
    row.start = parsed
  } else {
    if (parsed <= row.start) {
      ElMessage.error(t('subtitle.timeOverlapWarning'))
      row._endText = formatTimeInput(row.end)
      return
    }
    history.recordBeforeChange()
    row.end = parsed
  }
}

const insertAfter = (index: number) => {
  const cur = entries.value[index]
  const next = entries.value[index + 1]
  const start = cur.end
  const end = next ? Math.min(next.start, cur.end + 2) : cur.end + 2
  history.recordBeforeChange()
  entries.value.splice(index + 1, 0, toEditableEntry({ start, end: Math.max(end, start + 0.3), text: '' }))
}

// 字幕列表行内"删除"按钮：无需二次确认，直接删除（与波形块的删除行为
// 保持一致）；批量清空所有字幕仍然需要二次确认，见 clearAll()
const deleteEntry = (index: number) => {
  history.recordBeforeChange()
  entries.value.splice(index, 1)
}

const mergeWithNext = (index: number) => {
  const cur = entries.value[index]
  const next = entries.value[index + 1]
  if (!next) return
  history.recordBeforeChange()
  cur.end = next.end
  cur.text = `${cur.text}${next.text}`
  cur._endText = formatTimeInput(cur.end)
  entries.value.splice(index + 1, 1)
}

// 手动"拆分"某一行字幕为两行——字幕列表里的 ✂️ 按钮专用：交给后端按
// 标点/文本长度比例算出拆分点（这一行此时可能是识别结果，也可能已被
// 用户编辑/合并过，早就没有逐字时间戳了，所以拆分点只能靠文本本身
// 重新估算，与 /api/subtitle/export 一样是无状态接口，不依赖 job）。
// 波形时间轴上的"拆分"按钮不走这个函数，见下方 splitEntryTimeOnly：
// 那边只想按播放头位置切时间，不想让文本被自动拆分。
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
    history.recordBeforeChange()
    entries.value.splice(index, 1, left, right)
    activeUid.value = left._uid
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  } finally {
    // cur 对应的行可能已经被 splice 替换掉了，这里的 cur._splitting 只是
    // 让原引用在 splice 之前那一刻的 loading 状态能正确复位，不影响新行
    cur._splitting = false
  }
}

// 波形时间轴上的"拆分"按钮：只在播放头位置把时间切成两段，完全不调用
// 后端、不做任何文本拆分算法——左段保留原文本，右段留空，交给用户
// 用波形块的双击内联编辑自行分配文字。纯前端计算，无需 loading 状态。
// skipHistory：批量拆分（onWaveformSplitEntries）会在外层统一记一次撤销
// 快照，代表"这一整批拆分"算一步，所以循环内部调这个函数时不再重复记录
const splitEntryTimeOnly = (index: number, at: number, skipHistory = false) => {
  const cur = entries.value[index]
  if (!cur) return
  if (cur.end - cur.start < 0.05) {
    ElMessage.warning(t('subtitle.splitTooShort'))
    return
  }

  const minSeg = Math.min(0.1, (cur.end - cur.start) / 2)
  const splitAt = Math.max(cur.start + minSeg, Math.min(at, cur.end - minSeg))

  const left = toEditableEntry({ start: cur.start, end: splitAt, text: cur.text })
  const right = toEditableEntry({ start: splitAt, end: cur.end, text: '' })
  if (!skipHistory) history.recordBeforeChange()
  entries.value.splice(index, 1, left, right)
  activeUid.value = left._uid
}

const clearAll = async () => {
  try {
    await ElMessageBox.confirm(t('subtitle.clearAllConfirm'), '', { type: 'warning' })
  } catch {
    return
  }
  entries.value = []
  selectedUids.value = new Set()
  history.resetHistory()
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
  activeUid.value = row._uid
  const el = videoRef.value || audioRef.value
  if (!el) return
  el.currentTime = row.start
  el.play().catch(() => {
    // 部分浏览器要求用户手势才能自动播放，静默忽略
  })
}

// ─────────────────────────────────────────────────────────────────
// 波形时间轴联动：SubtitleWaveform 组件只负责展示与拖拽交互，具体的
// 数据变更（时间调整/新增条目）与播放器控制都由父组件这里统一处理，
// 与表格编辑（onTimeEdit 等）共用同一份 entries，两种编辑方式互相同步。
// ─────────────────────────────────────────────────────────────────
const activeUid = ref<number | null>(null)

// ─────────────────────────────────────────────────────────────────
// 撤销/恢复（Ctrl+Z / Ctrl+Y、Ctrl+Shift+Z）。历史记录本身在
// useSubtitleHistory 里实现，这里只负责：
// 1. 在每个"离散操作"修改 entries 之前调用 history.recordBeforeChange()
// 2. 全局快捷键分发 undo()/redo()——聚焦在输入框/文本域时不拦截，交给
//    浏览器原生的输入框撤销
// ─────────────────────────────────────────────────────────────────
const history = useSubtitleHistory(entries, activeUid)
const canUndo = history.canUndo
const canRedo = history.canRedo

const onUndo = () => history.undo()
const onRedo = () => history.redo()

const onUndoRedoKeydown = (evt: KeyboardEvent) => {
  if (!(evt.ctrlKey || evt.metaKey)) return
  const activeEl = document.activeElement as HTMLElement | null
  if (activeEl && ['INPUT', 'TEXTAREA'].includes(activeEl.tagName)) return // 交给原生输入框撤销
  const key = evt.key.toLowerCase()
  if (key === 'z' && !evt.shiftKey) {
    evt.preventDefault()
    onUndo()
  } else if (key === 'y' || (key === 'z' && evt.shiftKey)) {
    evt.preventDefault()
    onRedo()
  }
}

onMounted(() => {
  window.addEventListener('keydown', onUndoRedoKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onUndoRedoKeydown)
})

const onWaveformSeek = (time: number) => {
  const el = videoRef.value || audioRef.value
  if (el) el.currentTime = time
  currentTime.value = time
}

// ─────────────────────────────────────────────────────────────────
// 空格键播放/暂停：编辑文本（波形块内联编辑框、字幕列表里的时间/文本
// 输入框等）时不响应，避免和"输入空格字符"冲突；其余情况下（包括焦点
// 停在某个按钮上时）空格键一律用于切换播放/暂停，而不是触发按钮本身
// 的点击——因此需要 preventDefault 来同时抑制浏览器默认的按钮激活和
// <video>/<audio> 原生控件自身的空格键处理（否则会和这里的 play/pause
// 重复触发，导致来回抖动）。
// ─────────────────────────────────────────────────────────────────
const onSpaceKeydown = (evt: KeyboardEvent) => {
  if (evt.code !== 'Space' && evt.key !== ' ') return
  const activeEl = document.activeElement as HTMLElement | null
  const tag = activeEl?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || activeEl?.isContentEditable) return
  const el = videoRef.value || audioRef.value
  if (!el) return
  evt.preventDefault()
  if (el.paused) {
    el.play().catch(() => {
      // 部分浏览器要求用户手势才能自动播放，静默忽略
    })
  } else {
    el.pause()
  }
}

onMounted(() => {
  window.addEventListener('keydown', onSpaceKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onSpaceKeydown)
})

const onWaveformUpdateEntry = (payload: { uid: number; start?: number; end?: number }) => {
  const row = entries.value.find((e) => e._uid === payload.uid)
  if (!row) return
  if (payload.start !== undefined) {
    row.start = payload.start
    row._startText = formatTimeInput(row.start)
  }
  if (payload.end !== undefined) {
    row.end = payload.end
    row._endText = formatTimeInput(row.end)
  }
  activeUid.value = payload.uid
}

// 在时间轴空白处双击新增一条字幕：找到该时间点前后相邻的条目，夹在
// 中间插入一条 2 秒（或更短，避免越界侵占相邻条目）默认时长的空白字幕，
// 与表格里的"➕ 后插一条"（insertAfter）行为保持一致的默认时长策略。
const onWaveformAddEntry = (time: number) => {
  const sorted = entries.value
  let insertAt = sorted.length
  for (let i = 0; i < sorted.length; i++) {
    if (time < sorted[i].start) {
      insertAt = i
      break
    }
  }
  const prev = sorted[insertAt - 1]
  const next = sorted[insertAt]
  if (prev && time < prev.end) return // 双击落在已有字幕区块内部，交由拖拽/表格编辑处理，这里不重复插入
  const start = time
  const maxEnd = next ? next.start : start + 2
  const end = Math.min(start + 2, maxEnd)
  if (end - start < 0.1) return // 相邻条目间隙太窄，放不下新字幕
  const newEntry = toEditableEntry({ start, end, text: '' })
  history.recordBeforeChange()
  entries.value.splice(insertAt, 0, newEntry)
  activeUid.value = newEntry._uid
}

// 波形块单击选中 → 只更新 activeUid，联动列表高亮；点击空白处会传 null 取消选中
const onWaveformSelect = (uid: number | null) => {
  activeUid.value = uid
}

// 波形块内联编辑（双击）提交的文字，按 uid 定位对应行
const onWaveformEditText = (payload: { uid: number; text: string }) => {
  const row = entries.value.find((e) => e._uid === payload.uid)
  if (!row) return
  history.recordBeforeChange()
  row.text = payload.text
}

// 波形块工具栏"删除"按钮 / 选中后按 Delete 键：无需二次确认，直接删除
// （字幕列表里的 🗑️ 删除按钮同样不再二次确认，见 deleteEntry()）
const onWaveformDeleteEntry = (uid: number) => {
  const index = entries.value.findIndex((e) => e._uid === uid)
  if (index === -1) return
  history.recordBeforeChange()
  entries.value.splice(index, 1)
  if (activeUid.value === uid) activeUid.value = null
}

// 波形块多选后批量删除（Delete 键/工具栏"删除"按钮在多选状态下触发）：
// 同样无需二次确认，与单选删除保持一致的即时删除体验
const onWaveformDeleteEntries = (uids: number[]) => {
  if (!uids.length) return
  const uidSet = new Set(uids)
  history.recordBeforeChange()
  entries.value = entries.value.filter((e) => !uidSet.has(e._uid))
  if (activeUid.value !== null && uidSet.has(activeUid.value)) activeUid.value = null
  selectedUids.value = new Set()
}

// 波形块工具栏"拆分"按钮：按 uid 定位后复用 splitEntry 的按 index 实现
const onWaveformSplitEntry = (payload: { uid: number; at: number }) => {
  const index = entries.value.findIndex((e) => e._uid === payload.uid)
  if (index === -1) return
  splitEntryTimeOnly(index, payload.at)
}

// 波形块多选后批量拆分：每个 payload 各自按自己的拆分点独立处理。注意
// 每次 splitEntryTimeOnly 都会 splice 替换 entries，导致后续 uid 对应的
// index 失效，所以每次都重新按 uid 查找当前 index，而不是缓存一份索引表。
// 整批拆分只在开头记一次撤销快照（skipHistory=true 让内部不再重复记录），
// 让"一次多选拆分"作为一步撤销，而不是拆几条就要撤销几次
const onWaveformSplitEntries = (payloads: Array<{ uid: number; at: number }>) => {
  if (!payloads.length) return
  history.recordBeforeChange()
  for (const { uid, at } of payloads) {
    const index = entries.value.findIndex((e) => e._uid === uid)
    if (index === -1) continue
    splitEntryTimeOnly(index, at, true)
  }
  selectedUids.value = new Set()
}

// 波形块多选集合同步：仅用于字幕列表的多选行高亮，不影响 activeUid
// （单选锚点，仍然驱动拖拽/表格主高亮等既有逻辑）
const selectedUids = ref<Set<number>>(new Set())
const onWaveformSelectMulti = (uids: number[]) => {
  selectedUids.value = new Set(uids)
}

// 字幕列表行高亮：单选（activeUid）用原有的橙色高亮，多选（selectedUids）
// 用另一个 class 区分，与波形块的双色高亮方案保持一致
const rowClassName = ({ row }: { row: SubtitleEntry }) => {
  if (selectedUids.value.has(row._uid)) return 'row-multi-selected'
  return row._uid === activeUid.value ? 'row-active' : ''
}

// ─────────────────────────────────────────────────────────────────
// 导出（前端持有完整字幕数据，请求后端仅做格式转换，返回文本后
// 用 Blob 方式触发浏览器下载，不落盘到工作目录）
// ─────────────────────────────────────────────────────────────────
const exportSubtitle = async (format: 'srt' | 'lrc' | 'lab' | 'txt') => {
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

.waveform-heading {
  margin-top: 20px;
}

.waveform-block {
  margin-bottom: 8px;
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

/* 拖拽上传框 + 录音/预览按钮并排布局：.media-upload 本身撑满整行宽度，
   这里让它在 flex 容器里可以收缩，把空间让给右侧的录音/预览按钮。 */
.audio-upload-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.audio-upload-row .media-upload {
  flex: 1;
  min-width: 0;
  width: auto;
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

/* 与波形轴选中态（.subtitle-region.active，橙色）保持一致的高亮色，
   用 :deep 穿透 el-table 的 scoped 样式隔离；!important 是因为 element-plus
   自带的 hover/stripe 背景色优先级较高，不加的话选中行 hover 时会被盖掉。 */
.subtitle-table :deep(tr.row-active td.el-table__cell) {
  background-color: rgba(255, 145, 77, 0.16) !important;
}

/* 多选行高亮：与波形时间轴的紫色多选描边（.multi-selected）呼应，
   用不同色相和单选态区分开 */
.subtitle-table :deep(tr.row-multi-selected td.el-table__cell) {
  background-color: rgba(124, 58, 237, 0.14) !important;
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
