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

      <!-- ============== 共用高级设置（与单文件处理页面语义一致） ============== -->
      <el-form label-position="top" class="shared-form">
        <!-- 以下几项（对齐后端 / 对齐运行设备 / NeMo 模型覆盖 / 语言）仅在
             "完整处理"模式下生效——该模式需要执行对齐；"仅生成工程"模式
             跳过对齐，直接使用已提供的 LAB / MIDI，故隐藏这些设置。 -->
        <el-form-item v-if="processingMode === 'full'" :label="t('processor.backendLabel')">
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

        <el-form-item v-if="processingMode === 'full' && alignerBackend !== 'mfa'" :label="t('processor.alignDevice')">
          <el-radio-group v-model="advanced.aligner_device" :disabled="processing">
            <el-radio value="auto">{{ t('processor.deviceAuto') }}</el-radio>
            <el-radio value="cpu">{{ t('processor.deviceCpu') }}</el-radio>
            <el-radio value="cuda">{{ t('processor.deviceCuda') }}</el-radio>
          </el-radio-group>
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
          <el-radio-group v-model="processingMode" :disabled="processing">
            <el-radio value="full">{{ t('dialogue.processingModeFull') }}</el-radio>
            <el-radio value="project-only">{{ t('dialogue.processingModeProjectOnly') }}</el-radio>
          </el-radio-group>
          <div class="mode-help">
            <small v-if="processingMode === 'full'">{{ t('dialogue.processingModeFullHint') }}</small>
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
                  <el-input-number v-model="advanced.f0_smooth_window" :min="1" :max="99" :step="2" controls-position="right" :disabled="processing || !advanced.export_pitch_line" />
                  <span class="help-text">{{ t('processor.smoothWindowTip') }}</span>
                </el-form-item>
              </el-col>
            </el-row>

            <el-row v-if="outputFormat === 'vsqx' && advanced.f0_smooth" :gutter="20">
              <el-col :xs="24" :sm="12">
                <el-form-item :label="t('processor.vsqxPitchSmoothWindow')">
                  <el-input-number v-model="advanced.vsqx_pitch_smooth_window" :min="1" :max="99" :step="2" controls-position="right" :disabled="processing || !advanced.export_pitch_line" />
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
          <template v-if="!boxes.some((b) => b.audioFile)">
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
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

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
  nemo_model: string
  crepe_model: 'full' | 'tiny'
  precision: 'single' | 'double'
  f0_smooth: boolean
  f0_smooth_window: number
  vsqx_pitch_smooth_window: number
  f0_floor: number
  f0_ceil: number
}

// ============== 共用设置状态 ==============
const alignerBackend = ref<string>('mfa')
const alignerStatus = ref<Record<string, any>>({})
const checkingStatus = ref(false)

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

// ============== 对话框列表 ==============
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
})

const boxes = ref<DialogueBox[]>([createBox()])

const MAX_BOXES = 64

const addBox = () => {
  if (boxes.value.length >= MAX_BOXES) {
    ElMessage.warning(`Maximum ${MAX_BOXES} rows`)
    return
  }
  boxes.value.push(createBox())
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
  if (!boxes.value.some((b) => b.audioFile)) return true
  if (processingMode.value === 'project-only') {
    return !boxes.value.some((b) => b.audioFile && (b.labFile || b.midiFile))
  }
  return false
})

const buildFormData = (): FormData => {
  const fd = new FormData()
  fd.append('box_count', String(boxes.value.length))
  boxes.value.forEach((box, i) => {
    if (box.text.trim()) fd.append(`text_${i}`, box.text)
    if (box.audioFile) fd.append(`audio_${i}`, box.audioFile)
    // LAB 优先级高于 MID：同一个对话框里两者互斥（单一文件槽位），
    // 因此这里最多只会有其中一个被提交。
    if (box.labFile) fd.append(`lab_${i}`, box.labFile)
    else if (box.midiFile) fd.append(`mid_${i}`, box.midiFile)
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
  fd.append('whisperx_model', advanced.value.whisperx_model)
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

.box-remove {
  margin-left: auto;
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
