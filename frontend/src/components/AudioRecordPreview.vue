<template>
  <div class="audio-record-preview">
    <div class="audio-record-preview-controls">
      <el-tooltip v-if="showRecordButton" :content="recording ? t('audioRecorder.stop') : t('audioRecorder.record')" placement="top">
        <el-button
          size="small"
          :type="recording ? 'danger' : 'default'"
          :disabled="disabled || converting"
          circle
          @click="toggleRecording"
        >
          <span v-if="!recording">🎙️</span>
          <span v-else class="recording-icon">⏹️</span>
        </el-button>
      </el-tooltip>

      <span v-if="recording" class="recording-timer">{{ formattedElapsed }}</span>
      <span v-else-if="converting" class="recording-timer converting-label">{{ t('audioRecorder.converting') }}</span>

      <el-tooltip :content="playing ? t('audioRecorder.pause') : t('audioRecorder.preview')" placement="top">
        <el-button
          size="small"
          circle
          :disabled="disabled || !hasPreviewSource || recording || converting"
          @click="togglePlayback"
        >
          <span v-if="!playing">▶️</span>
          <span v-else>⏸️</span>
        </el-button>
      </el-tooltip>

      <el-tooltip :content="t('audioRecorder.download')" placement="top">
        <el-button
          size="small"
          circle
          :disabled="disabled || !hasPreviewSource || recording || converting"
          @click="downloadAudio"
        >
          <span>⬇️</span>
        </el-button>
      </el-tooltip>
    </div>

    <!-- 简易进度条：仅在已有音频（已上传/已录制）且非录音中/转码中时展开
         显示，纯 div + 原生 <audio> 事件驱动，不使用 <audio controls>
         （原生控件横向占用空间较大，放不进这种紧凑的按钮行布局）。 -->
    <div v-if="hasPreviewSource && !recording && !converting" class="progress-row">
      <span class="progress-time">{{ formattedCurrentTime }}</span>
      <div
        ref="progressTrackRef"
        class="progress-track"
        :class="{ disabled: disabled }"
        @mousedown="onProgressMouseDown"
      >
        <div class="progress-fill" :style="{ width: progressPercent + '%' }" />
        <div class="progress-thumb" :style="{ left: progressPercent + '%' }" />
      </div>
      <span class="progress-time">{{ formattedDuration }}</span>
    </div>

    <audio
      ref="audioElRef"
      style="display: none"
      @ended="onPlaybackEnded"
      @pause="onPlaybackPaused"
      @timeupdate="onTimeUpdate"
      @loadedmetadata="onLoadedMetadata"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAppLocale } from '../i18n'

const { t } = useAppLocale()

// ─────────────────────────────────────────────────────────────────
// 通用"录音 + 播放预览 + 时间轴 + 下载"小组件，供各个"导入音频"上传区
// 旁边复用（MFAProcessor.vue 的音频跟读/字幕跟读、DialogueBatch.vue 每个
// 对话框、SubtitleRecognizer.vue、SubtitleEditor.vue）。
//
// 设计上刻意保持"哑"（dumb component）：本组件不知道父组件把音频存到
// 哪个 ref、也不知道父组件的上传/提交逻辑——它只做三件事：
//   1. 录音：用 MediaRecorder 录制麦克风输入，停止后把浏览器原生编码
//      （webm/ogg/mp4，取决于浏览器，没有浏览器能直接录出 wav）用
//      Web Audio API 解码并重新编码为真正的 wav（PCM），再打包成标准
//      File 对象，通过 'recorded' 事件交给父组件，父组件按各自现有的
//      handleAudioSelect / handleFileSelect 等逻辑处理，和用户从本地选择
//      文件走的是完全相同的代码路径。转码期间显示"转换中"提示，转码
//      失败时（极端情况）回退使用原始录音，保证功能可用性优先。
//   2. 播放预览 + 简易时间轴：优先预览父组件传入的 currentFile（已上传/
//      已选择的音频），没有 currentFile 时依次退化为"刚刚录制但父组件
//      还没来得及回填"的本地录音结果、或父组件传入的 sourceUrl（服务端
//      已上传媒体的播放地址，用于 SubtitleRecognizer.vue /
//      SubtitleEditor.vue 这类"文件已经上传、浏览器本地不再持有原始
//      File 引用"的场景）。时间轴是纯 div 进度条（当前时间 / 总时长 +
//      可拖动的滑块），不使用原生 <audio controls>——那个横向占用空间
//      较大，放不进这种紧凑的按钮行布局。
//   3. 下载：把当前预览源（currentFile 优先，其次刚录制的 blob，最后
//      sourceUrl）保存为本地文件，文件名沿用 currentFile.name（File
//      自带文件名）、录音时生成的文件名，或父组件提供的 downloadFileName
//      （sourceUrl 模式下无法从 URL 可靠解析出原始文件名）。
// ─────────────────────────────────────────────────────────────────

