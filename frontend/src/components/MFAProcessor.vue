<template>
  <div class="processor-container">
    <el-card class="processor-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-title">📁 {{ t('processor.cardTitle') }}</span>
          <div class="header-actions">
            <el-tooltip :content="t('processor.githubTooltip')" placement="bottom">
              <el-button 
                link 
                @click="openGitHub"
                type="primary"
              >
                🔗 {{ t('processor.githubLink') }}
              </el-button>
            </el-tooltip>
            <el-tooltip :content="t('processor.checkStatus')" placement="bottom">
              <el-button link @click="refreshStatus" :loading="checkingStatus">
                🔄 {{ t('processor.checkStatus') }}
              </el-button>
            </el-tooltip>
          </div>
        </div>
      </template>

      <el-form :model="formData" label-position="top" class="processor-form">
        <!-- 输入模式：TTS跟读（讲述人 + EdgeTTS）/ 音频跟读（原有上传音频对齐流程） -->
        <el-form-item :label="t('processor.inputModeLabel')">
          <el-radio-group v-model="inputMode" @change="handleInputModeChange">
            <el-radio value="tts">{{ t('processor.inputModeTts') }}</el-radio>
            <el-radio value="audio">{{ t('processor.inputModeAudio') }}</el-radio>
            <el-radio value="subtitle">{{ t('processor.inputModeSubtitle') }}</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="inputMode === 'audio'" :label="t('processor.audioFile')">
          <el-upload
            :key="audioUploadKey"
            drag
            action="#"
            :auto-upload="false"
            :limit="1"
            :on-exceed="handleExceed"
            @change="handleAudioSelect"
            accept=".wav,.mp3,.flac,.m4a,.aac,.ogg"
          >
            <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
            <div class="el-upload__text">
              {{ t('processor.dragAudio') }}
            </div>
            <template #tip>
              <div class="el-upload__tip">
                {{ t('processor.supportedAudio') }}
              </div>
            </template>
          </el-upload>
          <div v-if="formData.audioFile" class="file-info">
            ✓ {{ formData.audioFile.name }} ({{ formatFileSize(formData.audioFile.size) }})
          </div>
        </el-form-item>

        <!-- 字幕跟读专属面板：完整音频 + SRT/LRC 字幕文件，按字幕时间轴切分后
             固定用 Qwen3-ForcedAligner 逐句强制对齐，拼接成覆盖整段音频的 LAB。 -->
        <template v-if="inputMode === 'subtitle'">
          <el-form-item :label="t('processor.audioFile')">
            <el-upload
              :key="subtitleAudioUploadKey"
              drag
              action="#"
              :auto-upload="false"
              :limit="1"
              :on-exceed="handleExceed"
              @change="handleSubtitleAudioSelect"
              accept=".wav,.mp3,.flac,.m4a,.aac,.ogg"
            >
              <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
              <div class="el-upload__text">{{ t('processor.dragAudio') }}</div>
              <template #tip>
                <div class="el-upload__tip">{{ t('processor.supportedAudio') }}</div>
              </template>
            </el-upload>
            <div v-if="subtitleImport.audioFile" class="file-info">
              ✓ {{ subtitleImport.audioFile.name }} ({{ formatFileSize(subtitleImport.audioFile.size) }})
            </div>
          </el-form-item>

          <el-form-item :label="t('processor.subtitleFile')">
            <el-upload
              :key="subtitleFileUploadKey"
              drag
              action="#"
              :auto-upload="false"
              :limit="1"
              :on-exceed="handleExceed"
              @change="handleSubtitleFileSelect"
              accept=".srt,.lrc,.txt"
            >
              <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
              <div class="el-upload__text">{{ t('processor.dragSubtitle') }}</div>
              <template #tip>
                <div class="el-upload__tip">{{ t('processor.supportedSubtitle') }}</div>
              </template>
            </el-upload>
            <div v-if="subtitleImport.subtitleFile" class="file-info">
              ✓ {{ subtitleImport.subtitleFile.name }} ({{ formatFileSize(subtitleImport.subtitleFile.size) }})
            </div>
            <div class="help-text">{{ t('processor.subtitleImportHint') }}</div>
          </el-form-item>
        </template>

        <!-- TTS跟读专属面板：讲述人 / EdgeTTS 音色 / 语速·音调·音量 / 预览
             （对应"音频跟读"下的音频上传区域；TTS跟读用合成语音代替上传音频） -->
        <template v-if="inputMode === 'tts'">
          <!-- 选择 TTS：引擎本身（讲述人 = Windows 内置 TTS / EdgeTTS / 未来可扩展），
               与下面的"语音预设"是两个不同层面——预设只是"引擎+音色+参数"的命名快捷方式。 -->
          <el-form-item :label="t('processor.ttsEngine')">
            <el-select
              v-model="ttsConfig.engine"
              :loading="ttsEnginesLoading"
              style="width: 220px"
              :placeholder="t('processor.ttsEnginePlaceholder')"
            >
              <el-option
                v-for="eng in ttsEngines"
                :key="eng.id"
                :label="engineLabel(eng.id)"
                :value="eng.id"
                :disabled="!eng.available"
              >
                <span>{{ engineLabel(eng.id) }}</span>
                <span v-if="!eng.available" style="float: right; color: var(--el-color-danger); font-size: 12px; margin-left: 12px">
                  {{ eng.message }}
                </span>
              </el-option>
            </el-select>
          </el-form-item>

          <el-form-item :label="t('processor.narrator')">
            <el-select
              v-model="ttsConfig.narratorId"
              @change="handleNarratorSelect"
              filterable
              style="width: 240px"
              :placeholder="t('processor.narratorCustom')"
            >
              <el-option :label="t('processor.narratorCustom')" value="" />
              <el-option v-for="n in filteredNarrators" :key="n.id" :label="n.name" :value="n.id" />
            </el-select>
            <el-button link type="primary" @click="openNarratorManager" style="margin-left: 8px">
              ⚙ {{ t('processor.manageNarrators') }}
            </el-button>
          </el-form-item>

          <el-form-item :label="t('processor.ttsVoice')">
            <el-select
              v-model="ttsConfig.voice"
              filterable
              :loading="ttsVoicesLoading"
              style="width: 320px"
              :placeholder="t('processor.ttsVoicePlaceholder')"
            >
              <el-option v-for="v in ttsVoices" :key="v.id" :label="`${v.name} (${v.locale})`" :value="v.id" />
            </el-select>
          </el-form-item>

          <el-form-item :label="t('processor.ttsRate')">
            <el-slider v-model="ttsConfig.rateNum" :min="-50" :max="100" show-input style="max-width: 420px" />
          </el-form-item>
          <el-form-item :label="t('processor.ttsPitch')">
            <el-slider v-model="ttsConfig.pitchNum" :min="-50" :max="50" show-input style="max-width: 420px" />
          </el-form-item>
          <el-form-item v-if="ttsConfig.engine === 'windows_sapi'">
            <el-alert :closable="false" type="info" :title="t('processor.ttsPitchBestEffortHint')" show-icon />
          </el-form-item>
          <el-form-item :label="t('processor.ttsVolume')">
            <el-slider v-model="ttsConfig.volumeNum" :min="-50" :max="50" show-input style="max-width: 420px" />
          </el-form-item>

          <!-- 手动分段预览：不再随输入自动防抖触发，只在用户点击按钮时按新分
               段规则（优先按换行分段，单行过长再按句号/逗号二次切割）逐句合成
               生成预览音频（不做 Qwen3-FA 对齐，也不再有句子数量上限，会合成
               完整文本）。生成期间"开始处理"会被暂时禁用；生成完成后点击
               "开始处理"会直接复用这份分句音频去对齐，不会重新合成一遍。若在
               生成预览后又修改了文本 / 引擎 / 音色 / 语速 / 音调 / 音量 /
               语种，这份预览会失效，"开始处理"将退回"先合成再对齐"的完整
               流程。 -->
          <el-form-item :label="t('processor.ttsSegmentPreviewTitle')">
            <div style="width: 100%">
              <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
                <el-button
                  size="small"
                  @click="runSegmentPreview"
                  :loading="segmentPreview.loading"
                  :disabled="!ttsConfig.voice || !formData.text.trim()"
                >
                  🔄 {{ segmentPreview.audioUrl ? t('processor.ttsRegeneratePreview') : t('processor.ttsGeneratePreview') }}
                </el-button>
                <audio v-if="segmentPreview.audioUrl" :src="segmentPreview.audioUrl" controls style="height: 32px; vertical-align: middle" />
                <span v-if="segmentPreview.loading" style="color: var(--el-text-color-secondary); font-size: 13px">
                  {{ t('processor.ttsSegmentPreviewGenerating') }}
                </span>
                <span v-else-if="!segmentPreview.audioUrl && !segmentPreview.error" style="color: var(--el-text-color-secondary); font-size: 13px">
                  {{ t('processor.ttsSegmentPreviewIdle') }}
                </span>
                <span v-else-if="segmentPreview.sentenceCount" style="color: var(--el-text-color-secondary); font-size: 13px">
                  {{ t('processor.ttsSegmentPreviewCount', { count: segmentPreview.sentenceCount }) }}
                </span>
              </div>
              <div v-if="segmentPreview.warnings.length" style="margin-top: 6px">
                <el-text type="warning" size="small">
                  {{ t('processor.ttsSegmentPreviewWarnings') }} ({{ segmentPreview.warnings.length }})
                </el-text>
              </div>
              <div v-if="segmentPreview.error" style="margin-top: 6px">
                <el-text type="danger" size="small">{{ segmentPreview.error }}</el-text>
              </div>
            </div>
          </el-form-item>

          <el-form-item>
            <el-alert :closable="false" type="info" :title="t('processor.ttsFixedBackendHint')" show-icon />
          </el-form-item>
        </template>

        <!-- 语音预设管理弹窗：新增 / 编辑 / 删除；音色列表跟随本弹窗内选择的
             引擎（narratorFormVoices），与主面板当前引擎（ttsVoices）互相独立 -->
        <el-dialog v-model="narratorManagerVisible" :title="t('processor.manageNarrators')" width="600px">
          <el-table :data="narrators" size="small" style="margin-bottom: 16px" max-height="240">
            <el-table-column prop="name" :label="t('processor.narratorName')" width="110" />
            <el-table-column :label="t('processor.ttsEngine')" width="90">
              <template #default="{ row }">{{ engineLabel(row.engine) }}</template>
            </el-table-column>
            <el-table-column prop="voice" :label="t('processor.ttsVoice')" show-overflow-tooltip />
            <el-table-column :label="t('processor.narratorParamsColumn')" width="140" show-overflow-tooltip>
              <template #default="{ row }">
                <span style="font-size: 12px; color: var(--el-text-color-secondary)">
                  {{ row.rate || '+0%' }} / {{ row.pitch || '+0Hz' }} / {{ row.volume || '+0%' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column width="110">
              <template #default="{ row }">
                <el-button link size="small" @click="editNarrator(row)">{{ t('processor.edit') }}</el-button>
                <el-button link size="small" type="danger" @click="deleteNarratorItem(row.id)">{{ t('processor.delete') }}</el-button>
              </template>
            </el-table-column>
          </el-table>

          <el-form label-position="top">
            <el-form-item :label="t('processor.narratorName')">
              <el-input v-model="narratorForm.name" :placeholder="t('processor.narratorNamePlaceholder')" />
            </el-form-item>
            <el-form-item :label="t('processor.ttsEngine')">
              <el-select v-model="narratorForm.engine" style="width: 100%" :placeholder="t('processor.ttsEnginePlaceholder')">
                <el-option
                  v-for="eng in ttsEngines"
                  :key="eng.id"
                  :label="engineLabel(eng.id)"
                  :value="eng.id"
                  :disabled="!eng.available"
                />
              </el-select>
            </el-form-item>
            <el-form-item :label="t('processor.ttsVoice')">
              <el-select
                v-model="narratorForm.voice"
                filterable
                :loading="narratorFormVoicesLoading"
                style="width: 100%"
                :placeholder="t('processor.ttsVoicePlaceholder')"
              >
                <el-option v-for="v in narratorFormVoices" :key="v.id" :label="`${v.name} (${v.locale})`" :value="v.id" />
              </el-select>
            </el-form-item>
            <!-- 语音预设需要连同语速/音调/音量一起保存，否则套用预设时只会
                 恢复引擎+音色，语速等参数还得用户手动重新调 -->
            <el-form-item :label="t('processor.ttsRate')">
              <el-slider v-model="narratorFormRateNum" :min="-50" :max="100" show-input style="max-width: 420px" />
            </el-form-item>
            <el-form-item :label="t('processor.ttsPitch')">
              <el-slider v-model="narratorFormPitchNum" :min="-50" :max="50" show-input style="max-width: 420px" />
            </el-form-item>
            <el-form-item v-if="narratorForm.engine === 'windows_sapi'">
              <el-alert :closable="false" type="info" :title="t('processor.ttsPitchBestEffortHint')" show-icon />
            </el-form-item>
            <el-form-item :label="t('processor.ttsVolume')">
              <el-slider v-model="narratorFormVolumeNum" :min="-50" :max="50" show-input style="max-width: 420px" />
            </el-form-item>
          </el-form>

          <template #footer>
            <el-button @click="resetNarratorForm">{{ t('processor.reset') }}</el-button>
            <el-button type="primary" :loading="narratorSaving" @click="saveNarrator">{{ t('processor.save') }}</el-button>
          </template>
        </el-dialog>

        <!-- "优化文本"弹窗：智能转换 / 仅转换（数字）/ 逐字转换（数字）/
             仅转换符号 / 英文加空格 / 去除多余符号 / 连字符转空格 /
             按逗号插入换行 / 按句号插入换行 / 按每几句插入换行，全部只在弹窗内的这份文本
             副本上生效；点击"应用"才会写回打开弹窗时指定的那个文本框，
             不点"应用"直接关闭则不影响原文本。与 pipeline.py /
             text_processor.py 的其它转换规则完全独立，只调用
             /api/text/optimize，不经过 MFA / TTS / 对齐等任何其它后端。 -->
        <el-dialog v-model="textOptimizer.visible" :title="t('processor.textOptimize')" width="640px">
          <el-input
            v-model="textOptimizer.draft"
            type="textarea"
            :rows="10"
            :placeholder="t('processor.textOptimizePlaceholder')"
          />
          <div class="text-optimize-toolbar">
            <el-button size="small" :loading="textOptimizer.loading === 'smart'" @click="runTextOptimize('smart')">
              ✨ {{ t('processor.textOptimizeSmart') }}
            </el-button>
            <el-button size="small" :loading="textOptimizer.loading === 'number_only'" @click="runTextOptimize('number_only')">
              🔢 {{ t('processor.textOptimizeNumberOnly') }}
            </el-button>
            <el-button size="small" :loading="textOptimizer.loading === 'digit_to_words'" @click="runTextOptimize('digit_to_words')">
              🔠 {{ t('processor.textOptimizeDigitToWords') }}
            </el-button>
            <el-button size="small" :loading="textOptimizer.loading === 'symbol_only'" @click="runTextOptimize('symbol_only')">
              ➕ {{ t('processor.textOptimizeSymbolOnly') }}
            </el-button>
            <el-button size="small" :loading="textOptimizer.loading === 'add_spaces'" @click="runTextOptimize('add_spaces')">
              🔤 {{ t('processor.textOptimizeAddSpaces') }}
            </el-button>
            <el-button size="small" :loading="textOptimizer.loading === 'strip_symbols'" @click="runTextOptimize('strip_symbols')">
              🧹 {{ t('processor.textOptimizeStripSymbols') }}
            </el-button>
            <el-button size="small" :loading="textOptimizer.loading === 'hyphen_to_space'" @click="runTextOptimize('hyphen_to_space')">
              ➖ {{ t('processor.textOptimizeHyphenToSpace') }}
            </el-button>
            <el-button size="small" :loading="textOptimizer.loading === 'newline_after_comma'" @click="runTextOptimize('newline_after_comma')">
              ↩️ {{ t('processor.textOptimizeNewlineAfterComma') }}
            </el-button>
            <el-button size="small" :loading="textOptimizer.loading === 'newline_after_period'" @click="runTextOptimize('newline_after_period')">
              ↩️ {{ t('processor.textOptimizeNewlineAfterPeriod') }}
            </el-button>
          </div>
          <div class="text-optimize-toolbar" style="margin-top: 8px; align-items: center">
            <el-button size="small" :loading="textOptimizer.loading === 'newline_every_n'" @click="runTextOptimize('newline_every_n')">
              ↩️ {{ t('processor.textOptimizeNewlineEveryN') }}
            </el-button>
            <el-input-number
              v-model="textOptimizer.everyN"
              :min="1"
              :max="99"
              size="small"
              style="width: 100px; margin-left: 4px"
            />
            <span style="margin-left: 4px; font-size: 13px; color: var(--el-text-color-secondary)">
              {{ t('processor.textOptimizeNewlineEveryNUnit') }}
            </span>
          </div>
          <div v-if="textOptimizer.error" style="margin-top: 8px">
            <el-text type="danger" size="small">{{ textOptimizer.error }}</el-text>
          </div>
          <template #footer>
            <el-button @click="textOptimizer.visible = false">{{ t('processor.textOptimizeCancel') }}</el-button>
            <el-button type="primary" @click="applyTextOptimize">{{ t('processor.textOptimizeApply') }}</el-button>
          </template>
        </el-dialog>

        <!-- "查找替换"弹窗：与"优化文本"弹窗完全同构——弹窗内编辑的是 draft
             副本，点击"应用"才写回打开弹窗时指定的那个文本框，不点"应用"
             直接关闭则不影响原文本。纯前端字符串/正则替换，不调用任何
             后端接口。支持"区分大小写""正则表达式"两个开关，以及"查找
             下一个""替换""全部替换"三个操作，与 Ctrl+H 的行为对齐。 -->
        <el-dialog v-model="findReplace.visible" :title="t('processor.findReplace')" width="640px">
          <el-input
            ref="findReplaceTextareaRef"
            v-model="findReplace.draft"
            type="textarea"
            :rows="10"
            :placeholder="t('processor.textOptimizePlaceholder')"
          />
          <div class="text-optimize-toolbar" style="margin-top: 12px">
            <el-input
              v-model="findReplace.find"
              :placeholder="t('processor.findReplaceFindPlaceholder')"
              style="flex: 1; min-width: 200px"
              size="small"
            />
            <el-input
              v-model="findReplace.replace"
              :placeholder="t('processor.findReplaceReplacePlaceholder')"
              style="flex: 1; min-width: 200px"
              size="small"
            />
          </div>
          <div class="text-optimize-toolbar" style="margin-top: 8px; align-items: center">
            <el-checkbox v-model="findReplace.caseSensitive">{{ t('processor.findReplaceCaseSensitive') }}</el-checkbox>
            <el-checkbox v-model="findReplace.useRegex">{{ t('processor.findReplaceUseRegex') }}</el-checkbox>
            <span style="margin-left: auto; font-size: 13px; color: var(--el-text-color-secondary)">
              {{ t('processor.findReplaceMatchCount', { count: findReplaceMatchCount }) }}
            </span>
          </div>
          <div class="text-optimize-toolbar" style="margin-top: 8px">
            <el-button size="small" :disabled="!findReplace.find" @click="findReplaceNext">
              ⏭️ {{ t('processor.findReplaceFindNext') }}
            </el-button>
            <el-button size="small" :disabled="!findReplace.find" @click="runFindReplaceOne">
              🔁 {{ t('processor.findReplaceOne') }}
            </el-button>
            <el-button size="small" :disabled="!findReplace.find" @click="runFindReplaceAll">
              🔁 {{ t('processor.findReplaceAll') }}
            </el-button>
          </div>
          <div v-if="findReplace.error" style="margin-top: 8px">
            <el-text type="danger" size="small">{{ findReplace.error }}</el-text>
          </div>
          <template #footer>
            <el-button @click="findReplace.visible = false">{{ t('processor.textOptimizeCancel') }}</el-button>
            <el-button type="primary" @click="applyFindReplace">{{ t('processor.textOptimizeApply') }}</el-button>
          </template>
        </el-dialog>

        <!-- 对齐后端选择器（TTS跟读 / 字幕跟读固定使用 Qwen3-FA，不显示；project-only 模式不需要对齐） -->
        <el-form-item v-if="inputMode === 'audio' && processingMode !== 'project-only'" :label="t('processor.backendLabel')">
          <el-radio-group v-model="alignerBackend">
            <el-radio value="mfa">
              <span>{{ t('processor.backendMfa') }}</span>
              <el-tag
                :type="systemStatus.mfa?.installed ? 'success' : 'danger'"
                size="small" style="margin-left:4px"
              >{{ systemStatus.mfa?.installed ? '✓' : '✗' }}</el-tag>
            </el-radio>
            <el-radio value="whisperx">
              <span>{{ t('processor.backendWhisperx') }}</span>
              <el-tag
                :type="alignerStatus['whisperx']?.available ? 'success' : 'info'"
                size="small" style="margin-left:4px"
              >{{ alignerStatus['whisperx']?.available ? '✓' : t('processor.backendStatusNeedInstall') }}</el-tag>
            </el-radio>
            <el-radio value="qwen3_asr">
              <span>{{ t('processor.backendQwen3Asr') }}</span>
              <el-tag
                :type="alignerStatus['qwen3_asr']?.available ? 'success' : 'info'"
                size="small" style="margin-left:4px"
              >{{ alignerStatus['qwen3_asr']?.available ? '✓' : t('processor.backendStatusNeedInstall') }}</el-tag>
            </el-radio>
            <el-radio value="qwen3_aligner">
              <span>{{ t('processor.backendQwen3Aligner') }}</span>
              <el-tag
                :type="alignerStatus['qwen3_aligner']?.available ? 'success' : 'info'"
                size="small" style="margin-left:4px"
              >{{ alignerStatus['qwen3_aligner']?.available ? '✓' : t('processor.backendStatusNeedInstall') }}</el-tag>
            </el-radio>
            <el-radio value="nemo_aligner">
              <span>{{ t('processor.backendNemoAligner') }}</span>
              <el-tag
                :type="alignerStatus['nemo_aligner']?.available ? 'success' : 'info'"
                size="small" style="margin-left:4px"
              >{{ alignerStatus['nemo_aligner']?.available ? '✓' : t('processor.backendStatusNeedInstall') }}</el-tag>
            </el-radio>
          </el-radio-group>
          <div class="help-text" style="margin-top:6px">
            <small v-if="alignerBackend === 'mfa'">
              🎯 {{ t('processor.backendMfaHelp') }}
            </small>
            <small v-else-if="alignerBackend === 'whisperx'">
              🤖 {{ t('processor.backendWhisperxHelp') }}
            </small>
            <small v-else-if="alignerBackend === 'qwen3_asr'">
              🌐 {{ t('processor.backendQwen3AsrHelp') }}
            </small>
            <small v-else-if="alignerBackend === 'qwen3_aligner'">
              📌 {{ t('processor.backendQwen3AlignerHelp') }}
            </small>
            <small v-else-if="alignerBackend === 'nemo_aligner'">
              🟩 {{ t('processor.backendNemoAlignerHelp') }}
            </small>
            <div v-if="alignerBackend !== 'mfa' && alignerStatus.models_dir"
                 style="margin-top:4px;color:#67c23a;font-size:12px">
              📁 {{ t('processor.modelCacheDir') }}：<code>{{ alignerStatus.models_dir }}</code>
            </div>
          </div>
          <el-alert
            v-if="alignerBackend !== 'mfa' && !alignerStatus[alignerBackend]?.available"
            type="warning" :closable="false" show-icon style="margin-top:8px"
          >
            <template #title>{{ alignerStatus[alignerBackend]?.message || t('processor.backendInstallHint') }}</template>
            <div style="font-size:12px;margin-top:4px">
              <span v-if="alignerBackend === 'whisperx'">{{ t('processor.packageHintWhisperx') }}</span>
              <span v-else-if="alignerBackend === 'nemo_aligner'">{{ t('processor.packageHintNemo') }}</span>
              <span v-else-if="alignerBackend === 'qwen3_aligner'">{{ t('processor.packageHintQwen3Aligner') }}</span>
              <span v-else-if="alignerBackend === 'qwen3_asr'">{{ t('processor.packageHintQwen3Asr') }}</span>
              <span v-else>{{ t('processor.packageHintTransformers') }} torchaudio accelerate</span>
            </div>
          </el-alert>
        </el-form-item>

        <!-- 对齐工具运行设备（非 MFA 后端 或 TTS跟读/字幕跟读固定的 Qwen3-FA 都需要） -->
        <el-form-item
          v-if="inputMode === 'tts' || inputMode === 'subtitle' || (processingMode !== 'project-only' && alignerBackend !== 'mfa')"
          :label="t('processor.alignDevice')"
        >
          <el-radio-group v-model="advancedConfig.aligner_device">
            <el-radio label="auto">{{ t('processor.deviceAuto') }}</el-radio>
            <el-radio label="cpu">{{ t('processor.deviceCpu') }}</el-radio>
            <el-radio label="cuda">{{ t('processor.deviceCuda') }}</el-radio>
          </el-radio-group>
          <div class="help-text" style="margin-top:6px">
            <small v-if="advancedConfig.aligner_device === 'cuda' && alignerBackend === 'whisperx'">
              💡 {{ t('processor.deviceWhisperxGpu') }}
            </small>
            <small v-else-if="advancedConfig.aligner_device === 'cuda' && alignerBackend.startsWith('qwen3')">
              💡 {{ t('processor.deviceQwen3Gpu') }}
            </small>
            <small v-else-if="advancedConfig.aligner_device === 'cuda' && alignerBackend === 'nemo_aligner'">
              💡 {{ t('processor.deviceNemoGpu') }}
            </small>
            <small v-else-if="advancedConfig.aligner_device === 'cpu'">
              ⚠️ {{ t('processor.deviceCpuHelp') }}
            </small>
            <small v-else>
              {{ t('processor.deviceAutoHelp') }}
            </small>
          </div>
        </el-form-item>

        <!-- WhisperX 模型选择 -->
        <el-form-item
          v-if="processingMode !== 'project-only' && alignerBackend === 'whisperx'"
          :label="t('processor.whisperModel')"
        >
          <el-select v-model="advancedConfig.whisperx_model" style="width:240px">
            <el-option value="large-v3"       :label="t('processor.whisperModelLargeV3')" />
            <el-option value="large-v3-turbo" :label="t('processor.whisperModelLargeV3Turbo')" />
            <el-option value="large-v2"       :label="t('processor.whisperModelLargeV2')" />
            <el-option value="medium"         :label="t('processor.whisperModelMedium')" />
            <el-option value="small"          :label="t('processor.whisperModelSmall')" />
            <el-option value="base"           :label="t('processor.whisperModelBase')" />
            <el-option value="tiny"           :label="t('processor.whisperModelTiny')" />
          </el-select>
          <div class="help-text" style="margin-top:6px">
            <small v-if="advancedConfig.whisperx_model === 'large-v3'">
              🌟 {{ t('processor.whisperDescLargeV3') }}
            </small>
            <small v-else-if="advancedConfig.whisperx_model === 'large-v3-turbo'">
              ⚡ {{ t('processor.whisperDescLargeV3Turbo') }}
            </small>
            <small v-else-if="advancedConfig.whisperx_model === 'large-v2'">
              🔵 {{ t('processor.whisperDescLargeV2') }}
            </small>
            <small v-else>
              ⚠️ {{ t('processor.whisperDescSmall') }}
            </small>
          </div>
        </el-form-item>

        <!-- WhisperX 批处理大小（仅 device=cuda 时对显存占用有实际意义；
             CPU 模式下该值基本不影响性能，为避免误导直接隐藏控件，
             提交时后端仍会收到默认值 16） -->
        <el-form-item
          v-if="processingMode !== 'project-only' && alignerBackend === 'whisperx' && advancedConfig.aligner_device === 'cuda'"
          :label="t('processor.whisperxBatchSize')"
        >
          <el-input-number
            v-model="advancedConfig.whisperx_batch_size"
            :min="1"
            :max="64"
            :step="1"
            style="width:160px"
          />
          <div class="help-text" style="margin-top:6px">
            <small>⚠️ {{ t('processor.whisperxBatchSizeHint') }}</small>
          </div>
        </el-form-item>

        <!-- NeMo Forced Aligner 模型覆盖（可选） -->
        <el-form-item
          v-if="processingMode !== 'project-only' && alignerBackend === 'nemo_aligner'"
          :label="t('processor.nemoModel')"
        >
          <el-input
            v-model="advancedConfig.nemo_model"
            :placeholder="t('processor.nemoModelPlaceholder')"
            style="width:360px"
            clearable
          />
          <div class="help-text" style="margin-top:6px">
            <small>{{ t('processor.nemoModelHint') }}</small>
          </div>
        </el-form-item>

		<!-- LAB / MIDI 单文件上传（仅 project-only 模式） -->
		<el-form-item v-if="processingMode === 'project-only'" :label="t('processor.labMidiFile')">
		  <el-upload
			:key="labMidiUploadKey"
			drag
			action="#"
			:auto-upload="false"
			:limit="1"
			:on-exceed="handleLabMidiExceed"
			@change="handleLabMidiChange"
			accept=".lab,.mid,.midi"
		  >
			<el-icon class="el-icon--upload"><UploadFilled /></el-icon>
			<div class="el-upload__text">
			  {{ t('processor.dragLabMidi') }}
			</div>
			<template #tip>
			  <div class="el-upload__tip">
				{{ t('processor.labMidiTip') }}
			  </div>
			</template>
		  </el-upload>

		  <div v-if="formData.labFile" class="file-info" style="margin-top:6px">
			📄 {{ formData.labFile.name }} ({{ formatFileSize(formData.labFile.size) }})
		  </div>
		  <div v-if="formData.midiFile" class="file-info midi-loaded" style="margin-top:4px">
			🎹 {{ formData.midiFile.name }} ({{ formatFileSize(formData.midiFile.size) }})
			<span v-if="midiInfo.loaded" class="midi-bpm-tag">BPM {{ midiInfo.bpm }}</span>
		  </div>

		  <el-alert
			v-if="midiLoaded"
			type="info"
			:closable="false"
			show-icon
			style="margin-top:8px"
		  >
			<template #title>{{ t('processor.midiImportedTitle') }}</template>
			<p style="margin:4px 0 0;font-size:12px;color:#606266">
			  🔒 {{ t('processor.midiImportedTip') }}
			</p>
		  </el-alert>
		</el-form-item>

		<el-form-item
		  v-if="processingMode !== 'project-only' && inputMode !== 'subtitle'"
		  :label="t('processor.inputText')"
		>
		  <el-input
			v-model="formData.text"
			type="textarea"
			:rows="4"
			style="width: 100%"
			:placeholder="
			  isTextOptional
				? t('processor.textPlaceholderOptional')
				: t('processor.textPlaceholderRequired')
			"
		  />

		  <div style="margin-top: 6px">
			<el-button size="small" @click="openTextOptimizer(formData, 'text')">
			  🛠️ {{ t('processor.textOptimize') }}
			</el-button>
			<el-button size="small" @click="openFindReplace(formData, 'text')">
			  🔍 {{ t('processor.findReplace') }}
			</el-button>
		  </div>

		  <div class="help-text" style="margin-top: 6px; font-size: 12px; color: #909399; line-height: 1.4; width: 100%;">
			<span v-if="isTextOptional" style="color: #67c23a; font-weight: 500;">
			  ✓ {{ t('processor.textOptionalHint') }} | {{ t('processor.currentChars') }}{{ formData.text.length }}
			</span>

			<span v-else>
			  {{ t('processor.currentChars') }}{{ formData.text.length }}
			</span>
		  </div>
		</el-form-item>

        <el-form-item v-if="processingMode !== 'project-only'" :label="t('processor.language')">
          <el-select v-model="formData.language" :placeholder="t('processor.languagePlaceholder')">
            <el-option :label="t('processor.languageCmn')" value="cmn" />
            <el-option :label="t('processor.languageEng')" value="eng" />
            <el-option :label="t('processor.languageJpn')" value="jpn" />
            <el-option :label="t('processor.languageKor')" value="kor" />
            <el-option :label="t('processor.languageYue')" value="yue" />
          </el-select>
        </el-form-item>

        <!-- 对齐辅助移调：只生成一份临时移调音频副本喂给对齐后端做时间戳
             识别，不影响最终 WAV / F0 / 工程文件的音高，也不影响 LAB 时间
             戳（纯移调不改变时长）。高音音频/高音色 TTS 偶发导致对齐模型
             内部特征错位时可以尝试调低（负值）。 -->
        <el-form-item
          v-if="processingMode !== 'project-only'"
          :label="t('processor.alignPitchShift')"
        >
          <el-input-number
            v-model="advancedConfig.align_pitch_shift_semitones"
            :min="-24" :max="24" :step="1"
            controls-position="right"
            style="width: 160px"
          />
          <span class="option-hint">
            {{ t('processor.alignPitchShiftHint') }}
          </span>
        </el-form-item>

        <!-- 英语单词级对齐：仅当语言非日语时显示 -->
        <el-form-item
          v-if="processingMode !== 'project-only' && formData.language !== 'jpn'"
          :label="t('processor.englishWordAlign')"
        >
          <el-switch v-model="englishWordAlign" />
          <span class="option-hint">
            {{ t('processor.englishWordAlignHint') }}
          </span>
        </el-form-item>

        <el-form-item :label="t('processor.processingMode')">
          <el-radio-group v-model="processingMode">
            <el-radio value="mfa-only">{{ t('processor.processingModeMfaOnly') }}</el-radio>
            <el-radio value="full">{{ t('processor.processingModeFull') }}</el-radio>
            <el-radio v-if="inputMode === 'audio'" value="project-only">{{ t('processor.processingModeProjectOnly') }}</el-radio>
          </el-radio-group>
          <div class="mode-help">
            <small v-if="inputMode === 'subtitle'">
              {{ t('processor.subtitleAlignerFixedHint') }}
            </small>
            <small v-else-if="processingMode === 'mfa-only'">
              {{ t('processor.processingModeMfaOnlyHint', { backend: alignerBackendLabel }) }}
            </small>
            <small v-else-if="processingMode === 'full'">
              {{ t('processor.processingModeFullHint', { backend: alignerBackendLabel }) }}
            </small>
            <small v-else>
              {{ t('processor.processingModeProjectOnlyHint') }}
            </small>
          </div>
        </el-form-item>

        <el-form-item v-if="processingMode === 'full' || processingMode === 'project-only'" :label="t('processor.outputFormat')">
          <el-select v-model="formData.outputFormat" :placeholder="t('processor.outputFormat')">
            <el-option :label="t('processor.outputFormatSv')" value="sv" />
            <el-option :label="t('processor.outputFormatUtau')" value="utau" />
            <el-option :label="t('processor.outputFormatVsqx')" value="vsqx" />
          </el-select>
        </el-form-item>

        <el-form-item v-if="processingMode === 'project-only'" :label="t('processor.phonemeMode')">
          <el-radio-group v-model="formData.phonemeMode">
            <el-radio value="none">{{ t('processor.phonemeNone') }}</el-radio>
            <el-radio value="merge">{{ t('processor.phonemeMerge') }}</el-radio>
            <el-radio value="hiragana">{{ t('processor.phonemeHiragana') }}</el-radio>
            <el-radio value="katakana">{{ t('processor.phonemeKatakana') }}</el-radio>
          </el-radio-group>
          <div class="help-text">
            <small v-if="formData.phonemeMode === 'none'">
              {{ t('processor.phonemeNoneHint') }}
            </small>
            <small v-else-if="formData.phonemeMode === 'merge'">
              {{ t('processor.phonemeMergeHint') }}
            </small>
            <small v-else-if="formData.phonemeMode === 'hiragana'">
              {{ t('processor.phonemeHiraganaHint') }}
            </small>
            <small v-else>
              {{ t('processor.phonemeKatakanaHint') }}
            </small>
          </div>
          <div class="help-text" style="margin-top:4px">
            <small style="color:#909399">
              ⚠ {{ t('processor.phonemeWarning') }}
            </small>
          </div>
        </el-form-item>

        <el-form-item v-if="processingMode === 'full' || processingMode === 'project-only'" :label="t('processor.projectTitle')">
          <el-input
            v-model="formData.projectTitle"
            :placeholder="t('processor.projectTitlePlaceholder')"
            maxlength="248"
          />
        </el-form-item>

        <el-collapse v-if="processingMode === 'full' || processingMode === 'project-only'" accordion>
          <el-collapse-item :title="`⚙️ ${t('processor.advancedSettingsTitle')}`" name="advanced">
            <el-row :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.bpm')">
                  <el-input-number
                    v-model="advancedConfig.bpm"
                    :min="20"
                    :max="300"
                    :step="1"
                    controls-position="right"
                    :disabled="midiLoaded"
                  />
                  <span v-if="midiLoaded && midiInfo.loaded" class="midi-lock-tip">
                    🔒 {{ midiInfo.bpm }} ({{ t('processor.midiImportedTitle') }})
                  </span>
                  <span v-else-if="midiLoaded" class="midi-lock-tip">
                    🔒 {{ t('processor.midiImportedMore') }}
                  </span>
                </el-form-item>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.basePitch')">
                  <div class="pitch-input-group">
                    <el-input-number
                      v-model="advancedConfig.base_pitch"
                      :min="12"
                      :max="108"
                      :step="1"
                      controls-position="right"
                      :disabled="midiLoaded"
                  />
                    <span class="pitch-name">{{ midiNoteToName(advancedConfig.base_pitch) }}</span>
                    <span v-if="midiLoaded" class="midi-lock-tip">🔒 {{ t('processor.midiImportedMore') }}</span>
                  </div>
                </el-form-item>
              </el-col>

              <el-col :xs="24">
                <el-divider>📈 {{ t('processor.pitchControl') }}</el-divider>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.autoNotePitch')">
                  <el-switch 
                    v-model="advancedConfig.auto_note_pitch"
                    :active-text="t('processor.autoNotePitchActive')"
                    :inactive-text="t('processor.autoNotePitchInactive')"
                    :disabled="midiLoaded"
                  />
                  <span v-if="midiLoaded" class="midi-lock-tip" style="display:block;margin-top:4px">
                    🔒 {{ t('processor.midiImportedTip') }}
                  </span>
                </el-form-item>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.exportPitchLine')">
                  <el-switch 
                    v-model="advancedConfig.export_pitch_line"
                    :active-text="t('processor.exportPitchLineActive')"
                    :inactive-text="t('processor.exportPitchLineInactive')"
                  />
                </el-form-item>
              </el-col>

              <el-col :xs="24">
                <el-divider>{{ t('processor.f0RangeDivider') }}</el-divider>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Method')">
                  <el-radio-group v-model="advancedConfig.f0_method" :disabled="!advancedConfig.export_pitch_line && !advancedConfig.auto_note_pitch">
                    <el-radio label="dio">
                      <span>{{ t('processor.f0Dio') }}</span>
                      <el-icon class="icon-tip"><InfoFilled /></el-icon>
                    </el-radio>
                    <el-radio label="harvest">
                      <span>{{ t('processor.f0Harvest') }}</span>
                      <el-icon class="icon-tip"><InfoFilled /></el-icon>
                    </el-radio>
                    <el-radio label="crepe" :disabled="(!advancedConfig.export_pitch_line && !advancedConfig.auto_note_pitch) || systemStatus.audio_processing?.f0_backends?.crepe?.available === false">
                      <span>{{ t('processor.f0Crepe') }}</span>
                      <el-icon class="icon-tip"><InfoFilled /></el-icon>
                    </el-radio>
                    <el-radio label="rmvpe" :disabled="(!advancedConfig.export_pitch_line && !advancedConfig.auto_note_pitch) || systemStatus.audio_processing?.f0_backends?.rmvpe?.available === false">
                      <span>{{ t('processor.f0Rmvpe') }}</span>
                      <el-icon class="icon-tip"><InfoFilled /></el-icon>
                    </el-radio>
                  </el-radio-group>
                  <p v-if="advancedConfig.f0_method === 'crepe' && systemStatus.audio_processing?.f0_backends?.crepe?.available === false" class="help-text">
                    ⚠ {{ t('processor.crepeDependencyMissing') }}
                  </p>
                  <p v-if="advancedConfig.f0_method === 'rmvpe' && systemStatus.audio_processing?.f0_backends?.rmvpe?.available === false" class="help-text">
                    ⚠ {{ t('processor.rmvpeModelMissing') }}
                  </p>
                </el-form-item>
              </el-col>

              <el-col v-if="advancedConfig.f0_method === 'crepe'" :xs="24" :sm="12">
                <el-form-item :label="t('processor.crepeModelSpec')">
                  <el-radio-group v-model="advancedConfig.crepe_model">
                    <el-radio label="full">{{ t('processor.crepeFull') }}</el-radio>
                    <el-radio label="tiny">{{ t('processor.crepeTiny') }}</el-radio>
                  </el-radio-group>
                </el-form-item>
              </el-col>

              <el-col v-if="advancedConfig.f0_method === 'crepe' || advancedConfig.f0_method === 'rmvpe'" :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Device')">
                  <el-radio-group v-model="advancedConfig.f0_device">
                    <el-radio label="auto">{{ t('processor.deviceAuto') }}</el-radio>
                    <el-radio label="cpu">{{ t('processor.deviceCpu') }}</el-radio>
                    <el-radio label="cuda">{{ t('processor.deviceCuda') }}</el-radio>
                  </el-radio-group>
                </el-form-item>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.precision')">
                  <el-radio-group v-model="advancedConfig.precision">
                    <el-radio label="single">{{ t('processor.precisionSingle') }}</el-radio>
                    <el-radio label="double">{{ t('processor.precisionDouble') }}</el-radio>
                  </el-radio-group>
                </el-form-item>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Smooth')">
                  <el-switch 
                    v-model="advancedConfig.f0_smooth"
                    :active-text="t('processor.enabled')"
                    :inactive-text="t('processor.disabled')"
                    :disabled="!advancedConfig.export_pitch_line"
                  />
                </el-form-item>
              </el-col>

              <el-col v-if="advancedConfig.f0_smooth" :xs="24" :sm="12">
                <el-form-item :label="t('processor.smoothWindow')">
                  <el-input-number
                    v-model="advancedConfig.f0_smooth_window"
                    :min="1"
                    :max="29"
                    :step="2"
                    controls-position="right"
                    :disabled="!advancedConfig.export_pitch_line"
                  />
                  <span class="help-text">{{ t('processor.smoothWindowTip') }}</span>
                </el-form-item>
              </el-col>

              <el-col v-if="formData.outputFormat === 'vsqx' && advancedConfig.f0_smooth" :xs="24" :sm="12">
                <el-form-item :label="t('processor.vsqxPitchSmoothWindow')">
                  <el-input-number
                    v-model="advancedConfig.vsqx_pitch_smooth_window"
                    :min="1"
                    :max="29"
                    :step="2"
                    controls-position="right"
                    :disabled="!advancedConfig.export_pitch_line"
                  />
                  <span class="help-text">{{ t('processor.vsqxPitchSmoothWindowTip') }}</span>
                </el-form-item>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Floor')">
                  <el-input-number
                    v-model="advancedConfig.f0_floor"
                    :min="40"
                    :max="200"
                    :step="5"
                    controls-position="right"
                  />
                </el-form-item>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Ceil')">
                  <el-input-number
                    v-model="advancedConfig.f0_ceil"
                    :min="300"
                    :max="1000"
                    :step="50"
                    controls-position="right"
                  />
                </el-form-item>
              </el-col>
			</el-row>

            <el-alert type="info" :closable="false" show-icon class="settings-info">
              <template #title>💡 {{ t('processor.advancedHelpTitle') }}</template>
              <p><strong>BPM:</strong> {{ t('processor.advancedHelpBpm') }}</p>
              <p><strong>{{ t('processor.basePitch') }}:</strong> {{ t('processor.advancedHelpBasePitch') }}</p>
              <p><strong>{{ t('processor.autoNotePitch') }}:</strong> {{ t('processor.advancedHelpAutoNotePitch') }}</p>
              <p><strong>{{ t('processor.exportPitchLine') }}:</strong> {{ t('processor.advancedHelpExportPitchLine') }}</p>
              <p><strong>DIO / Harvest / CREPE / RMVPE：</strong>{{ t('processor.advancedHelpF0Method') }}</p>
              <p><strong>{{ t('processor.f0Device') }}：</strong>{{ t('processor.advancedHelpDevice') }}</p>
              <p><strong>{{ t('processor.f0Floor') }} / {{ t('processor.f0Ceil') }}:</strong> {{ t('processor.advancedHelpRange') }}</p>
            </el-alert>
          </el-collapse-item>
        </el-collapse>

        <!-- 英语单词→音素映射（适用于 SVP / VSQX 工程文件）：
             放在"高级设置"折叠面板下方、始终可见（不需要展开折叠面板），
             仅当满足以下全部条件才出现：
             - 处理模式为"完整处理"（full）——"仅标注"不产出工程文件，
               "仅生成工程"跳过对齐，两者都不会做单词级英文处理；
             - 已开启"英语单词级对齐"（该开关在语言为日语时已被隐藏，
               value 恒为 false，因此"完整处理+日语"场景天然被排除，
               无需再单独判断语言）；
             - 输出格式支持 SVP / VSQX。
             【与"选择词典"解耦】该开关只控制"混合文本中的英语单词是否要
             转换为 ARPABET/VOCALOID4 音素"，不再决定下方"选择词典"是否
             显示——两者是相互独立的功能。 -->
        <el-form-item v-if="showWordPhonemeMap" :label="t('processor.wordPhonemeMap')">
          <el-switch v-model="wordPhonemeMap" />
          <div class="dict-source-hint">
            {{ t('processor.wordPhonemeMapSwitchHint') }}
          </div>
        </el-form-item>

        <!-- 选择词典：不受"英语单词→音素映射"开关的开启/关闭影响，
             只要是完整处理（任意语种：中/英/日/韩/粤）或仅生成工程文件
             （两者都会真正产出 SVP/VSQX 工程文件），且输出格式支持
             SVP/VSQX，就始终显示。词典可将任意语言的字词映射为音素，
             不局限于英语，且其匹配优先级高于"英语单词→音素映射"
             （命中词典时优先按词典输出，未命中才回退到英语单词→音素
             映射或软件默认转换）。 -->
        <el-form-item v-if="showDictSource" :label="t('processor.selectDictionary')">
          <el-select
            v-model="dictSource"
            style="width: 260px"
            :placeholder="t('processor.dictSourceDefault')"
            @visible-change="(open) => open && fetchDictionaries()"
          >
            <el-option value="default" :label="t('processor.dictSourceDefault')" />
            <el-option
              v-for="d in filteredDictionaries"
              :key="d.name"
              :value="d.name"
              :label="`${d.name} (${d.notation === 'vocaloid' ? t('dictionary.notationVocaloid') : t('dictionary.notationSynthesizerV')}, ${d.count})`"
            />
          </el-select>
          <div class="dict-source-hint">
            {{ selectDictionaryHint }}
            <span v-if="!filteredDictionaries.length"> {{ t('processor.dictSourceEmptyHint') }}</span>
          </div>
        </el-form-item>

        <el-form-item style="margin-top: 20px">
          <el-button
            type="primary"
            size="large"
            :loading="processing"
            @click="processAudio"
            :disabled="isSubmitDisabled"
          >
            <span v-if="!processing">🚀 {{ t('processor.startProcessing') }}</span>
            <span v-else>{{ t('processor.processing') }} {{ progressPercent }}%</span>
          </el-button>
          <el-button @click="reset" :disabled="processing">🔄 {{ t('processor.reset') }}</el-button>
          <span v-if="!isReady && processingMode !== 'project-only'" class="disabled-text">
            ({{ t('processor.systemReadyHint') }})
          </span>
        </el-form-item>

        <el-progress
          v-if="processing"
          :percentage="progressPercent"
          :indeterminate="true"
          class="progress-bar"
        />
      </el-form>

      <div v-if="result" class="result-section">
        <el-divider />

        <h3>✅ {{ t('processor.result') }}</h3>
        <div class="result-info">
          <el-row :gutter="20">
            <el-col :xs="24" :sm="12">
              <p><strong>{{ t('processor.processingTime') }}:</strong> {{ formatTime(result.processingTime) }}</p>
              <p v-if="result.labPath"><strong>{{ t('processor.labFile') }}:</strong> {{ getFileName(result.labPath) }}</p>
            </el-col>
            <el-col :xs="24" :sm="12">
              <p v-if="result.projectPath"><strong>{{ t('processor.projectFile') }}:</strong> {{ getFileName(result.projectPath) }}</p>
              <p v-if="result.segments"><strong>{{ t('processor.segmentCount') }}:</strong> {{ result.segments }}</p>
            </el-col>
          </el-row>
        </div>

        <el-tabs>
          <el-tab-pane v-if="result.labContent" :label="t('processor.labContentTab')">
            <el-input
              v-model="result.labContent"
              type="textarea"
              :rows="12"
              readonly
              class="output-text"
            />
            <div class="tab-actions">
              <el-button @click="copyLabToClipboard" size="small">
                📋 {{ t('processor.copyLab') }}
              </el-button>
              <el-button @click="downloadLab" size="small" type="success">
                📥 {{ t('processor.downloadLab') }}
              </el-button>
            </div>
          </el-tab-pane>

          <el-tab-pane v-if="result.projectPath" :label="t('processor.fileInfoTab')">
            <div class="file-info-box">
              <el-row :gutter="20">
                <el-col :xs="24" v-if="result.labPath">
                  <p><strong>{{ t('processor.labFile') }}:</strong></p>
                  <code>{{ result.labPath }}</code>
                </el-col>
                <el-col :xs="24">
                  <p><strong>{{ t('processor.projectFile') }}:</strong></p>
                  <code>{{ result.projectPath }}</code>
                </el-col>
                <el-col :xs="24">
                  <p><strong>{{ t('processor.outputFormat') }}:</strong>
                    {{
                      result.projectFormat === 'sv'   ? t('processor.outputFormatSv')   :
                      result.projectFormat === 'vsqx' ? t('processor.outputFormatVsqx') :
                                                        t('processor.outputFormatUtau')
                    }}
                  </p>
                  <p v-if="result.segments"><strong>{{ t('processor.segmentCount') }}:</strong> {{ result.segments }}</p>
                  <p v-if="result.config"><strong>{{ t('processor.processingConfig') }}:</strong></p>
                  <ul v-if="result.config">
                    <li>{{ t('processor.bpm') }}: {{ result.config.bpm }}</li>
                    <li>{{ t('processor.basePitch') }}: {{ midiNoteToName(result.config.base_pitch) }} (MIDI {{ result.config.base_pitch }})</li>
                    <li>{{ t('processor.autoNotePitch') }}: {{ result.config.auto_note_pitch ? t('processor.enabled') : t('processor.disabled') }}</li>
                    <li>{{ t('processor.exportPitchLine') }}: {{ result.config.export_pitch_line ? t('processor.enabled') : t('processor.disabled') }}</li>
                    <li>{{ t('processor.f0Method') }}: {{ result.config.f0_method?.toUpperCase?.() || result.config.f0_method }}</li>
                    <li v-if="result.config.f0_method === 'crepe'">{{ t('processor.crepeModelSpec') }}: {{ result.config.crepe_model }}</li>
                    <li v-if="result.config.f0_method === 'crepe' || result.config.f0_method === 'rmvpe'">{{ t('processor.f0Device') }}: {{ result.config.f0_device }}</li>
                    <li v-if="result.config.aligner_device !== undefined">{{ t('processor.alignDevice') }}: {{ result.config.aligner_device }}</li>
                    <li v-if="result.whisperxModel">{{ t('processor.whisperModel') }}: {{ result.whisperxModel }}</li>
                    <li>{{ t('processor.precision') }}: {{ result.config.use_double_precision ? t('processor.precisionDouble') : t('processor.precisionSingle') }}</li>
                  </ul>
                </el-col>
              </el-row>
            </div>
          </el-tab-pane>

          <el-tab-pane :label="t('processor.stageTab')">
            <div class="details-box">
              <el-table :data="processingDetails" stripe style="width: 100%">
                <el-table-column prop="stage" :label="t('processor.processingStage')" width="200" />
                <el-table-column prop="status" :label="t('processor.status')" width="100">
                  <template #default="{ row }">
                    <el-tag v-if="row.status === '完成'" type="success">{{ t('processor.statusDone') }}</el-tag>
                    <el-tag v-else-if="row.status === '跳过'" type="warning">{{ t('processor.statusSkipped') }}</el-tag>
                    <el-tag v-else-if="row.status === '等待'" type="info">{{ t('processor.statusWaiting') }}</el-tag>
                    <el-tag v-else-if="row.status === '进行中'" type="warning">{{ t('processor.statusProcessing') }}</el-tag>
                    <el-tag v-else type="info">{{ row.status }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="message" :label="t('processor.detail')" show-overflow-tooltip />
              </el-table>
            </div>
          </el-tab-pane>
        </el-tabs>

        <div class="action-buttons">
          <el-button v-if="result.labContent" type="success" @click="downloadLab" size="large">
            📥 {{ t('processor.downloadLabFile') }}
          </el-button>
          <el-button 
            v-if="result.projectPath" 
            type="success" 
            @click="downloadProject" 
            size="large"
            :loading="downloadingProject"
          >
            📥 {{ t('processor.downloadProjectFile') }}
          </el-button>
          <el-button v-if="result.labContent" @click="copyLabToClipboard" size="large">
            📋 {{ t('processor.copyLabContent') }}
          </el-button>
          <el-button type="info" @click="newProcess" size="large">
            🔄 {{ t('processor.processNext') }}
          </el-button>
        </div>
      </div>

      <div v-if="error" class="error-section">
        <el-alert
          :title="`${t('processor.errorPrefix')}: ${error}`"
          type="error"
          :closable="true"
          @close="error = ''"
          show-icon
        />
      </div>
    </el-card>

    <div v-if="systemStatus" class="status-box">
      <el-card shadow="hover">
        <template #header>
          <span>🔧 {{ t('app.systemStatus') }}</span>
        </template>

        <el-row :gutter="20">
          <el-col :xs="24" :sm="12">
            <div class="status-item">
              <span class="label">{{ t('processor.mfaStatus') }}:</span>
              <el-tag :type="systemStatus.mfa?.installed ? 'success' : 'danger'" size="large">
                {{ systemStatus.mfa?.installed ? `✓ ${t('processor.available')}` : `✗ ${t('processor.notInstalled')}` }}
              </el-tag>
            </div>
            <div v-if="systemStatus.mfa?.installed" class="status-item">
              <span class="label">{{ t('processor.version') }}:</span>
              <span>{{ systemStatus.mfa?.version }}</span>
            </div>
          </el-col>

          <el-col :xs="24" :sm="12">
            <div class="label">{{ t('processor.modelStatus') }}:</div>
            <div class="model-list">
              <div v-for="(downloaded, lang) in normalizedModels" :key="lang" class="model-item">
                <el-tag :type="downloaded ? 'success' : 'warning'" size="small">
                  {{ lang.toUpperCase() }}: {{ downloaded ? '✓' : '✗' }}
                </el-tag>
                <el-button
                  v-if="!downloaded"
                  link
                  size="small"
                  @click="downloadModel(lang)"
                  :loading="downloadingLangs.includes(lang)"
                >
                  {{ t('processor.download') }}
                </el-button>
              </div>
            </div>
          </el-col>

          <el-col :xs="24">
            <div class="label">{{ t('processor.processingModules') }}:</div>
            <div class="model-list">
              <div class="model-item">
                <el-tag 
                  :type="systemStatus.audio_processing?.pyworld_available ? 'success' : 'warning'" 
                  size="small"
                >
                  PyWORLD (DIO/Harvest): {{ systemStatus.audio_processing?.pyworld_available ? '✓' : '✗' }}
                </el-tag>
              </div>
              <div class="model-item">
                <el-tag
                  :type="systemStatus.audio_processing?.f0_backends?.crepe?.available ? 'success' : 'info'"
                  size="small"
                >
                  CREPE: {{ systemStatus.audio_processing?.f0_backends?.crepe?.available ? '✓' : '✗' }}
                </el-tag>
              </div>
              <div class="model-item">
                <el-tag
                  :type="systemStatus.audio_processing?.f0_backends?.rmvpe?.available ? 'success' : 'info'"
                  size="small"
                >
                  RMVPE: {{ systemStatus.audio_processing?.f0_backends?.rmvpe?.available ? '✓' : '✗' }}
                </el-tag>
              </div>
            </div>
          </el-col>

          <el-col :xs="24">
            <div class="label">{{ t('processor.altBackends') }}:</div>
            <div class="model-list">
              <div v-for="(info, key) in altBackends" :key="key" class="model-item">
                <el-tag :type="info.available ? 'success' : 'info'" size="small">
                  {{ key === 'whisperx' ? t('processor.backendWhisperx')
                     : key === 'qwen3_asr' ? 'Qwen3-ASR-1.7B'
                     : key === 'qwen3_aligner' ? 'Qwen3-FA-0.6B'
                     : key === 'nemo_aligner' ? t('processor.backendNemoAligner')
                     : key }}:
                  {{ info.available ? `✓ ${t('processor.available')}` : `✗ ${t('processor.notInstalled')}` }}
                </el-tag>
                <span v-if="!info.available" class="help-text" style="font-size:11px;margin-left:6px">
                  {{ key === 'whisperx' ? t('processor.packageHintWhisperx')
                     : key === 'nemo_aligner' ? t('processor.packageHintNemo')
                     : key === 'qwen3_aligner' ? t('processor.packageHintQwen3Aligner')
                     : key === 'qwen3_asr' ? t('processor.packageHintQwen3Asr')
                     : t('processor.packageHintTransformers') }}
                </span>
              </div>
            </div>
            <div v-if="alignerStatus.models_dir" style="margin-top:6px;font-size:12px;color:#909399">
              📁 {{ t('processor.modelsLocation') }}：<code style="color:#67c23a">{{ alignerStatus.models_dir }}</code>
              <span style="margin-left:6px">{{ t('processor.modelsLocationHint') }}</span>
            </div>
          </el-col>
        </el-row>
      </el-card>
    </div>

    <div v-if="systemStatus && !systemStatus.mfa?.installed" class="warning-box">
      <el-alert type="error" :closable="false" show-icon>
        <template #title>❌ {{ t('processor.mfaNotInstalledTitle') }}</template>
        <p>{{ t('processor.mfaNotInstalledBody') }}</p>
        <code>pip install montreal-forced-aligner</code>
        <p style="margin-top: 10px">{{ t('processor.mfaNotInstalledMore') }}</p>
      </el-alert>
    </div>

    <div v-if="systemStatus && systemStatus.mfa?.installed && !isReady && processingMode !== 'project-only'" class="warning-box">
      <el-alert type="warning" :closable="false" show-icon>
        <template #title>⚠️ {{ t('processor.notReadyTitle') }}</template>
        <p>{{ t('processor.notReadyBody') }}</p>
      </el-alert>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { UploadFilled, InfoFilled } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'

const emit = defineEmits<{
  (e: 'status-changed', status: SystemStatus): void
}>()

const { t, locale } = useI18n()

type ProcessingMode = 'mfa-only' | 'full' | 'project-only'

interface FormData {
  audioFile: File | null
  labFile: File | null
  midiFile: File | null       // MIDI 文件（仅 project-only 模式）
  text: string
  language: string
  outputFormat: string
  projectTitle: string
  phonemeMode: 'none' | 'merge' | 'hiragana' | 'katakana'
}

interface AdvancedConfig {
  bpm: number
  base_pitch: number
  auto_note_pitch: boolean
  export_pitch_line: boolean
  f0_method: 'dio' | 'harvest' | 'crepe' | 'rmvpe'
  f0_device: 'auto' | 'cpu' | 'cuda'
  aligner_device: 'auto' | 'cpu' | 'cuda'  // WhisperX / Qwen3 对齐工具运行设备
  align_pitch_shift_semitones: number       // 对齐辅助移调（半音，正数升调/负数降调）
                                              // 仅影响送入对齐后端的临时音频副本，不影响
                                              // 最终 LAB 时间戳换算、F0 提取或工程文件音高
  whisperx_model: string                    // WhisperX Whisper 模型选择
  whisperx_batch_size: number                // WhisperX 推理批大小（仅 aligner_device=cuda 时有实际意义）
  nemo_model: string                        // NeMo Forced Aligner 模型覆盖（可选，留空用语言默认模型）
  crepe_model: 'full' | 'tiny'
  precision: 'single' | 'double'
  f0_smooth: boolean
  f0_smooth_window: number
  vsqx_pitch_smooth_window: number
  f0_floor: number
  f0_ceil: number
}

interface F0BackendStatus {
  available: boolean
  torch?: boolean
  torchcrepe?: boolean
  cuda?: boolean
  model_path?: string
  model_found?: boolean
}

interface SystemStatus {
  mfa?: {
    installed: boolean
    version: string
    models?: Record<string, boolean>
  }
  audio_processing?: {
    pyworld_available: boolean
    supported_formats: string[]
    f0_backends?: {
      dio?: F0BackendStatus
      harvest?: F0BackendStatus
      crepe?: F0BackendStatus
      rmvpe?: F0BackendStatus
    }
  }
  alt_aligners?: Record<string, { available: boolean; message: string; requires_text?: boolean }>
}

// 核心表单与模式状态（合并唯一声明）
const processingMode = ref<ProcessingMode>('mfa-only')
const englishWordAlign = ref<boolean>(false)  // 英语单词级对齐（不做 ARPABET 音素拆分）
const wordPhonemeMap    = ref<boolean>(false)  // 英语单词→音素映射手动开关，默认关闭；
                                                // 仅在 showWordPhonemeMap（自动可见性条件）
                                                // 满足时才会展示给用户，需用户主动开启后
                                                // 才会随表单一起提交为 true（见下方 computed
                                                // wordPhonemeMapEffective）
// 【解耦】"选择词典"与"英语单词→音素映射"开关是两个独立功能：词典可以把
// 任意语言的字词映射为音素（不局限于英语单词），其显示条件只看是否处于
// 会产出 SVP/VSQX 工程文件的模式（见下方 showDictSource），不受
// englishWordAlign / wordPhonemeMap 开关影响；命中词典时的匹配优先级也
// 高于"英语单词→音素映射"（词典优先，未命中才回退）。
const dictSource        = ref<string>('default')  // 选择词典："default"（软件默认）或某个独立词典名
const dictionaries      = ref<{ name: string; notation: string; count: number }[]>([])  // 用户在"词典管理"页面创建的独立词典列表
const alignerBackend = ref<string>('mfa')   // 对齐后端选择
const alignerStatus = ref<Record<string, any>>({
  whisperx:      { available: false, message: t('processor.backendStatusChecking') },
  qwen3_asr:     { available: false, message: t('processor.backendStatusChecking') },
  qwen3_aligner: { available: false, message: t('processor.backendStatusChecking') },
  nemo_aligner:  { available: false, message: t('processor.backendStatusChecking') },
})

// ── TTS跟读（讲述人 + EdgeTTS）状态 ──────────────────────────────────
// 与"音频跟读"（inputMode='audio'，即原有的上传音频对齐流程）互斥；
// TTS跟读不需要用户上传音频，文本本身就是标注来源，音频由 EdgeTTS 合成，
// 对齐固定使用 Qwen3-ForcedAligner（不经过 alignerBackend 选择器）。
type TtsNarrator = { id: string; name: string; engine?: string; voice: string; rate: string; pitch: string; volume: string; language?: string }
type TtsVoice = { id: string; name: string; gender?: string; locale: string }
type TtsEngine = { id: string; label: string; label_zh: string; available: boolean; message: string }

const inputMode = ref<'audio' | 'tts' | 'subtitle'>('audio')
const ttsConfig = ref<{ engine: string; narratorId: string; voice: string; rateNum: number; pitchNum: number; volumeNum: number }>({
  engine: 'edge_tts',
  narratorId: '',
  voice: '',
  rateNum: 0,
  pitchNum: 0,
  volumeNum: 0,
})
const narrators = ref<TtsNarrator[]>([])
// 语音预设下拉框只展示与当前所选引擎匹配的预设，避免"选了讲述人引擎，
// 列表里却混着一堆 EdgeTTS 预设"的困惑。
const filteredNarrators = computed(() =>
  narrators.value.filter(n => (n.engine || 'edge_tts') === ttsConfig.value.engine)
)
const ttsEngines = ref<TtsEngine[]>([])
const ttsEnginesLoading = ref(false)
const ttsVoices = ref<TtsVoice[]>([])
const ttsVoicesLoading = ref(false)

// 引擎中/英文名按当前界面语言展示："选择 TTS"下拉框、语音预设管理对话框的
// 引擎选择器、预设列表的引擎列都复用这一个函数，不需要各处重复判断。
const engineLabel = (id?: string): string => {
  const eng = ttsEngines.value.find(e => e.id === id)
  if (!eng) return id || ''
  return locale.value.startsWith('zh') ? eng.label_zh : eng.label
}

// 语音预设管理对话框里的音色列表：跟随对话框内正在编辑的 narratorForm.engine，
// 而不是主面板当前选中的 ttsConfig.engine——用户可能想为一个当前未选中的
// 引擎创建/编辑预设（例如主面板选的是 EdgeTTS，但想顺手建一个讲述人预设）。
const narratorFormVoices = ref<TtsVoice[]>([])
const narratorFormVoicesLoading = ref(false)

// ── "优化文本"弹窗 ───────────────────────────────────────────────────
// 弹窗内编辑的是 draft（原文本框内容的一份副本），点击工具栏按钮调用
// /api/text/optimize 在 draft 上原地转换、可连续多次点击叠加效果（例如
// 先"智能转换"再"去除多余符号"）；只有点击"应用"才会把 draft 写回打开
// 弹窗时传入的目标文本框，取消/关闭弹窗不会影响原文本框。
interface TextOptimizerState {
  visible: boolean
  draft: string
  loading: string   // 当前正在请求的 action id，空字符串表示未在请求中
  error: string
  target: Record<string, any> | null   // 打开弹窗时绑定的目标对象（如 formData 或某个 box）
  field: string                         // 目标对象上要写回的字段名（如 'text'）
  language: string                      // 智能转换/仅转换（数字）/逐字转换/仅转换符号 使用的语种
  everyN: number                        // "按每几句插入换行"的句子数量 N，默认 2
}

const textOptimizer = ref<TextOptimizerState>({
  visible: false, draft: '', loading: '', error: '', target: null, field: 'text', language: 'cmn', everyN: 2,
})

// language 参数可选：不传时使用打开弹窗那一刻 target 上的 language 字段
// （如 formData.language）；对话框批量处理页面每个对话框没有独立语种，
// 使用的是页面共享的 sharedLanguage，由调用方显式传入。
const openTextOptimizer = (target: Record<string, any>, field: string, language?: string) => {
  textOptimizer.value = {
    visible: true,
    draft: target[field] || '',
    loading: '',
    error: '',
    target,
    field,
    language: language || target.language || 'cmn',
    everyN: 2,
  }
}

const runTextOptimize = async (action: string) => {
  textOptimizer.value.loading = action
  textOptimizer.value.error = ''
  try {
    const res = await fetch('/api/text/optimize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: textOptimizer.value.draft,
        action,
        language: textOptimizer.value.language,
        n: textOptimizer.value.everyN,
      }),
    })
    const data = await res.json()
    if (!data.success) {
      textOptimizer.value.error = data.error || t('processor.textOptimizeFailed')
      return
    }
    textOptimizer.value.draft = data.text
  } catch (e: any) {
    textOptimizer.value.error = e?.message || String(e)
  } finally {
    textOptimizer.value.loading = ''
  }
}

