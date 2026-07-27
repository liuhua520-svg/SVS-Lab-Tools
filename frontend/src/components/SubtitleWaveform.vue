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
      <span v-if="selectedUids.size >= 2" class="multi-select-badge">
        {{ t('subtitle.waveformMultiSelectCount', { count: selectedUids.size }) }}
      </span>
      <el-tooltip :content="t('subtitle.waveformSplitHint')" placement="top">
        <el-button size="small" :disabled="activeIndex < 0 && !selectedUids.size" @click="splitActive">
          ✂️ {{ t('subtitle.waveformSplit') }}
        </el-button>
      </el-tooltip>
      <el-tooltip :content="t('subtitle.waveformDeleteHint')" placement="top">
        <el-button size="small" type="danger" plain :disabled="activeIndex < 0 && !selectedUids.size" @click="deleteActive">
          🗑️ {{ t('subtitle.waveformDelete') }}
        </el-button>
      </el-tooltip>
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
          :class="{ active: idx === activeIndex, editing: editingUid === en._uid, 'multi-selected': isSelected(en._uid) }"
          :style="regionStyle(en)"
          @mousedown.stop="onRegionMouseDown($event, idx)"
          @dblclick.stop="onRegionDblClick(en)"
        >
          <div
            class="region-handle region-handle-left"
            @mousedown.stop="onHandleMouseDown($event, idx, 'start')"
          />
          <input
            v-if="editingUid === en._uid"
            ref="editInputRef"
            v-model="editingText"
            class="region-edit-input"
            :class="{ wrap: shouldWrapEdit(en) }"
            @mousedown.stop
            @dblclick.stop
            @keydown.stop
            @keydown.enter.prevent="commitEdit"
            @keydown.esc.prevent="cancelEdit"
            @blur="commitEdit"
          />
          <span v-else class="region-text" :title="en.text">{{ en.text || t('subtitle.waveformEmptyText') }}</span>
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
  (e: 'select', uid: number | null): void
  (e: 'select-multi', uids: number[]): void
  (e: 'edit-text', payload: { uid: number; text: string }): void
  (e: 'delete-entry', uid: number): void
  (e: 'delete-entries', uids: number[]): void
  (e: 'split-entry', payload: { uid: number; at: number }): void
  (e: 'split-entries', payloads: Array<{ uid: number; at: number }>): void
  (e: 'drag-start'): void
  (e: 'drag-end'): void
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

// ─────────────────────────────────────────────────────────────────
// 多选：Ctrl/Cmd+click 逐个加选或取消，Shift+click 从"锚点"（最近一次
// 单击选中的块）到当前点击块之间做连续范围选择。activeUid（单选，由
// 父组件持有，驱动拖拽/字幕列表高亮等既有逻辑）始终指向"锚点"本身；
// selectedUids 是叠加的多选集合，两者独立维护——多选不影响单选相关的
// 拖拽手柄等行为，只影响批量删除/拆分与视觉高亮。
// 普通单击（不按修饰键）会清空多选集合，回到原有的单选行为。
// ─────────────────────────────────────────────────────────────────
const selectedUids = ref<Set<number>>(new Set())
let selectionAnchorIndex = -1 // Shift 范围选择的起点，普通/Ctrl 点击时更新

const isSelected = (uid: number) => selectedUids.value.has(uid)

const emitSelectMulti = () => {
  emit('select-multi', Array.from(selectedUids.value))
}

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

// 区分"单击选中"与"拖拽移动"：mousedown 时先只记录起点，真正开始拖拽
// （鼠标移动超过阈值）后才在 onDragMove 里第一次触发 update-entry；
// mouseup 时如果全程没有超过阈值，就当作一次单击，emit select。
const CLICK_DRAG_THRESHOLD = 3
let dragMoved = false

const onRegionMouseDown = (evt: MouseEvent, index: number) => {
  if (editingUid.value !== null) return // 正在内联编辑文字时，不响应块的选中/拖拽
  const en = props.entries[index]

  // Ctrl/Cmd+click：不进入拖拽，直接切换该块的多选状态
  if (evt.ctrlKey || evt.metaKey) {
    const next = new Set(selectedUids.value)
    if (next.has(en._uid)) {
      next.delete(en._uid)
    } else {
      next.add(en._uid)
      selectionAnchorIndex = index
    }
    selectedUids.value = next
    emitSelectMulti()
    // 多选场景下仍需要一个"主选中"用于列表高亮等既有逻辑：取多选集合
    // 中最后加入的一个（即当前点击的块）；集合为空则清空单选。
    emit('select', next.size ? en._uid : null)
    return
  }

  // Shift+click：从锚点到当前索引做连续范围选择，替换整个多选集合
  if (evt.shiftKey && selectionAnchorIndex >= 0) {
    const [lo, hi] = selectionAnchorIndex <= index ? [selectionAnchorIndex, index] : [index, selectionAnchorIndex]
    const next = new Set<number>()
    for (let i = lo; i <= hi; i++) next.add(props.entries[i]._uid)
    selectedUids.value = next
    emitSelectMulti()
    emit('select', en._uid)
    return
  }

  // 普通点击：清空多选集合，回退到原有的"单击=选中，拖拽=移动"逻辑
  if (selectedUids.value.size) {
    selectedUids.value = new Set()
    emitSelectMulti()
  }
  selectionAnchorIndex = index
  dragMoved = false
  drag = { mode: 'move', index, startX: evt.clientX, origStart: en.start, origEnd: en.end }
  emit('drag-start')
  window.addEventListener('mousemove', onDragMove)
  window.addEventListener('mouseup', onDragEnd)
}

