<template>
  <div class="settings-container">
    <el-card class="settings-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-title">⚙️ {{ t('settings.pageTitle') }}</span>
        </div>
      </template>

      <p class="page-subtitle">{{ t('settings.pageSubtitle') }}</p>

      <el-form v-loading="loading" label-position="top" class="settings-form">
        <el-form-item :label="t('settings.autoUpdateModels')">
          <el-switch v-model="form.auto_update_models" />
          <p class="help-text">{{ t('settings.autoUpdateModelsHint') }}</p>
        </el-form-item>

        <el-form-item :label="t('settings.useMirror')">
          <el-switch v-model="form.use_mirror" />
          <p class="help-text">{{ t('settings.useMirrorHint') }}</p>
        </el-form-item>

        <el-form-item v-if="form.use_mirror" :label="t('settings.mirrorUrl')">
          <el-input
            v-model="form.mirror_url"
            :placeholder="t('settings.mirrorUrlPlaceholder')"
            style="max-width: 420px"
          />
        </el-form-item>

        <el-form-item :label="t('settings.hideConsoleWindow')">
          <el-switch v-model="form.hide_console_window" />
          <p class="help-text">{{ t('settings.hideConsoleWindowHint') }}</p>
        </el-form-item>

        <el-alert type="warning" :closable="false" show-icon class="restart-hint">
          <template #title>{{ t('settings.restartHint') }}</template>
        </el-alert>

        <el-form-item :label="t('settings.skipStartQwen3')">
          <el-switch v-model="form.skip_start_qwen3_server" />
          <p class="help-text">{{ t('settings.skipStartQwen3Hint') }}</p>
        </el-form-item>

        <el-form-item :label="t('settings.skipStartNemo')">
          <el-switch v-model="form.skip_start_nemo_server" />
          <p class="help-text">{{ t('settings.skipStartNemoHint') }}</p>
        </el-form-item>

        <el-alert type="info" :closable="false" show-icon class="no-restart-hint">
          <template #title>{{ t('settings.skipStartApplyHint') }}</template>
        </el-alert>

        <el-divider />

        <div class="section-heading">
          <span>🎚️ {{ t('settings.tuningSectionTitle') }}</span>
        </div>
        <p class="page-subtitle">{{ t('settings.tuningSectionSubtitle') }}</p>

        <el-alert type="success" :closable="false" show-icon class="no-restart-hint">
          <template #title>{{ t('settings.tuningNoRestartHint') }}</template>
        </el-alert>

        <p class="group-label">{{ t('settings.tuningGroupOnsetDelay') }}</p>
        <el-row :gutter="16">
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.qwen3FaOnsetDelaySec')">
              <el-input-number
                v-model="form.qwen3_fa_onset_delay_sec"
                :min="0" :max="2" :step="0.01" :precision="3"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.qwen3FaOnsetDelaySecHint') }}</p>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.qwen3AsrOnsetDelaySec')">
              <el-input-number
                v-model="form.qwen3_asr_onset_delay_sec"
                :min="0" :max="2" :step="0.01" :precision="3"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.qwen3AsrOnsetDelaySecHint') }}</p>
            </el-form-item>
          </el-col>
        </el-row>

        <p class="group-label">{{ t('settings.tuningGroupMinSylDur') }}</p>
        <el-row :gutter="16">
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.qwen3FaMinSylDurSec')">
              <el-input-number
                v-model="form.qwen3_fa_min_syl_dur_sec"
                :min="0" :max="1" :step="0.01" :precision="3"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.qwen3FaMinSylDurSecHint') }}</p>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.qwen3AsrMinSylDurSec')">
              <el-input-number
                v-model="form.qwen3_asr_min_syl_dur_sec"
                :min="0" :max="1" :step="0.01" :precision="3"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.qwen3AsrMinSylDurSecHint') }}</p>
            </el-form-item>
          </el-col>
        </el-row>

        <p class="group-label">{{ t('settings.tuningGroupCtcStretch') }}</p>
        <el-row :gutter="16">
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.ctcMaxCjkCharSec')">
              <el-input-number
                v-model="form.ctc_max_cjk_char_sec"
                :min="0.05" :max="5" :step="0.05" :precision="2"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.ctcMaxCjkCharSecHint') }}</p>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.ctcMaxCjkParticleSec')">
              <el-input-number
                v-model="form.ctc_max_cjk_particle_sec"
                :min="0.05" :max="5" :step="0.05" :precision="2"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.ctcMaxCjkParticleSecHint') }}</p>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.ctcMaxEnWordSec')">
              <el-input-number
                v-model="form.ctc_max_en_word_sec"
                :min="0.05" :max="5" :step="0.05" :precision="2"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.ctcMaxEnWordSecHint') }}</p>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.ctcMinSpSec')">
              <el-input-number
                v-model="form.ctc_min_sp_sec"
                :min="0" :max="2" :step="0.01" :precision="3"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.ctcMinSpSecHint') }}</p>
            </el-form-item>
          </el-col>
        </el-row>

        <p class="group-label">{{ t('settings.tuningGroupChunking') }}</p>
        <el-row :gutter="16">
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.qwen3FaMinSentenceChunkSec')">
              <el-input-number
                v-model="form.qwen3_fa_min_sentence_chunk_sec"
                :min="0.1" :max="600" :step="0.5" :precision="1"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.qwen3FaMinSentenceChunkSecHint') }}</p>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.qwen3FaMaxSentenceChunkSec')">
              <el-input-number
                v-model="form.qwen3_fa_max_sentence_chunk_sec"
                :min="1" :max="600" :step="1" :precision="1"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.qwen3FaMaxSentenceChunkSecHint') }}</p>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item>
          <el-button size="small" @click="resetTuningToDefaults">
            ↺ {{ t('settings.tuningResetButton') }}
          </el-button>
        </el-form-item>

        <el-divider />

        <div class="section-heading">
          <span>🚀 {{ t('settings.whisperxSectionTitle') }}</span>
        </div>
        <p class="page-subtitle">{{ t('settings.whisperxSectionSubtitle') }}</p>

        <el-alert type="success" :closable="false" show-icon class="no-restart-hint">
          <template #title>{{ t('settings.tuningNoRestartHint') }}</template>
        </el-alert>

        <el-form-item :label="t('settings.qwen3FaUseWhisperxPrepass')">
          <el-switch v-model="form.qwen3_fa_use_whisperx_prepass" />
          <p class="help-text">{{ t('settings.qwen3FaUseWhisperxPrepassHint') }}</p>
        </el-form-item>

        <el-row :gutter="16">
          <el-col v-if="form.qwen3_fa_use_whisperx_prepass" :xs="24" :sm="12">
            <el-form-item :label="t('settings.qwen3FaWhisperxPrepassModel')">
              <el-select v-model="form.qwen3_fa_whisperx_prepass_model" style="width: 100%; max-width: 320px">
                <el-option
                  v-for="model in WHISPERX_MODEL_OPTIONS"
                  :key="model"
                  :value="model"
                  :label="t(`processor.whisperModel${modelLabelKey(model)}`)"
                />
              </el-select>
              <p class="help-text">{{ t('settings.qwen3FaWhisperxPrepassModelHint') }}</p>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.whisperxBatchSize')">
              <el-input-number
                v-model="form.whisperx_batch_size"
                :min="1" :max="128" :step="1" :precision="0"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.whisperxBatchSizeHint') }}</p>
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider />

        <div class="section-heading">
          <span>✂️ {{ t('settings.ttsSegmentSectionTitle') }}</span>
        </div>
        <p class="page-subtitle">{{ t('settings.ttsSegmentSectionSubtitle') }}</p>

        <el-alert type="success" :closable="false" show-icon class="no-restart-hint">
          <template #title>{{ t('settings.tuningNoRestartHint') }}</template>
        </el-alert>

        <el-form-item :label="t('settings.ttsDisableNewlineSplit')">
          <el-switch v-model="form.tts_disable_newline_split" />
          <p class="help-text">{{ t('settings.ttsDisableNewlineSplitHint') }}</p>
        </el-form-item>

        <el-form-item v-if="!form.tts_disable_newline_split" :label="t('settings.ttsNewlineSplitEveryN')">
          <el-input-number
            v-model="form.tts_newline_split_every_n"
            :min="1" :max="100" :step="1" :precision="0"
            controls-position="right" style="width: 100%; max-width: 240px"
          />
          <p class="help-text">{{ t('settings.ttsNewlineSplitEveryNHint') }}</p>
        </el-form-item>

        <el-form-item :label="t('settings.ttsDisableSegmentLenSplit')">
          <el-switch v-model="form.tts_disable_segment_len_split" />
          <p class="help-text">{{ t('settings.ttsDisableSegmentLenSplitHint') }}</p>
        </el-form-item>

        <el-row v-if="!form.tts_disable_segment_len_split" :gutter="16">
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.ttsMinSegmentLen')">
              <el-input-number
                v-model="form.tts_min_segment_len"
                :min="1" :max="5000" :step="10" :precision="0"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.ttsMinSegmentLenHint') }}</p>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('settings.ttsMaxSegmentLen')">
              <el-input-number
                v-model="form.tts_max_segment_len"
                :min="1" :max="5000" :step="10" :precision="0"
                controls-position="right" style="width: 100%; max-width: 240px"
              />
              <p class="help-text">{{ t('settings.ttsMaxSegmentLenHint') }}</p>
            </el-form-item>
          </el-col>
        </el-row>

        <el-alert
          v-if="!form.tts_disable_segment_len_split && form.tts_min_segment_len > form.tts_max_segment_len"
          type="warning" :closable="false" show-icon class="restart-hint"
        >
          <template #title>{{ t('settings.ttsSegmentLenOrderWarning') }}</template>
        </el-alert>

        <el-form-item>
          <el-button size="small" @click="resetTtsSegmentLenToDefaults">
            ↺ {{ t('settings.tuningResetButton') }}
          </el-button>
        </el-form-item>

        <el-divider />

        <div class="section-heading">
          <span>🎙️ {{ t('settings.subtitleImportSectionTitle') }}</span>
        </div>
        <p class="page-subtitle">{{ t('settings.subtitleImportSectionSubtitle') }}</p>

        <el-alert type="success" :closable="false" show-icon class="no-restart-hint">
          <template #title>{{ t('settings.tuningNoRestartHint') }}</template>
        </el-alert>

        <el-form-item :label="t('settings.subtitleImportSkipSplitEveryN')">
          <el-input-number
            v-model="form.subtitle_import_skip_split_every_n"
            :min="1" :max="50" :step="1" :precision="0"
            controls-position="right" style="width: 100%; max-width: 240px"
          />
          <p class="help-text">{{ t('settings.subtitleImportSkipSplitEveryNHint') }}</p>
        </el-form-item>

        <el-form-item>
          <el-button size="small" @click="resetSubtitleImportSplitToDefaults">
            ↺ {{ t('settings.tuningResetButton') }}
          </el-button>
        </el-form-item>

        <el-alert
          v-for="item in restartSummary"
          :key="item.service"
          :type="item.type"
          :closable="false"
          show-icon
          class="restart-result"
        >
          <template #title>{{ item.text }}</template>
        </el-alert>

        <el-form-item style="margin-top: 20px">
          <el-button type="primary" size="large" :loading="saving" @click="save">
            💾 {{ t('settings.saveButton') }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

interface AppSettings {
  auto_update_models: boolean
  use_mirror: boolean
  mirror_url: string
  hide_console_window: boolean
  // 独立开关：勾选后，下次完整启动本应用（重新打开 exe 启动器）时不再
  // 拉起对应的微服务进程；只在下次启动时生效，不影响当前正在运行的
  // 进程，也不会像上面几项一样触发保存后的自动重启。
  skip_start_qwen3_server: boolean
  skip_start_nemo_server: boolean
  // ── alt_aligners.py 对齐调优参数（均以秒为单位）────────────────────────
  // 这批参数只被主进程内的 alt_aligners.py 消费，保存后无需重启任何进程，
  // 下一次对齐任务即可生效（与上面三项需要重启 Qwen3 / NeMo 微服务不同）。
  qwen3_fa_onset_delay_sec: number
  qwen3_asr_onset_delay_sec: number
  qwen3_fa_min_syl_dur_sec: number
  qwen3_asr_min_syl_dur_sec: number
  ctc_max_cjk_char_sec: number
  ctc_max_cjk_particle_sec: number
  ctc_max_en_word_sec: number
  ctc_min_sp_sec: number
  // Qwen3-ForcedAligner 长音频分段对齐；同样属于"实时生效，无需重启"的
  // 调优参数组。现在按参考文本句末标点分段，这两项只用来处理单句过短/
  // 过长这两种边缘情况（详见 alt_aligners.py _plan_sentence_aligned_
  // chunks() 顶部说明）。
  qwen3_fa_min_sentence_chunk_sec: number
  qwen3_fa_max_sentence_chunk_sec: number
  // ── WhisperX 相关（同样"实时生效，无需重启"）──────────────────────────
  // 开启后，Qwen3-ForcedAligner 在长音频分段对齐前先用 WhisperX 做一次
  // 轻量 ASR 粗测，用真实语音起止时间戳规划分段边界。
  qwen3_fa_use_whisperx_prepass: boolean
  // 上面粗测步骤专用的 Whisper 模型档位，仅在开关打开时才需要展示/生效。
  qwen3_fa_whisperx_prepass_model: string
  // WhisperX 转录 batch_size，独立对齐后端与上面的粗测预处理共用同一个值。
  whisperx_batch_size: number
  // ── tts_processor.py 逐句合成分段长度（字符数，同样"实时生效，无需
  // 重启"）── 单行文本超过 tts_max_segment_len 才会二次切割，切割点落在
  // [tts_min_segment_len, tts_max_segment_len] 区间内。
  tts_min_segment_len: number
  tts_max_segment_len: number
  // 每多少个换行才切一段（默认 1，与改造前行为一致）。仅在
  // tts_disable_newline_split 为 false 时生效。
  tts_newline_split_every_n: number
  // 完全禁用"按换行分段"这一步；打开后整段文本先合并为一个整体，是否
  // 切割完全交给 tts_disable_segment_len_split / 长度区间决定。
  tts_disable_newline_split: boolean
  // 完全禁用"单段过长再二次切割"这一步；打开后每个分段保持原样，不再
  // 按 tts_min_segment_len / tts_max_segment_len 区间寻找标点切割。
  tts_disable_segment_len_split: boolean
  // ── subtitle_import.py 字幕跟读：跳过分割音频（默认 1，与改造前行为
  // 一致）。仅影响"字幕跟读"这一个功能：连续且中间没有静音间隙的相邻
  // 字幕，每凑够 N 条才合并成一段音频一起送 Qwen3-FA 对齐一次，减少
  // 切分/对齐调用次数。静音间隙始终会打断合并，不受此设置影响。
  subtitle_import_skip_split_every_n: number
}

type RestartStatus = 'restarted' | 'not_running' | 'failed'

interface RestartResult {
  status: RestartStatus
  detail?: string
}

// 与 app_settings.py 的 DEFAULT_SETTINGS 保持一致，供“恢复默认值”按钮使用。
const TUNING_DEFAULTS = {
  qwen3_fa_onset_delay_sec: 0.06,
  qwen3_asr_onset_delay_sec: 0.06,
  qwen3_fa_min_syl_dur_sec: 0.02,
  qwen3_asr_min_syl_dur_sec: 0.02,
  ctc_max_cjk_char_sec: 0.50,
  ctc_max_cjk_particle_sec: 0.35,
  ctc_max_en_word_sec: 1.20,
  ctc_min_sp_sec: 0.15,
  qwen3_fa_min_sentence_chunk_sec: 3.0,
  qwen3_fa_max_sentence_chunk_sec: 20.0,
} as const

// WhisperX 相关默认值：与 app_settings.py 的 DEFAULT_SETTINGS 保持一致。
// 注意 qwen3_fa_use_whisperx_prepass 默认关闭，与后端一致（需要用户显式开启）。
const WHISPERX_DEFAULTS = {
  qwen3_fa_use_whisperx_prepass: false,
  qwen3_fa_whisperx_prepass_model: 'large-v3',
  whisperx_batch_size: 16,
} as const

// TTS 逐句合成分段长度默认值：与 app_settings.py 的 DEFAULT_SETTINGS 保持一致。
const TTS_SEGMENT_LEN_DEFAULTS = {
  tts_min_segment_len: 250,
  tts_max_segment_len: 500,
} as const

// TTS 换行/分段开关默认值：与 app_settings.py 的 DEFAULT_SETTINGS 保持一致。
const TTS_SPLIT_OPTION_DEFAULTS = {
  tts_newline_split_every_n: 1,
  tts_disable_newline_split: false,
  tts_disable_segment_len_split: false,
} as const

// 字幕跟读"跳过分割音频"默认值：与 app_settings.py 的 DEFAULT_SETTINGS 保持一致。
const SUBTITLE_IMPORT_SPLIT_DEFAULTS = {
  subtitle_import_skip_split_every_n: 1,
} as const

// 与 alt_aligners.py 里 WhisperXAligner.SUPPORTED_MODELS 保持一致，
// 复用 processor.whisperModelXxx 系列翻译（单文件处理页已有同一份模型列表）。
const WHISPERX_MODEL_OPTIONS = [
  'large-v3',
  'large-v3-turbo',
  'large-v2',
  'medium',
  'small',
  'base',
  'tiny',
] as const

// 'large-v3-turbo' → 'LargeV3Turbo'，用于拼出 processor.whisperModelLargeV3Turbo 这类 key
const modelLabelKey = (model: string) =>
  model
    .split('-')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('')

const form = ref<AppSettings>({
  auto_update_models: false,
  use_mirror: false,
  mirror_url: 'https://hf-mirror.com/',
  hide_console_window: false,
  skip_start_qwen3_server: false,
  skip_start_nemo_server: false,
  ...TUNING_DEFAULTS,
  ...WHISPERX_DEFAULTS,
  ...TTS_SEGMENT_LEN_DEFAULTS,
  ...TTS_SPLIT_OPTION_DEFAULTS,
  ...SUBTITLE_IMPORT_SPLIT_DEFAULTS,
})

const resetTuningToDefaults = () => {
  Object.assign(form.value, TUNING_DEFAULTS)
  ElMessage.info(t('settings.tuningResetHint'))
}

const resetTtsSegmentLenToDefaults = () => {
  Object.assign(form.value, TTS_SEGMENT_LEN_DEFAULTS, TTS_SPLIT_OPTION_DEFAULTS)
  ElMessage.info(t('settings.tuningResetHint'))
}

const resetSubtitleImportSplitToDefaults = () => {
  Object.assign(form.value, SUBTITLE_IMPORT_SPLIT_DEFAULTS)
  ElMessage.info(t('settings.tuningResetHint'))
}

const loading = ref(false)
const saving = ref(false)
// 最近一次保存后，Qwen3 / NeMo 两个微服务各自的重启结果（未保存过则为 null）
const restartResult = ref<{ qwen3: RestartResult; nemo: RestartResult } | null>(null)

const restartSummary = computed(() => {
  if (!restartResult.value) return []
  const rows: { service: string; type: 'success' | 'info' | 'warning'; text: string }[] = []
  for (const [key, label] of [['qwen3', 'Qwen3-ASR'], ['nemo', 'NeMo Forced Aligner']] as const) {
    const r = restartResult.value[key]
    if (!r) continue
    if (r.status === 'restarted') {
      rows.push({ service: key, type: 'success', text: t('settings.restartServiceRestarted', { service: label }) })
    } else if (r.status === 'not_running') {
      rows.push({ service: key, type: 'info', text: t('settings.restartServiceNotRunning', { service: label }) })
    } else {
      rows.push({ service: key, type: 'warning', text: t('settings.restartServiceFailed', { service: label }) })
    }
  }
  return rows
})

// 将后端返回的设置对象映射为表单值；调优参数缺失/非法时回退到默认值，
// 与 loadSettings / save 两处共用，避免重复。
const applySettingsToForm = (settings: Record<string, any> | undefined) => {
  const num = (key: keyof typeof TUNING_DEFAULTS): number => {
    const v = Number(settings?.[key])
    return Number.isFinite(v) ? v : TUNING_DEFAULTS[key]
  }
  // whisperx_batch_size 走独立的整数校验（1-128），与后端 save_settings() 的钳制范围一致
  const batchSize = Number(settings?.whisperx_batch_size)
  const prepassModel = String(settings?.qwen3_fa_whisperx_prepass_model || '').trim()
  // tts_min_segment_len / tts_max_segment_len：整数校验 + 钳制到 [1, 5000]，
  // 与后端 save_settings() 的钳制范围一致；若钳制后 min > max 则交换两者，
  // 与后端行为保持一致，避免表单里出现区间倒置的中间态。
  const rawTtsMinLen = Number(settings?.tts_min_segment_len)
  const rawTtsMaxLen = Number(settings?.tts_max_segment_len)
  let ttsMinLen = Number.isFinite(rawTtsMinLen)
    ? Math.min(Math.max(Math.round(rawTtsMinLen), 1), 5000)
    : TTS_SEGMENT_LEN_DEFAULTS.tts_min_segment_len
  let ttsMaxLen = Number.isFinite(rawTtsMaxLen)
    ? Math.min(Math.max(Math.round(rawTtsMaxLen), 1), 5000)
    : TTS_SEGMENT_LEN_DEFAULTS.tts_max_segment_len
  if (ttsMinLen > ttsMaxLen) {
    ;[ttsMinLen, ttsMaxLen] = [ttsMaxLen, ttsMinLen]
  }
  // tts_newline_split_every_n：整数校验 + 钳制到 [1, 100]，与后端
  // save_settings() 的钳制范围一致。
  const rawNewlineEveryN = Number(settings?.tts_newline_split_every_n)
  const newlineEveryN = Number.isFinite(rawNewlineEveryN)
    ? Math.min(Math.max(Math.round(rawNewlineEveryN), 1), 100)
    : TTS_SPLIT_OPTION_DEFAULTS.tts_newline_split_every_n
  // subtitle_import_skip_split_every_n：整数校验 + 钳制到 [1, 50]，与后端
  // save_settings() 的钳制范围一致。
  const rawSkipSplitN = Number(settings?.subtitle_import_skip_split_every_n)
  const skipSplitN = Number.isFinite(rawSkipSplitN)
    ? Math.min(Math.max(Math.round(rawSkipSplitN), 1), 50)
    : SUBTITLE_IMPORT_SPLIT_DEFAULTS.subtitle_import_skip_split_every_n
  form.value = {
    auto_update_models: !!settings?.auto_update_models,
    use_mirror: !!settings?.use_mirror,
    mirror_url: settings?.mirror_url || 'https://hf-mirror.com/',
    hide_console_window: !!settings?.hide_console_window,
    skip_start_qwen3_server: !!settings?.skip_start_qwen3_server,
    skip_start_nemo_server: !!settings?.skip_start_nemo_server,
    qwen3_fa_onset_delay_sec: num('qwen3_fa_onset_delay_sec'),
    qwen3_asr_onset_delay_sec: num('qwen3_asr_onset_delay_sec'),
    qwen3_fa_min_syl_dur_sec: num('qwen3_fa_min_syl_dur_sec'),
    qwen3_asr_min_syl_dur_sec: num('qwen3_asr_min_syl_dur_sec'),
    ctc_max_cjk_char_sec: num('ctc_max_cjk_char_sec'),
    ctc_max_cjk_particle_sec: num('ctc_max_cjk_particle_sec'),
    ctc_max_en_word_sec: num('ctc_max_en_word_sec'),
    ctc_min_sp_sec: num('ctc_min_sp_sec'),
    qwen3_fa_min_sentence_chunk_sec: num('qwen3_fa_min_sentence_chunk_sec'),
    qwen3_fa_max_sentence_chunk_sec: num('qwen3_fa_max_sentence_chunk_sec'),
    qwen3_fa_use_whisperx_prepass: !!settings?.qwen3_fa_use_whisperx_prepass,
    qwen3_fa_whisperx_prepass_model:
      (WHISPERX_MODEL_OPTIONS as readonly string[]).includes(prepassModel)
        ? prepassModel
        : WHISPERX_DEFAULTS.qwen3_fa_whisperx_prepass_model,
    whisperx_batch_size: Number.isFinite(batchSize)
      ? Math.min(Math.max(Math.round(batchSize), 1), 128)
      : WHISPERX_DEFAULTS.whisperx_batch_size,
    tts_min_segment_len: ttsMinLen,
    tts_max_segment_len: ttsMaxLen,
    tts_newline_split_every_n: newlineEveryN,
    tts_disable_newline_split: !!settings?.tts_disable_newline_split,
    tts_disable_segment_len_split: !!settings?.tts_disable_segment_len_split,
    subtitle_import_skip_split_every_n: skipSplitN,
  }
}

const loadSettings = async () => {
  loading.value = true
  try {
    const res = await fetch('/api/settings')
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)
    applySettingsToForm(data.settings)
  } catch (e: any) {
    ElMessage.error(t('settings.loadFailed', { error: e?.message || String(e) }))
  } finally {
    loading.value = false
  }
}