const props = withDefaults(defineProps<{
  // 当前已选择/已上传的音频文件（用于播放预览）。父组件传入 null 表示
  // 尚无本地 File 可用。
  currentFile: File | Blob | null
  // 服务端已上传媒体的可播放 URL（例如 SubtitleRecognizer.vue /
  // SubtitleEditor.vue 上传成功后 mediaInfo.play_url）。当 currentFile
  // 为 null 且提供了 sourceUrl 时，播放预览 / 下载改为直接使用这个 URL，
  // 不再依赖本地 File 对象——用于"文件已经上传到服务器，浏览器本地不再
  // 持有原始 File 引用"的场景（上传接口通常只返回一个 URL，不会把
  // File 对象原样传回）。currentFile 和 sourceUrl 同时提供时，
  // currentFile 优先。
  sourceUrl?: string | null
  // 下载时使用的文件名：仅在使用 sourceUrl 时需要（currentFile 是 File
  // 对象自带 name，可以直接取用；sourceUrl 只是个 URL，无法从中可靠地
  // 解析出原始文件名，需要父组件显式提供，例如 mediaInfo.filename）。
  downloadFileName?: string
  // 是否显示录音按钮。默认 true；SubtitleRecognizer.vue /
  // SubtitleEditor.vue 上传成功后（mediaInfo 存在）改用 sourceUrl 模式时
  // 传 false 隐藏录音按钮——重新录音在那两个场景里应该走"重新选择文件"
  // 的替换流程，而不是在已上传状态下静默录一段新音频、却不触发重新
  // 上传，导致预览的是新录音、但实际参与识别/编辑的仍是服务端旧文件。
  showRecordButton?: boolean
  // 外部禁用（例如正在处理中）时，录音和播放按钮都禁用。
  disabled?: boolean
  // 录音产物的文件名（不含扩展名），默认使用 i18n 的"录音"。
  fileNamePrefix?: string
}>(), {
  sourceUrl: null,
  downloadFileName: '',
  showRecordButton: true,
  disabled: false,
  fileNamePrefix: '',
})

const emit = defineEmits<{
  (e: 'recorded', file: File): void
}>()

const recording = ref(false)
const converting = ref(false)
const playing = ref(false)
const elapsedMs = ref(0)

// 播放进度状态：durationSec 在 <audio> 触发 loadedmetadata 之前是 0，
// durationReady 用来在此之前禁用进度条拖动（避免除以 0 / 拖到无意义的
// 位置）。currentTimeSec 由 timeupdate 事件驱动，拖动进度条时会临时脱离
// timeupdate 的驱动、直接跟手指/鼠标位置走（见 onProgressMouseDown）。
const durationSec = ref(0)
const currentTimeSec = ref(0)
const durationReady = computed(() => durationSec.value > 0 && Number.isFinite(durationSec.value))

// 录音刚完成、父组件尚未通过 currentFile 传回来之前的本地兜底预览源，
// 避免"录完马上点预览却因为 currentFile 还是旧值/null 而听不到刚录音频"
// 的时序问题。一旦父组件更新了 currentFile（watch 命中），就清空该值，
// 交回给 currentFile 作为唯一数据源。
const justRecordedBlob = ref<Blob | null>(null)
// 与 justRecordedBlob 配套的文件名（录音时生成），供下载按钮在没有
// currentFile 时使用；currentFile 存在时优先使用 currentFile.name。
const justRecordedFileName = ref('')

