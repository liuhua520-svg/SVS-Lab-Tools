<template>
  <div class="dialogue-container">
    <el-card class="dialogue-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-title">💬 {{ t('dialogue.pageTitle') }}</span>
          <el-tooltip :content="t('processor.checkStatus')" placement="bottom">
            <el-button link @click="refreshStatus" :loading="checkingStatus">
              🔄 {{ t('processor.checkStatus') }}
            </el-button>
          </el-tooltip>
        </div>
      </template>

      <p class="page-subtitle">{{ t('dialogue.pageSubtitle') }}</p>

      <!-- 输入模式：TTS跟读（讲述人 + EdgeTTS）/ 音频跟读（原有上传音频对齐流程） -->
      <el-form label-position="top" class="shared-form">
        <el-form-item :label="t('processor.inputModeLabel')">
          <el-radio-group v-model="inputMode" :disabled="processing">
            <el-radio value="tts">{{ t('processor.inputModeTts') }}</el-radio>
            <el-radio value="audio">{{ t('processor.inputModeAudio') }}</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>

      <!-- ============== 共用高级设置（与单文件处理页面语义一致） ============== -->
      <el-form label-position="top" class="shared-form">
        <!-- 以下几项（对齐后端 / 对齐运行设备 / NeMo 模型覆盖 / 语言）仅在
             "完整处理"模式下生效——该模式需要执行对齐；"仅生成工程"模式
             跳过对齐，直接使用已提供的 LAB / MIDI，故隐藏这些设置。
             TTS跟读模式固定使用 Qwen3-ForcedAligner，不显示后端选择器。 -->
        <el-form-item v-if="inputMode === 'audio' && processingMode === 'full'" :label="t('processor.backendLabel')">
          <el-radio-group v-model="alignerBackend" :disabled="processing">
            <el-radio value="mfa">
              <span>{{ t('processor.backendMfa') }}</span>
              <el-tag :type="alignerStatus.mfa?.available ? 'success' : 'danger'" size="small" style="margin-left:4px">
                {{ alignerStatus.mfa?.available ? '✓' : '✗' }}
              </el-tag>
            </el-radio>
            <el-radio value="whisperx">
              <span>{{ t('processor.backendWhisperx') }}</span>
              <el-tag :type="alignerStatus.whisperx?.available ? 'success' : 'info'" size="small" style="margin-left:4px">
                {{ alignerStatus.whisperx?.available ? '✓' : t('processor.backendStatusNeedInstall') }}
              </el-tag>
            </el-radio>
            <el-radio value="qwen3_asr">
              <span>{{ t('processor.backendQwen3Asr') }}</span>
              <el-tag :type="alignerStatus.qwen3_asr?.available ? 'success' : 'info'" size="small" style="margin-left:4px">
                {{ alignerStatus.qwen3_asr?.available ? '✓' : t('processor.backendStatusNeedInstall') }}
              </el-tag>
            </el-radio>
            <el-radio value="qwen3_aligner">
              <span>{{ t('processor.backendQwen3Aligner') }}</span>
              <el-tag :type="alignerStatus.qwen3_aligner?.available ? 'success' : 'info'" size="small" style="margin-left:4px">
                {{ alignerStatus.qwen3_aligner?.available ? '✓' : t('processor.backendStatusNeedInstall') }}
              </el-tag>
            </el-radio>
            <el-radio value="nemo_aligner">
              <span>{{ t('processor.backendNemoAligner') }}</span>
              <el-tag :type="alignerStatus.nemo_aligner?.available ? 'success' : 'info'" size="small" style="margin-left:4px">
                {{ alignerStatus.nemo_aligner?.available ? '✓' : t('processor.backendStatusNeedInstall') }}
              </el-tag>
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="inputMode === 'tts' || (processingMode === 'full' && alignerBackend !== 'mfa')" :label="t('processor.alignDevice')">
          <el-radio-group v-model="advanced.aligner_device" :disabled="processing">
            <el-radio value="auto">{{ t('processor.deviceAuto') }}</el-radio>
            <el-radio value="cpu">{{ t('processor.deviceCpu') }}</el-radio>
            <el-radio value="cuda">{{ t('processor.deviceCuda') }}</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="inputMode === 'tts'">
          <el-alert :closable="false" type="info" :title="t('processor.ttsFixedBackendHint')" show-icon />
        </el-form-item>

        <el-form-item v-if="inputMode === 'tts'" :label="t('processor.manageNarrators')">
          <el-button :disabled="processing" @click="openNarratorManager">⚙ {{ t('processor.manageNarrators') }}</el-button>
        </el-form-item>

        <el-form-item v-if="processingMode === 'full' && alignerBackend === 'whisperx'" :label="t('processor.whisperModel')">
          <el-select v-model="advanced.whisperx_model" :disabled="processing" style="width: 240px">
            <el-option value="large-v3" :label="t('processor.whisperModelLargeV3')" />
            <el-option value="large-v3-turbo" :label="t('processor.whisperModelLargeV3Turbo')" />
            <el-option value="large-v2" :label="t('processor.whisperModelLargeV2')" />
            <el-option value="medium" :label="t('processor.whisperModelMedium')" />
            <el-option value="small" :label="t('processor.whisperModelSmall')" />
            <el-option value="base" :label="t('processor.whisperModelBase')" />
            <el-option value="tiny" :label="t('processor.whisperModelTiny')" />
          </el-select>
        </el-form-item>

        <!-- WhisperX 批处理大小（仅 aligner_device=cuda 时对显存占用有实际意义；
             CPU 模式下隐藏控件，提交时后端仍会收到默认值 16） -->
        <el-form-item
          v-if="processingMode === 'full' && alignerBackend === 'whisperx' && advanced.aligner_device === 'cuda'"
          :label="t('processor.whisperxBatchSize')"
        >
          <el-input-number
            v-model="advanced.whisperx_batch_size"
            :disabled="processing"
            :min="1"
            :max="64"
            :step="1"
            style="width:160px"
          />
          <div class="help-text">⚠️ {{ t('processor.whisperxBatchSizeHint') }}</div>
        </el-form-item>

        <el-form-item v-if="processingMode === 'full' && alignerBackend === 'nemo_aligner'" :label="t('processor.nemoModel')">
          <el-input
            v-model="advanced.nemo_model"
            :disabled="processing"
            :placeholder="t('processor.nemoModelPlaceholder')"
            style="width: 360px"
            clearable
          />
          <div class="help-text">{{ t('processor.nemoModelHint') }}</div>
        </el-form-item>

        <el-form-item v-if="processingMode === 'full'" :label="t('processor.language')">
          <el-select v-model="sharedLanguage" :disabled="processing">
            <el-option :label="t('processor.languageCmn')" value="cmn" />
            <el-option :label="t('processor.languageEng')" value="eng" />
            <el-option :label="t('processor.languageJpn')" value="jpn" />
            <el-option :label="t('processor.languageKor')" value="kor" />
            <el-option :label="t('processor.languageYue')" value="yue" />
          </el-select>
        </el-form-item>

        <el-form-item v-if="sharedLanguage !== 'jpn' && processingMode !== 'project-only'" :label="t('processor.englishWordAlign')">
          <el-switch v-model="englishWordAlign" :disabled="processing" />
          <span class="option-hint">{{ t('processor.englishWordAlignHint') }}</span>
        </el-form-item>

        <!-- 处理模式：完整处理（对齐 + F0 + 工程文件）/ 仅生成工程（跳过对齐，
             直接使用每个对话框已提供的 LAB / MIDI）。 -->
        <el-form-item :label="t('dialogue.processingMode')">
          <el-radio-group v-model="processingMode" :disabled="processing || inputMode === 'tts'">
            <el-radio value="full">{{ t('dialogue.processingModeFull') }}</el-radio>
            <el-radio v-if="inputMode === 'audio'" value="project-only">{{ t('dialogue.processingModeProjectOnly') }}</el-radio>
          </el-radio-group>
          <div class="mode-help">
            <small v-if="inputMode === 'tts'">{{ t('processor.ttsProcessingModeHint') }}</small>
            <small v-else-if="processingMode === 'full'">{{ t('dialogue.processingModeFullHint') }}</small>
            <small v-else>{{ t('dialogue.processingModeProjectOnlyHint') }}</small>
          </div>
        </el-form-item>

        <el-form-item :label="t('processor.outputFormat')">
          <el-select v-model="outputFormat" :disabled="processing">
            <el-option :label="t('processor.outputFormatSv')" value="sv" />
            <el-option :label="t('dialogue.outputFormatUstx')" value="ustx" />
            <el-option :label="t('processor.outputFormatVsqx')" value="vsqx" />
          </el-select>
          <div class="help-text">{{ t('dialogue.outputFormatHint') }}</div>
          <div v-if="outputFormat === 'ustx'" class="dict-source-hint">
            💡 {{ t('dialogue.ustxDictHint') }}
          </div>
        </el-form-item>

        <!-- 音素转换：仅在"仅生成工程"模式下显示——该模式跳过对齐，直接
             使用已提供的 LAB 生成音轨，音素转换（合并辅音/平假名/片假名）
             才有意义；"完整处理"模式下音轨来自对齐结果，此设置不生效，
             故隐藏。仅在音轨来自 LAB 且输出格式非 USTX 时才真正生效，
             与后端语义一致。 -->
        <el-form-item v-if="processingMode === 'project-only'" :label="t('dialogue.phonemeMode')">
          <el-radio-group v-model="phonemeMode" :disabled="processing">
            <el-radio value="none">{{ t('dialogue.phonemeNone') }}</el-radio>
            <el-radio value="merge">{{ t('dialogue.phonemeMerge') }}</el-radio>
            <el-radio value="hiragana">{{ t('dialogue.phonemeHiragana') }}</el-radio>
            <el-radio value="katakana">{{ t('dialogue.phonemeKatakana') }}</el-radio>
          </el-radio-group>
          <div class="help-text">
            <small v-if="phonemeMode === 'none'">{{ t('dialogue.phonemeNoneHint') }}</small>
            <small v-else-if="phonemeMode === 'merge'">{{ t('dialogue.phonemeMergeHint') }}</small>
            <small v-else-if="phonemeMode === 'hiragana'">{{ t('dialogue.phonemeHiraganaHint') }}</small>
            <small v-else>{{ t('dialogue.phonemeKatakanaHint') }}</small>
          </div>
          <div class="help-text" style="margin-top:4px">
            <small style="color:#909399">⚠ {{ t('dialogue.phonemeWarning') }}</small>
          </div>
        </el-form-item>

        <el-form-item :label="t('dialogue.projectFileName')">
          <el-input
            v-model="projectFileName"
            :placeholder="t('dialogue.projectFileNamePlaceholder')"
            :disabled="processing"
            style="max-width: 320px"
          />
        </el-form-item>

        <el-collapse accordion>
          <el-collapse-item :title="`⚙️ ${t('processor.advancedSettingsTitle')}`" name="advanced">
            <el-row :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.bpm')">
                  <el-input-number v-model="advanced.bpm" :min="20" :max="300" :step="1" controls-position="right" :disabled="processing" />
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.basePitch')">
                  <div class="pitch-input-group">
                    <el-input-number v-model="advanced.base_pitch" :min="12" :max="108" :step="1" controls-position="right" :disabled="processing" />
                    <span class="pitch-name">{{ midiNoteToName(advanced.base_pitch) }}</span>
                  </div>
                </el-form-item>
              </el-col>

              <el-col :xs="24">
                <el-divider>📈 {{ t('processor.pitchControl') }}</el-divider>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.autoNotePitch')">
                  <el-switch
                    v-model="advanced.auto_note_pitch"
                    :active-text="t('processor.autoNotePitchActive')"
                    :inactive-text="t('processor.autoNotePitchInactive')"
                    :disabled="processing"
                  />
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.exportPitchLine')">
                  <el-switch
                    v-model="advanced.export_pitch_line"
                    :active-text="t('processor.exportPitchLineActive')"
                    :inactive-text="t('processor.exportPitchLineInactive')"
                    :disabled="processing"
                  />
                </el-form-item>
              </el-col>

              <el-col :xs="24">
                <el-divider>{{ t('processor.f0RangeDivider') }}</el-divider>
              </el-col>

              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Method')">
                  <el-radio-group
                    v-model="advanced.f0_method"
                    :disabled="processing || (!advanced.export_pitch_line && !advanced.auto_note_pitch)"
                  >
                    <el-radio value="dio">{{ t('processor.f0Dio') }}</el-radio>
                    <el-radio value="harvest">{{ t('processor.f0Harvest') }}</el-radio>
                    <el-radio value="crepe">{{ t('processor.f0Crepe') }}</el-radio>
                    <el-radio value="rmvpe">{{ t('processor.f0Rmvpe') }}</el-radio>
                  </el-radio-group>
                </el-form-item>
              </el-col>

              <el-col v-if="advanced.f0_method === 'crepe'" :xs="24" :sm="12">
                <el-form-item :label="t('processor.crepeModelSpec')">
                  <el-radio-group v-model="advanced.crepe_model" :disabled="processing">
                    <el-radio value="full">{{ t('processor.crepeFull') }}</el-radio>
                    <el-radio value="tiny">{{ t('processor.crepeTiny') }}</el-radio>
                  </el-radio-group>
                </el-form-item>
              </el-col>
              <el-col v-if="advanced.f0_method === 'crepe' || advanced.f0_method === 'rmvpe'" :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Device')">
                  <el-radio-group v-model="advanced.f0_device" :disabled="processing">
                    <el-radio value="auto">{{ t('processor.deviceAuto') }}</el-radio>
                    <el-radio value="cpu">{{ t('processor.deviceCpu') }}</el-radio>
                    <el-radio value="cuda">{{ t('processor.deviceCuda') }}</el-radio>
                  </el-radio-group>
                </el-form-item>
              </el-col>
            </el-row>

            <el-row :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.precision')">
                  <el-radio-group v-model="advanced.precision" :disabled="processing">
                    <el-radio value="single">{{ t('processor.precisionSingle') }}</el-radio>
                    <el-radio value="double">{{ t('processor.precisionDouble') }}</el-radio>
                  </el-radio-group>
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Smooth')">
                  <el-switch
                    v-model="advanced.f0_smooth"
                    :active-text="t('processor.enabled')"
                    :inactive-text="t('processor.disabled')"
                    :disabled="processing || !advanced.export_pitch_line"
                  />
                </el-form-item>
              </el-col>
            </el-row>

            <el-row v-if="advanced.f0_smooth" :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.smoothWindow')">
                  <el-input-number v-model="advanced.f0_smooth_window" :min="1" :max="29" :step="2" controls-position="right" :disabled="processing || !advanced.export_pitch_line" />
                  <span class="help-text">{{ t('processor.smoothWindowTip') }}</span>
                </el-form-item>
              </el-col>
            </el-row>

            <el-row v-if="outputFormat === 'vsqx' && advanced.f0_smooth" :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.vsqxPitchSmoothWindow')">
                  <el-input-number v-model="advanced.vsqx_pitch_smooth_window" :min="1" :max="29" :step="2" controls-position="right" :disabled="processing || !advanced.export_pitch_line" />
                  <span class="help-text">{{ t('processor.vsqxPitchSmoothWindowTip') }}</span>
                </el-form-item>
              </el-col>
            </el-row>

            <el-row :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Floor')">
                  <el-input-number v-model="advanced.f0_floor" :min="40" :max="200" :step="5" controls-position="right" :disabled="processing" />
                </el-form-item>
              </el-col>
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.f0Ceil')">
                  <el-input-number v-model="advanced.f0_ceil" :min="300" :max="1000" :step="50" controls-position="right" :disabled="processing" />
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
             但只在"确实会用到英文单词映射"时才出现：
             - 需要开启"英语单词级对齐"（该开关本身在语种为日语时就已隐藏，
               所以日语场景天然不会显示）；
             - 需要批量列表里至少有一个对话框会真正走对齐流程（即没有手动
               提供 .lab 标注）——如果所有对话框都用 .lab 走"仅生成工程"，
               就不会做任何单词级英文处理，该开关也就没有意义。
             【与"选择词典"解耦】该开关只控制"混合文本中的英语单词是否要
             转换为 ARPABET/VOCALOID4 音素"，不再决定下方"选择词典"是否
             显示——两者是相互独立的功能。 -->
        <el-form-item v-if="showWordPhonemeMap" :label="t('processor.wordPhonemeMap')">
          <el-switch v-model="wordPhonemeMap" :disabled="processing" />
          <div class="dict-source-hint">
            {{ t('processor.wordPhonemeMapSwitchHint') }}
          </div>
        </el-form-item>

        <!-- 选择词典：不受"英语单词→音素映射"开关的开启/关闭影响。
             批量对话框页面每次提交都会生成工程文件（等同于 MFAProcessor.vue
             的"完整处理"/"仅生成工程"两种场景的合集），因此只要输出格式
             支持 SVP/VSQX 就始终显示。词典可将任意语言的字词映射为音素，
             不局限于英语，且其匹配优先级高于"英语单词→音素映射"
             （命中词典时优先按词典输出，未命中才回退到英语单词→音素
             映射或软件默认转换）。 -->
        <el-form-item v-if="showDictSource" :label="t('processor.selectDictionary')">
          <el-select
            v-model="dictSource"
            :disabled="processing"
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
            {{ t('processor.selectDictionaryHint') }}
            <span v-if="!filteredDictionaries.length"> {{ t('processor.dictSourceEmptyHint') }}</span>
          </div>
        </el-form-item>
      </el-form>

      <el-divider />

      <!-- ============== 文件夹导入 / 对话框列表 ============== -->
      <input
        ref="folderInputRef"
        type="file"
        webkitdirectory
        multiple
        style="display: none"
        @change="handleFolderSelect"
      />
      <div class="folder-import-bar">
        <el-button :disabled="processing" @click="triggerFolderImport">📁 {{ t('dialogue.importFolder') }}</el-button>
        <el-button :disabled="processing" @click="addBox">➕ {{ t('dialogue.addBox') }}</el-button>
        <el-button type="danger" plain :disabled="processing" @click="clearAllBoxes">🗑️ {{ t('dialogue.clearAll') }}</el-button>
      </div>
      <p class="help-text">{{ t('dialogue.importFolderHint') }}</p>

      <div class="box-list">
        <div v-for="(box, i) in boxes" :key="box.id" class="dialogue-box">
          <div class="box-header">
            <span class="box-index">{{ t('dialogue.boxIndex', { index: i + 1 }) }}</span>
            <el-tag :type="statusTagType(box.status)" size="small">{{ statusLabel(box) }}</el-tag>
            <el-tag v-if="box.override.enabled" type="warning" size="small" effect="plain">
              ⚙ {{ t('dialogue.boxSettingsCustomTag') }}
            </el-tag>
            <el-button link class="box-settings-btn" :disabled="processing" @click="openBoxSettings(box)">
              ⚙ {{ t('dialogue.boxSettings') }}
            </el-button>
            <el-button link type="danger" class="box-remove" :disabled="processing" @click="removeBox(i)">
              ✖ {{ t('dialogue.removeBox') }}
            </el-button>
          </div>

          <el-row :gutter="16">
            <el-col :xs="24" :sm="12">
              <div class="panel-label">{{ t('dialogue.leftPanelLabel') }}</div>

              <!-- 仅生成工程模式：跳过对齐，直接使用已提供的 LAB / MIDI，
                   左侧面板改为拖拽/点击选择 LAB/MIDI 文件的上传区域，
                   不再显示台词文本框（该模式下文本框没有意义）。 -->
              <template v-if="processingMode === 'project-only'">
                <div v-if="box.labFile" class="file-info">
                  📄 {{ box.labFile.name }}
                  <el-button link type="danger" size="small" :disabled="processing" @click="clearNotation(box)">✖</el-button>
                </div>
                <div v-else-if="box.midiFile" class="file-info midi-loaded">
                  🎹 {{ box.midiFile.name }}
                  <el-button link type="danger" size="small" :disabled="processing" @click="clearNotation(box)">✖</el-button>
                </div>
                <el-upload
                  v-else
                  :key="box.labUploadKey"
                  drag
                  action="#"
                  :auto-upload="false"
                  :limit="1"
                  :show-file-list="false"
                  :disabled="processing"
                  :on-change="(f: any) => handleNotationSelect(box, f)"
                  accept=".lab,.mid,.midi"
                  class="compact-upload"
                >
                  <div class="el-upload__text">{{ t('dialogue.dragLabMidi') }}</div>
                </el-upload>
              </template>

              <!-- 完整处理模式：保留原有台词文本框 + 附加 LAB/MIDI/TXT
                   的按钮式导入（可选，用于跳过对齐）。 -->
              <template v-else>
                <el-input
                  v-if="!box.labFile && !box.midiFile"
                  v-model="box.text"
                  type="textarea"
                  :rows="3"
                  :placeholder="t('dialogue.textPlaceholder')"
                  :disabled="processing"
                />
                <div v-if="!box.labFile && !box.midiFile" style="margin-top: 6px">
                  <el-button size="small" :disabled="processing" @click="openTextOptimizer(box, 'text', sharedLanguage)">
                    🛠️ {{ t('processor.textOptimize') }}
                  </el-button>
                  <el-button size="small" :disabled="processing" @click="openFindReplace(box, 'text')">
                    🔍 {{ t('processor.findReplace') }}
                  </el-button>
                </div>
                <div v-if="box.labFile" class="file-info">
                  📄 {{ box.labFile.name }}
                  <el-button link type="danger" size="small" :disabled="processing" @click="clearNotation(box)">✖</el-button>
                </div>
                <div v-else-if="box.midiFile" class="file-info midi-loaded">
                  🎹 {{ box.midiFile.name }}
                  <el-button link type="danger" size="small" :disabled="processing" @click="clearNotation(box)">✖</el-button>
                </div>
                <!-- LAB / MIDI 二选一，单个文件槽位：.lab 标注优先级最高，
                     其次是 .mid/.midi（两者都能跳过对齐直接生成音轨）；
                     .txt 走"参考文本"模式，填入左侧文本框供对齐使用。 -->
                <el-upload
                  :key="box.labUploadKey"
                  action="#"
                  :auto-upload="false"
                  :show-file-list="false"
                  :disabled="processing"
                  :on-change="(f: any) => handleNotationSelect(box, f)"
                  accept=".lab,.txt,.mid,.midi"
                >
                  <el-button size="small" style="margin-top: 6px" :disabled="processing">
                    📎 {{ t('dialogue.labLabel') }}
                  </el-button>
                </el-upload>
              </template>
            </el-col>

            <el-col :xs="24" :sm="12">
              <div class="panel-label">{{ t('dialogue.rightPanelLabel') }}</div>

              <!-- TTS跟读：讲述人 / EdgeTTS 音色 / 语速·音调 / 预览播放，
                   替代原有的"音频导入"拖拽区域（该框的音频由 EdgeTTS 当场合成）。 -->
              <template v-if="inputMode === 'tts'">
                <el-select
                  v-model="box.ttsEngine"
                  @change="(eng: string) => handleBoxEngineChange(box, eng)"
                  size="small"
                  :loading="ttsEnginesLoading"
                  style="width: 100%"
                  :disabled="processing"
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
                <el-select
                  v-model="box.ttsNarratorId"
                  @change="(id: string) => handleBoxNarratorSelect(box, id)"
                  filterable
                  size="small"
                  style="width: 100%; margin-top: 6px"
                  :disabled="processing"
                  :placeholder="t('processor.narratorCustom')"
                >
                  <el-option :label="t('processor.narratorCustom')" value="" />
                  <el-option v-for="n in narratorsForEngine(box.ttsEngine)" :key="n.id" :label="n.name" :value="n.id" />
                </el-select>
                <el-select
                  v-model="box.ttsVoice"
                  filterable
                  size="small"
                  :loading="box.ttsVoicesLoading"
                  style="width: 100%; margin-top: 6px"
                  :disabled="processing"
                  :placeholder="t('processor.ttsVoicePlaceholder')"
                >
                  <el-option v-for="v in box.ttsVoices" :key="v.id" :label="`${v.name} (${v.locale})`" :value="v.id" />
                </el-select>
				<div class="tts-box-sliders">
				  <span class="tts-mini-label">{{ t('processor.ttsRate') }}</span>
				  <el-input-number v-model="box.ttsRate" :min="-50" :max="100" size="small" :disabled="processing" controls-position="right" style="width: 90px" />

				  <span class="tts-mini-label">{{ t('processor.ttsPitch') }}</span>
				  <el-input-number v-model="box.ttsPitch" :min="-50" :max="50" size="small" :disabled="processing" controls-position="right" style="width: 90px" />

				  <span class="tts-mini-label">{{ t('processor.ttsVolume') }}</span>
				  <el-input-number v-model="box.ttsVolume" :min="-50" :max="50" size="small" :disabled="processing" controls-position="right" style="width: 90px" />
				</div>
                <!-- 手动分段预览：只在点击按钮时把该框完整文本按句合成
                     （不做对齐）；生成期间共享的"开始处理"按钮会被禁用，
                     生成完成后点击"开始处理"会直接复用这份分句音频去
                     对齐。若在生成预览后又改动了该框文本/引擎/音色/
                     语速/音调/音量，这份预览会失效，"开始处理"会退回
                     "先合成再对齐"的完整流程（仅针对这一个框）。 -->
                <div style="margin-top: 6px">
                  <el-button
                    size="small"
                    :loading="box.ttsSegmentPreviewLoading"
                    :disabled="processing || !box.ttsVoice || !box.text.trim()"
                    @click="runBoxSegmentPreview(box)"
                  >
                    🔄 {{ box.ttsSegmentPreviewUrl ? t('processor.ttsRegeneratePreview') : t('processor.ttsGeneratePreview') }}
                  </el-button>
                  <audio v-if="box.ttsSegmentPreviewUrl" :src="box.ttsSegmentPreviewUrl" controls style="height: 28px; margin-left: 8px; vertical-align: middle" />
                  <div v-if="box.ttsSegmentPreviewSentenceCount && !box.ttsSegmentPreviewError" style="margin-top: 4px">
                    <el-text type="info" size="small">
                      {{ t('processor.ttsSegmentPreviewCount', { count: box.ttsSegmentPreviewSentenceCount }) }}
                    </el-text>
                  </div>
                  <div v-if="box.ttsSegmentPreviewWarnings.length" style="margin-top: 4px">
                    <el-text type="warning" size="small">
                      {{ t('processor.ttsSegmentPreviewWarnings') }} ({{ box.ttsSegmentPreviewWarnings.length }})
                    </el-text>
                  </div>
                  <div v-if="box.ttsSegmentPreviewError" style="margin-top: 4px">
                    <el-text type="danger" size="small">{{ box.ttsSegmentPreviewError }}</el-text>
                  </div>
                </div>

                <!-- 对齐辅助移调：每个对话框独立生效，只在该框的对齐阶段
                     使用临时移调音频副本，不影响最终 WAV / F0 / 工程文件
                     音高。"仅生成工程"模式跳过对齐，故隐藏。 -->
                <div v-if="processingMode !== 'project-only'" class="box-pitch-shift">
                  <span class="tts-mini-label">{{ t('processor.alignPitchShift') }}</span>
                  <el-input-number
                    v-model="box.align_pitch_shift_semitones"
                    :min="-24" :max="24" :step="1"
                    size="small"
                    controls-position="right"
                    :disabled="processing"
                    style="width: 110px"
                  />
                  <el-tooltip :content="t('processor.alignPitchShiftHint')" placement="top">
                    <span class="option-hint-icon">❓</span>
                  </el-tooltip>
                </div>
              </template>

              <template v-else>
                <el-upload
                  :key="box.audioUploadKey"
                  drag
                  action="#"
                  :auto-upload="false"
                  :limit="1"
                  :disabled="processing"
                  :on-change="(f: any) => handleAudioSelect(box, f)"
                  accept=".wav,.mp3,.flac,.m4a,.aac,.ogg"
                  class="compact-upload"
                >
                  <div class="el-upload__text">{{ t('dialogue.dragAudio') }}</div>
                </el-upload>
                <div v-if="box.audioFile" class="file-info">
                  🎵 {{ box.audioFile.name }} ({{ formatFileSize(box.audioFile.size) }})
                  <el-button link type="danger" size="small" :disabled="processing" @click="box.audioFile = null">✖</el-button>
                </div>

                <!-- 对齐辅助移调：每个对话框独立生效，只在该框的对齐阶段
                     使用临时移调音频副本，不影响最终 WAV / F0 / 工程文件
                     音高。"仅生成工程"模式跳过对齐，故隐藏。 -->
                <div v-if="processingMode !== 'project-only'" class="box-pitch-shift">
                  <span class="tts-mini-label">{{ t('processor.alignPitchShift') }}</span>
                  <el-input-number
                    v-model="box.align_pitch_shift_semitones"
                    :min="-24" :max="24" :step="1"
                    size="small"
                    controls-position="right"
                    :disabled="processing"
                    style="width: 110px"
                  />
                  <el-tooltip :content="t('processor.alignPitchShiftHint')" placement="top">
                    <span class="option-hint-icon">❓</span>
                  </el-tooltip>
                </div>
              </template>
            </el-col>
          </el-row>

          <div v-if="box.status === 'failed' && box.error" class="box-error">
            ⚠️ {{ box.error }}
            <el-button link type="danger" size="small" @click="showBoxError(box)">{{ t('dialogue.viewError') }}</el-button>
          </div>
        </div>
      </div>

      <!-- ============== 开始 / 停止处理 ============== -->
      <div class="action-row">
        <el-button
          type="primary"
          size="large"
          :loading="processing"
          :disabled="isSubmitDisabled"
          @click="startProcessing"
        >
          <span v-if="!processing">🚀 {{ t('dialogue.startProcessing') }}</span>
          <span v-else>{{ t('dialogue.processingProgress', { done: doneCount, total: totalCount }) }}</span>
        </el-button>
        <el-button v-if="processing" @click="stopProcessing">⏹ {{ t('dialogue.stopProcessing') }}</el-button>
        <span v-if="isSubmitDisabled && !processing" class="disabled-text">
          <template v-if="inputMode === 'tts'">
            <template v-if="!boxes.some((b) => b.text.trim() && b.ttsVoice)">
              ({{ t('dialogue.ttsEmptyBoxesWarning') }})
            </template>
          </template>
          <template v-else-if="!boxes.some((b) => b.audioFile)">
            ({{ t('dialogue.emptyBoxesWarning') }})
          </template>
          <template v-else-if="processingMode === 'project-only'">
            ({{ t('dialogue.projectOnlyEmptyWarning') }})
          </template>
        </span>
      </div>
      <el-progress v-if="processing" :percentage="progressPercent" class="progress-bar" />

      <!-- ============== 处理结果 ============== -->
      <div v-if="projectResult" class="result-section">
        <el-divider />
        <h3>✅ {{ t('processor.result') }}</h3>
        <div class="result-info">
          <p>{{ projectResult.message }}</p>
          <p><strong>{{ t('processor.processingTime') }}:</strong> {{ formatTime(projectResult.processingTime) }}</p>
          <p v-if="projectResult.path">
            <strong>{{ t('processor.projectFile') }}:</strong> {{ getFileName(projectResult.path) }}
          </p>
        </div>
        <el-button
          v-if="projectResult.path"
          type="success"
          size="large"
          :loading="downloading"
          @click="downloadResult"
        >
          📥 {{ t('dialogue.resultDownload') }}
        </el-button>
      </div>

      <div v-if="topError" class="error-section">
        <el-alert :title="topError" type="error" :closable="true" show-icon @close="topError = ''" />
      </div>

      <!-- 语音预设管理弹窗：新增 / 编辑 / 删除；音色列表跟随本弹窗内选择的
           引擎（narratorFormVoices），与每个对话框各自的 box.ttsVoices 互相独立 -->
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
           副本上生效；点击"应用"才会写回打开弹窗时指定的那个对话框文本框，
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
           副本，点击"应用"才写回打开弹窗时指定的那个对话框文本框，不点
           "应用"直接关闭则不影响原文本。纯前端字符串/正则替换，不调用任何
           后端接口。支持"区分大小写""正则表达式""全部替换"三个开关，以及
           "查找下一个"用于在不替换的情况下定位。 -->
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

      <el-dialog
        v-model="boxSettings.visible"
        :title="t('dialogue.boxSettingsTitle')"
        width="640px"
        class="box-settings-dialog"
      >
        <el-form label-position="top">
          <el-form-item>
            <el-switch v-model="boxSettings.draft.enabled" :active-text="t('dialogue.boxSettingsEnable')" />
            <div class="dict-source-hint">{{ t('dialogue.boxSettingsEnableHint') }}</div>
          </el-form-item>

          <template v-if="boxSettings.draft.enabled">
            <el-divider />

            <!-- ============== 完整处理模式专属 ============== -->
            <template v-if="processingMode === 'full'">
              <el-form-item :label="t('processor.backendLabel')">
                <el-radio-group v-model="boxSettings.draft.alignerBackend">
                  <el-radio value="mfa">
                    <span>{{ t('processor.backendMfa') }}</span>
                    <el-tag :type="alignerStatus.mfa?.available ? 'success' : 'danger'" size="small" style="margin-left:4px">
                      {{ alignerStatus.mfa?.available ? '✓' : '✗' }}
                    </el-tag>
                  </el-radio>
                  <el-radio value="whisperx">
                    <span>{{ t('processor.backendWhisperx') }}</span>
                    <el-tag :type="alignerStatus.whisperx?.available ? 'success' : 'info'" size="small" style="margin-left:4px">
                      {{ alignerStatus.whisperx?.available ? '✓' : t('processor.backendStatusNeedInstall') }}
                    </el-tag>
                  </el-radio>
                  <el-radio value="qwen3_asr">
                    <span>{{ t('processor.backendQwen3Asr') }}</span>
                    <el-tag :type="alignerStatus.qwen3_asr?.available ? 'success' : 'info'" size="small" style="margin-left:4px">
                      {{ alignerStatus.qwen3_asr?.available ? '✓' : t('processor.backendStatusNeedInstall') }}
                    </el-tag>
                  </el-radio>
                  <el-radio value="qwen3_aligner">
                    <span>{{ t('processor.backendQwen3Aligner') }}</span>
                    <el-tag :type="alignerStatus.qwen3_aligner?.available ? 'success' : 'info'" size="small" style="margin-left:4px">
                      {{ alignerStatus.qwen3_aligner?.available ? '✓' : t('processor.backendStatusNeedInstall') }}
                    </el-tag>
                  </el-radio>
                  <el-radio value="nemo_aligner">
                    <span>{{ t('processor.backendNemoAligner') }}</span>
                    <el-tag :type="alignerStatus.nemo_aligner?.available ? 'success' : 'info'" size="small" style="margin-left:4px">
                      {{ alignerStatus.nemo_aligner?.available ? '✓' : t('processor.backendStatusNeedInstall') }}
                    </el-tag>
                  </el-radio>
                </el-radio-group>
                <div class="dict-source-hint">{{ t('dialogue.boxSettingsFieldFallbackHint') }}</div>
              </el-form-item>

              <el-form-item :label="t('processor.language')">
                <el-select v-model="boxSettings.draft.language">
                  <el-option :label="t('processor.languageCmn')" value="cmn" />
                  <el-option :label="t('processor.languageEng')" value="eng" />
                  <el-option :label="t('processor.languageJpn')" value="jpn" />
                  <el-option :label="t('processor.languageKor')" value="kor" />
                  <el-option :label="t('processor.languageYue')" value="yue" />
                </el-select>
              </el-form-item>

              <el-form-item v-if="boxSettings.draft.language !== 'jpn'" :label="t('processor.englishWordAlign')">
                <el-switch v-model="boxSettings.draft.englishWordAlign" />
                <span class="option-hint">{{ t('processor.englishWordAlignHint') }}</span>
              </el-form-item>
            </template>

            <!-- ============== 仅生成工程模式专属 ============== -->
            <template v-else>
              <el-form-item :label="t('dialogue.phonemeMode')">
                <el-radio-group v-model="boxSettings.draft.phonemeMode">
                  <el-radio value="none">{{ t('dialogue.phonemeNone') }}</el-radio>
                  <el-radio value="merge">{{ t('dialogue.phonemeMerge') }}</el-radio>
                  <el-radio value="hiragana">{{ t('dialogue.phonemeHiragana') }}</el-radio>
                  <el-radio value="katakana">{{ t('dialogue.phonemeKatakana') }}</el-radio>
                </el-radio-group>
                <div class="help-text">
                  <small v-if="boxSettings.draft.phonemeMode === 'none'">{{ t('dialogue.phonemeNoneHint') }}</small>
                  <small v-else-if="boxSettings.draft.phonemeMode === 'merge'">{{ t('dialogue.phonemeMergeHint') }}</small>
                  <small v-else-if="boxSettings.draft.phonemeMode === 'hiragana'">{{ t('dialogue.phonemeHiraganaHint') }}</small>
                  <small v-else>{{ t('dialogue.phonemeKatakanaHint') }}</small>
                </div>
              </el-form-item>
            </template>

            <!-- ============== 高级设置（两种模式共用，不含 BPM） ============== -->
            <el-collapse accordion>
              <el-collapse-item :title="`⚙️ ${t('processor.advancedSettingsTitle')}`" name="advanced">
                <div class="dict-source-hint" style="margin-bottom: 8px">{{ t('dialogue.boxSettingsNoBpmHint') }}</div>
                <el-row :gutter="20">
                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.basePitch')">
                      <div class="pitch-input-group">
                        <el-input-number v-model="boxSettings.draft.advanced.base_pitch" :min="12" :max="108" :step="1" controls-position="right" />
                        <span class="pitch-name">{{ midiNoteToName(boxSettings.draft.advanced.base_pitch) }}</span>
                      </div>
                    </el-form-item>
                  </el-col>

                  <el-col :xs="24">
                    <el-divider>📈 {{ t('processor.pitchControl') }}</el-divider>
                  </el-col>

                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.autoNotePitch')">
                      <el-switch
                        v-model="boxSettings.draft.advanced.auto_note_pitch"
                        :active-text="t('processor.autoNotePitchActive')"
                        :inactive-text="t('processor.autoNotePitchInactive')"
                      />
                    </el-form-item>
                  </el-col>
                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.exportPitchLine')">
                      <el-switch
                        v-model="boxSettings.draft.advanced.export_pitch_line"
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
                      <el-radio-group
                        v-model="boxSettings.draft.advanced.f0_method"
                        :disabled="!boxSettings.draft.advanced.export_pitch_line && !boxSettings.draft.advanced.auto_note_pitch"
                      >
                        <el-radio value="dio">{{ t('processor.f0Dio') }}</el-radio>
                        <el-radio value="harvest">{{ t('processor.f0Harvest') }}</el-radio>
                        <el-radio value="crepe">{{ t('processor.f0Crepe') }}</el-radio>
                        <el-radio value="rmvpe">{{ t('processor.f0Rmvpe') }}</el-radio>
                      </el-radio-group>
                    </el-form-item>
                  </el-col>

                  <el-col v-if="boxSettings.draft.advanced.f0_method === 'crepe'" :xs="24" :sm="12">
                    <el-form-item :label="t('processor.crepeModelSpec')">
                      <el-radio-group v-model="boxSettings.draft.advanced.crepe_model">
                        <el-radio value="full">{{ t('processor.crepeFull') }}</el-radio>
                        <el-radio value="tiny">{{ t('processor.crepeTiny') }}</el-radio>
                      </el-radio-group>
                    </el-form-item>
                  </el-col>
                  <el-col v-if="boxSettings.draft.advanced.f0_method === 'crepe' || boxSettings.draft.advanced.f0_method === 'rmvpe'" :xs="24" :sm="12">
                    <el-form-item :label="t('processor.f0Device')">
                      <el-radio-group v-model="boxSettings.draft.advanced.f0_device">
                        <el-radio value="auto">{{ t('processor.deviceAuto') }}</el-radio>
                        <el-radio value="cpu">{{ t('processor.deviceCpu') }}</el-radio>
                        <el-radio value="cuda">{{ t('processor.deviceCuda') }}</el-radio>
                      </el-radio-group>
                    </el-form-item>
                  </el-col>
                </el-row>

                <el-row :gutter="20">
                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.precision')">
                      <el-radio-group v-model="boxSettings.draft.advanced.precision">
                        <el-radio value="single">{{ t('processor.precisionSingle') }}</el-radio>
                        <el-radio value="double">{{ t('processor.precisionDouble') }}</el-radio>
                      </el-radio-group>
                    </el-form-item>
                  </el-col>
                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.f0Smooth')">
                      <el-switch
                        v-model="boxSettings.draft.advanced.f0_smooth"
                        :active-text="t('processor.enabled')"
                        :inactive-text="t('processor.disabled')"
                        :disabled="!boxSettings.draft.advanced.export_pitch_line"
                      />
                    </el-form-item>
                  </el-col>
                </el-row>

                <el-row v-if="boxSettings.draft.advanced.f0_smooth" :gutter="20">
                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.smoothWindow')">
                      <el-input-number
                        v-model="boxSettings.draft.advanced.f0_smooth_window"
                        :min="1" :max="29" :step="2" controls-position="right"
                        :disabled="!boxSettings.draft.advanced.export_pitch_line"
                      />
                      <span class="help-text">{{ t('processor.smoothWindowTip') }}</span>
                    </el-form-item>
                  </el-col>
                </el-row>

                <el-row v-if="outputFormat === 'vsqx' && boxSettings.draft.advanced.f0_smooth" :gutter="20">
                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.vsqxPitchSmoothWindow')">
                      <el-input-number
                        v-model="boxSettings.draft.advanced.vsqx_pitch_smooth_window"
                        :min="1" :max="29" :step="2" controls-position="right"
                        :disabled="!boxSettings.draft.advanced.export_pitch_line"
                      />
                      <span class="help-text">{{ t('processor.vsqxPitchSmoothWindowTip') }}</span>
                    </el-form-item>
                  </el-col>
                </el-row>

                <el-row :gutter="20">
                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.f0Floor')">
                      <el-input-number v-model="boxSettings.draft.advanced.f0_floor" :min="40" :max="200" :step="5" controls-position="right" />
                    </el-form-item>
                  </el-col>
                  <el-col :xs="24" :sm="12">
                    <el-form-item :label="t('processor.f0Ceil')">
                      <el-input-number v-model="boxSettings.draft.advanced.f0_ceil" :min="300" :max="1000" :step="50" controls-position="right" />
                    </el-form-item>
                  </el-col>
                </el-row>
              </el-collapse-item>
            </el-collapse>

            <!-- 英语单词→音素映射：仅完整处理模式 + 已开启英语单词级对齐时显示 -->
            <el-form-item v-if="processingMode === 'full' && showBoxWordPhonemeMap" :label="t('processor.wordPhonemeMap')">
              <el-switch v-model="boxSettings.draft.wordPhonemeMap" />
              <div class="dict-source-hint">{{ t('processor.wordPhonemeMapSwitchHint') }}</div>
            </el-form-item>

            <!-- 选择词典：两种模式都显示 -->
            <el-form-item :label="t('processor.selectDictionary')">
              <el-select
                v-model="boxSettings.draft.dictSource"
                style="width: 260px"
                :placeholder="t('processor.dictSourceDefault')"
                @visible-change="(open: boolean) => open && fetchDictionaries()"
              >
                <el-option value="default" :label="t('processor.dictSourceDefault')" />
                <el-option
                  v-for="d in filteredDictionaries"
                  :key="d.name"
                  :value="d.name"
                  :label="`${d.name} (${d.notation === 'vocaloid' ? t('dictionary.notationVocaloid') : t('dictionary.notationSynthesizerV')}, ${d.count})`"
                />
              </el-select>
              <div class="dict-source-hint">{{ t('processor.selectDictionaryHint') }}</div>
            </el-form-item>
          </template>
        </el-form>
        <template #footer>
          <el-button @click="resetBoxSettings">{{ t('dialogue.boxSettingsReset') }}</el-button>
          <el-button @click="boxSettings.visible = false">{{ t('processor.textOptimizeCancel') }}</el-button>
          <el-button type="primary" @click="applyBoxSettings">{{ t('processor.textOptimizeApply') }}</el-button>
        </template>
      </el-dialog>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'