const applyTextOptimize = () => {
  if (textOptimizer.value.target) {
    textOptimizer.value.target[textOptimizer.value.field] = textOptimizer.value.draft
  }
  textOptimizer.value.visible = false
}

// ── "查找替换"弹窗（类似 Ctrl+H）：与"优化文本"弹窗共用同一套"draft 副本 +
// 应用才写回"的模式，但纯前端字符串/正则替换，不经过任何后端接口。
// caseSensitive 控制是否区分大小写，useRegex 控制"查找"内容是否作为正则
// 表达式解析（此时"替换为"支持 $1 $2 等捕获组引用）。cursor 记录"查找
// 下一个"/"替换"时上一次匹配结束的位置，用于循环定位与逐个替换。 ──
interface FindReplaceState {
  visible: boolean
  draft: string
  find: string
  replace: string
  caseSensitive: boolean
  useRegex: boolean
  target: Record<string, any> | null
  field: string
  error: string
  cursor: number
}

const findReplace = ref<FindReplaceState>({
  visible: false, draft: '', find: '', replace: '', caseSensitive: false, useRegex: false,
  target: null, field: 'text', error: '', cursor: 0,
})
// el-input 组件实例的模板引用，用于"查找下一个"时定位并选中 textarea
// 内的匹配文本；el-input 把内部原生 <textarea> 暴露在 .textarea 属性上
// （视 Element Plus 版本而定，这里做兼容性兜底）。
const findReplaceTextareaRef = ref<any>(null)