const save = async () => {
  saving.value = true
  restartResult.value = null
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(form.value),
    })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || res.statusText)

    if (data.settings) {
      applySettingsToForm(data.settings)
    }
    // 后端在保存设置后会自动尝试重启 Qwen3 / NeMo 两个独立微服务，
    // 这里把结果展示给用户，而不是只留一句"请自行重启"的静态提示。
    if (data.restart) {
      restartResult.value = data.restart
    }
    ElMessage.success(t('settings.saveSuccess'))
  } catch (e: any) {
    ElMessage.error(t('settings.saveFailed', { error: e?.message || String(e) }))
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadSettings()
})
</script>

<style scoped>
.settings-container {
  width: 100%;
}

.settings-card {
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

.settings-form :deep(.el-form-item) {
  margin-bottom: 22px;
}

.help-text {
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
  margin: 8px 0 0;
  max-width: 640px;
}

.restart-hint {
  margin-top: 4px;
}

.restart-result {
  margin-top: 10px;
}

.section-heading {
  font-size: 15px;
  font-weight: bold;
  color: #333;
  margin: 4px 0 4px;
}

.group-label {
  color: #606266;
  font-size: 13px;
  font-weight: 600;
  margin: 18px 0 8px;
}

.no-restart-hint {
  margin-bottom: 8px;
}
</style>