const { t, locale } = useI18n()

// ============== 类型 ==============
type BoxStatus = 'idle' | 'queued' | 'processing' | 'done' | 'failed' | 'skipped_empty' | 'skipped_no_notation'

interface DialogueBox {
  id: number
  text: string
  labFile: File | null
  midiFile: File | null   // LAB 与 MIDI 互斥（单一文件槽位）：LAB 优先级高于 MIDI
  audioFile: File | null
  labUploadKey: number
  audioUploadKey: number
  status: BoxStatus
  error: string
  // 对齐辅助移调（半音）：每个对话框独立生效，只在该框的对齐阶段使用
  // 临时移调音频副本，不影响最终 WAV / F0 / 工程文件音高。TTS跟读与
  // 音频跟读两种输入模式共用同一个字段。
  align_pitch_shift_semitones: number
  // ── TTS跟读专属（inputMode='tts' 时生效，替代 audioFile） ──────────
  ttsNarratorId: string   // 语音预设 id，空字符串表示"自定义"（手动选引擎+音色）
  ttsEngine: string       // 选择的 TTS 引擎（讲述人 / EdgeTTS / 未来可扩展），每个对话框可独立选择
  ttsVoice: string        // 该引擎下的音色 id
  ttsVoices: TtsVoice[]   // 该框当前引擎可用的音色列表（每框独立，不与其它框共用，
                          // 避免"框A切到讲述人后框B的音色列表也被顶掉"的串扰）
  ttsVoicesLoading: boolean
  ttsRate: number         // 语速，百分比增量，如 10 → "+10%"
  ttsPitch: number        // 音调，Hz 增量，如 -5 → "-5Hz"
  ttsVolume: number       // 音量，百分比增量
  ttsPreviewUrl: string
  ttsPreviewLoading: boolean
  // ── 手动分段预览（与上面的"试听音色"按钮是两回事）：点击后调用
  // /api/tts/synthesize_preview 把该框的完整文本按句合成好，不做
  // Qwen3-FA 对齐；返回的 previewId 若在点击"开始处理"时仍然有效
  // （文本/引擎/音色/语速/音调/音量都未再变化），后端会直接复用这份
  // 分句音频去对齐，不会重新合成一遍。
  ttsSegmentPreviewLoading: boolean
  ttsSegmentPreviewUrl: string
  ttsSegmentPreviewId: string
  ttsSegmentPreviewSentenceCount: number
  ttsSegmentPreviewWarnings: string[]
  ttsSegmentPreviewError: string
  // 该对话框的"单独设置"（对齐后端 / 语言 / 英语单词级对齐 / 词典 /
  // 音素转换 / 高级设置），默认不开启，跟随页面顶部全局设置提交。
  override: BoxOverride
}

