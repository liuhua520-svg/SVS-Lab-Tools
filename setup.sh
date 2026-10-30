#!/bin/bash
# SVS Lab Tools 完整一键安装脚本 (Linux/Mac)

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m'

# 脚本配置
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/backend/venv"
REQ_FILE="$SCRIPT_DIR/backend/requirements.txt"
PYTHON_MIN_VERSION="3.8"
NODE_MIN_VERSION="16"

# MFA语言配置
declare -A LANGUAGE_MODELS=(
    ["cmn"]="中文普通话"
    ["eng"]="英语"
    ["jpn"]="日语"
    ["kor"]="韩语"
    ["yue"]="粤语"
)

# 日志函数
log_section() {
    echo ""
    echo -e "${MAGENTA}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${MAGENTA}║${NC} $1"
    echo -e "${MAGENTA}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

log_step() { echo -e "${BLUE}➜${NC} $1"; }
log_ok() { echo -e "${GREEN}✓${NC} $1"; }
log_warn() { echo -e "${YELLOW}!${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }
log_info() { echo -e "${CYAN}ℹ${NC} $1"; }

version_ge() {
    [ "$(printf '%s\n' "$1" "$2" | sort -V | head -n 1)" = "$2" ]
}

command_exists() {
    command -v "$1" &> /dev/null
}

confirm() {
    local prompt="$1"
    local response
    while true; do
        read -p "$(echo -e "${BLUE}➜${NC} $prompt [y/n/all]: ")" -r response
        case "$response" in
            [yY]) return 0 ;;
            [nN]) return 1 ;;
            [aA][lL][lL]) return 2 ;;
            *) log_warn "请输入 y, n 或 all" ;;
        esac
    done
}

clear
cat << "EOF"

╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║            🚀 SVS Lab Tools 完整安装程序 (Linux/Mac)                       ║
║                                                                              ║
║  本脚本将自动完成以下步骤:                                                   ║
║    ✓ 检查系统依赖 (Python, Node.js)                                          ║
║    ✓ 创建虚拟环境并安装 requirements.txt 依赖                                ║
║    ✓ 安装并构建 Vue 前端                                                     ║
║    ✓ 交互式选择语言模型并下载                                                ║
║    ✓ (可选) 创建独立环境并安装 NeMo Forced Aligner                           ║
║    ✓ (可选) 创建独立环境并安装 Qwen3-ASR/ForcedAligner                       ║
║    ✓ (可选) 创建独立环境并安装 Qwen3-TTS                                     ║
║                                                                              ║
║  预计耗时: 15-30 分钟 (取决于网络和模型大小)                                 ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝

EOF

read -p "按 Enter 键开始安装..." -r

# ─────────────────────────────────────────────────────────────────────────
# Step 1: 检查系统依赖
# ─────────────────────────────────────────────────────────────────────────
log_section "Step 1: 检查系统依赖"

log_step "检查 Python3..."
if ! command_exists python3; then
    log_error "未安装 Python3。请先安装 Python 3.8+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
if ! version_ge "$PYTHON_VERSION" "$PYTHON_MIN_VERSION"; then
    log_error "Python 版本过低 (当前: $PYTHON_VERSION, 需要: ≥$PYTHON_MIN_VERSION)"
    exit 1
fi
log_ok "Python $PYTHON_VERSION"

log_step "检查 pip3..."
if ! command_exists pip3; then
    log_error "未安装 pip3"
    exit 1
fi
log_ok "pip $(pip3 --version | awk '{print $2}')"

log_step "检查 Node.js..."
if ! command_exists node; then
    log_error "未安装 Node.js，前端构建需要 npm"
    exit 1
fi

NODE_VERSION=$(node --version | sed 's/^v//')
if ! version_ge "$NODE_VERSION" "$NODE_MIN_VERSION"; then
    log_warn "Node.js 版本较低 (当前: $NODE_VERSION, 推荐: ≥$NODE_MIN_VERSION)"
fi
log_ok "Node.js $NODE_VERSION"
log_ok "npm $(npm --version)"

# ─────────────────────────────────────────────────────────────────────────
# Step 2: 创建虚拟环境
# ─────────────────────────────────────────────────────────────────────────
log_section "Step 2: 创建 Python 虚拟环境"

