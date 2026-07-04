<template>
  <div class="dict-container">
    <el-card class="dict-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-title">📖 {{ t('dictionary.pageTitle') }}</span>
        </div>
      </template>

      <p class="page-subtitle">{{ t('dictionary.pageSubtitle') }}</p>

      <!-- ============== 独立词典列表 + 新建 ============== -->
      <div class="dict-picker-row">
        <el-select
          v-model="activeSource"
          :placeholder="t('dictionary.selectPlaceholder')"
          style="min-width: 240px"
          @change="onSourceChange"
        >
          <el-option
            v-for="d in dictionaries"
            :key="d.name"
            :value="d.name"
            :label="`${d.name} (${d.notation === 'vocaloid' ? t('dictionary.notationVocaloid') : t('dictionary.notationSynthesizerV')}, ${d.count})`"
          />
        </el-select>

        <el-button v-if="activeSource" type="danger" plain @click="confirmDeleteDictionary">
          🗑️ {{ t('dictionary.deleteDictionary') }}
        </el-button>

        <el-button type="primary" @click="showCreateDialog = true">
          ➕ {{ t('dictionary.createDictionary') }}
        </el-button>
      </div>

      <p v-if="!dictionaries.length && !loadingList" class="help-text">
        {{ t('dictionary.noDictionaries') }}
      </p>

      <template v-if="activeSource">
        <p class="source-hint">
          {{ t('dictionary.notationLabel') }}:
          {{ activeNotation === 'vocaloid' ? t('dictionary.notationVocaloid') : t('dictionary.notationSynthesizerV') }}
          —
          {{ activeNotation === 'vocaloid' ? t('dictionary.sourceHintVocaloid') : t('dictionary.sourceHintSynthesizerV') }}
        </p>

        <!-- 新增 / 更新词条 -->
        <el-form :inline="true" class="entry-form" @submit.prevent>
          <el-form-item :label="t('dictionary.wordLabel')">
            <el-input
              v-model="newWord"
              :placeholder="t('dictionary.wordPlaceholder')"
              style="width: 160px"
              @keyup.enter="submitEntry"
            />
          </el-form-item>
          <el-form-item :label="t('dictionary.phonemesLabel')">
            <el-input
              v-model="newPhonemes"
              :placeholder="t('dictionary.phonemesPlaceholder')"
              style="width: 280px"
              @keyup.enter="submitEntry"
            />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="saving" @click="submitEntry">
              ➕ {{ t('dictionary.addEntry') }}
            </el-button>
          </el-form-item>
        </el-form>

        <!-- 导入 / 导出工具栏 -->
        <div class="toolbar">
          <el-upload
            :key="importUploadKey"
            action="#"
            :auto-upload="false"
            :show-file-list="false"
            :on-change="handleImportFileSelect"
            accept=".csv,.json"
          >
            <el-button>📂 {{ t('dictionary.importFile') }}</el-button>
          </el-upload>
          <el-checkbox v-model="importOverwrite">{{ t('dictionary.importOverwrite') }}</el-checkbox>
          <el-button
            v-if="pendingImportFile"
            type="success"
            :loading="importing"
            @click="doImport"
          >
            ⬆️ {{ t('dictionary.importButton') }} ({{ pendingImportFile.name }})
          </el-button>

          <el-divider direction="vertical" />

          <el-button @click="exportJson">⬇️ {{ t('dictionary.exportJson') }}</el-button>
          <el-button @click="exportCsv">⬇️ {{ t('dictionary.exportCsv') }}</el-button>
        </div>
        <p class="help-text">{{ t('dictionary.csvFormatHint') }}</p>

        <el-divider />

        <div class="entry-count">{{ t('dictionary.entryCount', { count: entryList.length }) }}</div>

        <el-table
          v-if="entryList.length"
          v-loading="loading"
          :data="entryList"
          stripe
          style="width: 100%"
        >
          <el-table-column prop="word" :label="t('dictionary.tableWord')" width="220">
            <template #default="{ row }">
              <div
                v-if="!isEditingCell(row.word, 'word')"
                class="editable-cell"
                @click="startEditCell(row, 'word')"
              >
                {{ row.word }}
                <span class="edit-icon">✏️</span>
              </div>
              <el-input
                v-else
                :ref="(el) => setEditInputRef(el, row.word, 'word')"
                v-model="editDraft"
                size="small"
                @keyup.enter="commitCellEdit(row)"
                @keyup.esc="cancelCellEdit"
                @blur="commitCellEdit(row)"
              />
            </template>
          </el-table-column>
          <el-table-column prop="phonemes" :label="t('dictionary.tablePhonemes')">
            <template #default="{ row }">
              <div
                v-if="!isEditingCell(row.word, 'phonemes')"
                class="editable-cell"
                @click="startEditCell(row, 'phonemes')"
              >
                {{ row.phonemes }}
                <span class="edit-icon">✏️</span>
              </div>
              <el-input
                v-else
                :ref="(el) => setEditInputRef(el, row.word, 'phonemes')"
                v-model="editDraft"
                size="small"
                @keyup.enter="commitCellEdit(row)"
                @keyup.esc="cancelCellEdit"
                @blur="commitCellEdit(row)"
              />
            </template>
          </el-table-column>
          <el-table-column :label="t('dictionary.tableActions')" width="110" align="center">
            <template #default="{ row }">
              <el-button link type="danger" @click="removeEntry(row.word)">
                🗑️ {{ t('dictionary.deleteEntry') }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="entryList.length" class="help-text">{{ t('dictionary.editHint') }}</p>
        <el-empty v-else v-loading="loading" :description="t('dictionary.emptyState')" />
      </template>
      <el-empty v-else-if="!loadingList" :description="t('dictionary.noDictionarySelected')" />
    </el-card>

    <!-- 新建词典弹窗 -->
    <el-dialog v-model="showCreateDialog" :title="t('dictionary.createDictionary')" width="420px">
      <el-form label-position="top">
        <el-form-item :label="t('dictionary.dictNameLabel')">
          <el-input v-model="newDictName" :placeholder="t('dictionary.dictNamePlaceholder')" maxlength="60" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('dictionary.notationLabel')">
          <el-radio-group v-model="newDictNotation">
            <el-radio value="synthesizerv">{{ t('dictionary.notationSynthesizerV') }}</el-radio>
            <el-radio value="vocaloid">{{ t('dictionary.notationVocaloid') }}</el-radio>
          </el-radio-group>
          <p class="help-text">
            {{ newDictNotation === 'vocaloid' ? t('dictionary.sourceHintVocaloid') : t('dictionary.sourceHintSynthesizerV') }}
          </p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">{{ t('dictionary.cancel') }}</el-button>
        <el-button type="primary" :loading="creating" @click="doCreateDictionary">
          {{ t('dictionary.createDictionary') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

interface DictMeta {
  name: string
  notation: 'synthesizerv' | 'vocaloid'
  count: number
}

const dictionaries = ref<DictMeta[]>([])
const activeSource = ref<string>('')
const loadingList = ref(false)

const activeNotation = computed(
  () => dictionaries.value.find(d => d.name === activeSource.value)?.notation || 'synthesizerv'
)

const entries = ref<Record<string, string>>({})
const loading = ref(false)
const saving = ref(false)
const importing = ref(false)
const creating = ref(false)

const newWord = ref('')
const newPhonemes = ref('')

const showCreateDialog = ref(false)
const newDictName = ref('')
const newDictNotation = ref<'synthesizerv' | 'vocaloid'>('synthesizerv')

const pendingImportFile = ref<File | null>(null)
const importOverwrite = ref(true)
const importUploadKey = ref(0)

// ============== 表格内联编辑 ==============
type EditField = 'word' | 'phonemes'
const editingCell = ref<{ word: string; field: EditField } | null>(null)
const editDraft = ref('')
const editInputRefs: Record<string, any> = {}
let suppressBlurCommit = false

const cellKey = (word: string, field: EditField) => `${word}::${field}`

const setEditInputRef = (el: any, word: string, field: EditField) => {
  if (el) editInputRefs[cellKey(word, field)] = el
}

const isEditingCell = (word: string, field: EditField) =>
  editingCell.value?.word === word && editingCell.value?.field === field

const startEditCell = (row: { word: string; phonemes: string }, field: EditField) => {
  editingCell.value = { word: row.word, field }
  editDraft.value = field === 'word' ? row.word : row.phonemes
  requestAnimationFrame(() => {
    editInputRefs[cellKey(row.word, field)]?.focus?.()
  })
}

const cancelCellEdit = () => {
  suppressBlurCommit = true
  editingCell.value = null
  editDraft.value = ''
  requestAnimationFrame(() => {
    suppressBlurCommit = false
  })
}

const commitCellEdit = async (row: { word: string; phonemes: string }) => {
  if (suppressBlurCommit) return
  const editing = editingCell.value
  if (!editing) return

  const draft = editDraft.value.trim()
  const originalWord = row.word
  const newWordValue = editing.field === 'word' ? draft : row.word
  const newPhonemesValue = editing.field === 'phonemes' ? draft : row.phonemes

  const unchanged =
    (editing.field === 'word' && draft === originalWord) ||
    (editing.field === 'phonemes' && draft === row.phonemes)
  if (!draft || unchanged) {
    editingCell.value = null
    editDraft.value = ''
    return
  }

  editingCell.value = null
  editDraft.value = ''

  try {
    const res = await fetch(`/api/dictionary/${encodeURIComponent(activeSource.value)}/entry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ word: newWordValue, phonemes: newPhonemesValue }),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)

    if (editing.field === 'word' && newWordValue !== originalWord) {
      await fetch(`/api/dictionary/${encodeURIComponent(activeSource.value)}/entry`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ word: originalWord }),
      })
    }

    ElMessage.success(t('dictionary.updateSuccess'))
    await fetchEntries(activeSource.value)
  } catch (e: any) {
    ElMessage.error(t('dictionary.updateFailed', { error: e?.message || String(e) }))
  }
}

const entryList = computed(() =>
  Object.entries(entries.value)
    .map(([word, phonemes]) => ({ word, phonemes }))
    .sort((a, b) => a.word.localeCompare(b.word))
)

const fetchDictionaries = async (selectAfter?: string) => {
  loadingList.value = true
  try {
    const res = await fetch('/api/dictionary')
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)
    dictionaries.value = data.dictionaries || []

    if (selectAfter && dictionaries.value.some(d => d.name === selectAfter)) {
      activeSource.value = selectAfter
    } else if (!dictionaries.value.some(d => d.name === activeSource.value)) {
      activeSource.value = dictionaries.value[0]?.name || ''
    }

    if (activeSource.value) {
      await fetchEntries(activeSource.value)
    } else {
      entries.value = {}
    }
  } catch (e: any) {
    ElMessage.error(t('dictionary.loadFailed', { error: e?.message || String(e) }))
  } finally {
    loadingList.value = false
  }
}

const fetchEntries = async (source: string) => {
  if (!source) {
    entries.value = {}
    return
  }
  loading.value = true
  try {
    const res = await fetch(`/api/dictionary/${encodeURIComponent(source)}`)
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)
    entries.value = data.entries || {}
  } catch (e: any) {
    ElMessage.error(t('dictionary.loadFailed', { error: e?.message || String(e) }))
    entries.value = {}
  } finally {
    loading.value = false
  }
}

const onSourceChange = () => {
  pendingImportFile.value = null
  importUploadKey.value += 1
  fetchEntries(activeSource.value)
}

const doCreateDictionary = async () => {
  const name = newDictName.value.trim()
  if (!name) {
    ElMessage.warning(t('dictionary.dictNameRequired'))
    return
  }
  creating.value = true
  try {
    const res = await fetch('/api/dictionary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, notation: newDictNotation.value }),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)

    ElMessage.success(t('dictionary.createSuccess'))
    showCreateDialog.value = false
    newDictName.value = ''
    await fetchDictionaries(name)
  } catch (e: any) {
    ElMessage.error(t('dictionary.createFailed', { error: e?.message || String(e) }))
  } finally {
    creating.value = false
  }
}

const confirmDeleteDictionary = async () => {
  if (!activeSource.value) return
  try {
    await ElMessageBox.confirm(
      t('dictionary.deleteDictionaryConfirm', { name: activeSource.value }),
      t('dictionary.deleteDictionary'),
      { type: 'warning' }
    )
  } catch {
    return // 用户取消
  }

  try {
    const res = await fetch(`/api/dictionary/${encodeURIComponent(activeSource.value)}`, {
      method: 'DELETE',
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)

    ElMessage.success(t('dictionary.deleteDictionarySuccess'))
    activeSource.value = ''
    await fetchDictionaries()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

const submitEntry = async () => {
  const word = newWord.value.trim()
  const phonemes = newPhonemes.value.trim()
  if (!word || !phonemes || !activeSource.value) return

  saving.value = true
  try {
    const res = await fetch(`/api/dictionary/${encodeURIComponent(activeSource.value)}/entry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ word, phonemes }),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)

    ElMessage.success(t('dictionary.addSuccess'))
    newWord.value = ''
    newPhonemes.value = ''
    await fetchEntries(activeSource.value)
    const meta = dictionaries.value.find(d => d.name === activeSource.value)
    if (meta) meta.count = Object.keys(entries.value).length
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    saving.value = false
  }
}

const removeEntry = async (word: string) => {
  try {
    const res = await fetch(`/api/dictionary/${encodeURIComponent(activeSource.value)}/entry`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ word }),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)

    ElMessage.success(t('dictionary.deleteSuccess'))
    await fetchEntries(activeSource.value)
    const meta = dictionaries.value.find(d => d.name === activeSource.value)
    if (meta) meta.count = Object.keys(entries.value).length
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

const handleImportFileSelect = (file: any) => {
  const raw: File | null = file?.raw || null
  pendingImportFile.value = raw
}

const doImport = async () => {
  if (!pendingImportFile.value || !activeSource.value) return
  importing.value = true
  try {
    const fd = new FormData()
    fd.append('file', pendingImportFile.value)
    fd.append('overwrite', importOverwrite.value.toString())
    fd.append('notation', activeNotation.value)

    const res = await fetch(`/api/dictionary/${encodeURIComponent(activeSource.value)}/import`, {
      method: 'POST',
      body: fd,
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)

    ElMessage.success(t('dictionary.importSuccess', { added: data.added, updated: data.updated }))
    pendingImportFile.value = null
    importUploadKey.value += 1
    await fetchEntries(activeSource.value)
    const meta = dictionaries.value.find(d => d.name === activeSource.value)
    if (meta) meta.count = Object.keys(entries.value).length
  } catch (e: any) {
    ElMessage.error(t('dictionary.importFailed', { error: e?.message || String(e) }))
  } finally {
    importing.value = false
  }
}

// 通过 fetch + Blob 触发下载，而不是直接把 API 地址塞进 <a href>。
// 【CSV 导出无法下载的根因修复】直接导航到接口地址在部分环境
// （前端与后端分别运行在不同端口、又没有为普通页面导航配置反向代理，
// 只为 fetch/XHR 配置了 /api 代理）下会把请求发到当前页面所在的
// 前端源而不是后端端口，导致 404 / 空白页，看起来就是"点了没反应/
// 下载不下来"。exportJson 一直用 fetch 是能正常工作的，这里统一用
// 同样的方式获取内容后再触发浏览器保存，不依赖整页导航。
const exportJson = async () => {
  if (!activeSource.value) return
  try {
    const res = await fetch(`/api/dictionary/${encodeURIComponent(activeSource.value)}/export?format=json`)
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)
    const payload = data.data?.[activeSource.value] ?? data.data ?? {}
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = window.URL.createObjectURL(blob)
    const el = document.createElement('a')
    el.href = url
    el.download = `${activeSource.value}_dictionary.json`
    document.body.appendChild(el)
    el.click()
    document.body.removeChild(el)
    window.URL.revokeObjectURL(url)
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

const exportCsv = async () => {
  if (!activeSource.value) return
  try {
    const res = await fetch(`/api/dictionary/${encodeURIComponent(activeSource.value)}/export?format=csv`)
    if (!res.ok) {
      // 出错时后端返回的是 JSON，不是 CSV，取出错误信息展示
      const data = await res.json().catch(() => null)
      throw new Error(data?.error || res.statusText)
    }
    const blob = await res.blob()
    const url = window.URL.createObjectURL(blob)
    const el = document.createElement('a')
    el.href = url
    el.download = `${activeSource.value}_dictionary.csv`
    document.body.appendChild(el)
    el.click()
    document.body.removeChild(el)
    window.URL.revokeObjectURL(url)
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

onMounted(() => {
  fetchDictionaries()
})
</script>

<style scoped>
.dict-container {
  width: 100%;
}

.dict-card {
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

.dict-picker-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.source-hint {
  color: #909399;
  font-size: 12px;
  margin: 4px 0 12px;
}

.entry-form {
  margin-top: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 12px 0 4px;
}

.help-text {
  color: #909399;
  font-size: 12px;
  margin: 4px 0 0;
}

.entry-count {
  color: #606266;
  font-size: 13px;
  margin-bottom: 10px;
}

.editable-cell {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
  min-height: 22px;
}

.editable-cell:hover {
  background: #ecf5ff;
}

.editable-cell .edit-icon {
  opacity: 0;
  font-size: 12px;
  transition: opacity 0.15s;
}

.editable-cell:hover .edit-icon {
  opacity: 0.7;
}

@media (max-width: 768px) {
  .entry-form :deep(.el-form-item) {
    width: 100%;
    margin-right: 0;
  }

  .entry-form :deep(.el-input) {
    width: 100% !important;
  }

  .dict-picker-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