interface AdvancedConfig {
  bpm: number
  base_pitch: number
  auto_note_pitch: boolean
  export_pitch_line: boolean
  f0_method: 'dio' | 'harvest' | 'crepe' | 'rmvpe'
  f0_device: 'auto' | 'cpu' | 'cuda'
  aligner_device: 'auto' | 'cpu' | 'cuda'
  whisperx_model: string
  whisperx_batch_size: number
  nemo_model: string
  crepe_model: 'full' | 'tiny'
  precision: 'single' | 'double'
  f0_smooth: boolean
  f0_smooth_window: number
  vsqx_pitch_smooth_window: number
  f0_floor: number
  f0_ceil: number
}

// ── 每个对话框的"单独设置"：不包含 BPM（BPM 决定整批对话框合并后的
// 时间轴换算，必须全局统一，单独覆盖会破坏时间轴对齐，见设置弹窗与
// buildFormData 附近的说明）。未开启 enabled 时，该框完全跟随页面顶部
// 的全局设置提交；开启后，下面各字段的值会覆盖对应的全局设置，仅对该
// 对话框自己生效。 ──
interface BoxAdvancedOverride {
  base_pitch: number
  auto_note_pitch: boolean
  export_pitch_line: boolean
  f0_method: 'dio' | 'harvest' | 'crepe' | 'rmvpe'
  f0_device: 'auto' | 'cpu' | 'cuda'
  crepe_model: 'full' | 'tiny'
  precision: 'single' | 'double'
  f0_smooth: boolean
  f0_smooth_window: number
  vsqx_pitch_smooth_window: number
  f0_floor: number
  f0_ceil: number
}