const hasPreviewSource = computed(() => !!props.currentFile || !!justRecordedBlob.value || !!props.sourceUrl)

// 统一解析出"当前应该预览/下载哪个 File|Blob 源"，currentFile 优先于
// 本地兜底的 justRecordedBlob；两者都没有时返回 null（此时如果
// hasPreviewSource 仍为 true，说明走的是 sourceUrl 模式）。
const resolvedFileSource = computed<File | Blob | null>(() => props.currentFile || justRecordedBlob.value)

const formattedElapsed = computed(() => {
  const totalSec = Math.floor(elapsedMs.value / 1000)
  const m = Math.floor(totalSec / 60).toString().padStart(2, '0')
  const s = (totalSec % 60).toString().padStart(2, '0')
  return `${m}:${s}`
})

const formatSeconds = (sec: number) => {
  if (!Number.isFinite(sec) || sec < 0) sec = 0
  const totalSec = Math.floor(sec)
  const m = Math.floor(totalSec / 60).toString().padStart(2, '0')
  const s = (totalSec % 60).toString().padStart(2, '0')
  return `${m}:${s}`
}

const formattedCurrentTime = computed(() => formatSeconds(currentTimeSec.value))
const formattedDuration = computed(() => formatSeconds(durationSec.value))

const progressPercent = computed(() => {
  if (!durationReady.value) return 0
  return Math.min(100, Math.max(0, (currentTimeSec.value / durationSec.value) * 100))
})

let mediaRecorder: MediaRecorder | null = null
let mediaStream: MediaStream | null = null
let recordedChunks: BlobPart[] = []
let timerHandle: ReturnType<typeof setInterval> | null = null
let recordStartedAt = 0

const audioElRef = ref<HTMLAudioElement | null>(null)
const progressTrackRef = ref<HTMLDivElement | null>(null)
// 当前 <audio> 元素正在播放的 objectURL，播放结束/切换前必须显式
// revokeObjectURL，否则每次预览都会泄漏一个 Blob URL。
let currentObjectUrl: string | null = null

const stopTimer = () => {
  if (timerHandle !== null) {
    clearInterval(timerHandle)
    timerHandle = null
  }
}

const cleanupStream = () => {
  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop())
    mediaStream = null
  }
}

// ── 录音结果重新编码为真正的 WAV（PCM）──────────────────────────────────
// MediaRecorder 规范本身不包含 wav 这个输出格式：Chrome/Edge 只能录制
// webm（Opus），Firefox 能录 ogg，没有浏览器能直接录出 wav。为了让"录音"
// 产物统一是普适性最好、后端处理链路最不容易出岔子的 wav，这里在录音
// 结束后用 Web Audio API 把 webm/ogg 解码成 PCM 样本，再手工按 WAV
// 容器格式（RIFF header + 16-bit PCM data）重新打包——所有主流浏览器都
// 支持 AudioContext.decodeAudioData，因此这个转换步骤不依赖任何特定
// 浏览器的编码器支持情况，结果稳定可靠。
const audioBufferToWavBlob = (buffer: AudioBuffer): Blob => {
  const numChannels = buffer.numberOfChannels
  const sampleRate = buffer.sampleRate
  const numFrames = buffer.length
  const bytesPerSample = 2 // 16-bit PCM
  const blockAlign = numChannels * bytesPerSample
  const dataSize = numFrames * blockAlign
  const headerSize = 44
  const arrayBuffer = new ArrayBuffer(headerSize + dataSize)
  const view = new DataView(arrayBuffer)

  const writeString = (offset: number, str: string) => {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i))
  }

  // RIFF header
  writeString(0, 'RIFF')
  view.setUint32(4, 36 + dataSize, true)
  writeString(8, 'WAVE')
  // fmt subchunk
  writeString(12, 'fmt ')
  view.setUint32(16, 16, true) // subchunk1 size (PCM)
  view.setUint16(20, 1, true) // audio format = 1 (PCM)
  view.setUint16(22, numChannels, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * blockAlign, true) // byte rate
  view.setUint16(32, blockAlign, true)
  view.setUint16(34, bytesPerSample * 8, true) // bits per sample
  // data subchunk
  writeString(36, 'data')
  view.setUint32(40, dataSize, true)

  // 交织各声道样本，float [-1, 1] → 16-bit signed PCM。
  const channelData: Float32Array[] = []
  for (let ch = 0; ch < numChannels; ch++) {
    channelData.push(buffer.getChannelData(ch))
  }
  let offset = headerSize
  for (let frame = 0; frame < numFrames; frame++) {
    for (let ch = 0; ch < numChannels; ch++) {
      const sample = Math.max(-1, Math.min(1, channelData[ch][frame]))
      const intSample = sample < 0 ? sample * 0x8000 : sample * 0x7fff
      view.setInt16(offset, intSample, true)
      offset += bytesPerSample
    }
  }

  return new Blob([arrayBuffer], { type: 'audio/wav' })
}

