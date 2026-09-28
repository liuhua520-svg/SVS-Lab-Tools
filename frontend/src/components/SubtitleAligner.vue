<template>
  <div class="subtitle-container">
    <el-card class="subtitle-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-title">🎯 {{ t('subtitleAlign.pageTitle') }}</span>
        </div>
      </template>

      <!-- ============== 依赖状态检查 ============== -->
      <el-alert
        v-if="!statusLoading && !statusInfo.ready"
        type="warning"
        show-icon
        :closable="false"
        class="status-alert"
      >
        <template #title>
          <div v-if="!statusInfo.ffmpeg.available">⚠️ {{ statusInfo.ffmpeg.message || t('subtitleAlign.statusFfmpegMissing') }}</div>
          <div v-if="!statusInfo.qwen3_fa.available">⚠️ {{ statusInfo.qwen3_fa.message || t('subtitleAlign.statusQwenMissing') }}</div>
        </template>
        <el-button size="small" text @click="checkStatus" :loading="statusLoading">
          🔄 {{ t('subtitleAlign.statusRecheck') }}
        </el-button>
      </el-alert>
      <el-alert
        v-else-if="!statusLoading && statusInfo.ready"
        type="success"
        show-icon
        :closable="false"
        class="status-alert"
      >
        <template #title>✓ {{ t('subtitleAlign.statusReady') }}</template>
      </el-alert>
      <el-alert v-else type="info" show-icon :closable="false" class="status-alert">
        <template #title>{{ t('subtitleAlign.statusChecking') }}</template>
      </el-alert>

      <!-- ============== 上传区 ============== -->
      <div class="section-block">
        <div class="section-heading">📁 {{ t('subtitleAlign.uploadTitle') }}</div>

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
            <div class="el-upload__text">{{ t('subtitleAlign.uploadHint') }}</div>
          </el-upload>
          <AudioRecordPreview
            :current-file="null"
            :disabled="uploading"
            @recorded="(f: File) => handleFileSelect({ raw: f })"
          />
        </div>

        <div v-if="uploading" class="upload-progress">
          <el-progress :percentage="100" :indeterminate="true" :duration="1.5" />
          <span>{{ t('subtitleAlign.uploading') }}</span>
        </div>

        <div v-if="mediaInfo" class="media-info-card">
          <div class="media-info-row">
            <span class="media-icon">{{ mediaInfo.is_video ? '🎞️' : '🎵' }}</span>
            <span class="media-name" :title="mediaInfo.filename">{{ mediaInfo.filename }}</span>
            <el-tag size="small" :type="mediaInfo.is_video ? 'primary' : 'success'">
              {{ mediaInfo.is_video ? t('subtitleAlign.fileTypeVideo') : t('subtitleAlign.fileTypeAudio') }}
            </el-tag>
            <span v-if="mediaInfo.duration" class="media-duration">
              {{ t('subtitleAlign.fileDuration') }}: {{ formatDuration(mediaInfo.duration) }}
            </span>
            <AudioRecordPreview
              :current-file="null"
              :source-url="mediaInfo.play_url"
              :download-file-name="mediaInfo.filename"
              :show-record-button="false"
              :disabled="running"
            />
          </div>
          <el-button size="small" :disabled="running" @click="resetMedia">
            🔁 {{ t('subtitleAlign.uploadReplace') }}
          </el-button>
        </div>
      </div>

      <!-- ============== 文本与对齐设置 ============== -->
      <div v-if="mediaInfo" class="section-block">
        <div class="section-heading">⚙️ {{ t('subtitleAlign.settingsTitle') }}</div>

        <el-form label-width="150px" class="settings-form">
          <el-form-item :label="t('subtitleAlign.inputText')">
            <el-input
              v-model="text"
              type="textarea"
              :rows="8"
              :disabled="running"
              :placeholder="t('subtitleAlign.inputTextPlaceholder')"
            />
          </el-form-item>
          <el-form-item :label="t('subtitleAlign.language')">
            <el-select v-model="settings.language" style="width:260px" :disabled="running">
              <el-option v-for="x in languages" :key="x.value" :label="x.label" :value="x.value" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('subtitleAlign.device')">
            <el-radio-group v-model="settings.device" :disabled="running">
              <el-radio value="auto">{{ t('subtitleAlign.deviceAuto') }}</el-radio>
              <el-radio value="cpu">{{ t('subtitleAlign.deviceCpu') }}</el-radio>
              <el-radio value="cuda">{{ t('subtitleAlign.deviceCuda') }}</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="t('subtitleAlign.maxChars')">
            <el-input-number v-model="settings.maxChars" :min="1" :max="500" :disabled="running" />
          </el-form-item>
          <el-form-item :label="t('subtitleAlign.splitSentence')">
            <el-switch v-model="settings.splitSentence" :disabled="running" />
            <el-tooltip :content="t('subtitleAlign.splitSentenceHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>
          <el-form-item v-if="settings.splitSentence" :label="t('subtitleAlign.splitComma')">
            <el-switch v-model="settings.splitComma" :disabled="running" />
            <el-tooltip :content="t('subtitleAlign.splitCommaHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>
          <el-form-item :label="t('subtitleAlign.removeSymbols')">
            <el-switch v-model="settings.removePunctuation" :disabled="running" />
            <el-tooltip :content="t('subtitleAlign.removeSymbolsHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>
          <el-form-item :label="t('subtitleAlign.vadGap')">
            <el-switch v-model="settings.vadGapEnabled" :disabled="running" />
            <el-tooltip :content="t('subtitleAlign.vadGapHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>
          <el-form-item v-if="settings.vadGapEnabled" :label="t('subtitleAlign.vadGapThreshold')">
            <el-input-number v-model="settings.vadGapThresholdSec" :min="0.05" :max="5" :step="0.1" :precision="2" :disabled="running" />
            <span class="unit">{{ t('subtitleAlign.vadGapUnit') }}</span>
            <el-tooltip :content="t('subtitleAlign.vadGapThresholdHint')" placement="top">
              <span class="option-hint-icon">❓</span>
            </el-tooltip>
          </el-form-item>
        </el-form>

        <el-button
          type="primary"
          size="large"
          :loading="running"
          :disabled="!statusInfo.ready || !media || !text.trim()"
          @click="run"
        >
          {{ running ? t('subtitleAlign.aligning') : `▶️ ${t('subtitleAlign.start')}` }}
        </el-button>

        <div v-if="running" class="align-progress">
          <el-progress :percentage="progress" :indeterminate="progress === 0" />
        </div>

        <el-alert v-if="error" type="error" show-icon :closable="true" @close="error = ''" class="status-alert">
          <template #title>{{ error }}</template>
        </el-alert>
      </div>

      <!-- ============== 预览播放器 + 波形时间轴 + 字幕列表 ============== -->
      <div v-if="mediaInfo && entries.length" class="section-block">
        <div class="section-heading">🖥️ {{ t('subtitleAlign.playerTitle') }}</div>

        <div class="player-layout">
          <div class="player-wrap">
            <video
              v-if="mediaInfo.is_video"
              ref="videoRef"
              :src="mediaInfo.play_url"
              controls
              class="media-player"
              @timeupdate="onTimeUpdate"
            />
            <audio
              v-else
              ref="audioRef"
              :src="mediaInfo.play_url"
              controls
              class="media-player audio-player"
              @timeupdate="onTimeUpdate"
            />
            <div v-if="currentEntry" class="subtitle-overlay">{{ currentEntry.text }}</div>
          </div>
        </div>

        <div class="section-heading waveform-heading">
          <span>🌊 {{ t('subtitleAlign.waveformTitle') }}</span>
        </div>
        <SubtitleWaveform
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
          <span>📝 {{ t('subtitleAlign.subtitleListTitle') }}</span>
          <div class="list-actions">
            <el-tooltip :content="t('subtitleAlign.undoHint')" placement="top">
              <el-button size="small" :disabled="!canUndo" @click="onUndo">↩️ {{ t('subtitleAlign.undo') }}</el-button>
            </el-tooltip>
            <el-tooltip :content="t('subtitleAlign.redoHint')" placement="top">
              <el-button size="small" :disabled="!canRedo" @click="onRedo">↪️ {{ t('subtitleAlign.redo') }}</el-button>
            </el-tooltip>
            <el-button size="small" @click="insertAtEnd">➕ {{ t('subtitleAlign.addEntry') }}</el-button>
            <el-button size="small" type="danger" plain :disabled="!entries.length" @click="clearAllEntries">
              🗑️ {{ t('subtitleAlign.clearAll') }}
            </el-button>
          </div>
        </div>

        <el-table
          :data="entries"
          size="small"
          max-height="420"
          class="subtitle-table"
          row-key="_uid"
          :row-class-name="rowClassName"
        >
          <el-table-column :label="t('subtitleAlign.columnIndex')" width="50">
            <template #default="{ $index }">{{ $index + 1 }}</template>
          </el-table-column>
          <el-table-column :label="t('subtitleAlign.columnStart')" width="130">
            <template #default="{ row }">
              <el-input v-model="row._startText" size="small" @change="onTimeEdit(row, 'start')" />
            </template>
          </el-table-column>
          <el-table-column :label="t('subtitleAlign.columnEnd')" width="130">
            <template #default="{ row }">
              <el-input v-model="row._endText" size="small" @change="onTimeEdit(row, 'end')" />
            </template>
          </el-table-column>
          <el-table-column :label="t('subtitleAlign.columnText')">
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
          <el-table-column :label="t('subtitleAlign.columnAction')" width="230">
            <template #default="{ row, $index }">
              <el-tooltip :content="t('subtitleAlign.jumpToTime')" placement="top">
                <el-button size="small" circle @click="jumpToEntry(row)">▶</el-button>
              </el-tooltip>
              <el-tooltip :content="t('subtitleAlign.splitEntry')" placement="top">
                <el-button size="small" circle :loading="row._splitting" @click="splitEntry($index)">✂️</el-button>
              </el-tooltip>
              <el-tooltip :content="t('subtitleAlign.addAfter')" placement="top">
                <el-button size="small" circle @click="insertAfter($index)">➕</el-button>
              </el-tooltip>
              <el-tooltip v-if="$index < entries.length - 1" :content="t('subtitleAlign.mergeNext')" placement="top">
                <el-button size="small" circle @click="mergeWithNext($index)">🔗</el-button>
              </el-tooltip>
              <el-tooltip :content="t('subtitleAlign.deleteEntry')" placement="top">
                <el-button size="small" circle type="danger" @click="deleteEntry($index)">🗑️</el-button>
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- ============== 导出 ============== -->
      <div v-if="entries.length" class="section-block">
        <div class="section-heading">📤 {{ t('subtitleAlign.exportTitle') }}</div>
        <div class="export-buttons">
          <el-button @click="exportSubtitle('srt')">📥 {{ t('subtitleAlign.exportSrt') }}</el-button>
          <el-button @click="exportSubtitle('lrc')">📥 {{ t('subtitleAlign.exportLrc') }}</el-button>
          <el-button @click="exportSubtitle('lab')">📥 {{ t('subtitleAlign.exportLab') }}</el-button>
          <el-button @click="exportSubtitle('txt')">📥 {{ t('subtitleAlign.exportText') }}</el-button>
          <el-tooltip v-if="mediaInfo && !mediaInfo.is_video" :content="t('subtitle.embedAudioHint')" placement="top">
            <el-button type="primary" :loading="embedding === 'soft'" :disabled="embedding === 'burn'" @click="embedSubtitleIntoMedia('soft')">
              🎵 {{ t('subtitle.embedIntoAudio') }}
            </el-button>
          </el-tooltip>
          <el-button v-else-if="mediaInfo" type="primary" :loading="embedding === 'soft'" :disabled="embedding === 'burn'" @click="embedSubtitleIntoMedia('soft')">
            🎬 {{ t('subtitle.embedIntoVideo') }}
          </el-button>
          <el-tooltip v-if="mediaInfo && !mediaInfo.is_video" :content="t('subtitle.embedVideoHint')" placement="top">
            <el-button type="success" :loading="embedding === 'burn'" :disabled="embedding === 'soft'" @click="embedSubtitleIntoMedia('burn')">
              🔥 {{ t('subtitle.embedBurnVideo') }}
            </el-button>
          </el-tooltip>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { useAppLocale } from '../i18n'
