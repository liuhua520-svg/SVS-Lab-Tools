<template>
  <div class="waveform-root">
    <div class="waveform-toolbar">
      <el-button size="small" circle @click="zoomOut" :disabled="pxPerSec <= MIN_PX_PER_SEC">
        <span class="zoom-icon">−</span>
      </el-button>
      <span class="zoom-label">{{ Math.round(pxPerSec) }} px/s</span>
      <el-button size="small" circle @click="zoomIn" :disabled="pxPerSec >= MAX_PX_PER_SEC">
        <span class="zoom-icon">＋</span>
      </el-button>
      <el-button size="small" @click="fitToWidth">{{ t('subtitle.waveformFit') }}</el-button>
      <span class="waveform-hint">{{ t('subtitle.waveformHint') }}</span>
    </div>

    <div ref="scrollRef" class="waveform-scroll" @scroll="onScroll">
      <div
        class="waveform-inner"
        :style="{ width: totalWidth + 'px' }"
        @mousedown="onTrackMouseDown"
        @dblclick="onTrackDblClick"
      >
        <!-- 波形画布：故意不用 Vue 的 :width/:height 响应式绑定去控制画布
             位图尺寸——canvas 的 width/height 属性一旦被赋值就会清空位图，
             而 Vue 的 DOM patch 发生在下一个 tick，会和 redraw() 的同步
             绘制产生时序竞争（详见脚本区 redraw() 上方的说明）。改为完全
             由 redraw() 在同一次调用里同步设置尺寸并绘制。 -->
        <canvas
          ref="canvasRef"
          class="waveform-canvas"
        />

        <!-- 时间刻度 -->
        <div class="waveform-ruler">
          <span
            v-for="tick in rulerTicks"
            :key="tick.sec"
            class="ruler-tick"
            :style="{ left: tick.x + 'px' }"
          >{{ tick.label }}</span>
        </div>

        <!-- 字幕区块 -->
        <div
          v-for="(en, idx) in entries"
          :key="en._uid"
          class="subtitle-region"
          :class="{ active: idx === activeIndex }"
          :style="regionStyle(en)"
          @mousedown.stop="onRegionMouseDown($event, idx)"
        >
          <div
            class="region-handle region-handle-left"
            @mousedown.stop="onHandleMouseDown($event, idx, 'start')"
          />
          <span class="region-text" :title="en.text">{{ en.text || t('subtitle.waveformEmptyText') }}</span>
          <div
            class="region-handle region-handle-right"
            @mousedown.stop="onHandleMouseDown($event, idx, 'end')"
          />
        </div>

        <!-- 播放头 -->
        <div class="playhead" :style="{ left: currentTime * pxPerSec + 'px' }" />
      </div>
    </div>

    <div v-if="loadingWaveform" class="waveform-loading">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>{{ t('subtitle.waveformLoading') }}</span>
    </div>
    <div v-else-if="waveformError" class="waveform-error">
      ⚠️ {{ waveformError }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import { useAppLocale } from '../i18n'

const { t } = useAppLocale()

// ─────────────────────────────────────────────────────────────────
// 与外部字幕编辑区共用的条目结构：只依赖 start/end/text + _uid，
// 不关心 _startText/_endText 这些格式化字段（由父组件在 update 事件
// 里自行同步）。
// ─────────────────────────────────────────────────────────────────
interface WaveformEntry {
  _uid: number
  start: number
  end: number
  text: string
  [key: string]: any
}

const props = defineProps<{
  entries: WaveformEntry[]
  mediaUrl: string | null
  duration: number
  currentTime: number
  activeUid?: number | null
}>()

const emit = defineEmits<{
  (e: 'seek', time: number): void
  (e: 'update-entry', payload: { uid: number; start?: number; end?: number }): void
  (e: 'add-entry', time: number): void
}>()

// ─────────────────────────────────────────────────────────────────
// 缩放与滚动
// ─────────────────────────────────────────────────────────────────
const MIN_PX_PER_SEC = 10
const MAX_PX_PER_SEC = 400
const pxPerSec = ref(60)
const scrollRef = ref<HTMLDivElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)