if [ -d "$VENV_DIR" ]; then
    log_warn "虚拟环境已存在: $VENV_DIR"
    read -p "是否删除并重新创建? [y/n]: " -r response
    if [[ "$response" =~ ^[yY]$ ]]; then
        log_step "删除旧虚拟环境..."
        rm -rf "$VENV_DIR"
        log_ok "已删除"
    else
        log_info "使用现有虚拟环境"
        goto_skip_venv=1
    fi
fi

if [ -z "$goto_skip_venv" ]; then
    log_step "创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
    log_ok "虚拟环境已创建"
fi

log_step "激活虚拟环境..."
source "$VENV_DIR/bin/activate"
log_ok "虚拟环境已激活"

# ─────────────────────────────────────────────────────────────────────────
# Step 2.5: 安装 kaldi (MFA 强制对齐的二进制依赖)
# ─────────────────────────────────────────────────────────────────────────
# requirements.txt 里的 montreal-forced-aligner 走的是 pip 安装（见下面
# Step 3），pip 版只是纯 Python 胶水代码，运行时需要调用 kaldi 的编译
# 二进制。这里不用 `conda install -c conda-forge montreal-forced-aligner`
# 这个官方完整包，是因为它在部分平台上会连带装一堆图形渲染相关依赖
# (pango/cairo/gdk-pixbuf，通常是给 fstdraw 之类的可视化子命令用的)，
# 装起来体积更大也更容易因为环境问题报错；而只装 kaldi 本身就是纯二进制，
# 不含任何图形依赖，装不装图形组件完全不影响核心对齐流程。
log_section "Step 2.5: 安装 kaldi"

# 【修复】.kaldi_env 必须和上面 $VENV_DIR 用同一个 Python 大版本
# （X.Y，如 3.10）。kalpy 会编译出一个和安装它的 Python 版本绑定的二进制
# 扩展模块（如 _kalpy.cpython-310-x86_64-linux-gnu.so），如果不显式给
# `conda create` 指定 python=X.Y，conda 会按 conda-forge 当前最新的 Python
# 版本解析依赖，很可能和 $VENV_DIR 的版本对不上。版本一旦不匹配，
# $VENV_DIR 里的 Python 解释器加载不了这个 ABI 不同的 .so，
# `import _kalpy` 会报 ModuleNotFoundError——文件确实存在、PYTHONPATH 也
#指对了，但 Python 的 import 机制扫描到 ABI tag 不匹配的扩展模块时会
# 直接跳过，不会给出更明确的 ABI 不匹配提示，非常容易被误判成"没装"。
VENV_PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log_info "虚拟环境 Python 版本: $VENV_PYTHON_VERSION（.kaldi_env 将匹配此版本）"

