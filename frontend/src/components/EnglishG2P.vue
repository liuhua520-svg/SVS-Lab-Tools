<template>
  <el-card class="english-g2p-container">
    <template #header>
      <div class="card-header">
        <span class="title">
          <el-icon><Document /></el-icon>
          {{ t('englishG2P.title') }}
        </span>
        <el-tooltip :content="t('englishG2P.helpText')" placement="top">
          <el-icon class="help-icon"><QuestionFilled /></el-icon>
        </el-tooltip>
      </div>
    </template>

    <!-- 输入区域 -->
    <el-form label-width="120px" class="input-section">
      <el-form-item :label="t('englishG2P.inputLabel')">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="4"
          :placeholder="t('englishG2P.inputPlaceholder')"
          clearable
        />
      </el-form-item>

      <div class="button-group">
        <el-button type="primary" @click="extractAndConvert">
          <el-icon><DocumentCopy /></el-icon>
          {{ t('englishG2P.extract') }}
        </el-button>
        <el-button @click="clearAll">
          <el-icon><Delete /></el-icon>
          {{ t('englishG2P.clear') }}
        </el-button>
        <el-button @click="triggerImportDictionary">
          <el-icon><Upload /></el-icon>
          {{ t('englishG2P.importDictionary') }}
        </el-button>
        <input
          ref="importFileInput"
          type="file"
          accept=".json,.csv"
          style="display: none"
          @change="handleImportFile"
        />
        <el-popover placement="top" :width="300">
          <template #reference>
            <el-button text type="info">
              <el-icon><Setting /></el-icon>
              {{ t('englishG2P.options') }}
            </el-button>
          </template>
          <div class="options-panel">
            <div class="option-item">
              <el-checkbox v-model="options.caseSensitive">
                {{ t('englishG2P.caseSensitive') }}
              </el-checkbox>
            </div>
            <div class="option-item">
              <el-checkbox v-model="options.includeNumbers">
                {{ t('englishG2P.includeNumbers') }}
              </el-checkbox>
            </div>
            <div class="option-item">
              <el-checkbox v-model="options.splitBySpace">
                {{ t('englishG2P.splitBySpace') }}
              </el-checkbox>
            </div>
          </div>
        </el-popover>
      </div>

      <!-- 自定义词典状态提示 -->
      <div v-if="customArpaMap || customVocaloidMap" class="custom-dict-banner">
        <el-tag v-if="customArpaMap" type="success" closable @close="clearCustomDict('arpa')">
          {{ t('englishG2P.customArpaLoaded') }}
        </el-tag>
        <el-tag v-if="customVocaloidMap" type="success" closable @close="clearCustomDict('vocaloid')">
          {{ t('englishG2P.customVocaloidLoaded') }}
        </el-tag>
      </div>

      <!-- 处理状态 -->
      <div v-if="loading" class="loading-state">
        <el-progress :percentage="progress" striped />
        <span class="status-text">{{ statusText }}</span>
      </div>
      <div v-if="error" class="error-message">
        <el-alert :title="error" type="error" :closable="true" @close="error = ''" />
      </div>
    </el-form>

    <!-- 结果区域 -->
    <div v-if="results.length > 0" class="results-section">
      <div class="results-header">
        <h3>
          <el-icon><DataAnalysis /></el-icon>
          {{ t('englishG2P.results') }}
          <el-tag type="info">{{ results.length }}</el-tag>
        </h3>
        <div class="result-actions">
          <el-dropdown trigger="click" @command="(cmd: 'arpa' | 'vocaloid' | 'all') => exportResults('json', cmd)">
            <el-button size="small" type="primary">
              <el-icon><Download /></el-icon>
              {{ t('englishG2P.exportJSON') }}
              <el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="arpa">{{ t('englishG2P.downloadArpaOnly') }}</el-dropdown-item>
                <el-dropdown-item command="vocaloid">{{ t('englishG2P.downloadVocaloidOnly') }}</el-dropdown-item>
                <el-dropdown-item command="all" divided>{{ t('englishG2P.downloadAll') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-dropdown trigger="click" @command="(cmd: 'arpa' | 'vocaloid' | 'all') => exportResults('csv', cmd)">
            <el-button size="small" type="primary">
              <el-icon><Download /></el-icon>
              {{ t('englishG2P.exportCSV') }}
              <el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="arpa">{{ t('englishG2P.downloadArpaOnly') }}</el-dropdown-item>
                <el-dropdown-item command="vocaloid">{{ t('englishG2P.downloadVocaloidOnly') }}</el-dropdown-item>
                <el-dropdown-item command="all" divided>{{ t('englishG2P.downloadAll') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button size="small" @click="copyAllToClipboard">
            <el-icon><DocumentCopy /></el-icon>
            {{ t('englishG2P.copy') }}
          </el-button>
        </div>
      </div>

      <!-- 搜索单词：定位并跳转到该单词所在的分页 -->
      <div class="search-row">
        <el-input
          v-model="searchQuery"
          :placeholder="t('englishG2P.searchPlaceholder')"
          clearable
          style="max-width: 320px"
          @keyup.enter="jumpToWord(searchQuery)"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button type="primary" plain @click="jumpToWord(searchQuery)">
          <el-icon><Search /></el-icon>
          {{ t('englishG2P.searchButton') }}
        </el-button>
        <span v-if="searchQuery" class="search-result-hint">
          {{ t('englishG2P.searchResultCount', { count: filteredResults.length }) }}
        </span>
      </div>

      <!-- 分页表格 -->
      <el-table
        ref="resultsTableRef"
        :data="paginatedResults"
        border
        stripe
        :default-sort="{ prop: 'index', order: 'ascending' }"
        max-height="500"
        class="results-table"
        :row-class-name="rowClassName"
      >
        <el-table-column prop="index" :label="t('englishG2P.index')" width="60" align="center" />
        <el-table-column prop="word" :label="t('englishG2P.word')" min-width="120">
          <template #default="scope">
            <el-tag>{{ scope.row.word }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="arpa" :label="t('englishG2P.arpa')" min-width="180">
          <template #default="scope">
            <code class="phoneme-code">{{ scope.row.arpa }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="vocaloid" :label="t('englishG2P.vocaloid')" min-width="180">
          <template #default="scope">
            <code class="phoneme-code vocaloid-code">{{ scope.row.vocaloid }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="count" :label="t('englishG2P.repeatCount')" width="110" align="center">
          <template #default="scope">
            <el-tag v-if="scope.row.count > 1" type="danger">
              × {{ scope.row.count }}
            </el-tag>
            <el-tag v-else type="info">{{ t('englishG2P.none') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('englishG2P.actions')" width="120" align="center">
          <template #default="scope">
            <el-button
              size="small"
              type="primary"
              text
              @click="copyToClipboard(scope.row.arpa)"
            >
              {{ t('englishG2P.copy') }}
            </el-button>
            <el-button
              size="small"
              type="danger"
              text
              @click="removeResult(scope.row.index)"
            >
              {{ t('englishG2P.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50, 100, 500, 1000]"
        :total="filteredResults.length"
        layout="total, sizes, prev, pager, next, jumper"
        class="pagination"
        @size-change="handlePageSizeChange"
        @current-change="handlePageChange"
      />
    </div>

    <!-- 统计信息 -->
    <div v-if="results.length > 0" class="statistics-section">
      <div class="stat-item">
        <span class="stat-label">{{ t('englishG2P.totalWords') }}</span>
        <span class="stat-value">{{ results.length }}</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">{{ t('englishG2P.repeatedWords') }}</span>
        <span class="stat-value">{{ repeatedWordsCount }}</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">{{ t('englishG2P.avgPhonemes') }}</span>
        <span class="stat-value">{{ avgPhonemes.toFixed(1) }}</span>
      </div>
      <div class="stat-item">
        <span class="stat-label">{{ t('englishG2P.uniqueWords') }}</span>
        <span class="stat-value">{{ uniqueWords }}</span>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, watch } from 'vue'
import {
  Document,
  QuestionFilled,
  DocumentCopy,
  Delete,
  Setting,
  DataAnalysis,
  Download,
  ArrowDown,
  Upload,
  Search,
} from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'

const { t } = useI18n()

// ═══════════════════════════════════════════════════════════
// ARPABET → VOCALOID 音素映射表
// 确保本工具生成的 VOCALOID 音素与词典导入功能完全兼容。
// ═══════════════════════════════════════════════════════════

const ARPABET_TO_VOCALOID: Record<string, string> = {
  // --- Vowels (元音) ---
  aa: 'Q',
  ae: '{',
  ah: 'V',
  ao: 'O:',
  aw: 'aU',
  ax: '@',
  ay: 'aI',
  eh: 'e',
  er: '@r',
  ey: 'eI',
  ih: 'I',
  iy: 'i:',
  ow: '@U',
  oy: 'OI',
  uh: 'U',
  uw: 'u:',

  // --- Consonants (辅音) ---
  b: 'b',
  ch: 'tS',
  d: 'd',
  dh: 'D',
  dx: 'd',  // 对应 Lua 中的 ["dx"] = "d"
  f: 'f',
  g: 'g',
  hh: 'h',
  jh: 'dZ',
  k: 'k',
  l: 'l',
  m: 'm',
  n: 'n',
  ng: 'N',
  p: 'p',
  r: 'r',
  s: 's',
  sh: 'S',
  t: 't',
  th: 'T',
  v: 'v',
  w: 'w',
  y: 'j',
  z: 'z',
  zh: 'Z',
};

/**
 * 用户可通过"导入词典"按钮上传符合词典导入格式的 JSON/CSV 文件，
 * 覆盖内置的 ARPABET / VOCALOID 音素映射表：
 *   - JSON: {"notation": "synthesizerv" | "vocaloid", "entries": {PHONEME: mapped}}
 *   - CSV : "word,phonemes" 两列（首行表头），word 列即 ARPABET 音素记号，
 *           phonemes 列即该音素对应的目标记号
 * "synthesizerv" → 视为 ARPABET 音素映射（覆盖/追加 arpa 记号本身，
 *                  一般无需改动，因为 arpa 直接来自 G2P 结果）
 * "vocaloid"     → 视为 ARPABET → VOCALOID 的音素映射表，覆盖内置表
 *
 * 导入后仅覆盖表中出现的音素键，未出现的音素仍使用内置默认映射兜底。
 */
const customArpaMap = ref<Record<string, string> | null>(null)
const customVocaloidMap = ref<Record<string, string> | null>(null)
const importFileInput = ref<HTMLInputElement | null>(null)

const triggerImportDictionary = () => {
  importFileInput.value?.click()
}

/** 解析 "word,phonemes" 两列 CSV 文本（首行表头可选）为 {word: phonemes} */
const parseDictCsv = (text: string): Record<string, string> => {
  const lines = text.replace(/^\ufeff/, '').split(/\r?\n/).filter((l) => l.trim() !== '')
  const entries: Record<string, string> = {}
  let startIdx = 0
  if (lines.length > 0) {
    const firstCols = splitCsvLine(lines[0]).map((c) => c.trim().toLowerCase())
    if (firstCols[0] === 'word' && firstCols[1] === 'phonemes') {
      startIdx = 1
    }
  }
  for (let i = startIdx; i < lines.length; i++) {
    const cols = splitCsvLine(lines[i])
    if (cols.length < 2) continue
    const key = cols[0].trim()
    const value = cols[1].trim()
    if (key && value) entries[key] = value
  }
  return entries
}

/** 简单 CSV 单行解析，支持双引号包裹与转义（""） */
const splitCsvLine = (line: string): string[] => {
  const result: string[] = []
  let cur = ''
  let inQuotes = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          cur += '"'
          i++
        } else {
          inQuotes = false
        }
      } else {
        cur += ch
      }
    } else if (ch === '"') {
      inQuotes = true
    } else if (ch === ',') {
      result.push(cur)
      cur = ''
    } else {
      cur += ch
    }
  }
  result.push(cur)
  return result
}

const handleImportFile = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  try {
    const text = await file.text()
    const filename = file.name.toLowerCase()

    let notation = ''
    let entries: Record<string, string> = {}

    if (filename.endsWith('.json')) {
      const payload = JSON.parse(text)
      if (payload && typeof payload === 'object' && payload.entries && typeof payload.entries === 'object') {
        notation = (payload.notation || '').toString().toLowerCase()
        entries = payload.entries
      } else {
        throw new Error(t('englishG2P.importFormatError'))
      }
    } else if (filename.endsWith('.csv')) {
      entries = parseDictCsv(text)
      // CSV 无法携带 notation 字段，弹出选择让用户指定这份 CSV 对应哪套记号
      notation = await pickNotationForCsv()
    } else {
      throw new Error(t('englishG2P.importFormatError'))
    }

    if (!entries || Object.keys(entries).length === 0) {
      throw new Error(t('englishG2P.importEmptyError'))
    }

    if (notation === 'vocaloid') {
      customVocaloidMap.value = { ...(customVocaloidMap.value || {}), ...entries }
      ElMessage.success(t('englishG2P.customVocaloidLoaded'))
    } else if (notation === 'synthesizerv') {
      customArpaMap.value = { ...(customArpaMap.value || {}), ...entries }
      ElMessage.success(t('englishG2P.customArpaLoaded'))
    } else {
      throw new Error(t('englishG2P.importNotationError'))
    }

    // 已有结果时，用新导入的映射表重新计算 ARPABET / VOCALOID 列
    recomputeResults()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : t('englishG2P.importFormatError'))
  } finally {
    input.value = ''
  }
}

/** CSV 导入时无法从文件内容判断 notation，弹窗询问用户 */
const pickNotationForCsv = (): Promise<string> => {
  return ElMessageBox.confirm(
    t('englishG2P.pickNotationMessage'),
    t('englishG2P.pickNotationTitle'),
    {
      confirmButtonText: t('englishG2P.notationVocaloid'),
      cancelButtonText: t('englishG2P.notationArpa'),
      distinguishCancelAndClose: true,
      type: 'info',
    },
  )
    .then(() => 'vocaloid')
    .catch((action) => (action === 'cancel' ? 'synthesizerv' : ''))
}

const recomputeResults = () => {
  if (results.value.length === 0) return
  results.value = results.value.map((r) => {
    const customArpa =
      customArpaMap.value &&
      (customArpaMap.value[r.word] ?? customArpaMap.value[(r.word || '').toLowerCase()])
    const arpa = customArpa || r.rawArpa
    return {
      ...r,
      arpa,
      vocaloid: arpaToVocaloid(arpa),
    }
  })
}

const clearCustomDict = (which: 'arpa' | 'vocaloid') => {
  if (which === 'arpa') {
    customArpaMap.value = null
  } else {
    customVocaloidMap.value = null
  }
  recomputeResults()
}

/** 将一个 ARPABET 音素字符串（空格分隔）转换为 VOCALOID 音素字符串。
 *  优先查用户导入的自定义映射表，未覆盖的音素回退到内置默认表。 */
/** 将后端返回的 ARPABET 音素（大写 + 重音数字，如 "OW1 K AH0"）
 *  规整为小写且去除重音标记的形式（如 "ow k ah"），仅影响展示/导出，
 *  不影响 arpaToVocaloid 的映射逻辑（该函数内部本就会自行归一化）。 */
const formatArpaDisplay = (arpa: string): string => {
  if (!arpa) return ''
  return arpa
    .split(/\s+/)
    .filter(Boolean)
    .map((ph) => ph.toLowerCase().replace(/\d+$/, ''))
    .join(' ')
}

const arpaToVocaloid = (arpa: string): string => {
  if (!arpa) return ''
  return arpa
    .split(/\s+/)
    .filter(Boolean)
    .map((ph) => {
      const clean = ph.toLowerCase().replace(/\d+$/, '')
      if (customVocaloidMap.value && clean in customVocaloidMap.value) {
        return customVocaloidMap.value[clean]
      }
      return ARPABET_TO_VOCALOID[clean] ?? clean
    })
    .join(' ')
}

// ═══════════════════════════════════════════════════════════
// 数据结构与响应式变量
// ═══════════════════════════════════════════════════════════

interface ExtractedWord {
  index: number
  word: string
  arpa: string
  rawArpa: string
  vocaloid: string
  count: number
}

const inputText = ref('')
const results = ref<ExtractedWord[]>([])
const loading = ref(false)
const error = ref('')
const progress = ref(0)
const statusText = ref('')
const currentPage = ref(1)
const pageSize = ref(10)

const options = ref({
  caseSensitive: false,
  includeNumbers: true,
  splitBySpace: true,
})

// ═══════════════════════════════════════════════════════════
// 计算属性
// ═══════════════════════════════════════════════════════════

const paginatedResults = computed(() => {
  const totalPages = Math.max(1, Math.ceil(filteredResults.value.length / pageSize.value))
  if (currentPage.value > totalPages) {
    currentPage.value = totalPages
  }
  const start = (currentPage.value - 1) * pageSize.value
  const end = start + pageSize.value
  return filteredResults.value.slice(start, end)
})

/** 每页条数变化时：跳回第一页，避免出现"当前页超出总页数"导致列表空白的问题 */
const handlePageSizeChange = (size: number) => {
  pageSize.value = size
  currentPage.value = 1
}

/** 页码变化 —— el-pagination 的 v-model:current-page 已同步 currentPage，
 *  这里仅保留 hook 以便未来扩展（例如滚动回表格顶部）。 */
const handlePageChange = (page: number) => {
  currentPage.value = page
}

// ============== 搜索单词并跳转 ==============
const searchQuery = ref('')
const resultsTableRef = ref()
const highlightedWord = ref('')
let highlightTimer: ReturnType<typeof setTimeout> | null = null

const filteredResults = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return results.value
  return results.value.filter(r => r.word.toLowerCase().includes(q))
})

watch(searchQuery, () => {
  currentPage.value = 1
})

const rowClassName = ({ row }: { row: ExtractedWord }) =>
  row.word === highlightedWord.value ? 'highlighted-row' : ''

/** 搜索并跳转到指定单词所在的分页，滚动到该行并高亮提示 */
const jumpToWord = async (query: string) => {
  const q = query.trim().toLowerCase()
  if (!q) return

  const idx = results.value.findIndex(r => r.word.toLowerCase() === q)
  const targetIdx = idx >= 0 ? idx : results.value.findIndex(r => r.word.toLowerCase().includes(q))

  if (targetIdx < 0) {
    ElMessage.warning(t('englishG2P.searchNotFound', { word: query }))
    return
  }

  const target = results.value[targetIdx]
  const filteredIdx = filteredResults.value.findIndex(r => r.word === target.word && r.index === target.index)
  const posIdx = filteredIdx >= 0 ? filteredIdx : targetIdx
  currentPage.value = Math.floor(posIdx / pageSize.value) + 1

  highlightedWord.value = target.word
  if (highlightTimer) clearTimeout(highlightTimer)
  highlightTimer = setTimeout(() => {
    highlightedWord.value = ''
  }, 2000)

  await nextTick()
  const rows = resultsTableRef.value?.$el?.querySelectorAll?.('.el-table__row')
  if (rows) {
    for (const rowEl of rows) {
      if (rowEl.classList.contains('highlighted-row')) {
        rowEl.scrollIntoView({ behavior: 'smooth', block: 'center' })
        break
      }
    }
  }
}

const repeatedWordsCount = computed(() => {
  return results.value.filter((r) => r.count > 1).length
})

const avgPhonemes = computed(() => {
  if (results.value.length === 0) return 0
  const totalPhonemes = results.value.reduce((sum, r) => sum + (r.arpa.split(' ').length || 0), 0)
  return totalPhonemes / results.value.length
})

const uniqueWords = computed(() => {
  return new Set(results.value.map((r) => r.word.toLowerCase())).size
})

// ═══════════════════════════════════════════════════════════
// 方法
// ═══════════════════════════════════════════════════════════

const extractAndConvert = async () => {
  if (!inputText.value.trim()) {
    error.value = t('englishG2P.emptyInput')
    return
  }

  // 1. --- 核心修改：直接提取目标单词 ---
  // [a-zA-Z']+ 匹配连续的英文字母（含撇号如 don't）
  // 如果开启了包含数字，则加上 0-9
  const regex = options.value.includeNumbers ? /[a-zA-Z0-9']+/g : /[a-zA-Z']+/g;
  
  // match 会直接忽略所有中文、日文、标点，抓取出一个纯净的数组
  const matchedWords = inputText.value.match(regex);

  // 如果没有匹配到任何有效的单词，直接拦截
  if (!matchedWords || matchedWords.length === 0) {
    error.value = t('englishG2P.emptyInput') || '未检测到有效的英语单词';
    return
  }

  // --- 去重计数：重复出现的单词合并为一条，count 记录出现次数 ---
  // 分组键遵循「区分大小写」选项：未开启时按小写归并（VOCAL 与 vocal
  // 视为同一个词），开启时严格按原始大小写归并。
  const order: string[] = []
  const counts = new Map<string, number>()
  const displayWord = new Map<string, string>()

  for (const raw of matchedWords) {
    const key = options.value.caseSensitive ? raw : raw.toLowerCase()
    if (!counts.has(key)) {
      order.push(key)
      counts.set(key, 0)
      displayWord.set(key, raw)
    }
    counts.set(key, counts.get(key)! + 1)
  }

  // 发送给后端做 G2P 转换的文本：每个唯一单词只出现一次
  const uniqueWordsList = order.map((key) => displayWord.get(key)!)
  const pureText = uniqueWordsList.join(' ');
  // ---------------------------------

  loading.value = true
  error.value = ''
  progress.value = 0
  statusText.value = t('englishG2P.processing')

  try {
    const response = await fetch('/api/english/extract-g2p', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: pureText, // 2. --- 发送去重后的纯净单词列表 ---
        case_sensitive: options.value.caseSensitive,
        include_numbers: options.value.includeNumbers,
        split_by_space: true, // 强制后端按空格拆分，因为前端已经完美提取好了
      }),
    })

    if (!response.ok) {
      const errorData = await response.json()
      throw new Error(errorData.error || t('englishG2P.extractionFailed'))
    }

    const data = await response.json()
    results.value = data.results.map((r: any, idx: number) => {
      const key = options.value.caseSensitive ? r.word : (r.word || '').toLowerCase()
      // 自定义 ARPABET 词典按单词（不区分大小写）查找覆盖值；未命中时
      // 使用后端 g2p_en / MFA 词典给出的默认结果。
      const customArpa =
        customArpaMap.value &&
        (customArpaMap.value[r.word] ?? customArpaMap.value[(r.word || '').toLowerCase()])
      const rawArpa = formatArpaDisplay(r.arpa || '')
      const arpa = customArpa || rawArpa
      return {
        index: idx + 1,
        word: r.word,
        arpa,
        rawArpa,
        vocaloid: arpaToVocaloid(arpa),
        count: counts.get(key) ?? 1,
      }
    })

    progress.value = 100
    statusText.value = t('englishG2P.complete')
    currentPage.value = 1
    searchQuery.value = ''

    ElMessage.success(`${t('englishG2P.extractionSuccess')} ${results.value.length} ${t('englishG2P.words')}`)
  } catch (err) {
    error.value = err instanceof Error ? err.message : t('englishG2P.extractionFailed')
  } finally {
    loading.value = false
  }
}