const totalWidth = computed(() => Math.max(1, Math.ceil((props.duration || 0) * pxPerSec.value)))

const zoomIn = () => {
  pxPerSec.value = Math.min(MAX_PX_PER_SEC, Math.round(pxPerSec.value * 1.5))
}
const zoomOut = () => {
  pxPerSec.value = Math.max(MIN_PX_PER_SEC, Math.round(pxPerSec.value / 1.5))
}
const fitToWidth = () => {
  const el = scrollRef.value
  if (!el || !props.duration) return
  const availWidth = el.clientWidth || 800
  pxPerSec.value = Math.min(MAX_PX_PER_SEC, Math.max(MIN_PX_PER_SEC, availWidth / props.duration))
}

const onScroll = () => {
  // 目前仅依赖浏览器原生滚动，无需额外处理；预留扩展点（例如虚拟渲染）。
}

// ─────────────────────────────────────────────────────────────────
// 波形绘制：用 Web Audio API 解码整份媒体音频轨道，按峰值采样后画到
// canvas 上。不依赖任何第三方库（wavesurfer 等），媒体文件本身已经
// 能通过现有 <audio>/<video> 播放，这里只是额外解码一份用于可视化。
//
// 峰值采样分辨率固定，与当前缩放（pxPerSec）完全解耦：只在媒体
// 加载/切换时计算一次，缩放/适应宽度只改变绘制时的横向拉伸比例，不
// 重新采样、不重新请求音频。这样做是为了避开 canvas 元素一个容易踩坑
// 的特性——<canvas> 的 width/height 是"位图缓冲区尺寸"而不是普通
// CSS 属性，通过 Vue 的响应式 :width 绑定去改它，实际生效时机是下一次
// DOM patch（微任务之后），而不是当前这一行代码执行完就生效；如果像
// 之前那样"改 pxPerSec → 立刻 redraw()"，redraw 时读到的 canvas.width
// 还是旧值，画完之后 Vue 才把 width 属性更新到新值——而 HTML 规范规定
// 只要 width/height 属性发生赋值（哪怕数值相同）就会清空整个位图，
// 于是刚画上去的波形被这次"迟到"的属性更新清空，同时 CSS
// width:100% 又会把清空后的（或尺寸不对的）画布内容拉伸铺满容器，导致
// 波形要么消失、要么被拉伸得和字幕块的位置对不上。
//
// 解决方式：canvas 的位图尺寸完全由 redraw() 自己在同一个同步调用里
// 设置（不经过 Vue 的响应式属性绑定），确保"调整尺寸"和"画内容"是
// 原子操作，不存在时序空窗；同时 mediaUrl 的 watch 加上 immediate，
// 保证组件一挂载就会开始加载波形，而不是要等 mediaUrl 发生变化。
// ─────────────────────────────────────────────────────────────────
const loadingWaveform = ref(false)
const waveformError = ref('')
let peaks: Float32Array | null = null // 固定分辨率的峰值缓存，[-1,1] 幅度，不随缩放变化
let peaksDuration = 0 // 采样时对应的音频总时长（秒），redraw() 用它把峰值索引换算成时间位置
let audioCtx: AudioContext | null = null

// 固定采样分辨率：每秒 100 个采样点，介于 1000～20000 之间。短音频也能
// 保证基础清晰度，长音频不会占用过多内存/绘制时间；配合 redraw() 里
// 按时间比例映射到当前像素宽度的方式，缩放时不需要重新采样。
const PEAK_RESOLUTION_PER_SEC = 100
const MIN_PEAK_POINTS = 1000
const MAX_PEAK_POINTS = 20000