const openFindReplace = (target: Record<string, any>, field: string) => {
  findReplace.value = {
    visible: true,
    draft: target[field] || '',
    find: '',
    replace: '',
    caseSensitive: false,
    useRegex: false,
    target,
    field,
    error: '',
    cursor: 0,
  }
}

// 根据当前"正则表达式"/"区分大小写"开关构造一个全局匹配用的 RegExp；
// 非正则模式下先对查找字符串做转义，避免用户输入的 . * ( 等符号被
// 误当作正则特殊字符。构造失败（比如用户输入了不合法的正则）时返回
// null 并把错误信息写入 findReplace.error。
const buildFindRegex = (): RegExp | null => {
  findReplace.value.error = ''
  const raw = findReplace.value.find
  if (!raw) return null
  try {
    const flags = findReplace.value.caseSensitive ? 'g' : 'gi'
    const pattern = findReplace.value.useRegex ? raw : raw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    return new RegExp(pattern, flags)
  } catch (e: any) {
    findReplace.value.error = t('processor.findReplaceInvalidRegex', { msg: e?.message || String(e) })
    return null
  }
}

// 弹窗内实时显示的匹配数量，随 draft / find / caseSensitive / useRegex
// 任一变化自动重新计算。
const findReplaceMatchCount = computed<number>(() => {
  const re = buildFindRegex()
  if (!re) return 0
  const matches = findReplace.value.draft.match(re)
  return matches ? matches.length : 0
})