const onHandleMouseDown = (evt: MouseEvent, index: number, which: 'start' | 'end') => {
  const en = props.entries[index]
  drag = { mode: which, index, startX: evt.clientX, origStart: en.start, origEnd: en.end }
  emit('drag-start')
  window.addEventListener('mousemove', onDragMove)
  window.addEventListener('mouseup', onDragEnd)
}

const onDragMove = (evt: MouseEvent) => {
  if (!drag) return
  if (Math.abs(evt.clientX - drag.startX) > CLICK_DRAG_THRESHOLD) dragMoved = true
  const deltaSec = (evt.clientX - drag.startX) / pxPerSec.value
  const en = props.entries[drag.index]
  if (!en) return
  const prev = props.entries[drag.index - 1]
  const next = props.entries[drag.index + 1]

  if (drag.mode === 'move') {
    if (!dragMoved) return // 未超过阈值前不移动，避免单击也被当成一次极小的拖拽
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
  if (drag && drag.mode === 'move' && !dragMoved) {
    const en = props.entries[drag.index]
    if (en) emit('select', en._uid)
  }
  const wasDragging = drag !== null
  drag = null
  window.removeEventListener('mousemove', onDragMove)
  window.removeEventListener('mouseup', onDragEnd)
  if (wasDragging) emit('drag-end')
}

// 点击空白轨道区域（非字幕块）→ 跳转播放头到该时间点，并取消选中
const onTrackMouseDown = (evt: MouseEvent) => {
  const target = evt.target as HTMLElement
  if (target.closest('.subtitle-region')) return
  if (editingUid.value !== null) commitEdit()
  if (selectedUids.value.size) {
    selectedUids.value = new Set()
    emitSelectMulti()
  }
  selectionAnchorIndex = -1
  const time = xToTime(evt.clientX)
  emit('seek', time)
  emit('select', null)
}

// 双击空白轨道区域 → 在该时间点新增一条字幕（交给父组件决定默认时长/文本）
const onTrackDblClick = (evt: MouseEvent) => {
  const target = evt.target as HTMLElement
  if (target.closest('.subtitle-region')) return
  const time = xToTime(evt.clientX)
  emit('add-entry', time)
}

// ─────────────────────────────────────────────────────────────────
// 双击字幕块 → 内联编辑文字：编辑框直接出现在块内（不弹独立对话框），
// 块本身如果文字较长会临时变宽/换行显示，方便看清全文，参见
// shouldWrapEdit() 与样式区 .region-edit-input.wrap。
// ─────────────────────────────────────────────────────────────────
const editingUid = ref<number | null>(null)
const editingText = ref('')
const editInputRef = ref<HTMLInputElement[] | HTMLInputElement | null>(null)

const onRegionDblClick = (en: WaveformEntry) => {
  if (selectedUids.value.size) {
    selectedUids.value = new Set()
    emitSelectMulti()
  }
  emit('select', en._uid)
  editingUid.value = en._uid
  editingText.value = en.text
  nextTick(() => {
    const el = Array.isArray(editInputRef.value) ? editInputRef.value[0] : editInputRef.value
    el?.focus()
    el?.select()
  })
}

// 文字明显超过块宽度时，编辑框允许换行显示（块高度也会跟着撑开），
// 短文字则维持单行、块只是略微变宽，避免每次编辑都跳成一大块。
const shouldWrapEdit = (en: WaveformEntry): boolean => {
  const widthPx = Math.max(2, (en.end - en.start) * pxPerSec.value)
  return editingText.value.length * 7 > widthPx
}

const commitEdit = () => {
  if (editingUid.value === null) return
  emit('edit-text', { uid: editingUid.value, text: editingText.value })
  editingUid.value = null
}

const cancelEdit = () => {
  editingUid.value = null
}

// ─────────────────────────────────────────────────────────────────
// 删除选中字幕：Delete/Backspace 键、或工具栏"删除"按钮，二者共用同一
// 个 emit，均无需二次确认（父组件已有撤销能力兜底误删）。正在内联编辑
// 文字时不响应，避免和"删除输入框里的字符"冲突。存在多选集合时批量
// 删除全部选中项，并清空多选状态；否则退回原有的单选删除逻辑。
// ─────────────────────────────────────────────────────────────────
const deleteActive = () => {
  if (selectedUids.value.size) {
    emit('delete-entries', Array.from(selectedUids.value))
    selectedUids.value = new Set()
    emitSelectMulti()
    return
  }
  if (activeIndex.value < 0) return
  const en = props.entries[activeIndex.value]
  emit('delete-entry', en._uid)
}

const onKeyDown = (evt: KeyboardEvent) => {
  if (editingUid.value !== null) return
  if (activeIndex.value < 0 && !selectedUids.value.size) return
  const activeEl = document.activeElement as HTMLElement | null
  if (activeEl && ['INPUT', 'TEXTAREA'].includes(activeEl.tagName)) return

  if (evt.key === 'Delete' || evt.key === 'Backspace') {
    evt.preventDefault()
    deleteActive()
    return
  }

  // X 键：按播放头/块中点拆分当前选中字幕，与工具栏"拆分"按钮共用
  // splitActive() 逻辑。避开修饰键组合（Ctrl/Alt/Meta+X 等系统/浏览器
  // 快捷键），只响应裸按 X。
  if ((evt.key === 'x' || evt.key === 'X') && !evt.ctrlKey && !evt.altKey && !evt.metaKey) {
    evt.preventDefault()
    splitActive()
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeyDown)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown)
})