// 把 MediaRecorder 产出的原始 blob（webm/ogg/mp4，取决于浏览器）解码并
// 重新编码为 wav。解码失败时（极端边缘情况，例如浏览器不支持
// decodeAudioData 处理该编码）返回 null，调用方应回退使用原始 blob，
// 保证录音功能本身不会因为转码失败而彻底不可用。
const convertToWav = async (blob: Blob): Promise<Blob | null> => {
  try {
    const arrayBuffer = await blob.arrayBuffer()
    // Safari 仍然只暴露 webkitAudioContext，做个兼容兜底。
    const AudioContextCtor = window.AudioContext || (window as any).webkitAudioContext
    const audioContext = new AudioContextCtor()
    try {
      const audioBuffer = await audioContext.decodeAudioData(arrayBuffer)
      return audioBufferToWavBlob(audioBuffer)
    } finally {
      // decode 完成后立刻关闭，避免每次录音都新开一个 AudioContext、
      // 长期不释放导致浏览器"太多 AudioContext"的警告或资源浪费。
      audioContext.close().catch(() => { /* 关闭失败不影响功能，忽略 */ })
    }
  } catch (e) {
    return null
  }
}

const toggleRecording = async () => {
  if (recording.value) {
    mediaRecorder?.stop()
    return
  }

  // 已有音频（上传或之前录制）时，先确认是否要覆盖，避免误触丢失已选文件。
  if (hasPreviewSource.value) {
    try {
      await ElMessageBox.confirm(t('audioRecorder.replaceConfirm'), '', { type: 'warning' })
    } catch {
      return
    }
  }

  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true })
  } catch (e) {
    ElMessage.error(t('audioRecorder.micError'))
    return
  }

  recordedChunks = []
  // 优先使用浏览器原生支持的编码，避免 MediaRecorder 因不支持的
  // mimeType 直接抛错；不同浏览器支持的候选项不同，按常见程度依次尝试。
  const candidateMimeTypes = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4',
  ]
  const mimeType = candidateMimeTypes.find((type) => MediaRecorder.isTypeSupported?.(type)) || ''

  try {
    mediaRecorder = mimeType ? new MediaRecorder(mediaStream, { mimeType }) : new MediaRecorder(mediaStream)
  } catch (e) {
    ElMessage.error(t('audioRecorder.micError'))
    cleanupStream()
    return
  }

  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) recordedChunks.push(e.data)
  }

  mediaRecorder.onstop = async () => {
    stopTimer()
    cleanupStream()
    recording.value = false
    converting.value = true

    const rawBlobType = mediaRecorder?.mimeType || mimeType || 'audio/webm'
    const rawBlob = new Blob(recordedChunks, { type: rawBlobType })
    recordedChunks = []

    // 尝试把浏览器原生编码（webm/ogg/mp4，没有浏览器能直接录出 wav）
    // 重新编码为真正的 wav；转码失败时（极端情况）回退使用原始录音，
    // 保证功能可用性优先于格式统一。
    const wavBlob = await convertToWav(rawBlob)
    converting.value = false
    const blob = wavBlob || rawBlob
    const blobType = wavBlob ? 'audio/wav' : rawBlobType
    const ext = wavBlob ? 'wav' : (rawBlobType.includes('ogg') ? 'ogg' : rawBlobType.includes('mp4') ? 'm4a' : 'webm')

    const baseName = props.fileNamePrefix || t('audioRecorder.recordedFileName')
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-')
    const fileName = `${baseName}-${timestamp}.${ext}`
    const file = new File([blob], fileName, { type: blobType })

    justRecordedBlob.value = blob
    justRecordedFileName.value = fileName
    // 新录音会替换掉旧的播放进度状态：下一次预览要从头播放，而不是沿用
    // 上一段音频残留的 currentTime / duration。
    durationSec.value = 0
    currentTimeSec.value = 0
    emit('recorded', file)
  }

  mediaRecorder.start()
  recording.value = true
  recordStartedAt = Date.now()
  elapsedMs.value = 0
  stopTimer()
  timerHandle = setInterval(() => {
    elapsedMs.value = Date.now() - recordStartedAt
  }, 200)
}

