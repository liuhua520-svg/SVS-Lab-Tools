// ─────────────────────────────────────────────────────────────────
// 字幕嵌入（导出带字幕视频 / 音频、生成带字幕视频）
//
// 字幕识别、字幕编辑、字幕对齐三个页面共用同一套逻辑：把当前编辑区字幕
// 用 ffmpeg 封装/烧录进原始媒体，生成新文件后触发浏览器下载。
//
// 后端接口（/api/subtitle/embed、/api/subtitle/embed-video、
// /api/subtitle/job/<id>）只依赖 media_id 与 entries，与"字幕是怎么来的"
// （ASR 识别 / 强制对齐 / 导入文件）无关，所以三个页面可以直接共用。
//
// 两种模式：
//   - 'soft' : 软字幕封装（/api/subtitle/embed）。视频走原容器格式，
//              音频因容器限制统一封装成 .mka——多数播放器没问题，但
//              VLC 等在"纯音频文件"上不一定渲染字幕轨（没有画面可
//              叠加），仅推荐给熟悉播放器字幕轨切换的用户。
//   - 'burn' : 硬字幕烧录（/api/subtitle/embed-video），仅音频文件可
//              用。生成一个纯色背景 + 烧录字幕的 mp4，字幕不可关闭，
//              但保证任何播放器打开都能直接看到。
//
// 文案统一复用 i18n 里的 subtitle.* 命名空间（needUploadFirst /
// exportEmpty / embedFailed / embedSuccess），5 种语言均已齐全，所以
// 调用方不需要为自己的页面再额外维护一份同义 key。
// ─────────────────────────────────────────────────────────────────

import { ref, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useAppLocale } from '../i18n'

export type EmbedMode = 'soft' | 'burn'

interface EmbedMediaInfo {
  media_id: string
  is_video: boolean
}

interface EmbedEntry {
  start: number
  end: number
  text: string
}

export function useSubtitleEmbed(
  mediaInfo: Ref<EmbedMediaInfo | null>,
  entries: Ref<EmbedEntry[]>,
) {
  const { t } = useAppLocale()

  // 当前正在进行的嵌入模式；false 表示空闲。用于按钮 loading 态，同一时刻
  // 只允许一个嵌入任务，避免用户连点造成多个 ffmpeg 进程同时跑。
  const embedding = ref<EmbedMode | false>(false)

  // 轮询嵌入 job，直到完成/失败。与识别页的 pollRecognizeJob 完全独立，
  // 不共用进度条状态。
  const pollEmbedJob = (jobId: string): Promise<string> =>
    new Promise<string>((resolve, reject) => {
      const tick = async () => {
        try {
          const jobRes = await fetch(`/api/subtitle/job/${jobId}`)
          const jobData = await jobRes.json()
          if (!jobRes.ok || !jobData.success) {
            throw new Error(jobData.error || t('subtitle.embedFailed'))
          }

          const job = jobData.job || {}
          if (job.status === 'done') {
            const url = job.result?.download_url
            if (!url) throw new Error(t('subtitle.embedFailed'))
            resolve(url)
            return
          }
          if (job.status === 'failed') {
            reject(new Error(job.error || t('subtitle.embedFailed')))
            return
          }
          window.setTimeout(tick, 1200)
        } catch (e) {
          reject(e)
        }
      }
      tick()
    })

  const embedSubtitleIntoMedia = async (mode: EmbedMode) => {
    if (embedding.value) return
    if (!mediaInfo.value) {
      ElMessage.warning(t('subtitle.needUploadFirst'))
      return
    }
    if (!entries.value.length) {
      ElMessage.warning(t('subtitle.exportEmpty'))
      return
    }

    embedding.value = mode
    try {
      const endpoint = mode === 'burn' ? '/api/subtitle/embed-video' : '/api/subtitle/embed'
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          media_id: mediaInfo.value.media_id,
          entries: entries.value.map((e) => ({ start: e.start, end: e.end, text: e.text })),
        }),
      })
      const data = await res.json()
      if (!res.ok || !data.success) throw new Error(data.error || t('subtitle.embedFailed'))

      const downloadUrl = await pollEmbedJob(data.job_id)

      // 文件已经在服务端生成好，直接用 <a> 触发浏览器另存为即可，
      // 不需要像 exportSubtitle 那样先取文本再拼 Blob。
      const link = document.createElement('a')
      link.href = downloadUrl
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)

      ElMessage.success(`✅ ${t('subtitle.embedSuccess')}`)
    } catch (e: any) {
      ElMessage.error(`❌ ${e?.message || String(e)}`)
    } finally {
      embedding.value = false
    }
  }

  return { embedding, embedSubtitleIntoMedia }
}