// "查找下一个"：不修改文本，只是把光标移动到 textarea 内下一处匹配并
// 选中，方便用户在替换前先确认位置；到达末尾后回到开头循环查找。
const findReplaceNext = () => {
  const re = buildFindRegex()
  if (!re) return
  const text = findReplace.value.draft
  re.lastIndex = findReplace.value.cursor
  let m = re.exec(text)
  if (!m) {
    re.lastIndex = 0
    m = re.exec(text)
  }
  if (!m) {
    findReplace.value.error = t('processor.findReplaceNotFound')
    return
  }
  findReplace.value.cursor = m.index + m[0].length
  const matchStart = m.index
  const matchEnd = m.index + m[0].length
  focusAndSelectInFindReplaceTextarea(matchStart, matchEnd)
}

// 统一的"聚焦并选中 textarea 内一段文本"逻辑，供"查找下一个""替换"
// "全部替换"共用。之所以单独抽出来，是因为 el-dialog 自带 focus-trap：
// 弹窗打开时会持续管理焦点，如果只用一次 nextTick() 就调用
// el.focus() + setSelectionRange()，焦点很可能在紧接着的下一帧被
// dialog 的 focus-trap 抢回去（表现为选区仍然存在，但因为 textarea
// 已经 blur，浏览器不会渲染蓝色高亮，看起来像"完全没定位到"）。
// 这里用 requestAnimationFrame 再等一帧、并在下一次事件循环里二次
// 补上 focus + setSelectionRange，覆盖 dialog 抢焦点的情况。
//
// setSelectionRange 本身只设置选区，不会滚动 textarea 让选区可见——
// 如果匹配文本不在当前可视区域内（文本较长、匹配在后面几屏），选区
// 虽然生效但用户完全看不到、也不会自动滚过去。原生 textarea 没有
// "滚动到某字符位置"的 API，这里用一个隐藏的镜像 div 复刻 textarea
// 的字体/换行/内边距，把镜像 div 里对应位置的 offsetTop 当作滚动目标，
// 尽量把匹配行滚动到可视区域中间。
const scrollTextareaToSelection = (el: HTMLTextAreaElement, start: number) => {
  const mirror = document.createElement('div')
  const style = window.getComputedStyle(el)
  const propsToCopy = [
    'boxSizing', 'width', 'fontFamily', 'fontSize', 'fontWeight', 'fontStyle',
    'letterSpacing', 'lineHeight', 'paddingTop', 'paddingRight', 'paddingBottom',
    'paddingLeft', 'borderTopWidth', 'borderRightWidth', 'borderBottomWidth',
    'borderLeftWidth', 'whiteSpace', 'wordWrap', 'wordBreak', 'tabSize',
  ] as const
  propsToCopy.forEach((prop) => {
    ;(mirror.style as any)[prop] = style[prop as any]
  })
  mirror.style.position = 'absolute'
  mirror.style.visibility = 'hidden'
  mirror.style.whiteSpace = 'pre-wrap'
  mirror.style.wordWrap = 'break-word'
  mirror.style.height = 'auto'
  mirror.style.overflow = 'hidden'
  mirror.style.top = '0'
  mirror.style.left = '-9999px'

  const text = el.value
  const before = text.slice(0, start)
  const marker = document.createElement('span')
  marker.textContent = '\u200b' // 零宽字符占位，避免影响换行计算
  mirror.appendChild(document.createTextNode(before))
  mirror.appendChild(marker)
  mirror.appendChild(document.createTextNode(text.slice(start) || '\u200b'))

  document.body.appendChild(mirror)
  const markerTop = marker.offsetTop
  const lineHeight = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.2 || 20
  document.body.removeChild(mirror)

  // 把匹配所在行滚动到 textarea 可视区域的中间，而不是贴边，避免匹配
  // 行刚好卡在边缘不易察觉。
  const targetScrollTop = markerTop - el.clientHeight / 2 + lineHeight / 2
  el.scrollTop = Math.max(0, targetScrollTop)
}