interface BoxOverride {
  enabled: boolean              // 该框是否开启"单独设置"（总开关）
  // ── 完整处理模式专属 ──
  alignerBackend: string
  language: string
  englishWordAlign: boolean
  wordPhonemeMap: boolean
  // ── 仅生成工程模式专属 ──
  phonemeMode: 'none' | 'merge' | 'hiragana' | 'katakana'
  // ── 两种模式共用 ──
  dictSource: string
  advanced: BoxAdvancedOverride
}

// ============== 共用设置状态 ==============
const alignerBackend = ref<string>('mfa')
const alignerStatus = ref<Record<string, any>>({})
const checkingStatus = ref(false)

// 输入模式：TTS跟读（讲述人 + EdgeTTS，每个对话框不再上传音频，而是选择
// 音色由 EdgeTTS 合成）/ 音频跟读（原有的每框上传音频对齐流程）。
type TtsNarrator = { id: string; name: string; engine?: string; voice: string; rate: string; pitch: string; volume: string; language?: string }
type TtsVoice = { id: string; name: string; gender?: string; locale: string }
type TtsEngine = { id: string; label: string; label_zh: string; available: boolean; message: string }

const inputMode = ref<'audio' | 'tts'>('audio')
const narrators = ref<TtsNarrator[]>([])
const ttsEngines = ref<TtsEngine[]>([])
const ttsEnginesLoading = ref(false)
const narratorManagerVisible = ref(false)
const narratorForm = ref<TtsNarrator>({ id: '', name: '', engine: 'edge_tts', voice: '', rate: '+0%', pitch: '+0Hz', volume: '+0%' })
const narratorSaving = ref(false)

// ── "优化文本"弹窗（与 MFAProcessor.vue 的实现逻辑一致：弹窗内编辑的是
// draft 副本，点击"应用"才写回打开弹窗时绑定的目标对话框 box.text，取消/
// 关闭弹窗不影响原文本）。对话框批量处理页面没有每框独立的语种选择，
// 统一使用页面共享的 sharedLanguage，由调用方在打开弹窗时显式传入。 ──
interface TextOptimizerState {
  visible: boolean
  draft: string
  loading: string
  error: string
  target: Record<string, any> | null
  field: string
  language: string
  everyN: number   // "按每几句插入换行"的句子数量 N，默认 2
}