if command_exists conda; then
    log_step "检测到 conda，通过 conda-forge 安装 kaldi 到 .kaldi_env..."
    KALDI_ENV_DIR="$SCRIPT_DIR/.kaldi_env"
    if [ ! -d "$KALDI_ENV_DIR" ]; then
        conda create -y -p "$KALDI_ENV_DIR" -c conda-forge "python=$VENV_PYTHON_VERSION" kaldi
    else
        log_info "kaldi 环境已存在，跳过创建: $KALDI_ENV_DIR"
    fi
    KALDI_BIN_DIR="$KALDI_ENV_DIR/bin"
    if [ -d "$KALDI_BIN_DIR" ]; then
        export PATH="$KALDI_BIN_DIR:$PATH"
        log_ok "kaldi 已安装，并已加入 PATH: $KALDI_BIN_DIR"
        log_info "提示：run.sh / 启动脚本里也需要把这个路径加入 PATH，否则 MFA 运行时找不到 kaldi"
    else
        log_warn "kaldi 环境创建后未找到 bin 目录，请手动检查 $KALDI_ENV_DIR"
    fi

    # ─────────────────────────────────────────────────────────────────────
    # 【修复】kaldi 和 kalpy 是 conda-forge 上两个独立的包：kaldi 只提供裸的
    # Kaldi 可执行文件/共享库本身，kalpy 才是真正给 montreal-forced-aligner
    # 调用的 pybind11 绑定，提供 _kalpy / kalpy 这两个 Python 模块。此前这
    # 一步只装了 kaldi，没装 kalpy，导致 mfa align / mfa version 等命令
    # 一律报 "ModuleNotFoundError: No module named '_kalpy'"——不是共享库
    # 加载失败，是这个包压根没被装进任何环境。
    # 装进同一个 .kaldi_env（而不是 venv），是因为：
    #   1) kalpy 依赖 kaldi 的共享库，装在同一个 conda 前缀里最省心，conda
    #      会自动处理好两者的版本匹配；
    #   2) 主环境 $VENV_DIR 是纯 pip venv（见 Step 3），刻意不通过 conda
    #      装完整 MFA 包以避免 GDK/pango 图形依赖，kalpy 单独装进
    #      .kaldi_env 不会触发这些依赖；
    #   3) mfa_utils.py 的 build_kaldi_subprocess_env() 会在启动 mfa 相关
    #      子进程时把 .kaldi_env 的 site-packages 目录通过 PYTHONPATH 传给
    #      主 venv 的 Python，让它能找到装在 .kaldi_env 里的 kalpy。
    log_step "在 .kaldi_env 中安装 kalpy（MFA 的 Kaldi Python 绑定）..."
    if conda install -y -p "$KALDI_ENV_DIR" -c conda-forge kalpy; then
        log_ok "kalpy 已安装到独立环境: $KALDI_ENV_DIR"
    else
        log_error "kalpy 安装失败，请检查上方报错信息（网络问题居多，可重跑本脚本）。"
        log_info "也可稍后手动执行： conda install -y -p \"$KALDI_ENV_DIR\" -c conda-forge kalpy"
        exit 1
    fi

    # 【安全检查】如果 .kaldi_env 是之前某次跑坏的残留（比如之前没有
    # python 版本锁定，装出来的是别的 Python 版本），即使上面 kalpy
    # 装"成功"了，ABI 也可能和 $VENV_DIR 对不上。这里用 find 直接核对
    # _kalpy 扩展模块文件名里的 cpython 版本标签，尽早暴露问题而不是等
    # 到用户实际跑对齐任务时才在日志里看到一头雾水的 ModuleNotFoundError。
    KALDI_SITE_PACKAGES=$(find "$KALDI_ENV_DIR/lib" -maxdepth 1 -type d -name "python3.*" 2>/dev/null | head -n 1)
    if [ -n "$KALDI_SITE_PACKAGES" ]; then
        KALPY_SO=$(find "$KALDI_SITE_PACKAGES/site-packages" -maxdepth 1 -name "_kalpy*.so" 2>/dev/null | head -n 1)
        if [ -n "$KALPY_SO" ]; then
            KALPY_SO_TAG=$(basename "$KALPY_SO" | grep -oE 'cpython-3[0-9]+' | head -n 1)
            VENV_TAG="cpython-$(echo "$VENV_PYTHON_VERSION" | tr -d '.')"
            if [ -n "$KALPY_SO_TAG" ] && [ "$KALPY_SO_TAG" != "$VENV_TAG" ]; then
                log_error "检测到 ABI 不匹配：_kalpy 是为 ${KALPY_SO_TAG} 编译的，"
                log_error "但主虚拟环境是 Python ${VENV_PYTHON_VERSION}（${VENV_TAG}）。"
                log_info "这通常是 .kaldi_env 在旧版脚本下创建、未锁定 Python 版本导致的。"
                log_info "请删除 .kaldi_env 后重跑本脚本： rm -rf \"$KALDI_ENV_DIR\""
                exit 1
            fi
        fi
    fi

    # ─────────────────────────────────────────────────────────────────────
    # 【修复】montreal_forced_aligner/data.py 在 import 时会无条件
    # `import pynini`——pynini 是 OpenFst/OpenGrm 的 C++ 绑定，和 kaldi/
    # kalpy 一样只通过 conda-forge 分发预编译二进制，PyPI 上没有可用的
    # 通用 wheel（Windows/部分平台压根没有，Linux 上即使有也大多是走
    # manylinux 的旧版本，跟本项目固定的 Python 版本/依赖组合不一定兼容），
    # 之前这一步只装了 kaldi 和 kalpy，漏掉了 pynini，表现为运行 mfa align
    # 时报 "ModuleNotFoundError: No module named 'pynini'"。装进同一个
    # .kaldi_env（而不是单独再开一个环境）：
    #   1) 和 kalpy 一样需要链接同一套 OpenFst 共享库，装在同一个 conda
    #      前缀里 conda 会自动处理好版本匹配；
    #   2) backend/mfa_utils.py 的 build_kaldi_subprocess_env() /
    #      _kalpy_shim_dir() 已经把 .kaldi_env 里 pynini/pywrapfst 对应的
    #      文件一并纳入了传给主 venv 子进程的路径，不需要额外的注入逻辑。
    # pynini 目前只在部分语言的 G2P/文本规整路径上用到，不是 mfa align
    # 主流程的强依赖，所以这里装失败时只警告、不中断整个安装。
    log_step "在 .kaldi_env 中安装 pynini（MFA 的 G2P/文本规整依赖）..."
    if conda install -y -p "$KALDI_ENV_DIR" -c conda-forge pynini; then
        log_ok "pynini 已安装到独立环境: $KALDI_ENV_DIR"
    else
        log_warn "pynini 安装失败（非致命，不中断安装）。部分语言的文本规整/G2P"
        log_warn "功能可能会报 ModuleNotFoundError: No module named 'pynini'。"
        log_info "可稍后手动执行： conda install -y -p \"$KALDI_ENV_DIR\" -c conda-forge pynini"
    fi