const focusAndSelectInFindReplaceTextarea = (start: number, end: number) => {
  const doFocusSelect = () => {
    const inst = findReplaceTextareaRef.value
    const el: HTMLTextAreaElement | null =
      inst?.textarea || inst?.input || inst?.$el?.querySelector?.('textarea') || inst?.$el?.querySelector?.('input') || null
    if (el) {
      el.focus()
      el.setSelectionRange(start, end)
      scrollTextareaToSelection(el, start)
    }
    return el
  }
  nextTick(() => {
    doFocusSelect()
    // 再等一帧，覆盖 el-dialog focus-trap 抢焦点导致选区不可见的情况
    requestAnimationFrame(() => {
      doFocusSelect()
    })
  })
}

// 只替换当前光标所在的下一处匹配（相当于 Ctrl+H 里的"替换"单次按钮），
// 替换后 cursor 停在替换结果之后，方便连续点击逐个替换。
const runFindReplaceOne = () => {
  const re = buildFindRegex()
  if (!re) return
  const text = findReplace.value.draft
  re.lastIndex = findReplace.value.cursor
  let m = re.exec(text)
  if (!m) {
    re.lastIndex = 0
    m = re.exec(text)
  }
  if (!m) {
    findReplace.value.error = t('processor.findReplaceNotFound')
    return
  }
  const replacement = findReplace.value.useRegex
    ? m[0].replace(new RegExp(re.source, re.flags.replace('g', '')), findReplace.value.replace)
    : findReplace.value.replace
  findReplace.value.draft = text.slice(0, m.index) + replacement + text.slice(m.index + m[0].length)
  findReplace.value.cursor = m.index + replacement.length
  // 替换后选中刚刚替换出来的新文本，让用户能直接看到这次替换发生在哪
  focusAndSelectInFindReplaceTextarea(m.index, m.index + replacement.length)
}

// 全部替换（Ctrl+H 里的"全部替换"）：一次性替换 draft 内所有匹配项。
const runFindReplaceAll = () => {
  const re = buildFindRegex()
  if (!re) return
  const before = findReplace.value.draft
  const after = before.replace(re, findReplace.value.replace)
  if (before === after) {
    findReplace.value.error = t('processor.findReplaceNotFound')
    return
  }
  findReplace.value.draft = after
  findReplace.value.cursor = 0
  // 全部替换后没有单一"匹配位置"可选中，这里只是把焦点还给 textarea，
  // 光标放在开头，方便用户继续编辑或核对结果。
  focusAndSelectInFindReplaceTextarea(0, 0)
}

const applyFindReplace = () => {
  if (findReplace.value.target) {
    findReplace.value.target[findReplace.value.field] = findReplace.value.draft
  }
  findReplace.value.visible = false
}

// ── 手动分段预览：由"生成预览"按钮触发，按新分段规则（优先按换行分段，
// 单行过长再按句号/逗号二次切割）逐句合成生成预览音频（不做 Qwen3-FA
// 对齐，不再有句子数量上限）。previewId 是后端返回的缓存凭证——生成后若
// 没有改动文本/参数，点击"开始处理"会带上它直接复用这份分句音频去对齐；
// 一旦相关输入发生变化就会被清空，逼迫"开始处理"退回"先合成再对齐"的
// 完整流程，避免用旧音频对新文本。
const segmentPreview = ref<{
  loading: boolean
  audioUrl: string
  previewId: string
  sentenceCount: number
  warnings: string[]
  error: string
}>({
  loading: false,
  audioUrl: '',
  previewId: '',
  sentenceCount: 0,
  warnings: [],
  error: '',
})
let segmentPreviewRequestSeq = 0

const narratorManagerVisible = ref(false)
const narratorForm = ref<TtsNarrator>({ id: '', name: '', engine: 'edge_tts', voice: '', rate: '+0%', pitch: '+0Hz', volume: '+0%' })
const narratorSaving = ref(false)

// 语音预设对话框里的语速/音调/音量：narratorForm 里存的是 EdgeTTS 风格的
// 字符串（"+10%" / "-5Hz"），但 el-slider 需要绑定数字，这里用 computed
// get/set 做双向转换，写法与主面板的 ttsConfig.rateNum/pitchNum/volumeNum
// 保持一致，避免另外维护一套数字 ref 和 watch 同步逻辑。
const narratorFormRateNum = computed<number>({
  get: () => parseInt(narratorForm.value.rate) || 0,
  set: (v: number) => { narratorForm.value.rate = `${v >= 0 ? '+' : ''}${v}%` },
})
const narratorFormPitchNum = computed<number>({
  get: () => parseInt(narratorForm.value.pitch) || 0,
  set: (v: number) => { narratorForm.value.pitch = `${v >= 0 ? '+' : ''}${v}Hz` },
})
const narratorFormVolumeNum = computed<number>({
  get: () => parseInt(narratorForm.value.volume) || 0,
  set: (v: number) => { narratorForm.value.volume = `${v >= 0 ? '+' : ''}${v}%` },
})

const formData = ref<FormData>({
  audioFile: null,
  labFile: null,
  midiFile: null,
  text: '',
  language: 'cmn',
  outputFormat: 'sv',
  projectTitle: 'Project',
  phonemeMode: 'none'
})

const advancedConfig = ref<AdvancedConfig>({
  bpm: 120,
  base_pitch: 60,
  auto_note_pitch: true,
  export_pitch_line: true,
  f0_method: 'dio',
  f0_device: 'auto',
  aligner_device: 'auto',
  align_pitch_shift_semitones: 0,
  whisperx_model: 'large-v3',
  whisperx_batch_size: 16,
  nemo_model: '',
  crepe_model: 'full',
  precision: 'double',
  f0_smooth: true,
  f0_smooth_window: 5,
  vsqx_pitch_smooth_window: 5,
  f0_floor: 71,
  f0_ceil: 800
})

const processing = ref(false)
const progressPercent = ref(0)
const result = ref<any>(null)
const error = ref('')
const checkingStatus = ref(false)
const downloadingLangs = ref<string[]>([])
const downloadingProject = ref(false)

const systemStatus = ref<SystemStatus>({
  mfa: {
    installed: false,
    version: 'unknown',
    models: { cmn: false, eng: false, jpn: false, kor: false, yue: false }
  },
  audio_processing: {
    pyworld_available: false,
    supported_formats: [],
    f0_backends: {
      dio: { available: true },
      harvest: { available: true },
      crepe: { available: false },
      rmvpe: { available: false }
    }
  }
})

const processingDetails = ref<any[]>([
  { stage: t('processor.stageAlign'), status: '等待', message: t('processor.stagePrepareAlign') },
  { stage: t('processor.stageF0'), status: '等待', message: t('processor.stageExtractF0') },
  { stage: t('processor.stageProject'), status: '等待', message: t('processor.stageGenerateProject') },
])

const currentJobId = ref<string>('')
let jobPollTimer: number | null = null

// MIDI 导入状态
const midiInfo = ref<{ bpm: number; loaded: boolean }>({ bpm: 120, loaded: false })
const labMidiUploadKey = ref(0)
const audioUploadKey = ref(0)

const midiLoaded = computed(() => processingMode.value === 'project-only' && !!formData.value.midiFile)
const selectedNotationFile = computed(() => formData.value.labFile || formData.value.midiFile)

// ── 字幕跟读（SRT/LRC 导入）状态 ─────────────────────────────────────
// 与"音频跟读"/"TTS跟读"互斥：不需要用户手动填写文本，文本完全来自
// 字幕文件的每一条内容；对齐固定使用 Qwen3-ForcedAligner（与 TTS跟读
// 一致，不经过 alignerBackend 选择器）。
const subtitleImport = ref<{ audioFile: File | null; subtitleFile: File | null }>({
  audioFile: null,
  subtitleFile: null,
})
const subtitleAudioUploadKey = ref(0)
const subtitleFileUploadKey = ref(0)

const handleSubtitleAudioSelect = (file: any) => {
  const raw: File | null = file?.raw || null
  if (!raw) return
  subtitleImport.value.audioFile = raw
}

const handleSubtitleFileSelect = (file: any) => {
  const raw: File | null = file?.raw || null
  if (!raw) return
  subtitleImport.value.subtitleFile = raw
}


// VSQX 歌手名 / ID：按处理模式 + 语种自动切换
// project-only 无语种选择，固定使用日语歌手声库
const vsqxSingerConfig = computed((): { name: string; id: string } => {
  if (processingMode.value === 'project-only') {
    return { name: 'MIKU_V4X_Original_EVEC', id: 'BCNFCY43LB2LZCD4' }
  }
  // full 模式：按语种映射
  switch (formData.value.language) {
    case 'eng': return { name: 'MIKU_V4_English',         id: 'BMLTD846MLYP2MEK' }
    case 'jpn': return { name: 'MIKU_V4X_Original_EVEC', id: 'BCNFCY43LB2LZCD4' }
    case 'kor': return { name: 'SeeU_SV01_KOR',           id: 'BX77CNBZLBPHZX97' }
    default:    return { name: 'MIKU_V4_Chinese',         id: 'BNGE7CP7EMTRSNC3' }  // cmn / yue
  }
})

// 计算属性
const normalizedModels = computed(() => {
  const defaultModels = { cmn: false, eng: false, jpn: false, kor: false, yue: false }
  if (!systemStatus.value.mfa?.models || typeof systemStatus.value.mfa.models !== 'object') {
    return defaultModels
  }
  return { ...defaultModels, ...systemStatus.value.mfa.models }
})

const isReady = computed(() => {
  // TTS跟读 / 字幕跟读固定使用 Qwen3-ForcedAligner，就绪状态看它是否可用
  if (inputMode.value === 'tts' || inputMode.value === 'subtitle') {
    return alignerStatus.value['qwen3_aligner']?.available ?? false
  }
  // 替代后端不依赖 MFA 模型，只要后端可用或是 MFA 时检查模型
  if (alignerBackend.value !== 'mfa') {
    return alignerStatus.value[alignerBackend.value]?.available ?? false
  }
  return !!(systemStatus.value.mfa?.installed && normalizedModels.value[formData.value.language as keyof typeof normalizedModels.value])
})

// WhisperX / Qwen3-ASR 支持纯 ASR 模式（文本可选）
const isTextOptional = computed(() =>
  ['whisperx', 'qwen3_asr'].includes(alignerBackend.value)
)

// 控制"英语单词→音素映射"开关本身是否显示。
// 与"英语单词级对齐"开关挂钩：只有当用户明确要把混合文本中的英语
// 单词做单词级处理时，"是否要把这些英语单词转成音素"才有意义。
// 注意：这里只决定"英语单词→音素映射"开关的可见性，与下方"选择词典"
// （showDictSource）完全独立，互不影响彼此的显示/隐藏。
// - 必须是"完整处理"（full）：mfa-only 不产出工程文件，project-only 跳过
//   对齐，两者都不会走到 g2p_en / 音素映射这一步；
// - 语言切到日语时下方 watch(formData.value.language) 会主动把
//   englishWordAlign 重置为 false（仅靠上面的 v-if 隐藏开关不会重置其
//   底层状态，之前这里错误地假设"隐藏 = 恒为 false"，导致语言切到日语
//   后本开关未同步隐藏——已改为显式 watch 重置，而不是依赖 v-if）；
// - 输出格式需支持 SVP/VSQX（UTAU 没有 phonemes 字段可写）。
const showWordPhonemeMap = computed(() => {
  const format = formData.value.outputFormat?.toLowerCase() || ''
  const isSupportedFormat = format.includes('sv') || format.includes('vsqx')
  return processingMode.value === 'full' && englishWordAlign.value && isSupportedFormat
})

// 实际提交给后端的"英语单词→音素映射"值：只有在开关控件可见
// （showWordPhonemeMap）且用户主动打开了手动开关（wordPhonemeMap）时才为
// true。控件不可见时（比如切到日语或"仅生成工程"）wordPhonemeMap 本身
// 不会被用户看到/操作，但为了保险这里仍然显式 AND 一次
// showWordPhonemeMap，避免残留的开关状态在条件重新满足前被误提交。
const wordPhonemeMapEffective = computed(() => showWordPhonemeMap.value && wordPhonemeMap.value)

// 控制"选择词典"表单项是否显示。
// 与"英语单词→音素映射"开关完全解耦，不受其开启/关闭影响：
// - 只要处于会真正产出 SVP/VSQX 工程文件的模式即可显示——"完整处理"
//   （full，覆盖中文/英语/日语/韩语/粤语全部语种）或"仅生成工程文件"
//   （project-only，跳过对齐直接用已有 LAB/MIDI 生成工程）；
//   "仅标注"（mfa-only）不产出工程文件，词典无处可用，故不显示；
// - 输出格式需支持 SVP/VSQX（UTAU 没有 phonemes 字段可写）；
// 词典本身可以把任意语言的字词映射为音素，不局限于英语单词，因此其显示
// 不应像"英语单词→音素映射"那样依赖语种或 englishWordAlign 开关。
const showDictSource = computed(() => {
  const format = formData.value.outputFormat?.toLowerCase() || ''
  const isSupportedFormat = format.includes('sv') || format.includes('vsqx')
  return (processingMode.value === 'full' || processingMode.value === 'project-only') && isSupportedFormat
})

// 根据当前输出格式过滤"选择词典"下拉列表：
// - 输出格式为 SVP（Synthesizer V）时，只显示 notation === 'synthesizerv' 的词典，
//   隐藏 notation === 'vocaloid'（VOCALOID4）的词典；
// - 输出格式为 VSQX（VOCALOID4）时，只显示 notation === 'vocaloid' 的词典，
//   隐藏 notation === 'synthesizerv'（Synthesizer V）的词典。
// "使用软件默认值"选项不受影响，始终可选。
const filteredDictionaries = computed(() => {
  const format = formData.value.outputFormat?.toLowerCase() || ''
  if (format.includes('vsqx')) {
    return dictionaries.value.filter(d => d.notation === 'vocaloid')
  }
  if (format.includes('sv')) {
    return dictionaries.value.filter(d => d.notation === 'synthesizerv')
  }
  return dictionaries.value
})

// 若用户之前选中的独立词典因输出格式切换而被过滤掉（音素标注体系与新格式
// 不匹配），自动回退为"使用软件默认值"，避免提交一个已隐藏、不兼容的词典。
watch(filteredDictionaries, (list) => {
  if (dictSource.value === 'default') return
  if (!list.some(d => d.name === dictSource.value)) {
    dictSource.value = 'default'
  }
})

// "选择词典"的提示文本：强调词典可将任意语言映射为音素，且优先级高于
// "英语单词→音素映射"，根据输出格式（SVP/VSQX）动态调整措辞。
const selectDictionaryHint = computed(() => {
  const format = formData.value.outputFormat?.toLowerCase() || ''

  if (format.includes('vsqx')) {
    return t('processor.selectDictionaryHintVsqx')
  }

  return t('processor.selectDictionaryHintSvp')
})

// alignerStatus 去掉 models_dir 字段，只保留后端对象供 v-for 使用
const altBackends = computed(() => {
  const { models_dir: _md, ...backends } = alignerStatus.value as any
  return backends as Record<string, any>
})

const alignerBackendLabel = computed(() => {
  void locale.value
  const labels: Record<string, string> = {
    mfa: t('processor.backendMfa'),
    whisperx: t('processor.backendWhisperx'),
    qwen3_asr: t('processor.backendQwen3Asr'),
    qwen3_aligner: t('processor.backendQwen3Aligner'),
    nemo_aligner: t('processor.backendNemoAligner'),
  }
  // TTS跟读 / 字幕跟读固定使用 Qwen3-ForcedAligner，不经过 alignerBackend
  // 选择器（该 ref 在这两种模式下可能仍停留在用户上次在"音频跟读"模式
  // 选过的其它后端），这里显式返回固定标签，避免界面误显示成错误的后端名。
  if (inputMode.value === 'tts' || inputMode.value === 'subtitle') {
    return labels.qwen3_aligner
  }
  return labels[alignerBackend.value] || alignerBackend.value
})

watch(alignerBackend, (backend) => {
  if (processingMode.value === 'project-only' && ['whisperx', 'qwen3_asr', 'qwen3_aligner', 'nemo_aligner'].includes(backend)) {
    processingMode.value = 'mfa-only'
  }
})

