# qwen3_server.py — 已下线（2026-08）
#
# 【2026-08 架构调整】此前这个独立服务同时跑 Qwen3-ASR-1.7B 和
# Qwen3-ForcedAligner-0.6B（在独立的 .qwen3_env 里，原因是 qwen-asr
# 精确锁定的 transformers==4.57.6 与当时 .mfa_env 里 whisperx 需要的
# transformers==4.39.3 互斥，两者不能装进同一个环境）。
#
# 现在架构反过来了：
#   - WhisperX 迁出为新的独立服务 whisperx_server.py（.whisperx_env，
#     端口 5854），因为 .mfa_env 不再需要满足 whisperx 的
#     transformers 版本约束。
#   - Qwen3-ASR / Qwen3-ForcedAligner 迁入主进程 app.py（.mfa_env）内
#     本地加载，不再是独立微服务——见 alt_aligners.py 里
#     Qwen3ASRAligner / Qwen3ForcedAligner 类顶部的迁移说明。
#
# 这个文件本身不再被 launcher.py 拉起（SERVICES 列表里的 "qwen3" 条目
# 已替换为 "whisperx"），保留只是为了避免有人手动双击/运行旧的
# `python qwen3_server.py` 或旧的桌面快捷方式时得到一条难懂的
# "文件不存在" 错误——这里给出明确指引后直接退出，不做任何其它事情。
#
# 依赖清单见 requirements-qwen3.txt（同样已标注废弃）；新的依赖清单在
# requirements-whisperx.txt。发布目录下残留的 runtime/qwen3_env（或
# .qwen3_env）目录可以直接删除。

import sys

_MESSAGE = """\
qwen3_server.py 已下线（2026-08 架构调整）。

Qwen3-ASR / Qwen3-ForcedAligner 现在直接运行在主程序 app.py 进程内
（.mfa_env），不再需要单独启动这个服务——正常启动 启动器.exe 或
`python app.py` 即可，无需任何额外操作。

如果你是想要 WhisperX 独立服务，请改为运行 whisperx_server.py
（.whisperx_env，见 requirements-whisperx.txt）。
"""

if __name__ == "__main__":
    print(_MESSAGE)
    sys.exit(1)