const loadWaveform = async (url: string | null) => {
  peaks = null
  peaksDuration = 0
  waveformError.value = ''
  if (!url) {
    redraw()
    return
  }
  loadingWaveform.value = true
  try {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const arrayBuf = await res.arrayBuffer()
    if (!audioCtx) {
      const Ctor = window.AudioContext || (window as any).webkitAudioContext
      audioCtx = new Ctor()
    }
    const audioBuf = await audioCtx.decodeAudioData(arrayBuf.slice(0))
    const targetPoints = Math.min(
      MAX_PEAK_POINTS,
      Math.max(MIN_PEAK_POINTS, Math.round(audioBuf.duration * PEAK_RESOLUTION_PER_SEC)),
    )
    peaks = computePeaks(audioBuf, targetPoints)
    peaksDuration = audioBuf.duration
    redraw()
  } catch (e: any) {
    waveformError.value = e?.message ? `${t('subtitle.waveformDecodeFailed')}: ${e.message}` : t('subtitle.waveformDecodeFailed')
  } finally {
    loadingWaveform.value = false
  }
}

const computePeaks = (audioBuf: AudioBuffer, targetPoints: number): Float32Array => {
  const channels = audioBuf.numberOfChannels
  const len = audioBuf.length
  const points = Math.max(1, targetPoints)
  const blockSize = Math.max(1, Math.floor(len / points))
  const out = new Float32Array(points)
  const channelData: Float32Array[] = []
  for (let c = 0; c < channels; c++) channelData.push(audioBuf.getChannelData(c))

  for (let i = 0; i < points; i++) {
    const start = i * blockSize
    const end = Math.min(len, start + blockSize)
    let peak = 0
    for (let c = 0; c < channels; c++) {
      const data = channelData[c]
      for (let j = start; j < end; j++) {
        const v = Math.abs(data[j])
        if (v > peak) peak = v
      }
    }
    out[i] = peak
  }
  return out
}

// 同步地把 canvas 的位图尺寸调整到当前 totalWidth，再立即画内容——两步
// 在同一次调用里完成，不经过 Vue 的响应式 DOM patch，杜绝"resize 和绘制
// 不同步"的问题（原理见上方大段注释）。
const redraw = () => {
  const canvas = canvasRef.value
  if (!canvas) return
  const w = totalWidth.value
  const h = 88
  if (canvas.width !== w) canvas.width = w
  if (canvas.height !== h) canvas.height = h
  canvas.style.width = `${w}px`

  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.clearRect(0, 0, w, h)

  if (!peaks || !peaks.length || !peaksDuration) return

  // 按时间比例把固定分辨率的峰值缓存映射到当前 totalWidth 上，与
  // pxPerSec 无关——这样缩放只是重新拉伸同一份峰值数据，不需要重新
  // 采样，也就不存在"resample 完成前画面暂时不同步"的等待窗口。
  const mid = h / 2
  const pxPerPeak = w / peaks.length
  ctx.fillStyle = '#8e9dff'
  ctx.beginPath()
  for (let i = 0; i < peaks.length; i++) {
    const amp = peaks[i]
    const barHeight = Math.max(1, amp * (h - 8))
    const x = i * pxPerPeak
    ctx.rect(x, mid - barHeight / 2, Math.max(1, pxPerPeak * 0.8), barHeight)
  }
  ctx.fill()
}

watch(() => props.mediaUrl, (url) => {
  loadWaveform(url)
}, { immediate: true })

// 音频总时长确定后（例如 mediaInfo.duration 在挂载后才补齐）重新采样一次；
// 单纯的缩放/适应宽度不会触发这里，只会走下面的 pxPerSec watch 直接重绘。
watch(() => props.duration, (dur, oldDur) => {
  if (dur && dur !== oldDur && props.mediaUrl) {
    loadWaveform(props.mediaUrl)
  } else {
    redraw()
  }
})

// 缩放只改变绘制时的拉伸比例，峰值数据本身不需要重新计算，redraw()
// 是纯同步操作，点击缩放按钮后波形立即跟着重绘，不再有暂时空白或
// 与字幕块错位的窗口期。
watch(pxPerSec, () => {
  redraw()
})

onMounted(() => {
  redraw() // 音频还没解码完时先把画布尺寸对齐 totalWidth，避免短暂使用浏览器默认的 300×150
})

onBeforeUnmount(() => {
  if (audioCtx) {
    audioCtx.close().catch(() => {})
    audioCtx = null
  }
})