else
    log_warn "未检测到 conda，无法自动安装 kaldi / kalpy / pynini。"
    log_info "请先安装 Miniconda/Miniforge 后重跑本脚本，或手动通过发行版包管理器安装 kaldi 并确保其在 PATH 中"
    log_info "（kalpy / pynini 目前只通过 conda-forge 分发预编译二进制，强烈建议使用 conda 安装）"
fi

# ─────────────────────────────────────────────────────────────────────────
# Step 3: 安装所有 Python 依赖
# ─────────────────────────────────────────────────────────────────────────
log_section "Step 3: 安装 Python 依赖"

if [ ! -f "$REQ_FILE" ]; then
    log_error "找不到 requirements.txt 文件: $REQ_FILE"
    exit 1
fi

log_step "升级 pip/setuptools/wheel..."
pip install --upgrade pip setuptools wheel > /dev/null 2>&1
log_ok "已升级"

log_step "根据 requirements.txt 安装核心依赖 (含 pip 版 montreal-forced-aligner)..."
log_info "这可能需要几分钟，取决于您的网络环境..."

if pip install -r "$REQ_FILE"; then
    log_ok "所有 Python 依赖已成功安装"
else
    log_error "依赖安装失败，请查看上方的报错信息"
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────
# Step 3.5: 部署 speechbrain 路径分隔符 bug 补丁
# ─────────────────────────────────────────────────────────────────────────
# 【修复】speechbrain（MFA 通过 alt_aligners.py 用到的可选依赖）有一个懒
# 加载模块判断路径分隔符时假设了错误格式的 bug，会导致 MFA 跑 MFCC 特征
# 提取时，一次纯粹的内部 inspect.stack() 探测被错误地升级成真实的 import
# 尝试，进而因为 k2/flair 等未安装的可选依赖直接报错中断对齐任务（表现为
# "ModuleNotFoundError: No module named 'k2'" 或类似信息，MFA 自己的报错
# 文案会提示"改用 Python 3.12"或"装 standard-aifc standard-sunau"，这两条
# 都是误导，跟真正原因无关，照做也解决不了问题）。详见
# backend/mfa_env_sitecustomize.py 文件头部注释。
#
# sitecustomize.py 是 CPython 标准钩子，只要放在 site-packages 根目录下
# 就会在该环境每次启动时自动生效，不需要 MFA 或任何调用方代码显式 import
# 它，因此可以在这里"部署一次，之后 MFA 每次跑对齐都自动生效"。
#
# 【关键】定位主 venv 的 site-packages 目录不能简单假设固定相对路径
# （不同发行版/操作系统的 venv 布局不完全一致，比如 Debian/Ubuntu 系统
# Python 有时会用 dist-packages，某些系统还会有 lib64 而不是 lib），也
# 不能像 Windows 版脚本最初那样直接取 site.getsitepackages()[0]——那个
# 实测在部分平台/Python 构建下返回的是环境前缀本身而不是 site-packages
# 子目录（同一个 bug 在 Windows 版 setup.bat 上曾导致 sitecustomize.py
# 被复制到 .mfa_env 根目录而不是 site-packages，等于完全没生效，且没有
# 任何报错提示）。改用 sysconfig.get_paths()["purelib"]——这是标准库里
# 专门用来精确回答"这个 Python 解释器的纯 Python 第三方包应该装在哪"的
# API，跟 pip 实际安装目标目录用的是同一套逻辑，不存在这种歧义。
log_section "Step 3.5: 部署 speechbrain 路径分隔符 bug 补丁"