const stopPlayback = () => {
  const el = audioElRef.value
  if (el && !el.paused) el.pause()
  playing.value = false
}

// 当前 <audio> 加载的是哪个数据源，用来判断"重新点击播放"时是应该
// resume（同一个源，只是之前暂停了）还是需要重新加载并从头播放（源已经
// 变化：换了新文件、新录音，或是刚从"未加载过"的状态第一次播放）。
// File/Blob 直接存引用比较；sourceUrl 模式下存字符串本身即可（同一个
// URL 字符串视为同一个源）。
let loadedSource: File | Blob | string | null = null

// 取得"当前应该加载/播放/下载"的源标识：优先 File|Blob，其次 sourceUrl；
// 用于和 loadedSource 比较，判断是否需要重新加载。
const currentSourceIdentity = (): File | Blob | string | null => {
  return resolvedFileSource.value || props.sourceUrl || null
}

const loadAndPlay = (source: File | Blob | string) => {
  const el = audioElRef.value
  if (!el) return

  if (typeof source === 'string') {
    // sourceUrl 模式：直接用服务端 URL，不需要 createObjectURL；上一次
    // 如果是 File/Blob 模式遗留的 objectURL 仍要显式释放，避免泄漏。
    if (currentObjectUrl) {
      URL.revokeObjectURL(currentObjectUrl)
      currentObjectUrl = null
    }
    el.src = source
  } else {
    if (currentObjectUrl) {
      URL.revokeObjectURL(currentObjectUrl)
      currentObjectUrl = null
    }
    currentObjectUrl = URL.createObjectURL(source)
    el.src = currentObjectUrl
  }
  loadedSource = source

  el.currentTime = 0
  currentTimeSec.value = 0
  el.play().then(() => {
    playing.value = true
  }).catch(() => {
    playing.value = false
  })
}

const togglePlayback = () => {
  if (playing.value) {
    stopPlayback()
    return
  }
  if (!hasPreviewSource.value) {
    ElMessage.info(t('audioRecorder.noAudioHint'))
    return
  }

  const source = currentSourceIdentity()
  if (!source) return

  const el = audioElRef.value
  if (!el) return

  // 同一个源、且之前只是暂停（el.src 已经指向它）→ 直接从暂停位置继续，
  // 不重新加载 / 不把进度条弹回 0。
  if (source === loadedSource && el.src) {
    el.play().then(() => {
      playing.value = true
    }).catch(() => {
      playing.value = false
    })
    return
  }

  loadAndPlay(source)
}

const onPlaybackEnded = () => {
  playing.value = false
  currentTimeSec.value = 0
}

const onPlaybackPaused = () => {
  playing.value = false
}

const onTimeUpdate = () => {
  const el = audioElRef.value
  if (!el) return
  currentTimeSec.value = el.currentTime
}

const onLoadedMetadata = () => {
  const el = audioElRef.value
  if (!el) return
  // 部分浏览器/编码（尤其是某些 webm 录音）在 loadedmetadata 阶段
  // duration 会是 Infinity，需要等 durationchange 之后才有正确值；这里
  // 用 Number.isFinite 兜底，durationReady 计算属性已经处理了这种情况，
  // 拖动进度条前会先判断 durationReady。
  durationSec.value = el.duration
}