// ─────────────────────────────────────────────────────────────────
// 时间刻度尺
// ─────────────────────────────────────────────────────────────────
const rulerTicks = computed(() => {
  const dur = props.duration || 0
  if (!dur) return []
  // 目标：刻度间隔约 80px，取一个"整齐"的秒数步长
  const targetSec = 80 / pxPerSec.value
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
  const step = steps.find((s) => s >= targetSec) || 600
  const ticks: { sec: number; x: number; label: string }[] = []
  for (let s = 0; s <= dur; s += step) {
    ticks.push({ sec: s, x: s * pxPerSec.value, label: formatTick(s) })
  }
  return ticks
})

const formatTick = (sec: number): string => {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

// ─────────────────────────────────────────────────────────────────
// 字幕区块：样式与拖拽交互
// ─────────────────────────────────────────────────────────────────
const activeIndex = computed(() => {
  if (props.activeUid == null) return -1
  return props.entries.findIndex((e) => e._uid === props.activeUid)
})

const regionStyle = (en: WaveformEntry) => ({
  left: `${en.start * pxPerSec.value}px`,
  width: `${Math.max(2, (en.end - en.start) * pxPerSec.value)}px`,
})

type DragMode = 'move' | 'start' | 'end'
interface DragState {
  mode: DragMode
  index: number
  startX: number
  origStart: number
  origEnd: number
}
let drag: DragState | null = null

const xToTime = (clientX: number): number => {
  const el = scrollRef.value
  if (!el) return 0
  const rect = el.getBoundingClientRect()
  const x = clientX - rect.left + el.scrollLeft
  return Math.max(0, x / pxPerSec.value)
}

const onRegionMouseDown = (evt: MouseEvent, index: number) => {
  const en = props.entries[index]
  drag = { mode: 'move', index, startX: evt.clientX, origStart: en.start, origEnd: en.end }
  window.addEventListener('mousemove', onDragMove)
  window.addEventListener('mouseup', onDragEnd)
}

const onHandleMouseDown = (evt: MouseEvent, index: number, which: 'start' | 'end') => {
  const en = props.entries[index]
  drag = { mode: which, index, startX: evt.clientX, origStart: en.start, origEnd: en.end }
  window.addEventListener('mousemove', onDragMove)
  window.addEventListener('mouseup', onDragEnd)
}

const onDragMove = (evt: MouseEvent) => {
  if (!drag) return
  const deltaSec = (evt.clientX - drag.startX) / pxPerSec.value
  const en = props.entries[drag.index]
  if (!en) return
  const prev = props.entries[drag.index - 1]
  const next = props.entries[drag.index + 1]

  if (drag.mode === 'move') {
    const dur = drag.origEnd - drag.origStart
    let newStart = drag.origStart + deltaSec
    const lowerBound = prev ? prev.end : 0
    const upperBound = next ? next.start - dur : (props.duration || Infinity) - dur
    newStart = Math.max(lowerBound, Math.min(upperBound, newStart))
    emit('update-entry', { uid: en._uid, start: newStart, end: newStart + dur })
  } else if (drag.mode === 'start') {
    let newStart = drag.origStart + deltaSec
    const lowerBound = prev ? prev.end : 0
    const upperBound = en.end - 0.05
    newStart = Math.max(lowerBound, Math.min(upperBound, newStart))
    emit('update-entry', { uid: en._uid, start: newStart })
  } else if (drag.mode === 'end') {
    let newEnd = drag.origEnd + deltaSec
    const lowerBound = en.start + 0.05
    const upperBound = next ? next.start : (props.duration || Infinity)
    newEnd = Math.max(lowerBound, Math.min(upperBound, newEnd))
    emit('update-entry', { uid: en._uid, end: newEnd })
  }
}

const onDragEnd = () => {
  drag = null
  window.removeEventListener('mousemove', onDragMove)
  window.removeEventListener('mouseup', onDragEnd)
}

// 点击空白轨道区域（非字幕块）→ 跳转播放头到该时间点
const onTrackMouseDown = (evt: MouseEvent) => {
  const target = evt.target as HTMLElement
  if (target.closest('.subtitle-region')) return
  const time = xToTime(evt.clientX)
  emit('seek', time)
}

// 双击空白轨道区域 → 在该时间点新增一条字幕（交给父组件决定默认时长/文本）
const onTrackDblClick = (evt: MouseEvent) => {
  const target = evt.target as HTMLElement
  if (target.closest('.subtitle-region')) return
  const time = xToTime(evt.clientX)
  emit('add-entry', time)
}

// 播放头跟随时自动滚动到可视范围内
watch(() => props.currentTime, (t) => {
  const el = scrollRef.value
  if (!el) return
  const x = t * pxPerSec.value
  if (x < el.scrollLeft || x > el.scrollLeft + el.clientWidth - 40) {
    el.scrollLeft = Math.max(0, x - el.clientWidth / 3)
  }
})

// activeUid 变化（例如点击列表行的"跳转"）时，把对应区块滚动到可视范围
watch(() => props.activeUid, async () => {
  await nextTick()
  const idx = activeIndex.value
  if (idx < 0) return
  const en = props.entries[idx]
  const el = scrollRef.value
  if (!el || !en) return
  const x = en.start * pxPerSec.value
  if (x < el.scrollLeft || x > el.scrollLeft + el.clientWidth - 80) {
    el.scrollLeft = Math.max(0, x - 60)
  }
})

defineExpose({ fitToWidth })
</script>

<style scoped>
.waveform-root {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fafbff;
  overflow: hidden;
}

.waveform-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid #e4e7ed;
  background: #f5f6fb;
}