const textOptimizer = ref<TextOptimizerState>({
  visible: false, draft: '', loading: '', error: '', target: null, field: 'text', language: 'cmn', everyN: 2,
})

const openTextOptimizer = (target: Record<string, any>, field: string, language?: string) => {
  textOptimizer.value = {
    visible: true,
    draft: target[field] || '',
    loading: '',
    error: '',
    target,
    field,
    language: language || 'cmn',
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
// 下一个"时上一次匹配结束的位置，用于循环定位，与替换操作互不影响。 ──
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
// 内的匹配文本；el-input 把内部原生 <textarea> 暴露在 .textarea / .input
// 属性上（视 Element Plus 版本而定，这里做兼容性兜底）。
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
// null 并把错误信息写入 findReplace.error，由调用方决定如何处理。
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

// 语音预设对话框里的语速/音调/音量：narratorForm 里存的是 EdgeTTS 风格的
// 字符串（"+10%" / "-5Hz"），但 el-slider 需要绑定数字，这里用 computed
// get/set 做双向转换（与 MFAProcessor.vue 保持一致的写法）。
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
// 语音预设管理对话框内的音色列表：跟随对话框内正在编辑的 narratorForm.engine，
// 与每个对话框各自的 box.ttsVoices 互相独立，避免串扰（同 MFAProcessor.vue）。
const narratorFormVoices = ref<TtsVoice[]>([])
const narratorFormVoicesLoading = ref(false)

// 引擎中/英文名按当前界面语言展示，"选择 TTS"下拉框、语音预设管理对话框、
// 预设列表的引擎列都复用这一个函数。
const engineLabel = (id?: string): string => {
  const eng = ttsEngines.value.find(e => e.id === id)
  if (!eng) return id || ''
  return locale.value.startsWith('zh') ? eng.label_zh : eng.label
}

// 某个引擎下可选的语音预设列表（每个对话框各自的引擎可能不同，因此按引擎
// 过滤为一个函数而非单一 computed，避免所有框共用同一份"当前引擎"）。
const narratorsForEngine = (engine: string) =>
  narrators.value.filter(n => (n.engine || 'edge_tts') === engine)

const sharedLanguage = ref('cmn')
const englishWordAlign = ref(false)
// 英语单词→音素映射手动开关：与 MFAProcessor.vue 保持一致，默认关闭，仅在
// showWordPhonemeMap（自动可见性条件）满足时展示，用户需主动开启后才
// 会随表单一起提交为 true（见 wordPhonemeMapEffective）。
const wordPhonemeMap = ref(false)
// 【解耦】"选择词典"与"英语单词→音素映射"开关是两个独立功能：词典可以把
// 任意语言的字词映射为音素（不局限于英语单词），其显示条件只看输出格式
// 是否支持 SVP/VSQX（见下方 showDictSource），不受 englishWordAlign /
// wordPhonemeMap 开关影响；命中词典时的匹配优先级也高于"英语单词→音素
// 映射"（词典优先，未命中才回退）。
const dictSource = ref('default')
const dictionaries = ref<{ name: string; notation: string; count: number }[]>([])
const outputFormat = ref<'sv' | 'vsqx' | 'ustx'>('sv')
const projectFileName = ref('Dialogue Project')

// 处理模式："full"（完整处理：对齐 + F0 + 工程文件）/ "project-only"
// （仅生成工程：跳过对齐，只使用已提供的 WAV + LAB / MIDI；既没有 LAB
// 也没有 MIDI 的对话框会被跳过，不中断整体处理）。
const processingMode = ref<'full' | 'project-only'>('full')

// 音素转换：不转换 / 合并辅音 / 平假名 / 片假名。两种处理模式下都提交，
// 仅在音轨来自 LAB 且输出格式非 USTX 时才真正生效（与后端语义一致）。
const phonemeMode = ref<'none' | 'merge' | 'hiragana' | 'katakana'>('none')

const advanced = ref<AdvancedConfig>({
  bpm: 120,
  base_pitch: 60,
  auto_note_pitch: true,
  export_pitch_line: true,
  f0_method: 'dio',
  f0_device: 'auto',
  aligner_device: 'auto',
  whisperx_model: 'large-v3',
  whisperx_batch_size: 16,
  nemo_model: '',
  crepe_model: 'full',
  precision: 'double',
  f0_smooth: true,
  f0_smooth_window: 5,
  vsqx_pitch_smooth_window: 5,
  f0_floor: 71,
  f0_ceil: 800,
})

// 与 MFAProcessor.vue 保持一致：显示条件与"英语单词级对齐"开关挂钩。
// 这里只控制"英语单词→音素映射"开关本身的可见性，与下方"选择词典"
// （showDictSource）完全独立，互不影响彼此的显示/隐藏。
// - 必须是"完整处理"模式（project-only 跳过对齐，不会走到 g2p_en 这一步，
//   且该模式下 englishWordAlign 开关本身已被模板隐藏）；
// - 输出格式需支持 SVP/VSQX（USTX 没有 phonemes 字段可写，isSupportedFormat
//   天然排除 ustx）；
// - 需要批量列表里至少有一个对话框会真正走对齐流程（没有手动提供
//   LAB/MIDI）——如果所有对话框都用 LAB/MIDI 跳过对齐，就不会做任何
//   单词级英文处理，该开关也就没有意义。
// - 语种切到日语时下方 watch(sharedLanguage) 会主动把 englishWordAlign
//   重置为 false（仅靠 v-if 隐藏开关不会重置其底层状态，之前这里错误地
//   假设"隐藏 = 恒为 false"，导致语种切到日语后本开关未同步隐藏——已
//   改为显式 watch 重置，而不是依赖 v-if）。
const showWordPhonemeMap = computed(() => {
  const isSupportedFormat = outputFormat.value === 'sv' || outputFormat.value === 'vsqx'
  const hasAlignableBox = boxes.value.length === 0 || boxes.value.some((b) => !b.labFile && !b.midiFile)
  return processingMode.value === 'full' && englishWordAlign.value && isSupportedFormat && hasAlignableBox
})

// 实际提交给后端的"英语单词→音素映射"值：只有在开关控件可见
// （showWordPhonemeMap）且用户主动打开了手动开关（wordPhonemeMap）时才为
// true，与 MFAProcessor.vue 一致。
const wordPhonemeMapEffective = computed(() => showWordPhonemeMap.value && wordPhonemeMap.value)

// 语种切到日语时，"英语单词级对齐"开关对日语没有意义（上方 v-if 会隐藏
// 该开关），但仅隐藏控件不会重置其底层状态——这里显式重置为 false，
// showWordPhonemeMap 依赖 englishWordAlign.value，会跟着自动隐藏；同时
// 顺手把 wordPhonemeMap 本身也重置掉，避免切回非日语语种时该开关无声无息
// 地沿用切换前遗留的开启状态。与 MFAProcessor.vue 保持一致。
watch(sharedLanguage, (lang) => {
  if (lang === 'jpn') {
    englishWordAlign.value = false
    wordPhonemeMap.value = false
  }
  if (inputMode.value === 'tts') fetchAllBoxTtsVoices()
})

// TTS跟读模式下没有"复用已有音频"的概念（音频当场合成），不存在
// "仅生成工程"这种依赖已有音频的模式，切换到 TTS 跟读时强制回到"完整处理"。
watch(inputMode, (mode) => {
  if (mode === 'tts') {
    processingMode.value = 'full'
    fetchTtsEngines()
    fetchAllBoxTtsVoices()
  }
})

// 控制"选择词典"表单项是否显示。
// 与"英语单词→音素映射"开关完全解耦，不受其开启/关闭、语种或"是否存在
// 可对齐对话框"影响：只要输出格式支持 SVP/VSQX 就始终显示（完整处理与
// 仅生成工程两种模式都会生成 SVP/VSQX 工程文件）。USTX 没有 phonemes
// 字段，isSupportedFormat 天然排除 ustx，与"USTX 隐藏词典/单词映射"的
// 要求一致。词典本身可以把任意语言的字词映射为音素，不局限于英语单词。
const showDictSource = computed(() => outputFormat.value === 'sv' || outputFormat.value === 'vsqx')

// 根据当前输出格式过滤"选择词典"下拉列表：
// - 输出格式为 SVP（Synthesizer V）时，只显示 notation === 'synthesizerv' 的词典，
//   隐藏 notation === 'vocaloid'（VOCALOID4）的词典；
// - 输出格式为 VSQX（VOCALOID4）时，只显示 notation === 'vocaloid' 的词典，
//   隐藏 notation === 'synthesizerv'（Synthesizer V）的词典。
// "使用软件默认值"选项不受影响，始终可选。
const filteredDictionaries = computed(() => {
  if (outputFormat.value === 'vsqx') {
    return dictionaries.value.filter(d => d.notation === 'vocaloid')
  }
  if (outputFormat.value === 'sv') {
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

const fetchDictionaries = async () => {
  try {
    const res = await fetch('/api/dictionary')
    const data = await res.json()
    if (res.ok && data.success) {
      dictionaries.value = data.dictionaries || []
    }
  } catch {
    // 静默失败，见 MFAProcessor.vue 同名函数注释
  }
}

// ============== TTS跟读（讲述人 + EdgeTTS）辅助函数 ==============

const fetchTtsEngines = async () => {
  ttsEnginesLoading.value = true
  try {
    const res = await fetch('/api/tts/engines')
    const data = await res.json()
    if (data.success) {
      ttsEngines.value = data.engines || []
      // 兜底：新拉取的引擎列表里如果没有某个框当前用的引擎（几乎不会发生），
      // 让它落回第一个可用引擎，避免卡在一个后端已不再声明支持的引擎上。
      const validIds = new Set(ttsEngines.value.map(e => e.id))
      boxes.value.forEach(box => {
        if (!validIds.has(box.ttsEngine)) {
          const firstAvailable = ttsEngines.value.find(e => e.available)
          box.ttsEngine = (firstAvailable || ttsEngines.value[0])?.id || 'edge_tts'
        }
      })
    }
  } catch (e) {
    console.error('获取 TTS 引擎列表失败', e)
  } finally {
    ttsEnginesLoading.value = false
  }
}

// 每个对话框的音色列表独立获取、独立存放（box.ttsVoices），跟随该框自己的
// ttsEngine，不与其它框或语音预设管理对话框共用列表——避免"切换框A的引擎
// 后，框B的音色下拉也被联动清空/替换"这类串扰。
// 语言同理：优先使用该框"单独设置"里的 language（override.enabled 时），
// 未开启单独设置则回退到页面顶部的 sharedLanguage——与 buildFormData /
// 后端 process_dialogue_batch 里"box_override.get('language', language)"
// 的回退规则保持一致，否则单独设置了语言的框会拉到错误语种的音色列表
// （例如单独设成英语，音色下拉却仍是普通话音色）。
const boxEffectiveLanguage = (box: DialogueBox): string => {
  return box.override.enabled ? box.override.language : sharedLanguage.value
}

const fetchBoxTtsVoices = async (box: DialogueBox, engine?: string) => {
  const eng = engine || box.ttsEngine || 'edge_tts'
  box.ttsVoicesLoading = true
  try {
    const lang = boxEffectiveLanguage(box)
    const res = await fetch(`/api/tts/voices?engine=${encodeURIComponent(eng)}&language=${encodeURIComponent(lang)}`)
    const data = await res.json()
    if (data.success) {
      box.ttsVoices = data.voices || []
      if (box.ttsVoice && !box.ttsVoices.some(v => v.id === box.ttsVoice)) {
        box.ttsVoice = ''
      }
    } else {
      box.ttsVoices = []
      if (data.error) ElMessage.error(`❌ ${data.error}`)
    }
  } catch (e) {
    console.error('获取音色列表失败', e)
  } finally {
    box.ttsVoicesLoading = false
  }
}

// 批量刷新全部对话框的音色列表：每个框内部仍按 boxEffectiveLanguage(box)
// 取自己的有效语言（单独设置了语言的框用自己的，其余框用 sharedLanguage），
// 所以这里即使在"全局语言变化"时触发，也不会覆盖已单独设置了语言的框。
const fetchAllBoxTtsVoices = () => {
  boxes.value.forEach(box => fetchBoxTtsVoices(box, box.ttsEngine))
}

// 切换某个对话框的"选择 TTS"引擎：清空该框当前音色（不同引擎音色 ID 体系
// 不通用）并按新引擎重新拉取该框的音色列表；只影响这一个框。
const handleBoxEngineChange = (box: DialogueBox, engine: string) => {
  box.ttsVoice = ''
  fetchBoxTtsVoices(box, engine)
}

const fetchNarratorFormVoices = async (engine: string) => {
  narratorFormVoicesLoading.value = true
  try {
    const res = await fetch(`/api/tts/voices?engine=${encodeURIComponent(engine)}&language=${encodeURIComponent(sharedLanguage.value)}`)
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

// 对话框内切换"选择 TTS"引擎时，同步刷新该引擎下的音色列表；只在语音预设
// 管理对话框打开期间生效。
watch(() => narratorForm.value.engine, (engine) => {
  if (narratorManagerVisible.value && engine) fetchNarratorFormVoices(engine)
})

const fetchNarrators = async () => {
  try {
    const res = await fetch('/api/tts/narrators')
    const data = await res.json()
    if (data.success) narrators.value = data.narrators || []
  } catch (e) {
    console.error('获取讲述人列表失败', e)
  }
}

const handleBoxNarratorSelect = async (box: DialogueBox, narratorId: string) => {
  if (!narratorId) {
    // 切回"不使用预设"（自定义）时，把这个对话框的语速/音调/音量重置为
    // 默认值——否则下拉框显示"不使用预设"，滑块却还停留在上一个预设的
    // 参数上，容易误以为已经跟预设脱钩了。
    box.ttsRate = 0
    box.ttsPitch = 0
    box.ttsVolume = 0
    return
  }
  const n = narrators.value.find((x) => x.id === narratorId)
  if (!n) return
  const engine = n.engine || 'edge_tts'
  if (engine !== box.ttsEngine) {
    box.ttsEngine = engine
    await fetchBoxTtsVoices(box, engine)
  }
  box.ttsVoice = n.voice
  box.ttsRate = parseInt(n.rate) || 0
  box.ttsPitch = parseInt(n.pitch) || 0
  box.ttsVolume = parseInt(n.volume) || 0
}

const previewBoxTts = async (box: DialogueBox) => {
  if (!box.ttsVoice) {
    ElMessage.warning(t('processor.ttsVoiceRequired'))
    return
  }
  box.ttsPreviewLoading = true
  try {
    const previewText = (box.text || '').trim() || t('processor.ttsPreviewDefaultText')
    const res = await fetch('/api/tts/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: previewText,
        engine: box.ttsEngine,
        voice: box.ttsVoice,
        rate: `${box.ttsRate >= 0 ? '+' : ''}${box.ttsRate}%`,
        pitch: `${box.ttsPitch >= 0 ? '+' : ''}${box.ttsPitch}Hz`,
        volume: `${box.ttsVolume >= 0 ? '+' : ''}${box.ttsVolume}%`,
      }),
    })
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}))
      throw new Error(errData.error || t('processor.submitFailed'))
    }
    const blob = await res.blob()
    if (box.ttsPreviewUrl) URL.revokeObjectURL(box.ttsPreviewUrl)
    box.ttsPreviewUrl = URL.createObjectURL(blob)
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  } finally {
    box.ttsPreviewLoading = false
  }
}

