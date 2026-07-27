// ─────────────────────────────────────────────────────────────────
// 字幕编辑撤销/恢复历史（Ctrl+Z / Ctrl+Y、Ctrl+Shift+Z）
//
// 设计取舍：采用整体快照（深拷贝 entries + activeUid），而不是记录
// 每次操作的"反向 diff"。原因是字幕条目的变更来源非常多（增删、拆分、
// 合并、拖拽改时间、内联编辑文字、导入替换……），每种都单独写一份
// 精确的反向操作成本高且容易遗漏边界情况；深拷贝整份列表实现简单、
// 不会漏改，字幕条目数量通常在几十到几百条，深拷贝的开销可以忽略。
//
// 粒度控制：每一次"离散操作"（删除一行、插入、拆分、合并、清空、导入、
// 批量删除/拆分）都在操作完成后立即 push 一次快照。但对于"连续操作"——
// 拖拽波形块调整时间、连续打字编辑文字/时间输入框——如果每次微小变化
// 都 push，会导致撤销要按几十次才能回到操作前的状态，体验很差。所以
// 连续操作改用 beginGesture()/commitGesture() 包裹：拖拽开始/输入框
// 获得焦点时不 push，而是先记下"手势开始前"的快照；拖拽结束
// （mouseup）/输入框失焦（change）时才真正 push 一次，代表这一整个
// 手势产生的变化算作一步撤销。
//
// 输入框内部的撤销（Ctrl+Z 时输入框正处于聚焦状态）：按产品决定，不
// 拦截，交给浏览器原生的输入框撤销处理；只有当没有原生输入元素聚焦时，
// 全局 Ctrl+Z/Y 快捷键才会调用这里的 undo()/redo()。
// ─────────────────────────────────────────────────────────────────

import { ref, computed } from 'vue'

export interface HistorySnapshot<T> {
  entries: T[]
  activeUid: number | null
}

const MAX_HISTORY = 100

export function useSubtitleHistory<T extends { _uid: number }>(
  entries: { value: T[] },
  activeUid: { value: number | null },
) {
  const past = ref<HistorySnapshot<T>[]>([])
  const future = ref<HistorySnapshot<T>[]>([])

  // 手势进行中（拖拽/连续输入）时，这里存着手势开始前的快照；
  // commitGesture() 时会把它推进 past，而不是把手势中间的每一帧都推进去
  let pendingGestureSnapshot: HistorySnapshot<T> | null = null

  const cloneSnapshot = (): HistorySnapshot<T> => ({
    entries: JSON.parse(JSON.stringify(entries.value)),
    activeUid: activeUid.value,
  })

  const restoreSnapshot = (snap: HistorySnapshot<T>) => {
    entries.value = JSON.parse(JSON.stringify(snap.entries))
    activeUid.value = snap.activeUid
  }

  // 离散操作专用：操作发生后立刻调用，把"操作前"的快照推入历史栈。
  // 注意要在修改 entries 之前调用（传入 beforeMutate 回调），或者调用方
  // 自己保证调用时机在 mutate 之前——这里选择更直观的写法：
  // 调用方在执行修改前先调用 recordBeforeChange()，拿到"当前状态"存起来，
  // 修改完成后不需要再做任何事。
  const recordBeforeChange = () => {
    past.value.push(cloneSnapshot())
    if (past.value.length > MAX_HISTORY) past.value.shift()
    future.value = [] // 一旦发生新的修改，之前 undo 出来的"重做"分支作废
  }

  // 连续操作专用：手势开始时调用（例如波形块 mousedown、输入框 focus），
  // 记下这一刻的状态；手势结束时调用 commitGesture() 才真正入栈
  const beginGesture = () => {
    pendingGestureSnapshot = cloneSnapshot()
  }

  // 手势结束时调用。如果这次手势实际上什么都没改变（例如拖了一下又
  // 松开回原位、输入框聚焦后没改内容就失焦），则不产生多余的历史记录
  const commitGesture = () => {
    if (!pendingGestureSnapshot) return
    const before = pendingGestureSnapshot
    pendingGestureSnapshot = null
    const noChange = JSON.stringify(before.entries) === JSON.stringify(entries.value)
    if (noChange) return
    past.value.push(before)
    if (past.value.length > MAX_HISTORY) past.value.shift()
    future.value = []
  }

  // 手势中途取消（例如拖拽被 Esc 打断，若未来需要）时丢弃暂存快照，
  // 不产生历史记录
  const cancelGesture = () => {
    pendingGestureSnapshot = null
  }

  const canUndo = computed(() => past.value.length > 0)
  const canRedo = computed(() => future.value.length > 0)

  const undo = () => {
    if (!past.value.length) return
    future.value.push(cloneSnapshot())
    const prev = past.value.pop()!
    restoreSnapshot(prev)
  }

  const redo = () => {
    if (!future.value.length) return
    past.value.push(cloneSnapshot())
    const next = future.value.pop()!
    restoreSnapshot(next)
  }

  // 整份数据被替换的场景（导入字幕、清空全部、切换/重新上传媒体文件）
  // 直接清空历史——这些操作本身仍然要记一步"操作前"快照（用
  // recordBeforeChange），但历史栈本身没必要跨"文件"保留，避免用户在
  // 换了一个完全不同的媒体文件后，还能撤销回上一个文件的字幕内容
  const resetHistory = () => {
    past.value = []
    future.value = []
    pendingGestureSnapshot = null
  }

  return {
    recordBeforeChange,
    beginGesture,
    commitGesture,
    cancelGesture,
    canUndo,
    canRedo,
    undo,
    redo,
    resetHistory,
  }
}