.zoom-icon {
  font-weight: bold;
  line-height: 1;
}

.zoom-label {
  font-size: 12px;
  color: #909399;
  min-width: 56px;
  text-align: center;
}

.waveform-hint {
  margin-left: auto;
  font-size: 12px;
  color: #b0b3bf;
}

.waveform-scroll {
  overflow-x: auto;
  overflow-y: hidden;
  width: 100%;
}

.waveform-inner {
  position: relative;
  height: 128px;
  min-width: 100%;
  cursor: text;
  user-select: none;
}

.waveform-canvas {
  position: absolute;
  top: 0;
  left: 0;
  height: 88px;
  background: #1e2030;
  display: block;
}

.waveform-ruler {
  position: absolute;
  top: 88px;
  left: 0;
  right: 0;
  height: 18px;
  border-top: 1px solid #e4e7ed;
  background: #f0f1f7;
}

.ruler-tick {
  position: absolute;
  top: 2px;
  font-size: 10px;
  color: #909399;
  transform: translateX(-50%);
  white-space: nowrap;
}

.subtitle-region {
  position: absolute;
  top: 92px;
  height: 30px;
  background: rgba(103, 121, 255, 0.28);
  border: 1px solid #6779ff;
  border-radius: 4px;
  display: flex;
  align-items: center;
  overflow: hidden;
  cursor: grab;
}

.subtitle-region:hover {
  background: rgba(103, 121, 255, 0.4);
}

.subtitle-region.active {
  background: rgba(255, 145, 77, 0.35);
  border-color: #ff914d;
  z-index: 2;
}

.subtitle-region:active {
  cursor: grabbing;
}

.region-text {
  flex: 1;
  min-width: 0;
  padding: 0 6px;
  font-size: 12px;
  color: #2c2f4a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  pointer-events: none;
}

.region-handle {
  width: 6px;
  align-self: stretch;
  cursor: ew-resize;
  flex-shrink: 0;
}

.region-handle-left {
  border-radius: 4px 0 0 4px;
}

.region-handle-right {
  border-radius: 0 4px 4px 0;
}

.region-handle:hover {
  background: rgba(103, 121, 255, 0.6);
}

.playhead {
  position: absolute;
  top: 0;
  width: 2px;
  height: 110px;
  background: #f56c6c;
  pointer-events: none;
  z-index: 3;
}

.waveform-loading,
.waveform-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  font-size: 13px;
  color: #909399;
}

.waveform-error {
  color: #f56c6c;
}
</style>