const clearAll = () => {
  ElMessageBox.confirm(t('englishG2P.confirmClear'), t('englishG2P.warning'), {
    confirmButtonText: t('englishG2P.ok'),
    cancelButtonText: t('englishG2P.cancel'),
    type: 'warning',
  })
    .then(() => {
      inputText.value = ''
      results.value = []
      error.value = ''
      currentPage.value = 1
      searchQuery.value = ''
    })
    .catch(() => {})
}

const removeResult = (index: number) => {
  results.value = results.value.filter((r) => r.index !== index)
  // 重新编号
  results.value.forEach((r, idx) => {
    r.index = idx + 1
  })
}

const copyToClipboard = (text: string) => {
  navigator.clipboard.writeText(text).then(() => {
    ElMessage.success(t('englishG2P.copiedSuccess'))
  })
}

const copyAllToClipboard = () => {
  const text = results.value
    .map((r) => `${r.word}\t${r.arpa}`)
    .join('\n')
  navigator.clipboard.writeText(text).then(() => {
    ElMessage.success(t('englishG2P.copiedAllSuccess'))
  })
}

// 触发单个文件下载的小工具函数
const downloadFile = (content: string, filename: string, mime: string) => {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/**
 * 导出结果为符合词典导入格式的文件。
 *
 * 与 dictionary_manager.py 的导入逻辑（_extract_entries_from_json_payload /
 * import_csv_text）保持一致：
 *   - JSON: {"notation": "synthesizerv" | "vocaloid", "entries": {WORD: phones}}
 *   - CSV : "word,phonemes" 两列（首行表头）
 *
 * scope 参数决定实际下载哪些文件：
 *   'arpa'     → 仅下载 ARPABET（synthesizerv）词典
 *   'vocaloid' → 仅下载 VOCALOID 词典
 *   'all'      → 两个文件都下载（默认，对应旧版行为）
 */
const exportResults = (format: 'json' | 'csv', scope: 'arpa' | 'vocaloid' | 'all' = 'all') => {
  if (results.value.length === 0) return

  // 去重后的词条（以最新出现的单词/音素为准，词典本身按单词唯一存储）
  const arpaEntries: Record<string, string> = {}
  const vocaloidEntries: Record<string, string> = {}
  for (const r of results.value) {
    if (!r.word) continue
    arpaEntries[r.word] = r.arpa
    vocaloidEntries[r.word] = r.vocaloid
  }

  const wantArpa = scope === 'arpa' || scope === 'all'
  const wantVocaloid = scope === 'vocaloid' || scope === 'all'

  if (format === 'json') {
    if (wantArpa) {
      downloadFile(
        JSON.stringify({ notation: 'synthesizerv', entries: arpaEntries }, null, 2),
        'english-g2p-synthesizerv.json',
        'application/json',
      )
    }
    if (wantVocaloid) {
      downloadFile(
        JSON.stringify({ notation: 'vocaloid', entries: vocaloidEntries }, null, 2),
        'english-g2p-vocaloid.json',
        'application/json',
      )
    }
  } else {
    const toCsv = (entries: Record<string, string>) => {
      const rows = Object.keys(entries).map(
        (word) => `"${word.replace(/"/g, '""')}","${entries[word].replace(/"/g, '""')}"`,
      )
      return ['word,phonemes', ...rows].join('\n')
    }

    // 加 UTF-8 BOM，避免 Excel 等工具打开含特殊音素符号的 CSV 出现乱码
    if (wantArpa) {
      downloadFile('\ufeff' + toCsv(arpaEntries), 'english-g2p-synthesizerv.csv', 'text/csv')
    }
    if (wantVocaloid) {
      downloadFile('\ufeff' + toCsv(vocaloidEntries), 'english-g2p-vocaloid.csv', 'text/csv')
    }
  }

  ElMessage.success(t('englishG2P.exportSuccess'))
}
</script>

<style scoped>
.english-g2p-container {
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.95) 0%, rgba(245, 245, 250, 0.9) 100%);
  border-radius: 12px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.08);
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 18px;
  font-weight: 600;
  color: #2c3e50;
}