// ── 进度条拖动 seek ──────────────────────────────────────────────────
// 纯鼠标事件实现（而非 <input type="range">），方便完全自定义样式，和
// 项目里其它自绘控件（如 SubtitleWaveform.vue 的时间轴）风格一致。
const seekToClientX = (clientX: number) => {
  const track = progressTrackRef.value
  const el = audioElRef.value
  if (!track || !el || !durationReady.value) return
  const rect = track.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
  const newTime = ratio * durationSec.value
  el.currentTime = newTime
  currentTimeSec.value = newTime
}

const onProgressMouseMove = (e: MouseEvent) => {
  seekToClientX(e.clientX)
}

const onProgressMouseUp = () => {
  window.removeEventListener('mousemove', onProgressMouseMove)
  window.removeEventListener('mouseup', onProgressMouseUp)
}

const onProgressMouseDown = async (e: MouseEvent) => {
  if (disabledOrNotReady()) return
  const clientX = e.clientX
  // 拖动进度条前，若当前预览源还没加载进 <audio>（例如还没点过播放键），
  // 先加载但不自动播放，只是为了让 seek 生效；loadedmetadata 是异步的，
  // 必须等它触发、durationReady 变 true 之后再 seek，否则 duration 还是
  // 0，seekToClientX 会因为 !durationReady.value 直接跳过、拖动第一次
  // 毫无反应。
  await ensureLoadedForSeek()
  seekToClientX(clientX)
  window.addEventListener('mousemove', onProgressMouseMove)
  window.addEventListener('mouseup', onProgressMouseUp)
}

const disabledOrNotReady = () => props.disabled || !hasPreviewSource.value

// 拖动进度条时如果 <audio> 还没加载过任何源（loadedSource 为 null），
// 先静默加载一次（不调用 play()），只是为了让 duration/seek 生效，避免
// 用户第一次交互就是拖进度条、却因为 el.src 还是空的而毫无反应。返回
// 的 Promise 在 loadedmetadata 触发（或已经加载过、无需等待）后 resolve，
// 调用方 await 它以确保 durationReady 已经就绪再执行 seek。
const ensureLoadedForSeek = (): Promise<void> => {
  const source = currentSourceIdentity()
  const el = audioElRef.value
  if (!source || !el) return Promise.resolve()
  if (source === loadedSource && el.src) return Promise.resolve()

  if (typeof source === 'string') {
    if (currentObjectUrl) {
      URL.revokeObjectURL(currentObjectUrl)
      currentObjectUrl = null
    }
    loadedSource = source
    currentTimeSec.value = 0
    durationSec.value = 0
    return new Promise((resolve) => {
      const onceLoaded = () => {
        el.removeEventListener('loadedmetadata', onceLoaded)
        resolve()
      }
      el.addEventListener('loadedmetadata', onceLoaded)
      el.src = source
    })
  }

  if (currentObjectUrl) {
    URL.revokeObjectURL(currentObjectUrl)
    currentObjectUrl = null
  }
  currentObjectUrl = URL.createObjectURL(source)
  loadedSource = source
  currentTimeSec.value = 0
  durationSec.value = 0

  return new Promise((resolve) => {
    const onceLoaded = () => {
      el.removeEventListener('loadedmetadata', onceLoaded)
      resolve()
    }
    el.addEventListener('loadedmetadata', onceLoaded)
    el.src = currentObjectUrl as string
  })
}

// ── 下载 ──────────────────────────────────────────────────────────────
const downloadAudio = () => {
  const fileSource = resolvedFileSource.value

  // 纯 URL 模式（已上传到服务器，浏览器本地没有 File 引用）：直接用
  // <a download> 指向服务端地址触发下载，浏览器会按正常的下载流程处理
  // （同源或允许跨源下载的资源）；无需 createObjectURL。
  if (!fileSource && props.sourceUrl) {
    const a = document.createElement('a')
    a.href = props.sourceUrl
    a.download = props.downloadFileName || t('audioRecorder.recordedFileName')
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    return
  }

  if (!fileSource) {
    ElMessage.info(t('audioRecorder.noAudioHint'))
    return
  }
  const fileName = fileSource instanceof File
    ? fileSource.name
    : (justRecordedFileName.value || t('audioRecorder.recordedFileName'))

  const url = URL.createObjectURL(fileSource)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // 下载用的 objectURL 是独立创建的一次性对象，点击触发下载后即可立刻
  // 回收，不需要等待——与预览播放复用的 currentObjectUrl 生命周期无关。
  URL.revokeObjectURL(url)
}