// ─────────────────────────────────────────────────────────────────
// 拆分选中字幕：拆分点优先用播放头当前位置（若播放头落在选中块范围
// 内），否则退回块中点，交给父组件的 splitEntry() 做实际的文本/
// 时间切分。存在多选集合时，对每个选中块各自独立计算拆分点并批量
// emit；否则退回原有的单选拆分逻辑。
// ─────────────────────────────────────────────────────────────────
const splitPointFor = (en: WaveformEntry): number => {
  const inRange = props.currentTime > en.start && props.currentTime < en.end
  return inRange ? props.currentTime : (en.start + en.end) / 2
}

const splitActive = () => {
  if (selectedUids.value.size) {
    const payloads = props.entries
      .filter((en) => selectedUids.value.has(en._uid))
      .map((en) => ({ uid: en._uid, at: splitPointFor(en) }))
    if (!payloads.length) return
    emit('split-entries', payloads)
    return
  }
  if (activeIndex.value < 0) return
  const en = props.entries[activeIndex.value]
  emit('split-entry', { uid: en._uid, at: splitPointFor(en) })
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

// entries 列表变化（拆分/删除/外部增删等）后，多选集合里指向已不存在
// 条目的 uid 需要清掉，避免残留导致后续批量操作误删/误拆已经不在的项
watch(() => props.entries, (list) => {
  if (!selectedUids.value.size) return
  const liveUids = new Set(list.map((e) => e._uid))
  let changed = false
  const next = new Set<number>()
  selectedUids.value.forEach((uid) => {
    if (liveUids.has(uid)) next.add(uid)
    else changed = true
  })
  if (changed) {
    selectedUids.value = next
    emitSelectMulti()
  }
}, { deep: false })

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

.multi-select-badge {
  font-size: 12px;
  color: #7c3aed;
  background: rgba(124, 58, 237, 0.1);
  border: 1px solid rgba(124, 58, 237, 0.3);
  border-radius: 10px;
  padding: 1px 8px;
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

/* 多选高亮：与单选的橙色区分开，用蓝紫色双层描边（box-shadow 叠加
   border）表示"批量选中"，即便同时也是 active（锚点）也能看出叠加态 */
.subtitle-region.multi-selected {
  border-color: #7c3aed;
  box-shadow: 0 0 0 2px rgba(124, 58, 237, 0.45);
  z-index: 2;
}

.subtitle-region:active {
  cursor: grabbing;
}

/* 双击进入内联编辑：块本身临时"浮"起来（不改变实际 start/end，只是视觉
   上允许超出原宽度显示编辑框），避免短字幕块编辑时文字被挤成省略号看
   不清楚。min-width 保证即使原块很窄，编辑框也有基本可用宽度。 */
.subtitle-region.editing {
  overflow: visible;
  z-index: 5;
  min-width: 90px;
  background: rgba(255, 145, 77, 0.35);
  border-color: #ff914d;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18);
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

/* 编辑框默认单行、随文字内容自适应块的视觉宽度（由 .editing 的
   overflow: visible 保证不被裁切）；文字较长时 shouldWrapEdit() 会加上
   .wrap，改为允许换行显示，块高度也跟着一起变高（height: auto）。 */
.region-edit-input {
  flex: 1;
  min-width: 78px;
  height: 100%;
  padding: 0 6px;
  font-size: 12px;
  color: #2c2f4a;
  background: #fff;
  border: 1px solid #ff914d;
  border-radius: 3px;
  outline: none;
  box-sizing: border-box;
  cursor: text;
}

.region-edit-input.wrap {
  position: relative;
  height: auto;
  min-height: 100%;
  white-space: normal;
  word-break: break-all;
  padding: 4px 6px;
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