.help-icon {
  font-size: 18px;
  color: #a0aec0;
  cursor: pointer;
  transition: color 0.2s;
}

.help-icon:hover {
  color: #4f46e5;
}

.input-section {
  margin-bottom: 24px;
}

.button-group {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.custom-dict-banner {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.options-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.option-item {
  display: flex;
  align-items: center;
}

.loading-state {
  margin: 16px 0;
  padding: 12px;
  background: #f0f4ff;
  border-radius: 8px;
}

.status-text {
  display: block;
  margin-top: 8px;
  color: #4f46e5;
  font-size: 12px;
  text-align: center;
}

.error-message {
  margin: 16px 0;
}

.results-section {
  margin-top: 32px;
  padding-top: 24px;
  border-top: 1px solid #e5e7eb;
}

.results-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}

.results-header h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 16px;
  color: #2c3e50;
}

.result-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.results-table {
  margin-bottom: 16px;
}

.phoneme-code {
  background: #f5f5f5;
  padding: 4px 8px;
  border-radius: 4px;
  font-family: 'Courier New', monospace;
  color: #4f46e5;
  font-size: 12px;
}

.vocaloid-code {
  background: #fdf2f8;
  color: #be185d;
}

.parts-container {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.part-tag {
  font-size: 11px;
}

.search-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 12px 0;
}

.search-result-hint {
  color: #909399;
  font-size: 12px;
}

:deep(.highlighted-row) {
  animation: highlight-fade 2s ease-out;
}

@keyframes highlight-fade {
  0% {
    background-color: #fdf6ec;
  }
  100% {
    background-color: transparent;
  }
}

.pagination {
  margin-top: 16px;
  text-align: right;
}

.statistics-section {
  margin-top: 24px;
  padding: 16px;
  background: linear-gradient(135deg, rgba(79, 70, 229, 0.05) 0%, rgba(124, 58, 237, 0.05) 100%);
  border-radius: 8px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 16px;
}

.stat-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.stat-label {
  font-size: 12px;
  color: #64748b;
  font-weight: 500;
}

.stat-value {
  font-size: 24px;
  font-weight: 700;
  color: #4f46e5;
}
</style>