// 语言切到日语时，"英语单词级对齐"开关对日语没有意义（上方 v-if 会隐藏
// 该开关），但仅隐藏控件不会重置其底层状态——这里显式重置为 false，
// showWordPhonemeMap 依赖 englishWordAlign.value，会跟着自动隐藏；同时
// 顺手把 wordPhonemeMap 本身也重置掉，避免用户切回非日语语种时，该开关
// 无声无息地沿用切换前遗留的开启状态。
watch(() => formData.value.language, (lang) => {
  if (lang === 'jpn') {
    englishWordAlign.value = false
    wordPhonemeMap.value = false
  }
})

// 根据不同模式控制提交按钮的禁用状态
const isSubmitDisabled = computed(() => {
  if (inputMode.value === 'tts') {
    return !formData.value.text.trim() || !ttsConfig.value.voice || !isReady.value || segmentPreview.value.loading
  }
  if (inputMode.value === 'subtitle') {
    // 字幕跟读用的是独立的 subtitleImport 状态（音频+字幕文件两个字段），
    // 不经过 formData.value.audioFile / text，需要单独判断，否则会一直
    // 沿用下面音频跟读模式的判断条件，误判为"未选择音频"而永久禁用。
    return !subtitleImport.value.audioFile || !subtitleImport.value.subtitleFile || !isReady.value
  }
  if (processingMode.value === 'project-only') {
    return !formData.value.audioFile || (!formData.value.labFile && !formData.value.midiFile)
  }
  const noText = !formData.value.text.trim() && !isTextOptional.value
  return !formData.value.audioFile || noText || !isReady.value
})

const handleLabMidiExceed = () => {
  ElMessage.error(t('processor.chooseOneFile'))
}

const handleLabMidiChange = (file: any) => {
  const raw: File | null = file?.raw || null
  if (!raw) return

  const ext = raw.name.toLowerCase().split('.').pop() || ''

  // 先清空，确保只保留一个文件
  formData.value.labFile = null
  formData.value.midiFile = null
  midiInfo.value = { bpm: 120, loaded: false }

  if (ext === 'lab') {
    formData.value.labFile = raw
    labMidiUploadKey.value += 1
    return
  }

  if (ext === 'mid' || ext === 'midi') {
    formData.value.midiFile = raw
    extractMidiBpm(raw).then(({ bpm }) => {
      midiInfo.value = { bpm, loaded: true }
    })
    labMidiUploadKey.value += 1
    return
  }

  ElMessage.error(t('processor.onlySupportNotation'))
  labMidiUploadKey.value += 1
}

const fetchDictionaries = async () => {
  try {
    const res = await fetch('/api/dictionary')
    const data = await res.json()
    if (res.ok && data.success) {
      dictionaries.value = data.dictionaries || []
    }
  } catch {
    // 静默失败：词典列表拉取失败时选择器仍可用（只是只剩"使用软件默认值"），
    // 不应阻塞主流程或弹错误提示打扰用户。
  }
}

onMounted(() => {
  checkSystemStatus()
  fetchDictionaries()
  fetchNarrators()
})

// ── TTS跟读辅助函数 ──────────────────────────────────────────────────

const fetchTtsEngines = async () => {
  ttsEnginesLoading.value = true
  try {
    const res = await fetch('/api/tts/engines')
    const data = await res.json()
    if (data.success) {
      ttsEngines.value = data.engines || []
      // 当前选中的引擎如果不在返回列表里（几乎不会发生，兜底处理），
      // 或者尚未选择过引擎，优先选第一个"可用"的引擎，没有可用的就退回
      // 列表第一项，让用户能看到具体的不可用原因（message）。
      if (!ttsEngines.value.some(e => e.id === ttsConfig.value.engine)) {
        const firstAvailable = ttsEngines.value.find(e => e.available)
        ttsConfig.value.engine = (firstAvailable || ttsEngines.value[0])?.id || 'edge_tts'
      }
    }
  } catch (e) {
    console.error('获取 TTS 引擎列表失败', e)
  } finally {
    ttsEnginesLoading.value = false
  }
}

const fetchTtsVoices = async (language: string, engine?: string) => {
  ttsVoicesLoading.value = true
  try {
    const eng = engine || ttsConfig.value.engine || 'edge_tts'
    const res = await fetch(`/api/tts/voices?engine=${encodeURIComponent(eng)}&language=${encodeURIComponent(language)}`)
    const data = await res.json()
    if (data.success) {
      ttsVoices.value = data.voices || []
      // 若当前选中的音色不在新语种/新引擎的列表里，清空让用户重选，避免
      // "引擎/语种已经切换，但音色还是上一次选的"这种不一致状态。
      if (ttsConfig.value.voice && !ttsVoices.value.some(v => v.id === ttsConfig.value.voice)) {
        ttsConfig.value.voice = ''
      }
    } else {
      ttsVoices.value = []
      if (data.error) ElMessage.error(`❌ ${data.error}`)
    }
  } catch (e) {
    console.error('获取 TTS 音色列表失败', e)
  } finally {
    ttsVoicesLoading.value = false
  }
}

const fetchNarrators = async () => {
  try {
    const res = await fetch('/api/tts/narrators')
    const data = await res.json()
    if (data.success) narrators.value = data.narrators || []
  } catch (e) {
    console.error('获取语音预设列表失败', e)
  }
}

const fetchNarratorFormVoices = async (engine: string) => {
  narratorFormVoicesLoading.value = true
  try {
    const res = await fetch(`/api/tts/voices?engine=${encodeURIComponent(engine)}&language=${encodeURIComponent(formData.value.language)}`)
    const data = await res.json()
    if (data.success) {
      narratorFormVoices.value = data.voices || []
      if (narratorForm.value.voice && !narratorFormVoices.value.some(v => v.id === narratorForm.value.voice)) {
        narratorForm.value.voice = ''
      }
    } else {
      narratorFormVoices.value = []
      if (data.error) ElMessage.error(`❌ ${data.error}`)
    }
  } catch (e) {
    console.error('获取语音预设音色列表失败', e)
  } finally {
    narratorFormVoicesLoading.value = false
  }
}

// 对话框内切换"选择 TTS"引擎时，同步刷新该引擎下的音色列表并清空不匹配的
// 旧音色选择；只在对话框打开期间生效，避免和主面板的 ttsConfig.engine
// watch 互相干扰。
watch(() => narratorForm.value.engine, (engine) => {
  if (narratorManagerVisible.value && engine) fetchNarratorFormVoices(engine)
})

const handleInputModeChange = () => {
  if (inputMode.value === 'tts') {
    if (processingMode.value === 'project-only') processingMode.value = 'full'
    fetchTtsEngines()
    fetchTtsVoices(formData.value.language)
  } else if (inputMode.value === 'subtitle') {
    if (processingMode.value === 'project-only') processingMode.value = 'full'
  }
}

watch(() => formData.value.language, (lang) => {
  if (inputMode.value === 'tts') fetchTtsVoices(lang)
})

// 切换"选择 TTS"引擎时：清空当前音色（不同引擎的音色 ID 体系完全不同，
// 沿用旧值没有意义）并按新引擎重新拉取音色列表。
watch(() => ttsConfig.value.engine, (engine, oldEngine) => {
  if (!engine || engine === oldEngine) return
  ttsConfig.value.voice = ''
  if (inputMode.value === 'tts') fetchTtsVoices(formData.value.language, engine)
})

const handleNarratorSelect = (narratorId: string) => {
  if (!narratorId) {
    // 切回"不使用预设"（自定义）时，把语速/音调/音量重置为默认值——
    // 否则用户会看到下拉框显示"不使用预设"，滑块却还停留在上一个预设的
    // 参数上，误以为已经跟预设脱钩了。
    ttsConfig.value.rateNum = 0
    ttsConfig.value.pitchNum = 0
    ttsConfig.value.volumeNum = 0
    return
  }
  const n = narrators.value.find(x => x.id === narratorId)
  if (!n) return
  if (n.engine && n.engine !== ttsConfig.value.engine) {
    ttsConfig.value.engine = n.engine
  }
  ttsConfig.value.voice = n.voice
  ttsConfig.value.rateNum = parseInt(n.rate) || 0
  ttsConfig.value.pitchNum = parseInt(n.pitch) || 0
  ttsConfig.value.volumeNum = parseInt(n.volume) || 0
}

// ── 手动分段预览 ─────────────────────────────────────────────────────
// 只在用户点击"生成预览"按钮时触发（不再随输入防抖自动生成）：按句末
// 标点分段 → 逐句合成，返回完整拼接后的音频供试听。这一步不做 Qwen3-FA
// 对齐，也不再截断句子数量——会合成完整输入文本。
// 生成成功后会拿到一个 previewId：如果用户紧接着点"开始处理"、且没有
// 改动文本/引擎/音色/语速/音调/音量/语种，后端会直接复用这份分句音频
// 去对齐，不会重新合成一遍；只要上述任一项发生变化，下面的 watch 会清空
// previewId，"开始处理"就会退回"先合成再对齐"的完整流程。
const runSegmentPreview = async () => {
  if (inputMode.value !== 'tts') return
  const text = (formData.value.text || '').trim()
  if (!text || !ttsConfig.value.voice) {
    segmentPreview.value = {
      loading: false, audioUrl: '', previewId: '', sentenceCount: 0, warnings: [], error: '',
    }
    return
  }

  const mySeq = ++segmentPreviewRequestSeq
  segmentPreview.value.loading = true
  segmentPreview.value.error = ''
  try {
    const res = await fetch('/api/tts/synthesize_preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        language: formData.value.language,
        engine: ttsConfig.value.engine,
        voice: ttsConfig.value.voice,
        rate: `${ttsConfig.value.rateNum >= 0 ? '+' : ''}${ttsConfig.value.rateNum}%`,
        pitch: `${ttsConfig.value.pitchNum >= 0 ? '+' : ''}${ttsConfig.value.pitchNum}Hz`,
        volume: `${ttsConfig.value.volumeNum >= 0 ? '+' : ''}${ttsConfig.value.volumeNum}%`,
      }),
    })
    const data = await res.json()
    // 用户可能在等待响应期间又点了一次——这种情况下这次（更旧的）响应
    // 回来时应该被忽略，避免界面倒退回旧结果。
    if (mySeq !== segmentPreviewRequestSeq) return

    if (!data.success) {
      segmentPreview.value = {
        loading: false, audioUrl: '', previewId: '', sentenceCount: 0,
        warnings: [], error: data.error || t('processor.ttsSegmentPreviewFailed'),
      }
      return
    }

    if (segmentPreview.value.audioUrl) URL.revokeObjectURL(segmentPreview.value.audioUrl)
    const blob = await (await fetch(`data:audio/wav;base64,${data.audio_base64}`)).blob()
    segmentPreview.value = {
      loading: false,
      audioUrl: URL.createObjectURL(blob),
      previewId: data.preview_id || '',
      sentenceCount: data.sentence_count || 0,
      warnings: data.warnings || [],
      error: '',
    }
  } catch (e: any) {
    if (mySeq !== segmentPreviewRequestSeq) return
    segmentPreview.value = {
      loading: false, audioUrl: '', previewId: '', sentenceCount: 0,
      warnings: [], error: e?.message || String(e),
    }
  }
}

// 文本 / 引擎 / 音色 / 语速·音调·音量 / 语种任一变化都会让已生成的预览
// 音频与当前输入不再对应——清空 previewId 让"开始处理"退回完整流程，
// 而不是悄悄拿旧音频去对齐新文本。注意：这里只清空 previewId 状态，
// 不会自动重新生成预览（生成预览仍然只能靠用户手动点按钮触发）。
watch(
  () => [
    formData.value.text, formData.value.language, ttsConfig.value.engine, ttsConfig.value.voice,
    ttsConfig.value.rateNum, ttsConfig.value.pitchNum, ttsConfig.value.volumeNum,
  ],
  () => {
    if (inputMode.value === 'tts' && segmentPreview.value.previewId) {
      segmentPreview.value.previewId = ''
    }
  },
)

const openNarratorManager = () => {
  // 打开"语音预设管理"时，用主面板当前的引擎/音色/语速/音调/音量预填表单，
  // 方便用户把当前正在用的一套参数直接"另存为预设"，而不必再手动重设一遍。
  narratorForm.value = {
    id: '', name: '', engine: ttsConfig.value.engine, voice: ttsConfig.value.voice,
    rate: `${ttsConfig.value.rateNum >= 0 ? '+' : ''}${ttsConfig.value.rateNum}%`,
    pitch: `${ttsConfig.value.pitchNum >= 0 ? '+' : ''}${ttsConfig.value.pitchNum}Hz`,
    volume: `${ttsConfig.value.volumeNum >= 0 ? '+' : ''}${ttsConfig.value.volumeNum}%`,
  }
  narratorManagerVisible.value = true
  fetchNarratorFormVoices(narratorForm.value.engine || 'edge_tts')
}

const editNarrator = (n: TtsNarrator) => {
  narratorForm.value = { engine: 'edge_tts', ...n }
  fetchNarratorFormVoices(narratorForm.value.engine || 'edge_tts')
}

const resetNarratorForm = () => {
  narratorForm.value = { id: '', name: '', engine: ttsConfig.value.engine, voice: '', rate: '+0%', pitch: '+0Hz', volume: '+0%' }
  fetchNarratorFormVoices(narratorForm.value.engine || 'edge_tts')
}

const saveNarrator = async () => {
  if (!narratorForm.value.name.trim() || !narratorForm.value.voice) {
    ElMessage.warning(t('processor.narratorNameVoiceRequired'))
    return
  }
  narratorSaving.value = true
  try {
    const res = await fetch('/api/tts/narrators', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...narratorForm.value, language: formData.value.language }),
    })
    const data = await res.json()
    if (!data.success) throw new Error(data.error || t('processor.submitFailed'))
    await fetchNarrators()
    resetNarratorForm()
    ElMessage.success(`✅ ${t('processor.success')}`)
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  } finally {
    narratorSaving.value = false
  }
}

const deleteNarratorItem = async (narratorId: string) => {
  try {
    const res = await fetch(`/api/tts/narrators/${narratorId}`, { method: 'DELETE' })
    const data = await res.json()
    if (!data.success) throw new Error(data.error || t('processor.submitFailed'))
    if (ttsConfig.value.narratorId === narratorId) ttsConfig.value.narratorId = ''
    await fetchNarrators()
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  }
}

// 辅助工具函数
const midiNoteToName = (note: number): string => {
  const notes = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
  const octave = Math.floor(note / 12) - 1
  return `${notes[note % 12]}${octave}`
}

const formatFileSize = (bytes: number): string => {
  if (bytes === 0) return '0 Bytes'
  const k = 1024
  const sizes = ['Bytes', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i]
}

