<template>
  <div class="help-container">
    <el-card class="help-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-title">❓ {{ t('help.pageTitle') }}</span>
        </div>
      </template>

      <p class="page-subtitle">{{ t('help.pageSubtitle') }}</p>

      <!-- ============== 快速导航 ============== -->
      <div class="anchor-nav">
        <el-button size="small" text @click="scrollTo('tutorial')">{{ t('help.navTutorial') }}</el-button>
        <el-button size="small" text @click="scrollTo('faq')">{{ t('help.navFaq') }}</el-button>
      </div>

      <!-- ============== 使用教程 ============== -->
      <div id="tutorial" class="section-heading">
        <span>📘 {{ t('help.tutorialTitle') }}</span>
      </div>

      <el-tabs v-model="activeTutorialTab" class="tutorial-tabs">
        <el-tab-pane :label="t('help.tutorialLabelOnly')" name="labelOnly">
          <ol class="step-list">
            <li>{{ t('help.tutorialLabelOnlyStep1') }}</li>
            <li>{{ t('help.tutorialLabelOnlyStep2') }}</li>
            <li>{{ t('help.tutorialLabelOnlyStep3') }}</li>
            <li>{{ t('help.tutorialLabelOnlyStep4') }}</li>
            <li>{{ t('help.tutorialLabelOnlyStep5') }}</li>
            <li>{{ t('help.tutorialLabelOnlyStep6') }}</li>
          </ol>
        </el-tab-pane>

        <el-tab-pane :label="t('help.tutorialFull')" name="full">
          <ol class="step-list">
            <li>{{ t('help.tutorialFullStep1') }}</li>
            <li>{{ t('help.tutorialFullStep2') }}</li>
            <li>{{ t('help.tutorialFullStep3') }}</li>
            <li>{{ t('help.tutorialFullStep4') }}</li>
            <li>{{ t('help.tutorialFullStep5') }}</li>
            <li>{{ t('help.tutorialFullStep6') }}</li>
          </ol>
        </el-tab-pane>

        <el-tab-pane :label="t('help.tutorialProjectOnly')" name="projectOnly">
          <ol class="step-list">
            <li>{{ t('help.tutorialProjectOnlyStep1') }}</li>
            <li>{{ t('help.tutorialProjectOnlyStep2') }}</li>
            <li>{{ t('help.tutorialProjectOnlyStep3') }}</li>
            <li>{{ t('help.tutorialProjectOnlyStep4') }}</li>
            <li>{{ t('help.tutorialProjectOnlyStep5') }}</li>
          </ol>
        </el-tab-pane>

        <el-tab-pane :label="t('help.tutorialDialogue')" name="dialogue">
          <ol class="step-list">
            <li>{{ t('help.tutorialDialogueStep1') }}</li>
            <li>{{ t('help.tutorialDialogueStep2') }}</li>
            <li>{{ t('help.tutorialDialogueStep3') }}</li>
            <li>{{ t('help.tutorialDialogueStep4') }}</li>
          </ol>
        </el-tab-pane>

        <el-tab-pane :label="t('help.tutorialDictionary')" name="dictionary">
          <ol class="step-list">
            <li>{{ t('help.tutorialDictionaryStep1') }}</li>
            <li>{{ t('help.tutorialDictionaryStep2') }}</li>
            <li>{{ t('help.tutorialDictionaryStep3') }}</li>
          </ol>
        </el-tab-pane>
      </el-tabs>

      <el-divider />

      <!-- ============== 常见问题 ============== -->
      <div id="faq" class="section-heading">
        <span>💡 {{ t('help.faqTitle') }}</span>
      </div>

      <el-input
        v-model="faqSearch"
        :placeholder="t('help.faqSearchPlaceholder')"
        clearable
        class="faq-search"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>

      <el-collapse v-model="activeFaq" class="faq-collapse" accordion>
        <el-collapse-item
          v-for="(item, idx) in filteredFaqItems"
          :key="idx"
          :name="String(idx)"
          :title="item.q"
        >
          <p class="faq-answer">{{ item.a }}</p>
        </el-collapse-item>
      </el-collapse>

      <p v-if="!filteredFaqItems.length" class="faq-empty">{{ t('help.faqNoResults') }}</p>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="help-footnote"
      >
        <template #title>
          {{ t('help.stillNeedHelp') }}
          <a href="https://github.com/liuhua520-svg/SVS-Lab-Tools/issues" target="_blank">GitHub Issues</a>
        </template>
      </el-alert>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { useAppLocale } from '../i18n'

const { t } = useAppLocale()

const activeTutorialTab = ref('labelOnly')
const activeFaq = ref('')
const faqSearch = ref('')

const scrollTo = (id: string) => {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// FAQ 条目直接从 i18n 读取问答对，key 命名为 help.faqQ1 / help.faqA1 ...
const FAQ_COUNT = 10
const faqItems = computed(() =>
  Array.from({ length: FAQ_COUNT }, (_, i) => ({
    q: t(`help.faqQ${i + 1}`),
    a: t(`help.faqA${i + 1}`),
  }))
)

const filteredFaqItems = computed(() => {
  const kw = faqSearch.value.trim().toLowerCase()
  if (!kw) return faqItems.value
  return faqItems.value.filter(
    (item) => item.q.toLowerCase().includes(kw) || item.a.toLowerCase().includes(kw)
  )
})
</script>

<style scoped>
.help-container {
  width: 100%;
}

.help-card {
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

.anchor-nav {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.section-heading {
  font-size: 15px;
  font-weight: bold;
  color: #333;
  margin: 4px 0 12px;
  scroll-margin-top: 16px;
}

.tutorial-tabs {
  margin-bottom: 4px;
}

.step-list {
  margin: 0;
  padding-left: 22px;
  color: #606266;
  font-size: 13px;
  line-height: 2;
}

.step-list li {
  margin-bottom: 4px;
}

.faq-search {
  max-width: 360px;
  margin-bottom: 14px;
}

.faq-collapse {
  border-top: none;
}

.faq-answer {
  color: #606266;
  font-size: 13px;
  line-height: 1.8;
  margin: 0;
  white-space: pre-line;
}

.faq-empty {
  color: #909399;
  font-size: 13px;
  text-align: center;
  padding: 20px 0;
}

.help-footnote {
  margin-top: 20px;
}

.help-footnote a {
  color: #4f46e5;
  margin-left: 4px;
}
</style>