MFA_SITECUSTOMIZE_SRC="$SCRIPT_DIR/backend/mfa_env_sitecustomize.py"

if [ ! -f "$MFA_SITECUSTOMIZE_SRC" ]; then
    log_warn "未找到 backend/mfa_env_sitecustomize.py，跳过补丁部署"
else
    MFA_SITE_PACKAGES=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)
    # 【关键修复，与 Windows 版同源】仅"路径存在"不足以说明这就是正确的
    # site-packages 目录——之前用 site.getsitepackages()[0] 时，拿到的
    # 错误路径（环境根目录）同样是真实存在的目录，纯粹的存在性检查完全
    # 拦不住这个问题。这里额外校验路径末级目录名必须是 site-packages，
    # 不满足就当作获取失败处理，避免同类"看似合法但语义不对"的路径
    # 蒙混过关。
    MFA_SITE_PACKAGES_LEAF=$(basename "$MFA_SITE_PACKAGES" 2>/dev/null)

    if [ -z "$MFA_SITE_PACKAGES" ]; then
        log_warn "未能定位虚拟环境的 site-packages 目录，跳过 speechbrain 补丁部署"
        log_info "（不影响大部分功能，只有在 MFA 报 k2/flair 相关 ImportError 时才需要它）"
    elif [ ! -d "$MFA_SITE_PACKAGES" ]; then
        log_warn "获取到的 site-packages 路径不存在，跳过补丁部署：$MFA_SITE_PACKAGES"
    elif [ "$MFA_SITE_PACKAGES_LEAF" != "site-packages" ]; then
        log_warn "获取到的路径末级目录名不是 site-packages，跳过补丁部署，避免装错位置：$MFA_SITE_PACKAGES"
        log_info "（可能是 sysconfig.get_paths 在这套 Python 组合下返回了非预期的路径，可手动确认后重试）"
    else
        if cp -f "$MFA_SITECUSTOMIZE_SRC" "$MFA_SITE_PACKAGES/sitecustomize.py"; then
            # 【关键修复，与 Windows 版同源】cp 的退出码是 0（"成功"）不
            # 代表目标文件真的落盘到了预期位置——加一道二次验证，不满足
            # 就明确报错而不是盲目打印成功提示。
            if [ -f "$MFA_SITE_PACKAGES/sitecustomize.py" ]; then
                log_ok "speechbrain 路径分隔符补丁已部署到 site-packages: $MFA_SITE_PACKAGES"
            else
                log_warn "cp 命令返回成功但目标文件未出现在磁盘上，请手动确认: $MFA_SITE_PACKAGES/sitecustomize.py"
            fi
        else
            log_warn "speechbrain 补丁部署失败（非致命，跳过）"
        fi
    fi
fi

# ─────────────────────────────────────────────────────────────────────────
# Step 4: 安装前端依赖并构建
# ─────────────────────────────────────────────────────────────────────────
log_section "Step 4: 安装并构建前端"

cd "$SCRIPT_DIR/frontend" || exit 1

if [ ! -f "package.json" ]; then
    log_error "未找到 frontend/package.json"
    exit 1
fi

log_step "安装 npm 包..."
npm install --legacy-peer-deps > /dev/null 2>&1
if [ $? -ne 0 ]; then
    log_error "npm 依赖安装失败"
    exit 1
fi
log_ok "npm 依赖已安装"

log_step "构建前端应用..."
npm run build > /dev/null 2>&1
if [ $? -ne 0 ]; then
    log_error "前端构建失败"
    exit 1
fi
log_ok "前端已构建"

cd "$SCRIPT_DIR" || exit 1

# ─────────────────────────────────────────────────────────────────────────
# Step 5: 语言模型配置
# ─────────────────────────────────────────────────────────────────────────
log_section "Step 5: 下载 MFA 语言模型"

log_info "支持的语言:"
echo ""
for lang in cmn eng jpn kor yue; do
    lang_name="${LANGUAGE_MODELS[$lang]}"
    printf "  %-8s - %s\n" "$lang" "$lang_name"
done
echo ""

log_info "说明:"
echo "  • 输入 y     - 下载该语言的预训练模型"
echo "  • 输入 n     - 跳过该语言"
echo "  • 输入 all   - 下载所有剩余语言"
echo ""