// 父组件的 currentFile 一旦更新（例如上传/录音完成后回填到 formData），
// 说明父组件已经接管了"当前音频"这个状态，本地兜底的 justRecordedBlob
// 不再需要，清空以让 currentFile 成为唯一数据源；同时若正在播放旧音频，
// 停止播放，避免继续播放已被替换/清空的内容。注意：不在这里重置
// loadedSource / durationSec / currentTimeSec —— 如果新的 currentFile
// 恰好就是刚才那次录音产生的同一个 File（父组件把 recorded 事件的 file
// 原样存回来的常见情况），保留已加载的进度信息，避免用户点完预览、
// 进度条已经在动，父组件一回填 currentFile 就把进度条弹回 0 的观感跳动；
// 源不同的情况会在下次 togglePlayback / seek 时由 loadedSource 比较
// 自然识别为"新源"并重新加载，不需要在这里强制清空。
watch(() => props.currentFile, (newFile) => {
  justRecordedBlob.value = null
  justRecordedFileName.value = ''
  if (newFile !== loadedSource) {
    stopPlayback()
  }
})

// sourceUrl 变化（例如 SubtitleRecognizer.vue/SubtitleEditor.vue 重新
// 上传了一个新文件，mediaInfo.play_url 换成了新地址）时同样需要停止
// 正在播放的旧内容，避免继续播放已经被替换掉的媒体。逻辑与上面的
// currentFile watcher 对称。
watch(() => props.sourceUrl, (newUrl) => {
  if (newUrl !== loadedSource) {
    stopPlayback()
  }
})

onBeforeUnmount(() => {
  stopTimer()
  cleanupStream()
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    try { mediaRecorder.stop() } catch { /* 组件卸载时忽略 */ }
  }
  if (currentObjectUrl) {
    URL.revokeObjectURL(currentObjectUrl)
    currentObjectUrl = null
  }
  // 防止组件在拖动进度条过程中被卸载（例如父组件切换 inputMode 导致
  // v-if 整体销毁），残留的 window 级监听器会一直存在并持有闭包引用。
  window.removeEventListener('mousemove', onProgressMouseMove)
  window.removeEventListener('mouseup', onProgressMouseUp)
})
</script>

<style scoped>
.audio-record-preview {
  display: inline-flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
  margin-left: 8px;
  vertical-align: middle;
}

.audio-record-preview-controls {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.recording-timer {
  font-size: 12px;
  color: #f56c6c;
  font-variant-numeric: tabular-nums;
  min-width: 34px;
}

.converting-label {
  color: #909399;
  min-width: auto;
  white-space: nowrap;
}

.recording-icon {
  animation: audio-record-pulse 1.2s ease-in-out infinite;
}

@keyframes audio-record-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}

/* 简易播放进度条：当前时间 / 拖动轨道 / 总时长，三段一行。 */
.progress-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 160px;
}

.progress-time {
  font-size: 11px;
  color: #909399;
  font-variant-numeric: tabular-nums;
  min-width: 32px;
  text-align: center;
  flex-shrink: 0;
}

.progress-track {
  position: relative;
  flex: 1;
  height: 4px;
  border-radius: 2px;
  background: #e4e7ed;
  cursor: pointer;
}

.progress-track.disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.progress-fill {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  border-radius: 2px;
  background: #409eff;
  pointer-events: none;
}

.progress-thumb {
  position: absolute;
  top: 50%;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #409eff;
  transform: translate(-50%, -50%);
  pointer-events: none;
  box-shadow: 0 0 0 2px #fff;
}
</style>