// ── 手动分段预览（每个对话框独立）──────────────────────────────────
// 只在用户点击该框的"生成预览"按钮时触发：按句末标点分段 → 逐句合成，
// 返回完整拼接后的音频供试听，不做 Qwen3-FA 对齐，也不截断句子数量。
// 生成成功后拿到的 ttsSegmentPreviewId 会在点击共享的"开始处理"按钮时
// 一并提交；只要该框的文本/引擎/音色/语速/音调/音量之后没有变化，后端
// 会直接复用这份分句音频去对齐，不会重新合成一遍。
let boxSegmentPreviewSeq = 0
const runBoxSegmentPreview = async (box: DialogueBox) => {
  const text = (box.text || '').trim()
  if (!text || !box.ttsVoice) return

  const mySeq = ++boxSegmentPreviewSeq
  box.ttsSegmentPreviewLoading = true
  box.ttsSegmentPreviewError = ''
  try {
    const res = await fetch('/api/tts/synthesize_preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        // 使用该框自己的有效语言（单独设置了语言时用 override.language，
        // 否则回退到 sharedLanguage）——分句规则按语言区分（如中文用
        // "。"分句，英文用 "."），用错语言会导致预览分句/停顿与实际
        // 提交处理时不一致。
        language: boxEffectiveLanguage(box),
        engine: box.ttsEngine,
        voice: box.ttsVoice,
        rate: `${box.ttsRate >= 0 ? '+' : ''}${box.ttsRate}%`,
        pitch: `${box.ttsPitch >= 0 ? '+' : ''}${box.ttsPitch}Hz`,
        volume: `${box.ttsVolume >= 0 ? '+' : ''}${box.ttsVolume}%`,
      }),
    })
    const data = await res.json()
    // 用户可能在等待响应期间又点了一次（同一个框或另一个框）——更旧的
    // 响应回来时不应该覆盖较新的结果；这里用全局递增序号而非按框区分，
    // 足够避免同一个框内的乱序覆盖（不同框之间互不影响各自的状态字段）。
    if (mySeq !== boxSegmentPreviewSeq && box.ttsSegmentPreviewLoading === false) return

    if (!data.success) {
      box.ttsSegmentPreviewLoading = false
      box.ttsSegmentPreviewUrl = ''
      box.ttsSegmentPreviewId = ''
      box.ttsSegmentPreviewSentenceCount = 0
      box.ttsSegmentPreviewWarnings = []
      box.ttsSegmentPreviewError = data.error || t('processor.ttsSegmentPreviewFailed')
      return
    }

    if (box.ttsSegmentPreviewUrl) URL.revokeObjectURL(box.ttsSegmentPreviewUrl)
    const blob = await (await fetch(`data:audio/wav;base64,${data.audio_base64}`)).blob()
    box.ttsSegmentPreviewUrl = URL.createObjectURL(blob)
    box.ttsSegmentPreviewId = data.preview_id || ''
    box.ttsSegmentPreviewSentenceCount = data.sentence_count || 0
    box.ttsSegmentPreviewWarnings = data.warnings || []
    box.ttsSegmentPreviewError = ''
  } catch (e: any) {
    box.ttsSegmentPreviewUrl = ''
    box.ttsSegmentPreviewId = ''
    box.ttsSegmentPreviewSentenceCount = 0
    box.ttsSegmentPreviewWarnings = []
    box.ttsSegmentPreviewError = e?.message || String(e)
  } finally {
    box.ttsSegmentPreviewLoading = false
  }
}

// 每个框的文本 / 引擎 / 音色 / 语速·音调·音量任一变化都会让已生成的
// 分段预览音频与当前输入不再对应——清空该框的 previewId 让"开始处理"
// 对这一个框退回"先合成再对齐"的完整流程，而不是悄悄拿旧音频去对齐
// 新文本。这里只清空 previewId（保留试听音频/句数展示），生成新预览
// 仍需用户手动点击按钮触发。用一个按框缓存的"签名"字符串来判断是否
// 变化，避免共享语种切换或其它无关框的变化互相影响。
// （watch 本身需要在 boxes 声明之后才能建立，定义见下方 boxes 声明处）
const _boxSegmentPreviewSignatures = new Map<number, string>()
const _boxSegmentPreviewSignature = (box: DialogueBox, language: string) =>
  JSON.stringify([box.text, box.ttsEngine, box.ttsVoice, box.ttsRate, box.ttsPitch, box.ttsVolume, language])

const openNarratorManager = () => {
  narratorManagerVisible.value = true
  fetchNarratorFormVoices(narratorForm.value.engine || 'edge_tts')
}

const editNarrator = (n: TtsNarrator) => {
  narratorForm.value = { engine: 'edge_tts', ...n }
  fetchNarratorFormVoices(narratorForm.value.engine || 'edge_tts')
}

const resetNarratorForm = () => {
  narratorForm.value = { id: '', name: '', engine: 'edge_tts', voice: '', rate: '+0%', pitch: '+0Hz', volume: '+0%' }
  fetchNarratorFormVoices('edge_tts')
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
      body: JSON.stringify({ ...narratorForm.value, language: sharedLanguage.value }),
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
    await fetchNarrators()
  } catch (e: any) {
    ElMessage.error(`❌ ${e?.message || String(e)}`)
  }
}

// ============== 对话框列表 ==============

// 单个对话框"单独设置"的默认值：advanced 部分与页面顶部 advanced 的默认值
// 保持一致（仅去掉 bpm），其余开关默认关闭/回退到"使用软件默认值"，
// 与全局设置的默认值语义对齐，避免用户打开弹窗时看到不一致的初始状态。
const createBoxAdvancedOverride = (): BoxAdvancedOverride => ({
  base_pitch: 60,
  auto_note_pitch: true,
  export_pitch_line: true,
  f0_method: 'dio',
  f0_device: 'auto',
  crepe_model: 'full',
  precision: 'double',
  f0_smooth: true,
  f0_smooth_window: 5,
  vsqx_pitch_smooth_window: 5,
  f0_floor: 71,
  f0_ceil: 800,
})

const createBoxOverride = (): BoxOverride => ({
  enabled: false,
  alignerBackend: 'mfa',
  language: 'cmn',
  englishWordAlign: false,
  wordPhonemeMap: false,
  phonemeMode: 'none',
  dictSource: 'default',
  advanced: createBoxAdvancedOverride(),
})

let boxIdCounter = 0
const createBox = (): DialogueBox => ({
  id: ++boxIdCounter,
  text: '',
  labFile: null,
  midiFile: null,
  audioFile: null,
  labUploadKey: 0,
  audioUploadKey: 0,
  status: 'idle',
  error: '',
  align_pitch_shift_semitones: 0,
  ttsNarratorId: '',
  ttsEngine: 'edge_tts',
  ttsVoice: '',
  ttsVoices: [],
  ttsVoicesLoading: false,
  ttsRate: 0,
  ttsPitch: 0,
  ttsVolume: 0,
  ttsPreviewUrl: '',
  ttsPreviewLoading: false,
  ttsSegmentPreviewLoading: false,
  ttsSegmentPreviewUrl: '',
  ttsSegmentPreviewId: '',
  ttsSegmentPreviewSentenceCount: 0,
  ttsSegmentPreviewWarnings: [],
  ttsSegmentPreviewError: '',
  override: createBoxOverride(),
})

const boxes = ref<DialogueBox[]>([createBox()])

// ============== 单个对话框"单独设置"弹窗 ==============
// 弹窗内编辑的是打开时绑定的目标对话框 box.override 的一份深拷贝副本
// （boxSettings.draft），点击"应用"才写回原对话框；取消/关闭弹窗不影响
// 原有设置——与"优化文本"弹窗（textOptimizer）的编辑/应用模式保持一致。
// 必须放在 createBox / createBoxOverride 声明之后，否则 boxSettings 的
// 初始值 draft: createBoxOverride() 会在函数尚未定义时执行，导致
// "used before its declaration" 的运行时错误。
interface BoxSettingsState {
  visible: boolean
  targetBox: DialogueBox | null
  draft: BoxOverride
}

const boxSettings = ref<BoxSettingsState>({
  visible: false,
  targetBox: null,
  draft: createBoxOverride(),
})

const openBoxSettings = (box: DialogueBox) => {
  boxSettings.value = {
    visible: true,
    targetBox: box,
    // 深拷贝，避免弹窗内的调整在点击"应用"前就影响到原对话框
    // （尤其 advanced 是嵌套对象，浅拷贝会共享引用）。
    draft: JSON.parse(JSON.stringify(box.override)) as BoxOverride,
  }
}

const applyBoxSettings = () => {
  const box = boxSettings.value.targetBox
  if (!box) return
  // 应用前先记下旧的"有效语言"，用于判断是否需要重新拉取该框的 TTS
  // 音色列表（见下方说明）；必须在覆盖 box.override 之前读取。
  const prevEffectiveLanguage = boxEffectiveLanguage(box)
  box.override = JSON.parse(JSON.stringify(boxSettings.value.draft)) as BoxOverride
  boxSettings.value.visible = false
  ElMessage.success(t('dialogue.boxSettingsApplied'))

  // TTS跟读模式下，音色列表按语言过滤（同一 TTS 引擎，不同语言可用音色
  // 不同）；这里"单独设置"改的是该框自己的有效语言，如果确实变了，需要
  // 立即用新语言重新拉取这一个框的音色列表，否则下拉框会继续显示上一个
  // 语言下的音色（即便实际提交时后端已经在用新语言，界面上容易造成误选）。
  if (inputMode.value === 'tts') {
    const nextEffectiveLanguage = boxEffectiveLanguage(box)
    if (nextEffectiveLanguage !== prevEffectiveLanguage) {
      fetchBoxTtsVoices(box, box.ttsEngine)
    }
  }
}

const resetBoxSettings = () => {
  boxSettings.value.draft = createBoxOverride()
}

// 弹窗内"英语单词→音素映射"开关的可见性：与页面顶部 showWordPhonemeMap
// 同样的判定条件，但基于弹窗草稿里的语言/英语单词级对齐状态，而不是
// 全局的 sharedLanguage / englishWordAlign（该框可能覆盖了不同的语言）。
const showBoxWordPhonemeMap = computed(() => {
  const isSupportedFormat = outputFormat.value === 'sv' || outputFormat.value === 'vsqx'
  return boxSettings.value.draft.englishWordAlign && isSupportedFormat
})