INSTALL_ALL=false
SELECTED_LANGS=()

for lang in cmn eng jpn kor yue; do
    lang_name="${LANGUAGE_MODELS[$lang]}"
    
    if [ "$INSTALL_ALL" = true ]; then
        response=0
    else
        # 【修复】confirm() 在用户选 n 时会 return 1，脚本顶部开启了
        # `set -e`；confirm 这行如果单独作为一条语句调用（不在 if/while
        # 等会被检查返回值的上下文里），只要它返回非零，set -e 会立刻让
        # 整个脚本终止且不打印任何报错——用户选 n 跳过某个语言，安装
        # 流程会在这里悄悄整个退出，后面所有语言、以及 Step 6/7/8 都不会
        # 再被问到。用 `confirm ... || response=$?` 让非零返回值被显式
        # 捕获掉而不是被 set -e 当成脚本级错误；response 提前初始化为 0，
        # 兜底 confirm 返回 0（用户选 y）时 `||` 右边不会执行、变量仍需
        # 有值可用的情况。
        response=0
        confirm "下载 $lang ($lang_name) 的预训练模型?" || response=$?
    fi
    
    case $response in
        0)
            log_ok "选择 $lang"
            SELECTED_LANGS+=("$lang")
            ;;
        2)
            log_ok "选择所有剩余语言"
            INSTALL_ALL=true
            SELECTED_LANGS+=("$lang")
            ;;
        1)
            log_warn "跳过 $lang"
            ;;
    esac
done

echo ""

