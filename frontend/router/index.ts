import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

// 路由懒加载：每个页面组件按需加载，减小首屏 bundle 体积
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'aligner',
    component: () => import('../components/MFAProcessor.vue'),
  },
  {
    path: '/dictionary',
    name: 'dictionary',
    component: () => import('../components/DictionaryManager.vue'),
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('../components/SettingsPage.vue'),
  },
  {
    path: '/dialogue',
    name: 'dialogue',
    component: () => import('../components/DialogueBatch.vue'),
  },
  {
    path: '/subtitle',
    name: 'subtitle',
    component: () => import('../components/SubtitleRecognizer.vue'),
  },
  {
    path: '/about',
    name: 'about',
    component: () => import('../components/AboutPage.vue'),
  },
  {
    path: '/help',
    name: 'help',
    component: () => import('../components/HelpPage.vue'),
  },
  {
    path: '/english-g2p',
    name: 'englishG2P',
    component: () => import('../components/EnglishG2P.vue'),
  },
  {
    // 未知路径统一回退到主页（对齐处理页）
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