// 语言切到日语时，弹窗草稿里的"英语单词级对齐"/"英语单词→音素映射"同样
// 没有意义，随之重置，与页面顶部 watch(sharedLanguage) 逻辑一致。
watch(() => boxSettings.value.draft.language, (lang) => {
  if (lang === 'jpn') {
    boxSettings.value.draft.englishWordAlign = false
    boxSettings.value.draft.wordPhonemeMap = false
  }
})


// 分段预览失效判定 watch（定义见上方 _boxSegmentPreviewSignature 注释）：
// 必须放在 boxes 声明之后，否则会在 <script setup> 顶层执行阶段抛出
// "used before its declaration" 的运行时错误，导致整个组件挂载失败、
// 页面空白。
watch(
  [boxes, sharedLanguage],
  ([currentBoxes, language]) => {
    for (const box of currentBoxes) {
      const sig = _boxSegmentPreviewSignature(box, language)
      const prevSig = _boxSegmentPreviewSignatures.get(box.id)
      if (prevSig !== undefined && prevSig !== sig && box.ttsSegmentPreviewId) {
        box.ttsSegmentPreviewId = ''
      }
      _boxSegmentPreviewSignatures.set(box.id, sig)
    }
    // 清理已删除对话框留下的签名缓存，避免无限增长。
    const liveIds = new Set(currentBoxes.map((b) => b.id))
    for (const id of _boxSegmentPreviewSignatures.keys()) {
      if (!liveIds.has(id)) _boxSegmentPreviewSignatures.delete(id)
    }
  },
  { deep: true, immediate: true },
)

const MAX_BOXES = 64

const addBox = () => {
  if (boxes.value.length >= MAX_BOXES) {
    ElMessage.warning(`Maximum ${MAX_BOXES} rows`)
    return
  }
  const box = createBox()
  boxes.value.push(box)
  if (inputMode.value === 'tts') fetchBoxTtsVoices(box, box.ttsEngine)
}

const removeBox = (index: number) => {
  boxes.value.splice(index, 1)
  if (boxes.value.length === 0) boxes.value.push(createBox())
}

const clearAllBoxes = async () => {
  try {
    await ElMessageBox.confirm(t('dialogue.confirmClearAll'), '', { type: 'warning' })
  } catch {
    return
  }
  boxes.value = [createBox()]
  projectResult.value = null
  topError.value = ''
}

// LAB / MIDI 二选一，单一文件槽位：.lab 标注优先级最高（写入后自动清除
// 已有的 .mid，二者互斥），.mid/.midi 次之；.txt 走"参考文本"模式，读取
// 文本内容填入左侧文本框，不作为标注文件（不影响已有 LAB/MIDI）。
const handleNotationSelect = async (box: DialogueBox, file: any) => {
  const raw: File | null = file?.raw || null
  if (!raw) return
  const dot = raw.name.lastIndexOf('.')
  const ext = dot > 0 ? raw.name.slice(dot + 1).toLowerCase() : ''

  if (ext === 'txt') {
    // .txt 走"参考文本"模式：读取文本内容填入左侧文本框，不作为标注文件
    try {
      box.text = await raw.text()
    } catch {
      box.text = ''
    }
  } else if (ext === 'lab') {
    // .lab 跳过对齐，直接使用该 LAB 生成对应音轨（最高优先级，覆盖已有 MIDI）
    box.labFile = raw
    box.midiFile = null
    box.text = ''
  } else if (ext === 'mid' || ext === 'midi') {
    // .mid/.midi 跳过对齐，从 MIDI 音符自动生成段落（无 LAB 时才生效）
    box.midiFile = raw
    box.labFile = null
    box.text = ''
  } else {
    ElMessage.error(t('processor.onlySupportNotation'))
  }
  box.labUploadKey += 1
}

const clearNotation = (box: DialogueBox) => {
  box.labFile = null
  box.midiFile = null
}

const handleAudioSelect = (box: DialogueBox, file: any) => {
  const raw: File | null = file?.raw || null
  if (!raw) return
  box.audioFile = raw
}

const showBoxError = (box: DialogueBox) => {
  ElMessageBox.alert(box.error, t('dialogue.viewError'), { type: 'error' })
}

// ============== 文件夹导入（按文件名自动配对） ==============
const folderInputRef = ref<HTMLInputElement | null>(null)
const AUDIO_EXTS = ['wav', 'mp3', 'flac', 'm4a', 'aac', 'ogg']

const triggerFolderImport = () => {
  const probe = document.createElement('input')
  if (!('webkitdirectory' in probe)) {
    ElMessage.warning(t('dialogue.folderInputNotSupported'))
    return
  }
  folderInputRef.value?.click()
}

const handleFolderSelect = async (e: Event) => {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (!files.length) return

  // 每个 stem（不含扩展名的文件名）最多对应一个音频文件（audio 只能导入
  // 一个），以及一个 LAB 或 MIDI（二选一，LAB 优先级高于 MID：同名的
  // .lab 与 .mid/.midi 同时存在时，仅保留 .lab）。
  const groups: Record<string, { audio?: File; lab?: File; midi?: File; txt?: File }> = {}
  for (const file of files) {
    const dot = file.name.lastIndexOf('.')
    if (dot <= 0) continue
    const ext = file.name.slice(dot + 1).toLowerCase()
    const stem = file.name.slice(0, dot)
    if (!groups[stem]) groups[stem] = {}
    if (AUDIO_EXTS.includes(ext)) groups[stem].audio = file
    else if (ext === 'lab') groups[stem].lab = file
    else if (ext === 'mid' || ext === 'midi') groups[stem].midi = file
    else if (ext === 'txt') groups[stem].txt = file
  }

  const stems = Object.keys(groups)
  let matched = 0
  const newBoxes: DialogueBox[] = []

  for (const stem of stems) {
    const g = groups[stem]
    if (!g.audio && !g.lab && !g.midi && !g.txt) continue
    const box = createBox()
    if (g.audio) box.audioFile = g.audio
    if (g.lab) {
      // LAB 高优先级：即使同名 .mid/.midi 也一并存在，仍只保留 LAB
      box.labFile = g.lab
    } else if (g.midi) {
      box.midiFile = g.midi
    } else if (g.txt) {
      try {
        box.text = await g.txt.text()
      } catch {
        box.text = ''
      }
    }
    if (g.audio && (g.lab || g.midi || g.txt)) matched += 1
    newBoxes.push(box)
  }

  // 仅当当前只有一个"完全空白"的对话框时才整体替换；
  // 否则一律追加，这样可以连续多次点击"导入文件夹"导入多个文件夹，
  // 每个文件夹各自按文件名配对后追加到列表末尾，而不会覆盖之前已导入的内容。
  const firstIsBlank =
    boxes.value.length === 1 &&
    !boxes.value[0].audioFile &&
    !boxes.value[0].labFile &&
    !boxes.value[0].midiFile &&
    !boxes.value[0].text.trim()

  if (firstIsBlank && newBoxes.length) {
    boxes.value = newBoxes
  } else {
    boxes.value.push(...newBoxes)
  }

  // 导入后总数超出上限时裁剪，并提示用户
  if (boxes.value.length > MAX_BOXES) {
    boxes.value = boxes.value.slice(0, MAX_BOXES)
    ElMessage.warning(`Maximum ${MAX_BOXES} rows`)
  }

  ElMessage.success(t('dialogue.matchedCount', { matched, total: stems.length }))
  input.value = ''
}

// ============== 状态展示辅助 ==============
// 后端两种"跳过"场景（未提供音频 / 「仅生成工程」模式下未提供 LAB/MIDI）
// 统一使用同一个 status="skipped_empty"，仅 message 文本不同（见
// pipeline.py process_dialogue_batch）；这里按对话框自身状态（是否已有
// 音频）二次区分展示文案，未提供音频时兜底显示通用"已跳过"。
const statusLabel = (box: DialogueBox): string => {
  const map: Record<BoxStatus, string> = {
    idle: t('dialogue.boxStatusIdle'),
    queued: t('dialogue.boxStatusQueued'),
    processing: t('dialogue.boxStatusProcessing'),
    done: t('dialogue.boxStatusDone'),
    failed: t('dialogue.boxStatusFailed'),
    skipped_empty: box.audioFile
      ? t('dialogue.boxStatusSkippedNoNotation')
      : t('dialogue.boxStatusSkippedEmpty'),
    skipped_no_notation: t('dialogue.boxStatusSkippedNoNotation'),
  }
  return map[box.status] || box.status
}

const statusTagType = (status: BoxStatus): string => {
  const map: Record<BoxStatus, string> = {
    idle: 'info',
    queued: 'info',
    processing: 'warning',
    done: 'success',
    failed: 'danger',
    skipped_empty: 'warning',
    skipped_no_notation: 'warning',
  }
  return map[status] || 'info'
}

const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
const midiNoteToName = (note: number): string => {
  const octave = Math.floor(note / 12) - 1
  return `${NOTE_NAMES[((note % 12) + 12) % 12]}${octave}`
}

const formatFileSize = (bytes: number): string => {
  if (!bytes) return '0 Bytes'
  const k = 1024
  const sizes = ['Bytes', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i]
}