if [ ${#SELECTED_LANGS[@]} -eq 0 ]; then
    log_warn "未选择任何语言模型"
    log_info "可后续手动下载。"
else
    for lang in "${SELECTED_LANGS[@]}"; do
        lang_name="${LANGUAGE_MODELS[$lang]}"
        log_step "下载 $lang ($lang_name) 预训练模型..."
        
        python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/backend')
try:
    from mfa_utils import MFAChecker
    success, msg = MFAChecker.download_model('$lang')
    sys.exit(0 if success else 1)
except Exception:
    sys.exit(1)
"
        if [ $? -eq 0 ]; then
            log_ok "$lang 模型已下载"
        else
            log_warn "$lang 模型下载失败（请检查网络后重试）"
        fi
    done
fi

# ─────────────────────────────────────────────────────────────────────────
# Step 6: NeMo Forced Aligner 独立环境（可选）
# ─────────────────────────────────────────────────────────────────────────
log_section "Step 6: NeMo Forced Aligner 独立环境 (可选)"

log_info "NeMo Forced Aligner 是一个可选的对齐后端 (NVIDIA CTC 强制对齐)。"
log_info "由于 nemo_toolkit 对 packaging/fsspec/omegaconf/hydra-core/lightning"
log_info "等核心依赖有严格版本限制，与主环境一起安装会产生依赖冲突，"
log_info "因此它需要运行在独立的 Python 环境里，作为一个本地服务 (端口 5002)"
log_info "供主后端通过 HTTP 调用。不安装也完全不影响 MFA / WhisperX / Qwen3 后端使用。"
echo ""

NEMO_VENV_DIR="$SCRIPT_DIR/.nemo_env"

# 【修复】同上：confirm() 单独一行调用，用户选 n 时 return 1 会被顶部的
# `set -e` 当成脚本级错误，整个安装流程在这里悄悄终止，Step 7/8 和最后的
# 完成提示都不会执行、也不会有任何报错信息。用 `|| nemo_install_choice=$?`
# 显式捕获返回值，不让 set -e 有机会介入。
nemo_install_choice=0
confirm "是否现在创建独立环境并安装 NeMo Forced Aligner?" || nemo_install_choice=$?

if [ $nemo_install_choice -eq 0 ] || [ $nemo_install_choice -eq 2 ]; then
    if [ -d "$NEMO_VENV_DIR" ]; then
        log_warn "NeMo 环境已存在: $NEMO_VENV_DIR"
        read -p "是否删除并重新创建? [y/n]: " -r nemo_response
        if [[ "$nemo_response" =~ ^[yY]$ ]]; then
            log_step "删除旧 NeMo 环境..."
            rm -rf "$NEMO_VENV_DIR"
            log_ok "已删除"
        else
            log_info "使用现有 NeMo 环境"
            skip_nemo_venv_create=1
        fi
    fi

    if [ -z "$skip_nemo_venv_create" ]; then
        log_step "创建 NeMo 独立虚拟环境..."
        python3 -m venv "$NEMO_VENV_DIR"
        log_ok "NeMo 虚拟环境已创建: $NEMO_VENV_DIR"
    fi

    log_step "在独立环境中安装 nemo_toolkit[asr]（体积较大，可能需要较长时间）..."
    if "$NEMO_VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel > /dev/null 2>&1 && \
       "$NEMO_VENV_DIR/bin/python" -m pip install "nemo_toolkit[asr]>=2.7.0,<2.8.0" flask; then
        log_ok "NeMo Forced Aligner 依赖已安装"
        log_info "首次启动 nemo_server.py 时会按所选语言自动下载模型权重（数百 MB ~ 1GB）"
    else
        log_warn "NeMo 依赖安装失败，可稍后手动执行："
        log_warn "  $NEMO_VENV_DIR/bin/python -m pip install \"nemo_toolkit[asr]>=2.7.0,<2.8.0\" flask"
    fi
else
    log_info "已跳过，可后续手动运行以下命令安装："
    log_info "  python3 -m venv .nemo_env"
    log_info "  .nemo_env/bin/python -m pip install \"nemo_toolkit[asr]>=2.7.0,<2.8.0\" flask"
fi

# ─────────────────────────────────────────────────────────────────────────
# Step 7: WhisperX 独立环境（可选）
# ─────────────────────────────────────────────────────────────────────────
log_section "Step 7: WhisperX 独立环境 (可选)"

log_info "WhisperX 是可选的强制对齐后端，作为一个本地服务 (端口 5854) 供主"
log_info "后端通过 HTTP 调用。不安装不影响 MFA / Qwen3-ASR / Qwen3-FA / NeMo"
log_info "后端使用。依赖见 backend/requirements-whisperx.txt (Python 3.10)。"
echo ""

WHISPERX_VENV_DIR="$SCRIPT_DIR/.whisperx_env"
WHISPERX_REQ_FILE="$SCRIPT_DIR/backend/requirements-whisperx.txt"

# 【修复】同上：避免 confirm() 返回 1（用户选 n）时被 set -e 直接终止脚本。
whisperx_install_choice=0
confirm "是否现在创建独立环境并安装 WhisperX?" || whisperx_install_choice=$?

if [ $whisperx_install_choice -eq 0 ] || [ $whisperx_install_choice -eq 2 ]; then
    if [ ! -f "$WHISPERX_REQ_FILE" ]; then
        log_error "找不到 $WHISPERX_REQ_FILE，跳过 WhisperX 安装"
    else
        if [ -d "$WHISPERX_VENV_DIR" ]; then
            log_warn "WhisperX 环境已存在: $WHISPERX_VENV_DIR"
            read -p "是否删除并重新创建? [y/n]: " -r whisperx_response
            if [[ "$whisperx_response" =~ ^[yY]$ ]]; then
                log_step "删除旧 WhisperX 环境..."
                rm -rf "$WHISPERX_VENV_DIR"
                log_ok "已删除"
            else
                log_info "使用现有 WhisperX 环境"
                skip_whisperx_venv_create=1
            fi
        fi

        if [ -z "$skip_whisperx_venv_create" ]; then
            log_step "创建 WhisperX 独立虚拟环境..."
            python3 -m venv "$WHISPERX_VENV_DIR"
            log_ok "WhisperX 虚拟环境已创建: $WHISPERX_VENV_DIR"
        fi

        log_step "在独立环境中安装 WhisperX 依赖（安装过程会实时显示）..."
        if "$WHISPERX_VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel > /dev/null 2>&1 && \
           "$WHISPERX_VENV_DIR/bin/python" -m pip install -r "$WHISPERX_REQ_FILE"; then
            log_ok "WhisperX 依赖已安装"
            log_info "首次启动 whisperx_server.py 时会自动下载模型权重"
        else
            log_warn "WhisperX 依赖安装失败，可稍后手动执行："
            log_warn "  $WHISPERX_VENV_DIR/bin/python -m pip install -r \"$WHISPERX_REQ_FILE\""
        fi
    fi
else
    log_info "已跳过，可后续手动运行以下命令安装："
    log_info "  python3 -m venv .whisperx_env"
    log_info "  .whisperx_env/bin/python -m pip install -r backend/requirements-whisperx.txt"
fi

# ─────────────────────────────────────────────────────────────────────────
# Step 8: Qwen3-TTS 独立环境（可选）
# ─────────────────────────────────────────────────────────────────────────
log_section "Step 8: Qwen3-TTS 独立环境 (可选)"

log_info "Qwen3-TTS 是可选的语音合成后端 (Custom Voice / Voice Design / Voice"
log_info "Clone)，作为一个本地服务 (端口 5003) 供主后端通过 HTTP 调用。不安装"
log_info "不影响其它功能使用。官方要求 Python 3.12，依赖见"
log_info "backend/requirements-qwen3tts.txt。"
echo ""

QWEN3TTS_VENV_DIR="$SCRIPT_DIR/.qwen3tts_env"
QWEN3TTS_REQ_FILE="$SCRIPT_DIR/backend/requirements-qwen3tts.txt"

# 【修复】同上：避免 confirm() 返回 1（用户选 n）时被 set -e 直接终止脚本。
qwen3tts_install_choice=0
confirm "是否现在创建独立环境并安装 Qwen3-TTS?" || qwen3tts_install_choice=$?

if [ $qwen3tts_install_choice -eq 0 ] || [ $qwen3tts_install_choice -eq 2 ]; then
    if [ ! -f "$QWEN3TTS_REQ_FILE" ]; then
        log_error "找不到 $QWEN3TTS_REQ_FILE，跳过 Qwen3-TTS 安装"
    else
        if [ -d "$QWEN3TTS_VENV_DIR" ]; then
            log_warn "Qwen3-TTS 环境已存在: $QWEN3TTS_VENV_DIR"
            read -p "是否删除并重新创建? [y/n]: " -r qwen3tts_response
            if [[ "$qwen3tts_response" =~ ^[yY]$ ]]; then
                log_step "删除旧 Qwen3-TTS 环境..."
                rm -rf "$QWEN3TTS_VENV_DIR"
                log_ok "已删除"
            else
                log_info "使用现有 Qwen3-TTS 环境"
                skip_qwen3tts_venv_create=1
            fi
        fi

        if [ -z "$skip_qwen3tts_venv_create" ]; then
            log_step "创建 Qwen3-TTS 独立虚拟环境..."
            # 官方要求 Python 3.12；优先使用 python3.12，找不到则回退到默认
            # python3 并给出版本提示（venv 无法像 conda 那样指定任意版本）。
            if command_exists python3.12; then
                QWEN3TTS_PY=python3.12
            else
                QWEN3TTS_PY=python3
                log_warn "未找到 python3.12，将使用默认 $($QWEN3TTS_PY --version 2>&1)。"
                log_warn "Qwen3-TTS 官方推荐 Python 3.12，版本不符可能导致安装或运行异常。"
            fi
            "$QWEN3TTS_PY" -m venv "$QWEN3TTS_VENV_DIR"
            log_ok "Qwen3-TTS 虚拟环境已创建: $QWEN3TTS_VENV_DIR"
        fi

        log_step "在独立环境中安装 Qwen3-TTS 依赖（安装过程会实时显示）..."
        if "$QWEN3TTS_VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel > /dev/null 2>&1 && \
           "$QWEN3TTS_VENV_DIR/bin/python" -m pip install -r "$QWEN3TTS_REQ_FILE"; then
            log_ok "Qwen3-TTS 依赖已安装"
            log_info "首次启动 qwen3tts_server.py 时会自动下载模型权重"
        else
            log_warn "Qwen3-TTS 依赖安装失败，可稍后手动执行："
            log_warn "  $QWEN3TTS_VENV_DIR/bin/python -m pip install -r \"$QWEN3TTS_REQ_FILE\""
        fi
    fi
else
    log_info "已跳过，可后续手动运行以下命令安装："
    log_info "  python3.12 -m venv .qwen3tts_env"
    log_info "  .qwen3tts_env/bin/python -m pip install -r backend/requirements-qwen3tts.txt"
fi

# ─────────────────────────────────────────────────────────────────────────
# 完成
# ─────────────────────────────────────────────────────────────────────────
log_section "✅ 安装完全完成"

cat << EOF

${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}

📍 下一步:
   启动应用程序:
   ${CYAN}./run.sh${NC}

🔧 故障排除:
   查看日志: ${CYAN}tail -f backend/logs/app.log${NC}

${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}

EOF

echo ""