import SubtitleWaveform from './SubtitleWaveform.vue'
import { useSubtitleHistory } from './useSubtitleHistory'
import { useSubtitleEmbed } from './useSubtitleEmbed'
import AudioRecordPreview from './AudioRecordPreview.vue'

const { t } = useAppLocale()

// Qwen3-ForcedAligner 目前只支持这 5 种语言（见 alt_aligners._to_qwen_lang_name），
// 其余语言传给后端会直接触发 "不支持语言" 报错，因此这里不再列出。
const languages = [
  ['Chinese', '中文 (Chinese)'], ['English', 'English'], ['Cantonese', '粤语 (Cantonese)'],
  ['Japanese', '日本語 (Japanese)'], ['Korean', '한국어 (Korean)'],
].map(([value, label]) => ({ value, label }))

// ─────────────────────────────────────────────────────────────────
// 依赖状态检查（ffmpeg + Qwen3-ForcedAligner），与字幕识别页对称，
// 但检查的是对齐模型而非识别模型，两者依赖的具体可用性可能不同步。
// ─────────────────────────────────────────────────────────────────
interface DepStatus { available: boolean; message: string }
const statusLoading = ref(true)
const statusInfo = reactive<{ ffmpeg: DepStatus; qwen3_fa: DepStatus; ready: boolean }>({
  ffmpeg: { available: false, message: '' },
  qwen3_fa: { available: false, message: '' },
  ready: false,
})
const checkStatus = async () => {
  statusLoading.value = true
  try {
    const res = await fetch('/api/subtitle-align/status')
    const data = await res.json()
    if (data.success) {
      statusInfo.ffmpeg = data.ffmpeg
      statusInfo.qwen3_fa = data.qwen3_fa
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
// 媒体上传（与 SubtitleEditor.vue / SubtitleRecognizer.vue 共用同一套
// /api/subtitle/* 接口与服务端存储目录）
// ─────────────────────────────────────────────────────────────────
interface MediaInfo {
  media_id: string
  filename: string
  is_video: boolean
  duration: number | null
  play_url: string
  waveform_url: string | null
}

const media = ref<any>(null)
const mediaInfo = ref<MediaInfo | null>(null)
const uploading = ref(false)
const text = ref('')
const running = ref(false)
const progress = ref(0)
const error = ref('')
const settings = reactive({ language: 'Chinese', device: 'auto', maxChars: 34, splitSentence: true, splitComma: false, removePunctuation: false, vadGapEnabled: false, vadGapThresholdSec: 0.1 })

const handleFileSelect = async (file: any) => {
  const raw: File | null = file?.raw || null
  if (!raw) return

  if (mediaInfo.value) {
    try {
      await ElMessageBox.confirm(t('subtitleAlign.reuploadWarning'), '', { type: 'warning' })
    } catch {
      return
    }
  }

  uploading.value = true
  error.value = ''
  try {
    const fd = new FormData()
    fd.append('file', raw)
    const res = await fetch('/api/subtitle/upload', { method: 'POST', body: fd })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitleAlign.uploadFailed'))

    // 新媒体上传成功后，清空旧的对齐结果，避免时间轴与新媒体错位
    entries.value = []
    history.resetHistory()
    media.value = data
    mediaInfo.value = {
      media_id: data.media_id,
      filename: data.filename,
      is_video: data.is_video,
      duration: data.duration,
      play_url: data.play_url,
      waveform_url: data.waveform_url ?? null,
    }
    ElMessage.success(`✅ ${t('subtitleAlign.uploadSuccess')}`)
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
  media.value = null
  mediaInfo.value = null
  entries.value = []
  history.resetHistory()
  error.value = ''
}

// ─────────────────────────────────────────────────────────────────
// 字幕条目：编辑态数据结构（与 SubtitleEditor.vue 保持一致的字段约定，
// 方便共用 SubtitleWaveform 组件与时间格式化逻辑）
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
    ElMessage.error(t('subtitleAlign.invalidTimeFormat'))
    if (field === 'start') row._startText = formatTimeInput(row.start)
    else row._endText = formatTimeInput(row.end)
    return
  }
  if (field === 'start') {
    if (parsed >= row.end) {
      ElMessage.error(t('subtitleAlign.timeOverlapWarning'))
      row._startText = formatTimeInput(row.start)
      return
    }
    history.recordBeforeChange()
    row.start = parsed
  } else {
    if (parsed <= row.start) {
      ElMessage.error(t('subtitleAlign.timeOverlapWarning'))
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

const insertAtEnd = () => {
  const last = entries.value[entries.value.length - 1]
  const start = last ? last.end : 0
  const duration = mediaInfo.value?.duration || start + 2
  const end = Math.min(start + 2, duration)
  history.recordBeforeChange()
  entries.value.push(toEditableEntry({ start, end: Math.max(end, start + 0.3), text: '' }))
}

// 字幕列表行内"删除"按钮：无需二次确认，直接删除（与波形块的删除行为
// 保持一致）；批量清空所有字幕仍然需要二次确认，见 clearAllEntries()
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

// 手动"拆分"某一行字幕为两行——字幕列表里的 ✂️ 按钮专用，调用后端
// /api/subtitle/split_entry 按标点/文本长度比例把文本也一起拆开。
// 波形时间轴上的"拆分"按钮不走这个函数，见下方 splitEntryTimeOnly：
// 那边只想按播放头位置切时间，不想让文本被自动拆分。
const splitEntry = async (index: number) => {
  const cur = entries.value[index]
  if (!cur || cur._splitting) return
  if (cur.end - cur.start < 0.05) {
    ElMessage.warning(t('subtitleAlign.splitTooShort'))
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
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitleAlign.splitFailed'))

    const left = toEditableEntry(data.left)
    const right = toEditableEntry(data.right)
    history.recordBeforeChange()
    entries.value.splice(index, 1, left, right)
    activeUid.value = left._uid
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  } finally {
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
    ElMessage.warning(t('subtitleAlign.splitTooShort'))
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

const clearAllEntries = async () => {
  try {
    await ElMessageBox.confirm(t('subtitleAlign.clearAllConfirm'), '', { type: 'warning' })
  } catch {
    return
  }
  history.recordBeforeChange()
  entries.value = []
  selectedUids.value = new Set()
  activeUid.value = null
}

// ─────────────────────────────────────────────────────────────────
// 播放器联动：当前时间对应的字幕高亮 + 点击跳转 + 波形时间轴双向同步
// ─────────────────────────────────────────────────────────────────
const videoRef = ref<HTMLVideoElement | null>(null)
const audioRef = ref<HTMLAudioElement | null>(null)
const currentTime = ref(0)
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
  window.addEventListener('keydown', onSpaceKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onUndoRedoKeydown)
  window.removeEventListener('keydown', onSpaceKeydown)
})

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
// 空格键播放/暂停：编辑文本（波形块内联编辑框、字幕列表里的时间/文本
// 输入框、上方文本框等）时不响应，避免和"输入空格字符"冲突
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

const onWaveformSeek = (time: number) => {
  const el = videoRef.value || audioRef.value
  if (el) el.currentTime = time
  currentTime.value = time
}

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
  if (prev && time < prev.end) return
  const start = time
  const maxEnd = next ? next.start : start + 2
  const end = Math.min(start + 2, maxEnd)
  if (end - start < 0.1) return
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

// 波形块工具栏"拆分"按钮：按 uid 定位后复用 splitEntryTimeOnly 的按 index 实现
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
// 运行对齐（/api/subtitle-align/run）
// ─────────────────────────────────────────────────────────────────
const run = async () => {
  running.value = true
  error.value = ''
  progress.value = 0
  try {
    const res = await fetch('/api/subtitle-align/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // VAD 合并间隔：与字幕识别页统一使用 close_vad_gaps / vad_gap_threshold_sec。
      // 之前直接展开 settings，发出去的是 vadGapEnabled / vadGapThresholdSec，
      // 后端从不读取，开关等于没接线。
      body: JSON.stringify({
        media_id: media.value.media_id,
        text: text.value,
        ...settings,
        close_vad_gaps: settings.vadGapEnabled,
        vad_gap_threshold_sec: settings.vadGapThresholdSec,
      }),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitleAlign.alignFailed'))

    const rawEntries = (data.entries || []) as Array<{ start: number; end: number; text: string }>
    entries.value = rawEntries.map(toEditableEntry)
    history.resetHistory()
    selectedUids.value = new Set()
    activeUid.value = null
    progress.value = 100
    ElMessage.success(`✅ ${t('subtitleAlign.alignSuccess')}`)
    // 文本与 FA 结果对不上时，后端会退回按字数均摊——时间不准，必须明确告知，
    // 而不是让用户对着首尾相接的字幕以为是对齐结果。
    if (data.align_mode === 'proportional') {
      ElMessage.warning({ message: t('subtitleAlign.alignFallbackWarning'), duration: 8000, showClose: true })
    }
  } catch (e: any) {
    error.value = e?.message || String(e)
    ElMessage.error(`❌ ${error.value}`)
  } finally {
    running.value = false
  }
}

// ─────────────────────────────────────────────────────────────────
// 导出（复用 /api/subtitle/export，已支持 srt/lrc/txt/lab 四种格式）
// ─────────────────────────────────────────────────────────────────
const exportSubtitle = async (format: 'srt' | 'lrc' | 'txt' | 'lab') => {
  if (!entries.value.length) {
    ElMessage.warning(t('subtitleAlign.exportEmpty'))
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
    if (!res.ok || !data.success) throw new Error(data.error || t('subtitleAlign.exportFailed'))

    const baseName = mediaInfo.value ? mediaInfo.value.filename.replace(/\.[^.]+$/, '') : 'aligned'
    const blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${baseName}.${format}`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)

    ElMessage.success(`✅ ${t('subtitleAlign.exportSuccess')}`)
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  }
}

// ─────────────────────────────────────────────────────────────────
// 导出带字幕的视频/音频（软字幕封装 / 硬字幕烧录），逻辑见 useSubtitleEmbed.ts
// ─────────────────────────────────────────────────────────────────
const { embedding, embedSubtitleIntoMedia } = useSubtitleEmbed(mediaInfo, entries)

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

onBeforeUnmount(async () => {
  if (mediaInfo.value) {
    try {
      await fetch('/api/subtitle/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ media_id: mediaInfo.value.media_id }),
      })
    } catch {
      // 组件卸载时的清理失败不影响用户体验，静默忽略
    }
  }
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

.settings-form {
  margin-bottom: 8px;
}

.option-hint-icon {
  margin-left: 8px;
  color: #94a3b8;
  cursor: help;
}

.unit {
  margin-left: 10px;
  color: #64748b;
}

.align-progress {
  margin-top: 16px;
  max-width: 480px;
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