const formatTime = (ms: number): string => {
  const seconds = Math.floor((ms || 0) / 1000)
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)
  if (hours > 0) return `${hours}h ${minutes % 60}m ${seconds % 60}s`
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`
  return `${seconds}s`
}

const getFileName = (path: string): string => (path || '').split(/[\\/]/).pop() || path

// ============== 后端状态检查 ==============
const refreshStatus = async () => {
  checkingStatus.value = true
  try {
    const res = await fetch('/api/aligner/status')
    const data = await res.json()
    if (data.success && data.backends) {
      alignerStatus.value = data.backends
    }
  } catch {
    ElMessage.warning(t('processor.backendConnectionFailed'))
  } finally {
    checkingStatus.value = false
  }
}

// ============== 提交处理 ==============
const processing = ref(false)
const progressPercent = ref(0)
const doneCount = ref(0)
const totalCount = ref(0)
const topError = ref('')
const downloading = ref(false)
const projectResult = ref<{
  path: string
  format: string
  processedCount: number
  failedCount: number
  skippedCount: number
  processingTime: number
  message: string
} | null>(null)

let jobPollTimer: number | null = null
let pollActive = false

// 「仅生成工程」模式下，至少要有一个对话框同时提供音频与 LAB/MIDI，
// 否则全部对话框都会被跳过、无法生成任何音轨（与后端校验一致）。
const isSubmitDisabled = computed(() => {
  if (processing.value) return true
  if (boxes.value.some((b) => b.ttsSegmentPreviewLoading)) return true
  if (inputMode.value === 'tts') {
    return !boxes.value.some((b) => b.text.trim() && b.ttsVoice)
  }
  if (!boxes.value.some((b) => b.audioFile)) return true
  if (processingMode.value === 'project-only') {
    return !boxes.value.some((b) => b.audioFile && (b.labFile || b.midiFile))
  }
  return false
})

const buildFormData = (): FormData => {
  const fd = new FormData()
  fd.append('box_count', String(boxes.value.length))
  fd.append('input_mode', inputMode.value)
  boxes.value.forEach((box, i) => {
    if (box.text.trim()) fd.append(`text_${i}`, box.text)
    // 对齐辅助移调：每个对话框独立提交，只在该框自身的对齐阶段生效。
    fd.append(`align_pitch_shift_${i}`, String(box.align_pitch_shift_semitones))

    // ── 该对话框的"单独设置"：仅在开启了总开关（override.enabled）时才
    // 提交 override_enabled_{i}=true 及具体字段；后端未收到该字段或其为
    // false 时，该框完全沿用页面顶部的全局设置，不做任何改动（向后兼容：
    // 旧前端/未升级请求不受影响）。字段集合与"单独设置"弹窗一致，但不
    // 包含 BPM（BPM 恒定全局生效，见弹窗内说明）。──────────────────────
    const ov = box.override
    fd.append(`override_enabled_${i}`, String(ov.enabled))
    if (ov.enabled) {
      fd.append(`override_aligner_backend_${i}`, ov.alignerBackend)
      fd.append(`override_language_${i}`, ov.language)
      fd.append(
        `override_english_word_align_${i}`,
        String(processingMode.value === 'full' && ov.englishWordAlign && ov.language !== 'jpn')
      )
      const boxShowWordPhonemeMap =
        processingMode.value === 'full' &&
        ov.englishWordAlign &&
        (outputFormat.value === 'sv' || outputFormat.value === 'vsqx')
      fd.append(`override_word_phoneme_map_${i}`, String(boxShowWordPhonemeMap && ov.wordPhonemeMap))
      fd.append(`override_phoneme_mode_${i}`, processingMode.value === 'project-only' ? ov.phonemeMode : 'none')
      fd.append(`override_dict_source_${i}`, ov.dictSource)
      fd.append(`override_base_pitch_${i}`, String(ov.advanced.base_pitch))
      fd.append(`override_auto_note_pitch_${i}`, String(ov.advanced.auto_note_pitch))
      fd.append(`override_export_pitch_line_${i}`, String(ov.advanced.export_pitch_line))
      fd.append(`override_f0_method_${i}`, ov.advanced.f0_method)
      fd.append(`override_f0_device_${i}`, ov.advanced.f0_device)
      fd.append(`override_crepe_model_${i}`, ov.advanced.crepe_model)
      fd.append(`override_precision_${i}`, ov.advanced.precision)
      fd.append(`override_f0_smooth_${i}`, String(ov.advanced.f0_smooth))
      fd.append(`override_f0_smooth_window_${i}`, String(ov.advanced.f0_smooth_window))
      fd.append(`override_vsqx_pitch_smooth_window_${i}`, String(ov.advanced.vsqx_pitch_smooth_window))
      fd.append(`override_f0_floor_${i}`, String(ov.advanced.f0_floor))
      fd.append(`override_f0_ceil_${i}`, String(ov.advanced.f0_ceil))
    }

    if (inputMode.value === 'tts') {
      if (box.text.trim() && box.ttsVoice) {
        fd.append(`tts_text_${i}`, box.text)
        fd.append(`tts_engine_${i}`, box.ttsEngine)
        fd.append(`tts_voice_${i}`, box.ttsVoice)
        fd.append(`tts_rate_${i}`, `${box.ttsRate >= 0 ? '+' : ''}${box.ttsRate}%`)
        fd.append(`tts_pitch_${i}`, `${box.ttsPitch >= 0 ? '+' : ''}${box.ttsPitch}Hz`)
        fd.append(`tts_volume_${i}`, `${box.ttsVolume >= 0 ? '+' : ''}${box.ttsVolume}%`)
        // 若之前手动点过这一框的"生成预览"且之后未再改动文本/参数，
        // ttsSegmentPreviewId 仍然有效——带给后端复用已经合成好的分句
        // 音频，跳过重新合成直接对齐；否则是空字符串，该框退回"先合成
        // 再对齐"的完整流程。
        fd.append(`tts_preview_id_${i}`, box.ttsSegmentPreviewId)
      }
      // TTS 模式下极少见但仍保留兼容：若该框另外手动提供了 LAB/MIDI，
      // 后端会优先沿用它，不用 TTS 自动对齐结果覆盖（见 app.py 注释）。
      if (box.labFile) fd.append(`lab_${i}`, box.labFile)
      else if (box.midiFile) fd.append(`mid_${i}`, box.midiFile)
    } else {
      if (box.audioFile) fd.append(`audio_${i}`, box.audioFile)
      // LAB 优先级高于 MID：同一个对话框里两者互斥（单一文件槽位），
      // 因此这里最多只会有其中一个被提交。
      if (box.labFile) fd.append(`lab_${i}`, box.labFile)
      else if (box.midiFile) fd.append(`mid_${i}`, box.midiFile)
    }
  })

  fd.append('language', sharedLanguage.value)
  fd.append('format', outputFormat.value)
  if (outputFormat.value === 'vsqx') {
    fd.append('vsqx_pitch_smooth_window', String(advanced.value.vsqx_pitch_smooth_window))
  }
  fd.append('title', projectFileName.value || 'Dialogue Project')
  fd.append('processing_mode', processingMode.value)
  // 音素转换设置项仅在"仅生成工程"模式下可见/可编辑；"完整处理"模式下
  // 该设置对用户不可见，因此强制提交 'none'，避免残留的旧选择被后端
  // 静默应用到完整处理流程（该模式下音轨来自对齐结果，音素转换本就
  // 不该生效）。
  fd.append('phoneme_mode', processingMode.value === 'project-only' ? phonemeMode.value : 'none')
  fd.append('bpm', String(advanced.value.bpm))
  fd.append('base_pitch', String(advanced.value.base_pitch))
  fd.append('f0_method', advanced.value.f0_method)
  fd.append('f0_smooth', String(advanced.value.f0_smooth))
  fd.append('f0_smooth_window', String(advanced.value.f0_smooth_window))
  fd.append('precision', advanced.value.precision)
  fd.append('f0_floor', String(advanced.value.f0_floor))
  fd.append('f0_ceil', String(advanced.value.f0_ceil))
  fd.append('auto_note_pitch', String(advanced.value.auto_note_pitch))
  fd.append('export_pitch_line', String(advanced.value.export_pitch_line))
  fd.append('f0_device', advanced.value.f0_device)
  fd.append('crepe_model', advanced.value.crepe_model)
  fd.append('aligner_backend', alignerBackend.value)
  fd.append('aligner_device', advanced.value.aligner_device)
  fd.append('whisperx_model', advanced.value.whisperx_model)
  fd.append('whisperx_batch_size', String(advanced.value.whisperx_batch_size))
  fd.append('nemo_model', advanced.value.nemo_model || '')
  fd.append(
    'english_word_align',
    String(processingMode.value === 'full' && englishWordAlign.value && sharedLanguage.value !== 'jpn')
  )
  // word_phoneme_map（英语单词→音素映射）与 dict_source（选择词典）彼此
  // 解耦、独立提交，见 MFAProcessor.vue 同名注释。USTX 没有 phonemes
  // 字段，后端也会对 format === 'ustx' 时强制关闭 word_phoneme_map，
  // 这里仍始终提交 dict_source（"default" 时后端走软件默认转换）。
  fd.append('word_phoneme_map', String(wordPhonemeMapEffective.value))
  fd.append('dict_source', dictSource.value)
  return fd
}

const applyBoxResult = (r: any) => {
  if (typeof r?.index !== 'number') return
  const box = boxes.value[r.index]
  if (!box) return
  if (r.status) box.status = r.status
  box.error = r.error || r.message || ''
}

const clearJobPolling = () => {
  if (jobPollTimer !== null) {
    window.clearTimeout(jobPollTimer)
    jobPollTimer = null
  }
}

const pollJob = (jobId: string): Promise<void> => {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (!pollActive) {
        resolve()
        return
      }
      try {
        const res = await fetch(`/api/pipeline/job/${jobId}`)
        const data = await res.json()
        if (!res.ok || !data.success) {
          throw new Error(data.error || t('processor.jobStatusFailed'))
        }
        const job = data.job || {}

        if (job.progress) {
          doneCount.value = job.progress.done ?? doneCount.value
          totalCount.value = job.progress.total ?? totalCount.value
          progressPercent.value = totalCount.value
            ? Math.round((doneCount.value / totalCount.value) * 100)
            : 0
        }
        if (job.last_box) applyBoxResult(job.last_box)

        if (job.status === 'done' || job.status === 'failed') {
          const result = job.result
          if (result?.boxes) {
            result.boxes.forEach((r: any) => applyBoxResult(r))
          }

          if (result?.success) {
            projectResult.value = {
              path: result.project_path,
              format: result.project_format,
              processedCount: result.processed_count,
              failedCount: result.failed_count,
              skippedCount: result.skipped_count,
              processingTime: result.processing_time,
              message: result.message,
            }
            progressPercent.value = 100
            ElMessage.success(`✅ ${t('processor.success')}`)
            resolve()
            return
          }

          reject(new Error(result?.error || job.error || t('processor.jobFailed')))
          return
        }

        jobPollTimer = window.setTimeout(tick, 1500)
      } catch (e) {
        reject(e)
      }
    }
    tick()
  })
}

const startProcessing = async () => {
  if (!boxes.value.some((b) => b.audioFile)) {
    ElMessage.warning(t('dialogue.emptyBoxesWarning'))
    return
  }
  if (
    processingMode.value === 'project-only' &&
    !boxes.value.some((b) => b.audioFile && (b.labFile || b.midiFile))
  ) {
    ElMessage.warning(t('dialogue.projectOnlyEmptyWarning'))
    return
  }
  if (boxes.value.length > MAX_BOXES) {
    ElMessage.warning(`Maximum ${MAX_BOXES} rows`)
    return
  }

  topError.value = ''
  projectResult.value = null
  boxes.value.forEach((b) => {
    b.error = ''
    if (!b.audioFile) {
      b.status = 'idle'
    } else if (processingMode.value === 'project-only' && !b.labFile && !b.midiFile) {
      // 「仅生成工程」模式下，既没有 LAB 也没有 MIDI 的对话框会被后端直接
      // 跳过（不报错中断整体），这里提前标记，避免误显示为"排队中"。
      b.status = 'skipped_empty'
    } else {
      b.status = 'queued'
    }
  })
  doneCount.value = 0
  totalCount.value = boxes.value.length
  progressPercent.value = 0
  processing.value = true
  pollActive = true

  try {
    const res = await fetch('/api/dialogue/process', { method: 'POST', body: buildFormData() })
    const data = await res.json()
    if (!res.ok || !data.success) throw new Error(data.error || t('processor.submitFailed'))

    await pollJob(data.job_id)
    // 后端一旦读取某个框的 tts_preview_id_{i} 就会立刻把那条缓存记录
    // 消费掉（无论最终对齐是否成功），所以这里统一清空所有框的
    // ttsSegmentPreviewId——避免用户不改文本、直接再点一次"开始处理"
    // 时，前端还误以为存在可复用的预览音频。
    boxes.value.forEach((b) => {
      if (b.ttsSegmentPreviewId) b.ttsSegmentPreviewId = ''
    })
  } catch (e: any) {
    topError.value = e?.message || String(e)
    ElMessage.error(`❌ ${topError.value}`)
  } finally {
    processing.value = false
    pollActive = false
    clearJobPolling()
  }
}

const stopProcessing = () => {
  pollActive = false
  clearJobPolling()
  processing.value = false
  ElMessage.info(t('dialogue.stopProcessing'))
}

const downloadResult = async () => {
  if (!projectResult.value?.path) return
  downloading.value = true
  try {
    const filename = getFileName(projectResult.value.path)
    const res = await fetch(`/api/work-dir/download/${encodeURIComponent(filename)}`)
    if (!res.ok) throw new Error(t('processor.submitFailed'))
    const blob = await res.blob()
    const url = window.URL.createObjectURL(blob)
    const el = document.createElement('a')
    el.href = url
    el.download = filename
    document.body.appendChild(el)
    el.click()
    document.body.removeChild(el)
    window.URL.revokeObjectURL(url)
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    downloading.value = false
  }
}

onMounted(() => {
  refreshStatus()
  fetchDictionaries()
  fetchNarrators()
})
</script>

<style scoped>
.dialogue-container {
  width: 100%;
}

.dialogue-card {
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

.shared-form :deep(.el-form-item) {
  margin-bottom: 18px;
}

.help-text {
  color: #909399;
  font-size: 12px;
  margin-top: 5px;
}

.option-hint {
  margin-left: 10px;
  color: #909399;
  font-size: 12px;
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
}

.dict-source-hint {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}

.folder-import-bar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
}

.box-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 16px 0;
}

.dialogue-box {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 14px;
  background: #fafbfc;
}

.box-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.box-index {
  font-weight: bold;
  color: #333;
  font-size: 13px;
}

.box-settings-btn {
  margin-left: auto;
}

.box-remove {
  margin-left: 4px;
}

.panel-label {
  font-size: 12px;
  color: #909399;
  margin-bottom: 6px;
}

.file-info {
  padding: 8px 12px;
  background: #ecf5ff;
  color: #409eff;
  border-radius: 4px;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.file-info.midi-loaded {
  background: #f0f9eb;
  color: #67c23a;
  border: 1px solid #c2e7b0;
}

.mode-help {
  margin-top: 6px;
  color: #909399;
  font-size: 12px;
  line-height: 1.5;
}

.compact-upload :deep(.el-upload-dragger) {
  padding: 14px 10px;
}

.tts-box-sliders {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}

.tts-mini-label {
  font-size: 12px;
  color: #909399;
  white-space: nowrap;
}

.box-pitch-shift {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}

.option-hint-icon {
  cursor: help;
  font-size: 12px;
  color: #909399;
}

.box-error {
  margin-top: 10px;
  padding: 8px 12px;
  background: #fef0f0;
  color: #f56c6c;
  border-radius: 4px;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.action-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 20px;
}

.disabled-text {
  color: #f56c6c;
  font-size: 12px;
}

.progress-bar {
  margin-top: 15px;
}

.result-section {
  margin-top: 10px;
}

.result-info {
  margin: 10px 0;
  padding: 15px;
  background: #f0f9ff;
  border-radius: 4px;
  border-left: 4px solid #409eff;
}

.result-info p {
  margin: 6px 0;
  color: #606266;
  font-size: 12px;
}

.error-section {
  margin-top: 20px;
}

@media (max-width: 768px) {
  .card-header {
    flex-direction: column;
    gap: 10px;
  }

  .action-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