const formatTime = (ms: number): string => {
  const seconds = Math.floor(ms / 1000)
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)
  if (hours > 0) return `${hours}h ${minutes % 60}m ${seconds % 60}s`
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`
  return `${seconds}s`
}

const getFileName = (path: string): string => {
  return path.split(/[\\/]/).pop() || path
}

const clearJobPolling = () => {
  if (jobPollTimer !== null) {
    window.clearTimeout(jobPollTimer)
    jobPollTimer = null
  }
}

const resetProcessingSteps = () => {
  const stageLabel = alignerBackendLabel.value
  processingDetails.value = [
    { stage: `1. ${stageLabel}`, status: '等待', message: t('processor.stagePrepareAlign') },
    { stage: t('processor.stageF0'), status: '等待', message: t('processor.stageExtractF0') },
    { stage: t('processor.stageProject'), status: '等待', message: t('processor.stageGenerateProject') }
  ]
}

const updateProcessingStep = (index: number, status: string, message: string) => {
  if (!processingDetails.value[index]) return
  processingDetails.value[index] = { ...processingDetails.value[index], status, message }
}

const extractProjectPath = (payload: any): string => {
  return payload?.project_file || payload?.projectPath || payload?.project_path || payload?.output_path || payload?.svp_path || payload?.ustx_path || ''
}

const SIL_PHONES = new Set(['sp', 'spn', 'sil', 'silence', 'pau', 'breath', 'noise', 'ap', 'blank'])

// 统计 LAB 文件中的非静音标注段数
const countLabSegments = (labContent: string): number => {
  if (!labContent) return 0
  return labContent.trim().split('\n').filter(line => {
    const parts = line.trim().split(/\s+/)
    const phone = (parts[2] || '').toLowerCase()
    return phone && !SIL_PHONES.has(phone)
  }).length
}

const normalizeResult = (payload: any) => {
  const projectPath = extractProjectPath(payload)
  return {
    labContent: payload?.lab_content || payload?.labContent || '',
    processingTime: payload?.processing_time || payload?.processingTime || 0,
    labPath: payload?.lab_path || payload?.labPath || '',
    projectPath,
    projectFormat: payload?.project_format || payload?.projectFormat || formData.value.outputFormat,
    segments: payload?.segments || 0,
    whisperxModel: alignerBackend.value === 'whisperx' ? advancedConfig.value.whisperx_model : undefined,
    config: payload?.config || {
      bpm: advancedConfig.value.bpm,
      base_pitch: advancedConfig.value.base_pitch,
      auto_note_pitch: advancedConfig.value.auto_note_pitch,
      export_pitch_line: advancedConfig.value.export_pitch_line,
      f0_method: advancedConfig.value.f0_method,
      f0_device: advancedConfig.value.f0_device,
      aligner_device: advancedConfig.value.aligner_device,
      nemo_model: alignerBackend.value === 'nemo_aligner' ? advancedConfig.value.nemo_model : undefined,
      crepe_model: advancedConfig.value.crepe_model,
      use_double_precision: advancedConfig.value.precision === 'double'
    }
  }
}

// 异步任务轮询（通用版，不含模式相关的步骤更新）
const waitForJobFinished = (jobId: string): Promise<any> => {
  clearJobPolling()
  currentJobId.value = jobId

  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const res = await fetch(`/api/pipeline/job/${jobId}`)
        const data = await res.json()

        if (!res.ok || !data.success) {
          throw new Error(data.error || t('processor.jobStatusFailed'))
        }

        const job = data.job || {}

        if (job.status === 'done') {
          resolve(job.result || job)
          return
        } else if (job.status === 'failed') {
          throw new Error(job.error || t('processor.jobFailed'))
        }
        // queued / running: 继续轮询

        jobPollTimer = window.setTimeout(tick, 1500)
      } catch (e) {
        reject(e)
      }
    }
    tick()
  })
}

// 后端 API 交互
const checkSystemStatus = async () => {
  checkingStatus.value = true
  try {
    const [pipelineRes, alignerRes] = await Promise.all([
      fetch('/api/pipeline/status'),
      fetch('/api/aligner/status'),
    ])
    const pipelineData = await pipelineRes.json()
    if (pipelineData.success) {
      systemStatus.value = pipelineData.status
      // 同步 alt_aligners 到 alignerStatus（如果 pipeline/status 已经包含了）
      if (pipelineData.status?.alt_aligners) {
        alignerStatus.value = pipelineData.status.alt_aligners
      }
    }
    const alignerData = await alignerRes.json()
    if (alignerData.success && alignerData.backends) {
      const { mfa: _mfa, ...altBacks } = alignerData.backends
      alignerStatus.value = altBacks
    }
    // 【修复】把已经拿到的 systemStatus 直接通过事件传给父组件，
    // 而不是只发一个空事件让父组件自己再 fetch 一次 /api/pipeline/status。
    // 否则父组件（右上角"系统就绪"标签）和这里（底部"系统状态"面板）
    // 永远是两次独立的网络请求结果，天然就会有先后顺序差 + 偶尔不一致。
    emit('status-changed', systemStatus.value)
  } catch (e) {
    console.warn('无法检查系统状态:', e)
    ElMessage.warning(t('processor.backendConnectionFailed'))
  } finally {
    checkingStatus.value = false
  }
}

const refreshStatus = async () => {
  await checkSystemStatus()
  ElMessage.success(t('processor.backendRefreshSuccess'))
}

const openGitHub = () => {
  window.open('https://github.com/liuhua520-svg/SVS-Lab-Tools', '_blank')
}

const handleExceed = () => {
  ElMessage.error(t('processor.chooseOneUpload'))
}

const handleAudioSelect = (file: any) => {
  const raw: File | null = file?.raw || null
  if (!raw) return

  formData.value.audioFile = raw
  error.value = ''
}

/**
 * 从 MIDI 文件的二进制内容中解析第一个 set_tempo 事件，换算成 BPM。
 * 纯浏览器端解析，不需要额外库。
 * MIDI Tempo meta event 格式: 0xFF 0x51 0x03 <3-byte microseconds>
 */
const extractMidiBpm = (file: File): Promise<{ bpm: number }> => {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = (e) => {
      try {
        const buf = new Uint8Array(e.target!.result as ArrayBuffer)
        let bpm = 120.0
        for (let i = 0; i < buf.length - 5; i++) {
          if (buf[i] === 0xFF && buf[i + 1] === 0x51 && buf[i + 2] === 0x03) {
            const us = (buf[i + 3] << 16) | (buf[i + 4] << 8) | buf[i + 5]
            if (us > 0) bpm = Math.round((60_000_000 / us) * 10) / 10
            break
          }
        }
        resolve({ bpm })
      } catch {
        resolve({ bpm: 120 })
      }
    }
    reader.onerror = () => resolve({ bpm: 120 })
    reader.readAsArrayBuffer(file)
  })
}

const downloadModel = async (lang: string) => {
  downloadingLangs.value.push(lang)
  try {
    const res = await fetch(`/api/mfa/download-model/${lang}`, { method: 'POST' })
    const data = await res.json()
    if (data.success) {
      ElMessage.success(t('processor.modelDownloaded', { lang: lang.toUpperCase() }))
      await checkSystemStatus()
    } else {
      ElMessage.error(t('processor.modelDownloadFailed', { error: data.error }))
    }
  } catch (e) {
    ElMessage.error(t('processor.modelDownloadError', { error: String(e) }))
  } finally {
    downloadingLangs.value = downloadingLangs.value.filter(l => l !== lang)
  }
}

// 核心核心控制逻辑：开始处理
const processAudio = async () => {
  // ============================================================
  // 分支 0) TTS跟读：讲述人 + EdgeTTS，不需要用户上传音频
  // ============================================================
  if (inputMode.value === 'tts') {
    if (!formData.value.text.trim()) {
      ElMessage.warning(t('processor.ttsTextRequired'))
      return
    }
    if (!ttsConfig.value.voice) {
      ElMessage.warning(t('processor.ttsVoiceRequired'))
      return
    }
    if (!isReady.value) {
      ElMessage.error(t('processor.backendNotReady'))
      return
    }

    clearJobPolling()
    processing.value = true
    progressPercent.value = 0
    error.value = ''
    result.value = null
    currentJobId.value = ''
    resetProcessingSteps()
    updateProcessingStep(0, t('processor.statusProcessing'), t('processor.ttsSynthesizing'))
    updateProcessingStep(1, t('processor.statusWaiting'), t('processor.stageExtractF0'))
    updateProcessingStep(2, t('processor.statusWaiting'), t('processor.projectModeWaitProject'))

    let progressTimer: number | null = null

    try {
      const formDataObj = new FormData()
      formDataObj.append('text', formData.value.text)
      formDataObj.append('language', formData.value.language)
      formDataObj.append('engine', ttsConfig.value.engine)
      formDataObj.append('voice', ttsConfig.value.voice)
      formDataObj.append('rate', `${ttsConfig.value.rateNum >= 0 ? '+' : ''}${ttsConfig.value.rateNum}%`)
      formDataObj.append('pitch', `${ttsConfig.value.pitchNum >= 0 ? '+' : ''}${ttsConfig.value.pitchNum}Hz`)
      formDataObj.append('volume', `${ttsConfig.value.volumeNum >= 0 ? '+' : ''}${ttsConfig.value.volumeNum}%`)
      formDataObj.append('aligner_device', advancedConfig.value.aligner_device)
      formDataObj.append('align_pitch_shift_semitones', advancedConfig.value.align_pitch_shift_semitones.toString())
      formDataObj.append('english_word_align', (englishWordAlign.value && formData.value.language !== 'jpn').toString())
      formDataObj.append('processing_mode', processingMode.value === 'full' ? 'full' : 'mfa-only')
      // 如果之前手动点过"生成预览"且文本/参数之后未再变化，previewId 仍然
      // 有效——带给后端复用已经合成好的分句音频，跳过重新合成直接对齐；
      // 否则这里是空字符串，后端会走"先合成再对齐"的完整流程。
      formDataObj.append('preview_id', segmentPreview.value.previewId)

      if (processingMode.value === 'full') {
        formDataObj.append('format', formData.value.outputFormat)
        if (formData.value.outputFormat === 'vsqx') {
          formDataObj.append('vsqx_singer',    vsqxSingerConfig.value.name)
          formDataObj.append('vsqx_singer_id', vsqxSingerConfig.value.id)
          formDataObj.append('vsqx_pitch_smooth_window', advancedConfig.value.vsqx_pitch_smooth_window.toString())
        }
        formDataObj.append('title', formData.value.projectTitle)
        formDataObj.append('bpm', advancedConfig.value.bpm.toString())
        formDataObj.append('base_pitch', advancedConfig.value.base_pitch.toString())
        formDataObj.append('f0_method', advancedConfig.value.f0_method)
        formDataObj.append('f0_device', advancedConfig.value.f0_device)
        formDataObj.append('crepe_model', advancedConfig.value.crepe_model)
        formDataObj.append('f0_smooth', advancedConfig.value.f0_smooth.toString())
        formDataObj.append('f0_smooth_window', advancedConfig.value.f0_smooth_window.toString())
        formDataObj.append('precision', advancedConfig.value.precision)
        formDataObj.append('f0_floor', advancedConfig.value.f0_floor.toString())
        formDataObj.append('f0_ceil', advancedConfig.value.f0_ceil.toString())
        formDataObj.append('auto_note_pitch', advancedConfig.value.auto_note_pitch.toString())
        formDataObj.append('export_pitch_line', advancedConfig.value.export_pitch_line.toString())
        formDataObj.append('word_phoneme_map', wordPhonemeMapEffective.value.toString())
        formDataObj.append('dict_source', dictSource.value)
      }

      progressTimer = window.setInterval(() => {
        if (progressPercent.value < 30) progressPercent.value += 3
      }, 400)

      const res = await fetch('/api/tts/process', { method: 'POST', body: formDataObj })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || t('processor.submitFailed'))

      // 后端一旦读取 preview_id 就会立刻把这条缓存记录消费掉（无论最终
      // 对齐是否成功），所以这里的 previewId 已经不能再被复用——清空它，
      // 避免用户不改文本、直接再点一次"开始处理"时，前端还误以为存在
      // 一份可复用的预览音频。
      if (segmentPreview.value.previewId) segmentPreview.value.previewId = ''

      if (progressTimer !== null) { window.clearInterval(progressTimer); progressTimer = null }
      progressPercent.value = 35

      const finalPayload = await waitForJobFinished(data.job_id)
      const normalized = normalizeResult(finalPayload)

      if (processingMode.value === 'full') {
        if (!normalized.projectPath) throw new Error(t('processor.projectMissing'))
        updateProcessingStep(0, t('processor.statusDone'), t('processor.ttsSynthesizeDone'))
        updateProcessingStep(1, t('processor.statusDone'), t('processor.projectModeF0Done'))
        updateProcessingStep(2, t('processor.statusDone'), `${t('processor.projectFile')}: ${getFileName(normalized.projectPath)}`)
      } else {
        if (!normalized.labContent) throw new Error(t('processor.labEmpty'))
        const segCount = countLabSegments(normalized.labContent)
        updateProcessingStep(0, t('processor.statusDone'), `${segCount} ${t('processor.segmentCount')}`)
        updateProcessingStep(1, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
        updateProcessingStep(2, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
      }

      result.value = normalized
      progressPercent.value = 100
      ElMessage.success(`✅ ${t('processor.success')}`)
    } catch (e: any) {
      error.value = e?.message || String(e)
      ElMessage.error(`❌ ${error.value}`)
    } finally {
      if (progressTimer !== null) window.clearInterval(progressTimer)
      clearJobPolling()
      processing.value = false
    }
    return
  }

  // ============================================================
  // 分支 0.5) 字幕跟读：完整音频 + SRT/LRC 字幕文件，按字幕时间轴切分
  // 后固定用 Qwen3-ForcedAligner 逐句强制对齐，拼接成覆盖整段音频的 LAB。
  // ============================================================
  if (inputMode.value === 'subtitle') {
    if (!subtitleImport.value.audioFile) {
      ElMessage.warning(t('processor.selectAudio'))
      return
    }
    if (!subtitleImport.value.subtitleFile) {
      ElMessage.warning(t('processor.selectSubtitleFile'))
      return
    }
    if (!isReady.value) {
      ElMessage.error(t('processor.backendNotReady'))
      return
    }

    clearJobPolling()
    processing.value = true
    progressPercent.value = 0
    error.value = ''
    result.value = null
    currentJobId.value = ''
    resetProcessingSteps()
    updateProcessingStep(0, t('processor.statusProcessing'), `${alignerBackendLabel.value} ${t('processor.processing')}...`)
    updateProcessingStep(1, t('processor.statusWaiting'), t('processor.stageExtractF0'))
    updateProcessingStep(2, t('processor.statusWaiting'), t('processor.projectModeWaitProject'))

    let progressTimer: number | null = null

    try {
      const formDataObj = new FormData()
      formDataObj.append('audio_file', subtitleImport.value.audioFile)
      formDataObj.append('subtitle_file', subtitleImport.value.subtitleFile)
      formDataObj.append('language', formData.value.language)
      formDataObj.append('aligner_device', advancedConfig.value.aligner_device)
      formDataObj.append('align_pitch_shift_semitones', advancedConfig.value.align_pitch_shift_semitones.toString())
      formDataObj.append('english_word_align', (englishWordAlign.value && formData.value.language !== 'jpn').toString())
      formDataObj.append('processing_mode', processingMode.value === 'full' ? 'full' : 'mfa-only')

      if (processingMode.value === 'full') {
        formDataObj.append('format', formData.value.outputFormat)
        if (formData.value.outputFormat === 'vsqx') {
          formDataObj.append('vsqx_singer',    vsqxSingerConfig.value.name)
          formDataObj.append('vsqx_singer_id', vsqxSingerConfig.value.id)
          formDataObj.append('vsqx_pitch_smooth_window', advancedConfig.value.vsqx_pitch_smooth_window.toString())
        }
        formDataObj.append('title', formData.value.projectTitle)
        formDataObj.append('bpm', advancedConfig.value.bpm.toString())
        formDataObj.append('base_pitch', advancedConfig.value.base_pitch.toString())
        formDataObj.append('f0_method', advancedConfig.value.f0_method)
        formDataObj.append('f0_device', advancedConfig.value.f0_device)
        formDataObj.append('crepe_model', advancedConfig.value.crepe_model)
        formDataObj.append('f0_smooth', advancedConfig.value.f0_smooth.toString())
        formDataObj.append('f0_smooth_window', advancedConfig.value.f0_smooth_window.toString())
        formDataObj.append('precision', advancedConfig.value.precision)
        formDataObj.append('f0_floor', advancedConfig.value.f0_floor.toString())
        formDataObj.append('f0_ceil', advancedConfig.value.f0_ceil.toString())
        formDataObj.append('auto_note_pitch', advancedConfig.value.auto_note_pitch.toString())
        formDataObj.append('export_pitch_line', advancedConfig.value.export_pitch_line.toString())
        formDataObj.append('word_phoneme_map', wordPhonemeMapEffective.value.toString())
        formDataObj.append('dict_source', dictSource.value)
      }

      progressTimer = window.setInterval(() => {
        if (progressPercent.value < 30) progressPercent.value += 3
      }, 400)

      const res = await fetch('/api/subtitle-import/align', { method: 'POST', body: formDataObj })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || t('processor.submitFailed'))

      if (progressTimer !== null) { window.clearInterval(progressTimer); progressTimer = null }
      progressPercent.value = 35

      const finalPayload = await waitForJobFinished(data.job_id)
      const normalized = normalizeResult(finalPayload)

      if (processingMode.value === 'full') {
        if (!normalized.projectPath) throw new Error(t('processor.projectMissing'))
        updateProcessingStep(0, t('processor.statusDone'), `${alignerBackendLabel.value} ${t('processor.statusDone')}`)
        updateProcessingStep(1, t('processor.statusDone'), t('processor.projectModeF0Done'))
        updateProcessingStep(2, t('processor.statusDone'), `${t('processor.projectFile')}: ${getFileName(normalized.projectPath)}`)
      } else {
        if (!normalized.labContent) throw new Error(t('processor.labEmpty'))
        const segCount = countLabSegments(normalized.labContent)
        updateProcessingStep(0, t('processor.statusDone'), `${segCount} ${t('processor.segmentCount')}`)
        updateProcessingStep(1, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
        updateProcessingStep(2, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
      }

      result.value = normalized
      progressPercent.value = 100
      ElMessage.success(`✅ ${t('processor.success')}`)
    } catch (e: any) {
      error.value = e?.message || String(e)
      ElMessage.error(`❌ ${error.value}`)
    } finally {
      if (progressTimer !== null) window.clearInterval(progressTimer)
      clearJobPolling()
      processing.value = false
    }
    return
  }

  // ============================================================
  // 分支 1) 仅工程文件模式：WAV + LAB -> 直接转工程文件
  // ============================================================
if (processingMode.value === 'project-only') {
  if (!formData.value.audioFile) {
    ElMessage.warning(t('processor.selectWav'))
    return
  }

  const notationFile = selectedNotationFile.value
  if (!notationFile) {
    ElMessage.warning(t('processor.selectLabOrMidi'))
    return
  }

  const notationExt = notationFile.name.toLowerCase().split('.').pop() || ''
  if (!['lab', 'mid', 'midi'].includes(notationExt)) {
    ElMessage.warning(t('processor.selectValidNotation'))
    return
  }

  clearJobPolling()
  processing.value = true
  progressPercent.value = 0
  error.value = ''
  result.value = null
  currentJobId.value = ''
  resetProcessingSteps()
  updateProcessingStep(0, t('processor.statusSkipped'), t('processor.projectModeSkipAlign'))
  updateProcessingStep(1, t('processor.statusProcessing'), t('processor.projectModeProcessing'))
  updateProcessingStep(2, t('processor.statusWaiting'), t('processor.projectModeWaitProject'))

  let progressTimer: number | null = null

  try {
    const formDataObj = new FormData()
    formDataObj.append('wav_file', formData.value.audioFile)
    formDataObj.append('format', formData.value.outputFormat)
    if (formData.value.outputFormat === 'vsqx') {
      formDataObj.append('vsqx_singer',    vsqxSingerConfig.value.name)
      formDataObj.append('vsqx_singer_id', vsqxSingerConfig.value.id)
      formDataObj.append('vsqx_pitch_smooth_window', advancedConfig.value.vsqx_pitch_smooth_window.toString())
    }
    formDataObj.append('title', formData.value.projectTitle)
    formDataObj.append('phoneme_mode', formData.value.phonemeMode)
    formDataObj.append('bpm', advancedConfig.value.bpm.toString())
    formDataObj.append('base_pitch', advancedConfig.value.base_pitch.toString())
    formDataObj.append('f0_method', advancedConfig.value.f0_method)
    formDataObj.append('f0_device', advancedConfig.value.f0_device)
    formDataObj.append('crepe_model', advancedConfig.value.crepe_model)
    formDataObj.append('f0_smooth', advancedConfig.value.f0_smooth.toString())
    formDataObj.append('f0_smooth_window', advancedConfig.value.f0_smooth_window.toString())
    formDataObj.append('precision', advancedConfig.value.precision)
    formDataObj.append('f0_floor', advancedConfig.value.f0_floor.toString())
    formDataObj.append('f0_ceil', advancedConfig.value.f0_ceil.toString())
    formDataObj.append('auto_note_pitch', advancedConfig.value.auto_note_pitch.toString())
    formDataObj.append('export_pitch_line', advancedConfig.value.export_pitch_line.toString())
    // word_phoneme_map（英语单词→音素映射）是"自动可见性条件 + 用户手动
    // 开关"的组合值（见 wordPhonemeMapEffective）：控件不可见时恒为
    // false，可见时由用户决定是否开启。
    // dict_source（选择词典）与上面完全独立提交：project-only 本身就在
    // showDictSource 的可见范围内，因此始终随表单一起提交；为 "default"
    // 时后端本就只走软件默认转换，不会产生额外副作用。
    formDataObj.append('word_phoneme_map', wordPhonemeMapEffective.value.toString())
    formDataObj.append('dict_source', dictSource.value)

    // 只传一个标注文件：LAB 或 MIDI 二选一
    if (notationExt === 'lab') {
      formDataObj.append('lab_file', notationFile)
    } else {
      formDataObj.append('midi_file', notationFile)
    }

    progressTimer = window.setInterval(() => {
      if (progressPercent.value < 30) progressPercent.value += 3
    }, 400)

    const res = await fetch('/api/pipeline/project-only', {
      method: 'POST',
      body: formDataObj,
    })
    const data = await res.json()

    if (!res.ok) throw new Error(data.error || t('processor.submitFailed'))

    if (data.job_id) {
      if (progressTimer !== null) { window.clearInterval(progressTimer); progressTimer = null }
      progressPercent.value = 35

      const finalPayload = await waitForJobFinished(data.job_id)
      const normalized = normalizeResult(finalPayload)

      if (!normalized.projectPath) throw new Error(t('processor.projectMissing'))

      result.value = normalized
      progressPercent.value = 100
      updateProcessingStep(0, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
      updateProcessingStep(1, t('processor.statusDone'), t('processor.projectModeF0Done'))
      updateProcessingStep(2, t('processor.statusDone'), `${t('processor.projectFile')}: ${getFileName(normalized.projectPath)}`)
      ElMessage.success(`✅ ${t('processor.projectModeSuccess')}`)
      return
    }

    if (!data.success) throw new Error(data.error || t('processor.submitFailed'))
    const normalized = normalizeResult(data)
    result.value = normalized
    progressPercent.value = 100
    updateProcessingStep(0, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
    updateProcessingStep(1, t('processor.statusDone'), t('processor.projectModeF0Done'))
    updateProcessingStep(2, t('processor.statusDone'), `${t('processor.projectFile')}: ${getFileName(normalized.projectPath || '')}`)
    ElMessage.success(`✅ ${t('processor.projectModeSuccess')}`)
  } catch (e: any) {
    error.value = e?.message || String(e)
    ElMessage.error(`❌ ${error.value}`)
  } finally {
    if (progressTimer !== null) window.clearInterval(progressTimer)
    clearJobPolling()
    processing.value = false
  }
  return
}

  // ============================================================
  // 分支 2) 其他传统模式：需要音频，非 ASR 后端需要文本
  // ============================================================
  if (!formData.value.audioFile) {
    ElMessage.warning(t('processor.selectAudio'))
    return
  }
  if (!formData.value.text.trim() && !isTextOptional.value) {
    ElMessage.warning(t('processor.selectText'))
    return
  }
  if (!isReady.value) {
    ElMessage.error(t('processor.backendNotReady'))
    return
  }

  const maxSize = 512 * 1024 * 1024
  if (formData.value.audioFile.size > maxSize) {
    ElMessage.warning(t('processor.fileTooLarge'))
  }

  clearJobPolling()
  processing.value = true
  progressPercent.value = 0
  error.value = ''
  result.value = null
  currentJobId.value = ''
  resetProcessingSteps()

  let progressTimer: number | null = null

  try {
    const formDataObj = new FormData()
    formDataObj.append('audio_file', formData.value.audioFile)
    formDataObj.append('text', formData.value.text)
    formDataObj.append('language', formData.value.language)
    formDataObj.append('aligner_backend', alignerBackend.value)
    formDataObj.append('aligner_device', advancedConfig.value.aligner_device)
    formDataObj.append('align_pitch_shift_semitones', advancedConfig.value.align_pitch_shift_semitones.toString())
    formDataObj.append('whisperx_model', advancedConfig.value.whisperx_model)
    formDataObj.append('whisperx_batch_size', advancedConfig.value.whisperx_batch_size.toString())
    formDataObj.append('nemo_model', advancedConfig.value.nemo_model || '')
    formDataObj.append('english_word_align', (englishWordAlign.value && formData.value.language !== 'jpn').toString())

    if (processingMode.value === 'full') {
      formDataObj.append('format', formData.value.outputFormat)
      if (formData.value.outputFormat === 'vsqx') {
        formDataObj.append('vsqx_singer',    vsqxSingerConfig.value.name)
        formDataObj.append('vsqx_singer_id', vsqxSingerConfig.value.id)
        formDataObj.append('vsqx_pitch_smooth_window', advancedConfig.value.vsqx_pitch_smooth_window.toString())
      }
      formDataObj.append('title', formData.value.projectTitle)
      formDataObj.append('bpm', advancedConfig.value.bpm.toString())
      formDataObj.append('base_pitch', advancedConfig.value.base_pitch.toString())
      formDataObj.append('f0_method', advancedConfig.value.f0_method)
      formDataObj.append('f0_device', advancedConfig.value.f0_device)
      formDataObj.append('crepe_model', advancedConfig.value.crepe_model)
      formDataObj.append('f0_smooth', advancedConfig.value.f0_smooth.toString())
      formDataObj.append('f0_smooth_window', advancedConfig.value.f0_smooth_window.toString())
      formDataObj.append('precision', advancedConfig.value.precision)
      formDataObj.append('f0_floor', advancedConfig.value.f0_floor.toString())
      formDataObj.append('f0_ceil', advancedConfig.value.f0_ceil.toString())
      formDataObj.append('auto_note_pitch', advancedConfig.value.auto_note_pitch.toString())
      formDataObj.append('export_pitch_line', advancedConfig.value.export_pitch_line.toString())
      // 同上：word_phoneme_map（英语单词→音素映射）为 wordPhonemeMapEffective
      // （自动可见性 AND 用户手动开关），后端会在其为 true 时自动补齐所需的
      // 整词级对齐前提。dict_source（选择词典）与其解耦、独立提交——此分支
      // 本就在 processingMode === 'full' 内，属于 showDictSource 的可见范围。
      formDataObj.append('word_phoneme_map', wordPhonemeMapEffective.value.toString())
      formDataObj.append('dict_source', dictSource.value)
    }

    progressTimer = window.setInterval(() => {
      if (progressPercent.value < 30) progressPercent.value += 3
    }, 400)

    const endpoint = processingMode.value === 'full' ? '/api/pipeline/full' : '/api/pipeline/mfa-only'
    const res = await fetch(endpoint, { method: 'POST', body: formDataObj })
    const data = await res.json()

    if (!res.ok) throw new Error(data.error || t('processor.submitFailed'))

    // full 和 mfa-only 均走异步轮询（后端返回 job_id）
    if (data.job_id) {
      if (progressTimer !== null) { window.clearInterval(progressTimer); progressTimer = null }
      progressPercent.value = 35

      if (processingMode.value === 'mfa-only') {
        updateProcessingStep(0, t('processor.statusProcessing'), `${alignerBackendLabel.value} ${t('processor.processing')}...`)
        updateProcessingStep(1, t('processor.statusWaiting'), t('processor.projectModeNoAlign'))
        updateProcessingStep(2, t('processor.statusWaiting'), t('processor.projectModeNoAlign'))
      } else {
        updateProcessingStep(0, t('processor.statusProcessing'), `${alignerBackendLabel.value} ${t('processor.projectModeProcessing')}`)
        updateProcessingStep(1, t('processor.statusWaiting'), t('processor.stageExtractF0'))
        updateProcessingStep(2, t('processor.statusWaiting'), t('processor.projectModeWaitProject'))
      }

      const finalPayload = await waitForJobFinished(data.job_id)
      const normalized = normalizeResult(finalPayload)

      if (processingMode.value === 'full') {
        if (!normalized.projectPath) throw new Error(t('processor.projectMissing'))
        updateProcessingStep(0, t('processor.statusDone'), `${t('processor.backendMfa')} ${t('processor.statusDone')}`)
        updateProcessingStep(1, t('processor.statusDone'), t('processor.projectModeF0Done'))
        updateProcessingStep(2, t('processor.statusDone'), `${t('processor.projectFile')}: ${getFileName(normalized.projectPath)}`)
      } else {
        // mfa-only
        if (!normalized.labContent) throw new Error(t('processor.labEmpty'))
        const segCount = countLabSegments(normalized.labContent)
        updateProcessingStep(0, t('processor.statusDone'), `${segCount} ${t('processor.segmentCount')}`)
        updateProcessingStep(1, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
        updateProcessingStep(2, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
      }

      result.value = normalized
      progressPercent.value = 100
      ElMessage.success(`✅ ${t('processor.success')}`)
      return
    }

    // 向下兼容：full 模式同步结果回退
    if (processingMode.value === 'full') {
      const normalized = normalizeResult(data)
      if (data.success && normalized.projectPath) {
        updateProcessingStep(0, t('processor.statusDone'), `${t('processor.backendMfa')} ${t('processor.statusDone')}`)
        updateProcessingStep(1, t('processor.statusDone'), t('processor.projectModeF0Done'))
        updateProcessingStep(2, t('processor.statusDone'), `${t('processor.projectFile')}: ${getFileName(normalized.projectPath)}`)
        result.value = normalized
        progressPercent.value = 100
        ElMessage.success(`✅ ${t('processor.success')}`)
        return
      }
      throw new Error(data.error || t('processor.projectMissing'))
    }

    // mfa-only 同步回退（后端已异步，此分支仅做兼容保留）
    if (!data.success) throw new Error(data.error || t('processor.jobFailed'))
    const normalized = normalizeResult(data)
    if (!normalized.labContent) throw new Error(t('processor.labEmpty'))
    const segCount = countLabSegments(normalized.labContent)
    result.value = normalized
    progressPercent.value = 100
    updateProcessingStep(0, t('processor.statusDone'), `${segCount} ${t('processor.segmentCount')}`)
    updateProcessingStep(1, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
    updateProcessingStep(2, t('processor.statusSkipped'), t('processor.projectModeNoAlign'))
    ElMessage.success(`✅ ${t('processor.success')}`)
  } catch (e: any) {
    error.value = e?.message || String(e)
    ElMessage.error(`❌ ${error.value}`)
  } finally {
    if (progressTimer !== null) window.clearInterval(progressTimer)
    clearJobPolling()
    processing.value = false
  }
}

const downloadLab = () => {
  if (!result.value?.labContent) {
    ElMessage.warning(t('processor.noLabContent'))
    return
  }

  // 智能获取与工程文件一致的 stem（包含随机后缀）
  let stem = 'alignment'

  if (result.value.projectPath) {
    const projName = getFileName(result.value.projectPath)
    stem = projName.replace(/\.(svp|ustx|sv|vsqx)$/, '')   // 去掉扩展名
  } else if (result.value.labPath) {
    const labName = getFileName(result.value.labPath)
    stem = labName.replace(/\.lab$/, '')
  } else if (formData.value.audioFile) {
    stem = formData.value.audioFile.name.replace(/\.\w+$/, '')
  }

  const filename = `${stem}.lab`

  const element = document.createElement('a')
  element.setAttribute('href', 'data:text/plain;charset=utf-8,' + encodeURIComponent(result.value.labContent))
  element.setAttribute('download', filename)
  document.body.appendChild(element)
  element.click()
  document.body.removeChild(element)

  ElMessage.success(`✅ ${t('processor.downloadLabFile')}: ${filename}`)
}

const downloadProject = async () => {
  if (!result.value?.projectPath) return
  downloadingProject.value = true
  try {
    const filename = result.value.projectPath.split(/[\\/]/).pop()
    const response = await fetch(`/api/work-dir/download/${encodeURIComponent(filename)}`)
    if (!response.ok) {
      ElMessage.error(t('processor.submitFailed'))
      return
    }
    const blob = await response.blob()
    const url = window.URL.createObjectURL(blob)
    const element = document.createElement('a')
    element.href = url
    element.download = filename
    document.body.appendChild(element)
    element.click()
    document.body.removeChild(element)
    window.URL.revokeObjectURL(url)
    ElMessage.success(t('processor.downloadProjectFile'))
  } catch (e) {
    ElMessage.error(`${t('processor.submitFailed')}: ${e}`)
  } finally {
    downloadingProject.value = false
  }
}

const copyLabToClipboard = () => {
  if (!result.value?.labContent) return
  navigator.clipboard.writeText(result.value.labContent).then(() => {
    ElMessage.success(t('processor.copied'))
  }).catch(() => {
    ElMessage.error(t('processor.copyFailed'))
  })
}

const reset = () => {
  clearJobPolling()
  formData.value = {
    audioFile: null,
    labFile: null,
    midiFile: null,
    text: '',
    language: 'cmn',
    outputFormat: 'sv',
    projectTitle: t('processor.defaultProjectTitle'),
    phonemeMode: 'none'
  }
  midiInfo.value = { bpm: 120, loaded: false }
  labMidiUploadKey.value += 1
  audioUploadKey.value += 1
  result.value = null
  error.value = ''
  progressPercent.value = 0
  currentJobId.value = ''
  resetProcessingSteps()
  // alignerBackend 保留用户选择，不重置
}

const newProcess = () => {
  reset()
}
</script>

<style scoped>
/* 样式部分保持不变 */
.processor-container {
  width: 100%;
}

.dict-source-hint {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}

.processor-card {
  background: white;
  border-radius: 8px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
  margin-bottom: 20px;
}

.processor-form :deep(.el-form-item) {
  margin-bottom: 20px;
}

.processor-form :deep(.el-form-item__label) {
  white-space: normal;
  line-height: 1.35;
  padding-bottom: 8px;
}

.processor-form :deep(.el-form-item__content) {
  width: 100%;
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

.header-actions {
  display: flex;
  gap: 15px;
  align-items: center;
}

.file-info {
  margin-top: 10px;
  padding: 8px 12px;
  background: #ecf5ff;
  color: #409eff;
  border-radius: 4px;
  font-size: 12px;
}

.help-text {
  color: #909399;
  font-size: 12px;
  margin-top: 5px;
}

.help-text .text-optional-hint {
  color: #67c23a;
  font-weight: 500;
}

/* 确保模式帮助文本在 Element 表单条目中强制换行，不向右侧外溢 */
.mode-help {
  width: 100%;
  display: block;
  color: #909399;
  font-size: 12px;
  margin-top: 6px;
}

.option-hint {
  margin-left: 10px;
  color: #909399;
  font-size: 12px;
  line-height: 1.5;
}

.option-hint code {
  background: #f0f0f0;
  border-radius: 3px;
  padding: 0 3px;
  font-family: monospace;
  color: #476582;
}

.text-optimize-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.pitch-input-group {
  display: flex;
  gap: 10px;
  align-items: center;
}

.pitch-name {
  color: #409eff;
  font-weight: bold;
  font-size: 14px;
  min-width: 50px;
}

/* MIDI 相关样式 */
.midi-loaded {
  background: #f0f9eb !important;
  color: #67c23a !important;
  border: 1px solid #c2e7b0;
}

.midi-bpm-tag {
  display: inline-block;
  margin-left: 10px;
  padding: 1px 8px;
  background: #67c23a;
  color: white;
  border-radius: 10px;
  font-size: 11px;
  font-weight: bold;
  letter-spacing: 0.5px;
}

.midi-lock-tip {
  color: #909399;
  font-size: 11px;
  margin-left: 8px;
}

.icon-tip {
  margin-left: 5px;
  color: #909399;
}

.settings-info {
  margin-top: 15px;
}

.settings-info p {
  margin: 8px 0;
  font-size: 12px;
}

.settings-info strong {
  color: #333;
}

.progress-bar {
  margin-top: 15px;
}

.disabled-text {
  color: #f56c6c;
  font-size: 12px;
  margin-left: 10px;
}

.result-section {
  margin-top: 30px;
  padding-top: 20px;
}

.result-info {
  margin-bottom: 15px;
  padding: 15px;
  background: #f0f9ff;
  border-radius: 4px;
  border-left: 4px solid #409eff;
}

.result-info p {
  margin: 8px 0;
  color: #606266;
  font-size: 12px;
}

.result-info code {
  background: #fff;
  padding: 2px 6px;
  border-radius: 2px;
  font-family: 'Courier New', monospace;
}

.output-text {
  font-family: 'Courier New', monospace;
  font-size: 11px;
  margin: 15px 0;
}

.tab-actions {
  margin-top: 10px;
  display: flex;
  gap: 10px;
}

.file-info-box {
  padding: 15px;
  background: #f5f5f5;
  border-radius: 4px;
}

.file-info-box p {
  margin: 12px 0 6px;
  font-weight: bold;
  color: #333;
}

.file-info-box code {
  display: block;
  background: white;
  padding: 8px;
  border-radius: 4px;
  margin: 0 0 12px;
  word-break: break-all;
  font-family: 'Courier New', monospace;
  font-size: 11px;
  border: 1px solid #dcdfe6;
}

.file-info-box ul {
  margin: 8px 0 0 20px;
  font-size: 12px;
  color: #606266;
}

.file-info-box li {
  margin: 4px 0;
}

.details-box {
  padding: 15px 0;
}

.action-buttons {
  display: flex;
  gap: 10px;
  margin-top: 20px;
  flex-wrap: wrap;
}

.action-buttons :deep(.el-button) {
  flex: 1;
  min-width: 150px;
}

.error-section {
  margin-top: 20px;
}

.status-box {
  background: white;
  border-radius: 8px;
  padding: 20px;
  margin-top: 20px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
}

.status-item {
  margin-bottom: 15px;
}

.status-item .label {
  display: block;
  color: #606266;
  font-weight: bold;
  margin-bottom: 5px;
}

.model-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.model-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px;
  background: #f5f5f5;
  border-radius: 4px;
}

.warning-box {
  background: white;
  border-radius: 8px;
  padding: 20px;
  margin-top: 20px;
  box-shadow: 0 2px 12px rgba(255, 177, 0, 0.2);
}

.warning-box code {
  background: #f5f5f5;
  padding: 8px 12px;
  border-radius: 4px;
  display: block;
  margin: 10px 0;
  font-family: 'Courier New', monospace;
  color: #d63200;
  font-size: 12px;
}

@media (max-width: 768px) {
  .card-header {
    flex-direction: column;
    gap: 15px;
  }

  .action-buttons {
    flex-direction: column;
  }

  .action-buttons :deep(.el-button) {
    width: 100%;
  }

  .pitch-input-group {
    flex-direction: column;
  }
}
</style>
