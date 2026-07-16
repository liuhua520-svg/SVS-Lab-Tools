# Qwen3-TTS 接入说明

按照思路图，给"TTS跟读"的"选择 TTS"新增了 **Qwen3-TTS** 引擎，独立虚拟环境 + 独立微服务（端口 5003），与 Qwen3-ASR（5001）、NeMo-FA（5002）同一套模式。三种官方模式全部实现：**Custom Voice**（预设音色+可选风格指令）、**Voice Design**（仅文本描述音色）、**Voice Clone**（导入参考音频克隆，支持 x-vector-only）。

## 部署步骤

```bash
cd backend
conda create --prefix ../.qwen3tts_env python=3.12 -y
conda activate ../.qwen3tts_env
pip install -r requirements-qwen3tts.txt
python qwen3tts_server.py
```

首次调用某个模式（Custom Voice / Voice Design / Voice Clone）时会从 HuggingFace 自动下载对应 checkpoint（走设置页面里已有的镜像/离线配置）。

## 改动清单

**新增**
- `backend/qwen3tts_server.py` — 独立微服务，端口 5003。三个模式各自懒加载、按 (mode, size, device) 缓存，GPU OOM 自动降级到 CPU（与 qwen3_server.py 同一套逻辑）。
- `backend/requirements-qwen3tts.txt` — 独立环境依赖清单。

**修改**
- `backend/tts_processor.py` — 注册 `qwen3_tts` 引擎；新增 HTTP 客户端调用三个 `/generate/*` 端点；讲述人预设（语音预设）扩展了 mode/instruct/ref_text/x_vector_only/ref_audio_path 字段，Voice Clone 的参考音频落盘持久化到 `settings/tts_narrator_ref_audio/`。
- `backend/app.py` — `/api/tts/preview`、`/api/tts/synthesize_preview`、`/api/tts/process`、`/api/tts/narrators` 均支持 `qwen3_tts_options`；设置保存后会一并尝试重启 Qwen3-TTS 微服务。
- `backend/app_settings.py` — 新增 `qwen3_tts_model_size`（1.7B/0.6B，两者都注册，设置页选择）、`qwen3_tts_x_vector_only_default`、`skip_start_qwen3tts_server`。
- `src/components/MFAProcessor.vue` — TTS跟读主面板 + "语音预设管理"弹窗均按照思路图新增了模式切换（Custom Voice / Voice Design / Voice Clone）、模型规模选择、风格指令输入、参考音频上传、x-vector 开关；EdgeTTS/讲述人的语速滑块在 Qwen3-TTS 模式下自动隐藏（Qwen3-TTS 的表现力由自然语言指令控制，没有数值滑块）。
- `src/components/SettingsPage.vue` — 新增 Qwen3-TTS 设置分区（默认模型规模、Voice Clone 默认 x-vector 开关、"下次不启动"开关）。
- `src/i18n.ts` — 5 个语言全部补齐了新增文案（已核对：所有 `t('processor.*')` / `t('settings.*')` 调用点在 5 个 locale 里均已存在对应 key）。

## 已做的验证

- 所有改动的 `.py` 文件 `py_compile` 通过。
- `qwen3tts_server.py` 用 mock 依赖做过路由级冒烟测试：健康检查、9 个预设音色列表、三个 `/generate/*` 的参数校验（缺 text / 缺 speaker / 缺 instruct / 缺参考音频 / 缺参考文本）、以及一次"参数校验通过 → 正确解析出 model_id → 正确选择 CPU 设备 → 到达模型加载"的完整链路。
- 所有改动的 `.vue` 文件 + `i18n.ts` 用 `@vue/compiler-sfc` 做过 SFC 解析 + script 编译 + template 编译，并用真实的 `vue`/`element-plus`/`vue-i18n` 类型声明跑过 `tsc --noEmit` 全量类型检查，零报错。

## 尚未做但可能需要关注的点

- `qwen3-tts` 这个 pip 包目前处于官方仓库刚发布阶段，具体的包名/版本号建议实际 `pip install` 时再核实一次（`requirements-qwen3tts.txt` 里已按 GitHub README 写的 `qwen-tts`，如果 PyPI 上实际名称不同需要相应调整）。
- Voice Design 模式官方暂时只有 1.7B 权重，代码里已经做了"选 0.6B 自动回退到 1.7B"的处理，但如果后续官方发布了 0.6B 权重，`qwen3tts_server.py` 里 `MODEL_IDS["voice_design"]["0.6B"]` 这一行需要更新成真实的 0.6B 模型 ID。
- DialogueBatch.vue（多轨道批量跟读）后端已经支持透传 `qwen3_tts_options`，但前端 UI 部分本次未改动（按你的思路图，本次重点是单文件"TTS跟读"面板）。如果后续需要多轨道场景也支持 Qwen3-TTS，可以照搬 MFAProcessor.vue 里的 UI 模式挂到 DialogueBatch.vue 上。
