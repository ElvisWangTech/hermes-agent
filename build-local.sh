#!/usr/bin/env bash
# =============================================================================
# build-local.sh — 在当前 checkout 内「编译 + 安装」Hermes 开发版
# =============================================================================
# 与官方 install.sh（把源码 clone 到 ~/.hermes/hermes-agent）不同，本脚本就地
# 使用当前工作区，所有产物收敛到仓库根目录下的 .build/：
#
#   .build/
#   ├── bin/                       hermes / hermes-acp 启动器（绑定本 checkout）
#   ├── hermes-home/               隔离的 HERMES_HOME（配置 / 会话 / 依赖代）
#   ├── products/tui/              TUI 编译产物   —— dist/entry.js
#   ├── products/web/              仪表盘产物     —— index.html + assets
#   ├── products/desktop/          桌面端产物     —— 仅 --with-desktop
#   ├── icons/                     生成的图标     —— Web/桌面端编译输入
#   ├── activate.sh                source 它即可把本环境装进当前 shell
#   ├── build-manifest.json        本次构建清单（可追溯）
#   └── .hermes-build-local.json   本脚本的目录归属标记（供 --clean 判断）
#
# 设计要点：
#   1. Python 侧不编译。所谓「构建」= PM 按 pm/lock.json + uv.lock 引导运行时。
#      setup.py 显式禁用了 wheel/sdist，`pip install .` / `uv build` 必然失败。
#   2. 真正编译的只有 JS 前端：TUI / Dashboard /（可选）Electron 桌面端。
#   3. HERMES_HOME 与 HERMES_RUNTIME_DIR 隔离在 .build/ 内，不碰 ~/.hermes 中
#      已有的正式安装（唯一例外：复用其工具 store，见 --isolated-store）。
#   4. 前端产物通过 HERMES_TUI_DIR / HERMES_WEB_DIST 被应用直接识别，因此
#      无需往 checkout 里回写 ui-tui/dist 或 hermes_cli/web_dist。
#   5. 全程不需要 clone。本脚本就地使用当前工作区，不做任何 git clone / git
#      fetch / 远端交互：git 只用于把 HEAD 与分支记进构建清单，且失败时降级为
#      unknown（即残缺的、没有 .git 的源码副本同样能构建）。
#      依赖侧也没有 git 拉取——pm/lock.json 的 21 个工具全部是 url + sha256
#      制品；uv.lock 里两个 git 源依赖在本机均不生效（psutil 仅 android；
#      misaki 属 kittentts 可选 extra，默认不装）。macOS 的 pin 集不含 git
#      工具包（它是 Windows 专用制品），系统 git 仅作可选前提。
#      对照：官方 scripts/install.sh 必须 clone（或对已有 checkout 执行
#      git fetch + stash + reset --hard），且没有离线开关。
#
# 与生产安装（~/.hermes）的隔离：
#   本脚本对「本机是否已有正式安装」两种情况都成立：
#     · 有正式安装：下面是一整套硬性防呆 + 事后核验，绝不写进 ~/.hermes
#       （例外：共享工具 store，见文末）；与生产共存时不要在本开发环境执行
#       `hermes update` / `hermes gateway ...`。
#     · 没有正式安装（全新机器）：无需任何前置，直接跑即可。工具 store 默认
#       落在 <out>/tools，CPython / Node / 依赖全部下载到 <out> 内，
#       $HOME 下只会有 uv 自己管理的解释器缓存（uv 的行为，可复用）。
#   硬性防呆（两种情况下都生效）：
#     · 拒绝把 --out / --home 指到 ~/.hermes 内，或把启动器写进 ~/.local/bin
#     · 全部可变状态（config / state.db / sessions / skills / cron / logs /
#       cache / installs）都落在 $OUT/hermes-home，依赖代 key 由 checkout 路径
#       决定（sha256 前 16 位），与任何已有安装天然不同
#     · 启动器只发布到 $OUT/bin；launchd 服务名带哈希后缀，不占用裸名
#       ai.hermes.gateway
#     · 若检测到正式安装，构建前后对其取指纹（.env / config.yaml /
#       install-stamp / 工具 store / 启动器）并比对，任何改动都会明确列出
#   唯一的共享项是工具 store（version+platform 寻址、只增不删）。若 pm/lock.json
#   与生产一致，则零写入；要绝对隔离用 --isolated-store。
#
# 网络与镜像加速（国内环境）：
#   症状：`uv sync` 卡在 `Failed to download pydantic==…`、`files.pythonhosted.org`
#   连接超时。
#   原因：PM 在无插件成员时走 `uv sync --frozen`，而 uv 的 --frozen 严格按
#   uv.lock 里记录的「绝对 URL」下载。因此只设索引（UV_INDEX_URL / PIP_INDEX_URL /
#   pip.conf 的 index-url，PM 都会转发给 uv）只能加速「解析」与 sdist 构建，
#   不能改变 wheel 的下载主机。实测：三种索引配置下下载主机都仍是
#   files.pythonhosted.org。
#   做法：--mirror 把 uv.lock 中
#       https://files.pythonhosted.org/packages/…
#   改写为
#       <mirror>/packages/…
#   （各镜像的 /packages/ 路径与 PyPI 完全一致，字节相同、sha256 一致，因此
#   既不改变锁定内容，也不影响 uv 的哈希校验）。
#   安全：改写前把 uv.lock 原文件备份到 <out>/.lock-pristine/，结束（或中断、
#   或异常）时立即还原并校验 sha256；若进程被强杀，下次运行会自动检测并还原。
#   --mirror auto（默认）的判据是「吞吐」而非「耗时」：拿 uv.lock 里 ≥512KB 的
#   制品做 Range 采样量吞吐，直连 ≥2 MB/s 就完全不动 uv.lock；否则实测各国内镜像，
#   最优者须 ≥1 MB/s 且 ≥1.5×直连，并通过 sha256 一致性校验后才改写。
#   （早期版本拿「最小制品」的下载耗时 ≤1.5s 当健康，而那个样本实测只有 2KB ——
#   量到的其实是握手延迟。本机直连 PythonHosted 实测 0.10 MB/s，uvloop 1.3MiB
#   要 27.7 秒，华为镜像 3.96 MB/s 只要 0.11 秒，却因为 2KB 样本 0.3s 就下完而
#   被判成「直连健康」，整套镜像加速被静默跳过。阈值可用 DIRECT_MIN_MBPS /
#   MIRROR_MIN_MBPS / MIRROR_MIN_RATIO 覆盖。）
#
#   但依赖只占国内首装的一小半：PM 还要按 pm/lock.json 下载工具链，这批制品
#   不在 PyPI 上，--mirror 改写管不到。实测（2026-10，本机）：
#     npmmirror（腾讯，国内 CDN）   3.65 MB/s   ← npm / node 走这里
#     pypi.tuna.tsinghua.edu.cn     镜像探测 0.26s，PyPI wheel 走这里
#     nodejs.org                    0.35 MB/s
#     ffmpeg.martin-riedl.de        0.18 MB/s
#     github.com（工具链主力）       0.09~0.18 MB/s
#     cdn.playwright.dev            0.09 MB/s
#   十个 GitHub 代理（ghfast/gh-proxy/ghproxy.net/gh.llkk.cc/gitmirror/…）实测
#   全在 0.09~0.17 MB/s，nousresearch 官方兜底 CDN 0.35 MB/s，都没能真正加速；
#   NJU 镜像覆盖 python-build-standalone，但不覆盖 llama.cpp / bitwarden 等。
#
#   所以真正的杠杆是「少下」而不是「快点下」：全新机器的工具闭包其实很小——
#     必需：ffmpeg、node、npm、python、ripgrep
#     可选：agent-browser（依赖 chromium）、cua-driver
#   其中 Chromium 约 150MB 且只能从 cdn.playwright.dev（0.09 MB/s）拉，单它一项
#   就要 ~28 分钟。不需要浏览器自动化时用 --minimal 裁掉它，首装可从半小时级
#   降到十分钟级（--without NAME / --with-browser 可精细控制与回退）。
#   另外两条「已验证字节一致」的加速会被默认启用：
#     · PyPI 依赖：改写 uv.lock（--mirror）
#     · node 制品：改写 pm/lock.json（--tool-mirror）
#       nodejs.org 0.04 MB/s → npmmirror 10.4 MB/s，30MB 从 10.7 分钟降到 3 秒
#     · uv 托管的 CPython：--python-mirror 默认指向南京大学（25.3MiB / 1.92s）
#   pm/lock.json 里其它可映射的源（ffmpeg / chromium / cua-driver / ripgrep /
#   llama.cpp / bws / uv 自身）国内都没有可用镜像，只能靠代理。
#   本脚本不猜：开下载前会先跑「工具链预检」，列出缺哪些制品、各自来源、
#   实测吞吐与预计耗时；主机不可达或过慢时明确提示改用 HTTPS_PROXY。
#   注意预检只报「连不上/太慢」，真正的失败仍由 PM 的 sha256 校验兜底。
#
# 前端 / JS 层的二进制 postinstall（npm registry 管不到）：
#   npm ci 的包体已经走 registry 镜像，但有些包的 postinstall 会**绕开 registry**
#   直接去 GitHub Releases 拉预编译二进制——registry 配置对它们完全无效。实测：
#     electron@40.10.2 → github.com/electron/electron/releases/…darwin-arm64.zip
#       109 MiB，本机 connect ETIMEDOUT（20.205.243.166:443），npm ci 直接失败
#     node-gyp（node-pty 回退到本地编译时用）→ nodejs.org 头文件，0.04 MB/s
#   处置：
#     · 桌面端编译本来就是 opt-in，默认构建用不到 Electron 的运行时二进制，所以
#       默认设 ELECTRON_SKIP_BINARY_DOWNLOAD=1 直接跳过下载（electron 的
#       install.js 第 14 行会在任何网络动作之前 exit 0），npm ci 不再卡住；
#     · --with-desktop 时才真正下载，并改走 npmmirror（1.76 MB/s，109 MiB 约
#       1 分钟；校验用的是包内自带的 checksums.json，不依赖镜像的 SHASUMS256.txt）。
#       要走 GitHub 官方源加 --electron-mirror none。
#     · node-gyp 的头文件走 NODEJS_ORG_MIRROR=npmmirror（9.8 MiB）。
#
# 用法：
#   ./build-local.sh [选项]
#
# 常用：
#   ./build-local.sh                  # 完整构建（运行时 + TUI + Dashboard）
#   ./build-local.sh --with-desktop   # 额外编译桌面端（最慢）
#   ./build-local.sh --skip-runtime   # 只重编前端
#   ./build-local.sh --minimal        # 全新机器首装：跳过 Chromium，快很多
#   ./build-local.sh --clean          # 先清空 .build 再全量重建
#   source .build/activate.sh         # 激活该开发环境
# =============================================================================

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$REPO/pm/lock.json"
UVLOCK="$REPO/uv.lock"
MIRROR_FILE="$REPO/pm/artifact-mirror.json"

# -----------------------------------------------------------------------------
# 选项
# -----------------------------------------------------------------------------
OUT=""
HERMES_HOME_OPT=""
STORE_OPT=""
ISOLATED_STORE=false
WITH_TEST_ENV=false
SKIP_RUNTIME=false
SKIP_JS=false
WITH_DESKTOP=false
COPY_ENV=false
DO_CLEAN=false
DO_VERIFY=false
MIRROR_MODE="auto"
MIRROR_BASE_OPT=""
MIRROR_INDEX=false
NPM_REGISTRY_OPT=""
ELECTRON_MIRROR_OPT=""   # --electron-mirror URL|none，仅 --with-desktop 时才会用到
PYTHON_MIRROR_OPT=""
WITHOUT_OPT=""      # 空格分隔的工具名，逐个转成 pm 的 --without
MINIMAL=false
WITH_BROWSER=false
TOOL_PREFLIGHT=true
TOOL_MIRROR="auto"   # 工具链制品的国内镜像改写：auto | none
DECLINED_NOW=""     # 本次实际被裁掉的可选工具（来自 PM 的裁剪记录）

usage() {
    sed -n '2,/^set -euo pipefail$/p' "$REPO/build-local.sh" | sed -e '$d' -e 's/^# \{0,1\}//'
    cat <<'EOF'

选项：
  --out DIR           输出根目录（默认 <repo>/.build）
  --home DIR          开发用 HERMES_HOME（默认 <out>/hermes-home）
  --store DIR         PM 工具 store
  --isolated-store    强制使用 <out>/tools，不与 ~/.hermes/tools 共用
  --with-test-env     额外构建隔离测试环境（dev/test 依赖组，较慢）
  --skip-runtime      跳过 Python 运行时引导（仅重编前端）
  --skip-js           跳过 npm 依赖与前端编译（仅引导运行时）
  --with-desktop      额外编译桌面端（Electron，最慢）
  --copy-env          从 ~/.hermes/.env 复制密钥到开发 HERMES_HOME
  --verify            构建后运行 pm doctor 做字节校验
  --clean             先清空输出目录（仅限本脚本创建/带标记的目录）
  --mirror MODE       PyPI 镜像：auto（默认）| aliyun | tencent | tuna | huawei | ustc | none
                      auto = 用 uv.lock 里 ≥512KB 的制品实测「吞吐」：直连 ≥2 MB/s 就
                      完全不动 uv.lock；否则选最快的国内镜像，经 sha256 校验后改写
                      阈值可用环境变量覆盖：DIRECT_MIN_MBPS / MIRROR_MIN_MBPS / MIRROR_MIN_RATIO
  --mirror-base URL   直接指定镜像基址（如 https://mirrors.aliyun.com/pypi）
  --mirror-index      同时把依赖「解析」索引也指向镜像（默认只改下载地址）
  --npm-registry URL  npm 镜像（默认 https://registry.npmmirror.com）
  --electron-mirror URL|none
                      Electron 运行时二进制的镜像（默认 npmmirror），仅 --with-desktop
                      需要；none = 走 GitHub 官方源。默认构建不下载该二进制（见下）
  --python-mirror URL  CPython 下载镜像（uv 的 UV_PYTHON_INSTALL_MIRROR）
                      默认走南京大学镜像（uv 自己的客户端，实测 25.3MiB/1.92s）；
                      传 --tool-mirror none 可关闭
  --tool-mirror MODE  工具链制品镜像改写：auto（默认）| none
                      auto 目前只收录一条经真实安装验证过的映射：
                        nodejs.org/dist  → npmmirror（实测 0.04 → 10.4 MB/s）
                      其它源国内无可用镜像，保持原始地址
  --without NAME      不装某个「可选默认工具」，可重复（可选项：agent-browser、cua-driver）
  --minimal           等价于 --without agent-browser --without cua-driver
                      省掉 Chromium（约 150MB，走 cdn.playwright.dev）——国内首装可省半小时
  --with-browser      反向清除上面的选择，把可选工具装回来
  --no-tool-preflight 关闭「工具链下载源预检」
  -h, --help          显示本帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --out)            OUT="${2:?--out 需要参数}"; shift 2 ;;
        --out=*)          OUT="${1#*=}"; shift ;;
        --home)           HERMES_HOME_OPT="${2:?--home 需要参数}"; shift 2 ;;
        --home=*)         HERMES_HOME_OPT="${1#*=}"; shift ;;
        --store)          STORE_OPT="${2:?--store 需要参数}"; shift 2 ;;
        --store=*)        STORE_OPT="${1#*=}"; shift ;;
        --isolated-store) ISOLATED_STORE=true; shift ;;
        --with-test-env)  WITH_TEST_ENV=true; shift ;;
        --skip-runtime)   SKIP_RUNTIME=true; shift ;;
        --skip-js)        SKIP_JS=true; shift ;;
        --with-desktop)   WITH_DESKTOP=true; shift ;;
        --copy-env)       COPY_ENV=true; shift ;;
        --verify)         DO_VERIFY=true; shift ;;
        --clean)          DO_CLEAN=true; shift ;;
        --mirror)         MIRROR_MODE="${2:?--mirror 需要参数}"; shift 2 ;;
        --mirror=*)       MIRROR_MODE="${1#*=}"; shift ;;
        --mirror-base)    MIRROR_BASE_OPT="${2:?--mirror-base 需要参数}"; shift 2 ;;
        --mirror-base=*)  MIRROR_BASE_OPT="${1#*=}"; shift ;;
        --mirror-index)   MIRROR_INDEX=true; shift ;;
        --npm-registry)   NPM_REGISTRY_OPT="${2:?--npm-registry 需要参数}"; shift 2 ;;
        --npm-registry=*) NPM_REGISTRY_OPT="${1#*=}"; shift ;;
        --electron-mirror)   ELECTRON_MIRROR_OPT="${2:?--electron-mirror 需要参数}"; shift 2 ;;
        --electron-mirror=*) ELECTRON_MIRROR_OPT="${1#*=}"; shift ;;
        --python-mirror)  PYTHON_MIRROR_OPT="${2:?--python-mirror 需要参数}"; shift 2 ;;
        --python-mirror=*) PYTHON_MIRROR_OPT="${1#*=}"; shift ;;
        --without)        WITHOUT_OPT="${WITHOUT_OPT} ${2:?--without 需要参数}"; shift 2 ;;
        --without=*)      WITHOUT_OPT="${WITHOUT_OPT} ${1#*=}"; shift ;;
        --minimal)        MINIMAL=true; shift ;;
        --with-browser)   WITH_BROWSER=true; shift ;;
        --no-tool-preflight) TOOL_PREFLIGHT=false; shift ;;
        --tool-mirror)    TOOL_MIRROR="${2:?--tool-mirror 需要参数}"; shift 2 ;;
        --tool-mirror=*)  TOOL_MIRROR="${1#*=}"; shift ;;
        -h|--help)        usage; exit 0 ;;
        *)                printf '未知选项：%s（用 --help 查看用法）\n' "$1" >&2; exit 2 ;;
    esac
done

if [ "$MINIMAL" = true ] && [ "$WITH_BROWSER" = true ]; then
    printf '✗ --minimal 与 --with-browser 互斥：前者裁掉可选工具，后者把它们装回来\n' >&2
    exit 2
fi
case "$TOOL_MIRROR" in
    auto|none) ;;
    *) printf '✗ 未知 --tool-mirror 值：%s（可选 auto | none）\n' "$TOOL_MIRROR" >&2; exit 2 ;;
esac
# 工具链镜像里同时覆盖 uv 托管的 CPython（python-build-standalone 在 NJU 有镜像）。
# 单独传 --python-mirror 时以用户值为准。
if [ "$TOOL_MIRROR" != none ] && [ -z "$PYTHON_MIRROR_OPT" ]; then
    PYTHON_MIRROR_OPT="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
fi
if [ "$MINIMAL" = true ]; then
    WITHOUT_OPT="${WITHOUT_OPT} agent-browser cua-driver"
fi
# 去掉重复与首尾空白，保持稳定顺序
if [ -n "$WITHOUT_OPT" ]; then
    WITHOUT_OPT="$(printf '%s\n' $WITHOUT_OPT | awk 'NF && !seen[$0]++' | tr '\n' ' ')"
    for _w in $WITHOUT_OPT; do
        case "$_w" in
            agent-browser|cua-driver) ;;
            *) printf '✗ 不可裁剪的工具：%s（可选项只有 agent-browser、cua-driver；\n' "$_w" >&2
               printf '  必需集是 ffmpeg/node/npm/python/ripgrep，始终安装）\n' >&2
               exit 2 ;;
        esac
    done
fi

case "$MIRROR_MODE" in
    auto|aliyun|tencent|tuna|huawei|ustc|none) ;;
    *) printf '未知 --mirror 值：%s（可选 auto|aliyun|tencent|tuna|huawei|ustc|none）\n' "$MIRROR_MODE" >&2; exit 2 ;;
esac

# -----------------------------------------------------------------------------
# 日志
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
    C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_CYAN='\033[0;36m'
    C_RED='\033[0;31m'; C_DIM='\033[2m'; C_NC='\033[0m'
else
    C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_RED=''; C_DIM=''; C_NC=''
fi
info() { printf "${C_CYAN}→${C_NC} %s\n" "$*"; }
ok()   { printf "${C_GREEN}✓${C_NC} %s\n" "$*"; }
warn() { printf "${C_YELLOW}!${C_NC} %s\n" "$*" >&2; }
die()  { printf "${C_RED}✗${C_NC} %s\n" "$*" >&2; exit 1; }
step() { printf "\n${C_CYAN}▸ %s${C_NC}\n" "$*"; }
dim()  { printf "${C_DIM}%s${C_NC}\n" "$*"; }

# -----------------------------------------------------------------------------
# 生产安装隔离护栏
# -----------------------------------------------------------------------------
# 本机可能已存在正式安装（~/.hermes + ~/.local/bin/hermes）。以下是硬性防呆：
# 任何会让开发构建写进生产目录的组合一律拒绝执行，而不是警告后继续。
PROD_HOME="$HOME/.hermes"
PROD_BIN="$HOME/.local/bin"

hash_of() { # 文件 sha256；不存在输出 "-"
    if [ -f "$1" ]; then
        if command -v shasum >/dev/null 2>&1; then
            shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1
        else
            sha256sum "$1" 2>/dev/null | cut -d' ' -f1
        fi
    else
        printf '%s' '-'
    fi
}

dir_listing_hash() { # 目录直属条目名（排序）的 sha256，用来发现「多出/少了条目」
    local tmp out
    tmp="$(mktemp)"
    if [ -d "$1" ]; then
        find "$1" -maxdepth 1 -mindepth 1 -exec basename {} \; 2>/dev/null | sort > "$tmp" || true
    fi
    out="$(hash_of "$tmp")"
    rm -f "$tmp"
    printf '%s' "$out"
}

production_fingerprint() { # 生产安装中「不可再生」的小文件 + 目录结构的指纹
    printf 'env              %s\n' "$(hash_of "$PROD_HOME/.env")"
    printf 'config.yaml      %s\n' "$(hash_of "$PROD_HOME/config.yaml")"
    printf 'install-stamp    %s\n' "$(hash_of "$PROD_HOME/hermes-agent/install-stamp.json")"
    printf 'tools/facts.json %s\n' "$(hash_of "$PROD_HOME/tools/facts.json")"
    printf 'tools/entries    %s\n' "$(dir_listing_hash "$PROD_HOME/tools")"
    printf 'installs/keys    %s\n' "$(dir_listing_hash "$PROD_HOME/installs")"
    printf 'bin/hermes       %s\n' "$(hash_of "$PROD_BIN/hermes")"
    printf 'bin/hermes-acp   %s\n' "$(hash_of "$PROD_BIN/hermes-acp")"
    printf 'bin/hermes-agent %s\n' "$(hash_of "$PROD_BIN/hermes-agent")"
    for f in "$PROD_HOME"/installs/*/facts.json; do
        [ -f "$f" ] || continue
        printf 'install-facts    %-16s %s\n' "$(basename "$(dirname "$f")")" "$(hash_of "$f")"
    done
}

assert_isolation() { # 在解析出全部目标路径后调用；不满足即退出
    local problem=""

    case "$HERMES_HOME_DIR/" in
        "$PROD_HOME"/*)
            problem="开发 HERMES_HOME 落在生产目录内：${HERMES_HOME_DIR}" ;;
    esac
    if [ -z "$problem" ]; then
        case "$OUT/" in
            "$PROD_HOME"/*)
                problem="产物根目录落在生产目录内：${OUT}" ;;
        esac
    fi
    if [ -z "$problem" ]; then
        case "$BIN_DIR" in
            "$PROD_BIN"|"$PROD_BIN"/*)
                problem="启动器目录就是生产启动器目录：${BIN_DIR}" ;;
        esac
    fi
    if [ -n "$problem" ]; then
        printf "${C_RED}✗ 隔离检查未通过：%s${C_NC}\n" "$problem" >&2
        printf '  本脚本拒绝运行，以免破坏 %s 下的正式安装。\n' "$PROD_HOME" >&2
        printf '  改用默认输出目录（<repo>/.build）或另指定 --out / --home。\n' >&2
        exit 1
    fi

    # 开发 install key 必须与生产不同，否则会共用依赖代目录。
    DEV_INSTALL_KEY="$(printf '%s' "$REPO" | (shasum -a 256 2>/dev/null || sha256sum) | cut -c1-16)"
    PROD_INSTALL_KEY="$(printf '%s' "$PROD_HOME/hermes-agent" | (shasum -a 256 2>/dev/null || sha256sum) | cut -c1-16)"
    if [ "$DEV_INSTALL_KEY" = "$PROD_INSTALL_KEY" ]; then
        die "开发 install key 与生产相同（${DEV_INSTALL_KEY}）——两者会争用同一依赖代目录"
    fi

    # launchd 服务名后缀：生产占据裸名 ai.hermes.gateway，开发必须是哈希后缀。
    if [ "$HERMES_HOME_DIR" = "$PROD_HOME" ]; then
        die "开发 HERMES_HOME 不允许等于生产家目录"
    fi

    SHARED_STORE=false
    case "$STORE/" in
        "$PROD_HOME"/*) SHARED_STORE=true ;;
    esac
}

# -----------------------------------------------------------------------------
# 前置检查
# -----------------------------------------------------------------------------
for tool in git curl tar unzip; do
    command -v "$tool" >/dev/null 2>&1 || die "缺少前置命令：$tool"
done
[ -f "$LOCK" ] || die "找不到 pm/lock.json，请在 Hermes 仓库根目录运行本脚本"

case "$(uname -s)" in
    Linux)  OS=linux ;;
    Darwin) OS=darwin ;;
    *)      die "不支持的操作系统：$(uname -s)" ;;
esac
case "$(uname -m)" in
    arm64|aarch64) ARCH=arm64 ;;
    x86_64|amd64)  ARCH=x64 ;;
    *)             die "不支持的架构：$(uname -m)" ;;
esac
TARGET="$OS-$ARCH"

if [ "$OS" = darwin ]; then
    if ! command -v xcode-select >/dev/null 2>&1 \
       || [ ! -d "$(xcode-select -p 2>/dev/null || echo /nonexistent)" ]; then
        warn "未检测到 Xcode Command Line Tools，原生依赖可能编译失败"
    fi
fi

# -----------------------------------------------------------------------------
# 输出目录与 --clean 安全护栏
# -----------------------------------------------------------------------------
OUT="${OUT:-$REPO/.build}"
case "$OUT" in
    ""|/|"$HOME"|"$HOME"/|"$REPO"|"$REPO"/) die "拒绝使用「${OUT}」作为输出目录" ;;
esac
# 在创建任何目录之前就先挡住「写进生产目录」的路径（否则下面的 mkdir -p 会先落地）
case "$OUT"/ in
    "$PROD_HOME"/*) die "拒绝把产物写进生产目录：${OUT}" ;;
esac
mkdir -p "$(dirname "$OUT")"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
MARKER="$OUT/.hermes-build-local.json"

# -----------------------------------------------------------------------------
# 镜像改写的崩溃恢复
# -----------------------------------------------------------------------------
# 镜像加速靠「临时改写锁定文件里的绝对下载地址」实现，涉及两个文件：
#   uv.lock      —— PyPI wheel / sdist（--mirror）
#   pm/lock.json —— 工具链制品（--tool-mirror，如 node / CPython）
# 若上一次运行被强杀（SIGKILL 无法被 trap 捕获），这里负责在本次运行最前面把
# 它们还原——先于 --clean，避免备份被一起删掉。判定条件刻意收窄：只有
# 「有备份」且「当前文件已不含原始主机」才认定是我们留下的改写状态。
PRISTINE_DIR="$OUT/.lock-pristine"
UVLOCK_BACKUP="$PRISTINE_DIR/uv.lock"
TOOLLOCK_BACKUP="$PRISTINE_DIR/pm-lock.json"
LOCK_REWRITTEN=false
TOOLLOCK_REWRITTEN=false
UVLOCK_SHA_BEFORE="$(hash_of "$UVLOCK")"
TOOLLOCK_SHA_BEFORE="$(hash_of "$LOCK")"

# 还原一个被改写的锁文件。$1=备份路径 $2=目标路径 $3=标签 $4=原始 sha
# 注意：这里**不能**删 PRISTINE_DIR —— uv.lock 与 pm/lock.json 的备份共用一个
# 目录，先还原哪个就把目录删掉会让另一个还原时找不到备份而静默跳过。
# 目录统一由 restore_lock 在所有还原完成后再清。
restore_one_lock() {
    local backup="$1" target="$2" label="$3" want="$4"
    [ -f "$backup" ] || return 0
    cp -p "$backup" "$target"
    if [ "$(hash_of "$target")" = "$want" ]; then
        ok "${label} 已还原（sha256 与改写前一致）"
    else
        warn "${label} 还原后 sha256 与改写前不一致，请人工检查 ${target}"
    fi
}

# EXIT/INT/TERM/HUP 的统一收尾：还原所有被改写的锁文件
restore_lock() {
    if [ "$LOCK_REWRITTEN" = true ]; then
        restore_one_lock "$UVLOCK_BACKUP" "$UVLOCK" "uv.lock" \
                         "$UVLOCK_SHA_BEFORE" 'files\.pythonhosted\.org/packages/'
        LOCK_REWRITTEN=false
    fi
    if [ "$TOOLLOCK_REWRITTEN" = true ]; then
        restore_one_lock "$TOOLLOCK_BACKUP" "$LOCK" "pm/lock.json" \
                         "$TOOLLOCK_SHA_BEFORE" 'nodejs\.org/dist/'
        TOOLLOCK_REWRITTEN=false
    fi
    rm -rf "$PRISTINE_DIR" 2>/dev/null || true
}

# 恢复上次运行时遗留的改写（先于 --clean，避免备份被删）
if [ -f "$UVLOCK_BACKUP" ] \
   && [ "$(hash_of "$UVLOCK")" != "$(cat "$PRISTINE_DIR/uv.lock.sha256" 2>/dev/null || echo '-')" ] \
   && ! grep -q 'files\.pythonhosted\.org/packages/' "$UVLOCK" 2>/dev/null; then
    warn "检测到上次运行遗留的镜像改写（uv.lock 未还原），正在自动还原"
    UVLOCK_SHA_BEFORE="$(cat "$PRISTINE_DIR/uv.lock.sha256")"
    LOCK_REWRITTEN=true
    restore_lock
fi
if [ -f "$TOOLLOCK_BACKUP" ] \
   && [ "$(hash_of "$LOCK")" != "$(cat "$PRISTINE_DIR/pm-lock.json.sha256" 2>/dev/null || echo '-')" ] \
   && ! grep -q 'nodejs\.org/dist/' "$LOCK" 2>/dev/null; then
    warn "检测到上次运行遗留的工具镜像改写（pm/lock.json 未还原），正在自动还原"
    TOOLLOCK_SHA_BEFORE="$(cat "$PRISTINE_DIR/pm-lock.json.sha256")"
    TOOLLOCK_REWRITTEN=true
    restore_lock
fi
rm -rf "$PRISTINE_DIR" 2>/dev/null || true

# 兜底提示：备份是跟着 --out 目录走的，一旦那个目录被删/被移走，上面的自恢复
# 就无从下手，锁定文件会一直停在改写状态（让人误以为「依赖来自镜像」是常态）。
# 判定条件：uv.lock 里一条 files.pythonhosted.org 都没有——正常仓库必然有几千条。
if [ -f "$UVLOCK" ] && ! grep -q 'files\.pythonhosted\.org/packages/' "$UVLOCK" 2>/dev/null; then
    warn "uv.lock 中不含 files.pythonhosted.org，疑似仍处于镜像改写状态且未找到备份"
    if [ -d "$REPO/.git" ]; then
        dim "  还原：git -C $REPO checkout -- uv.lock"
    else
        dim "  还原：本 checkout 没有 .git，请从原始副本重新取回 uv.lock"
    fi
fi

if [ "$DO_CLEAN" = true ] && [ -e "$OUT" ]; then
    if [ ! -d "$OUT" ]; then
        die "拒绝清理：「${OUT}」不是目录"
    elif [ -n "$(ls -A "$OUT" 2>/dev/null || true)" ] && [ ! -f "$MARKER" ]; then
        die "拒绝清理：「${OUT}」非空且缺少归属标记（${MARKER}）。请手工确认后再删。"
    fi
    info "清空输出目录 $OUT"
    rm -rf "$OUT"
fi

HERMES_HOME_DIR="${HERMES_HOME_OPT:-$OUT/hermes-home}"
BIN_DIR="$OUT/bin"
PRODUCTS_DIR="$OUT/products"
ICONS_DIR="$OUT/icons"

if [ -n "${HERMES_HOME:-}" ] && [ "$HERMES_HOME" != "$HERMES_HOME_DIR" ]; then
    warn "当前 shell 的 HERMES_HOME=$HERMES_HOME 将被覆盖为 $HERMES_HOME_DIR"
fi

# 工具 store：默认复用已有的 ~/.hermes/tools（省掉数 GB 下载），否则落在
# .build/tools。工具条目按「版本 + 平台」寻址，多安装共用是官方设计。
if [ -n "$STORE_OPT" ]; then
    STORE="$STORE_OPT"
elif [ "$ISOLATED_STORE" = true ]; then
    STORE="$OUT/tools"
elif [ -f "$HOME/.hermes/tools/facts.json" ]; then
    STORE="$HOME/.hermes/tools"
else
    STORE="$OUT/tools"
fi

assert_isolation

# 生产安装指纹：构建前取一次，结束后比对。只覆盖不可再生的小文件与目录结构，
# 保证「构建是否动过生产安装」是一个可判定的事实，而不是靠假设。
# 注意：必须在创建任何目录之前，否则隔离检查失败时已经留下了痕迹。
PROD_BEFORE="$(mktemp)"
production_fingerprint > "$PROD_BEFORE"

# 共享 store 时额外记录条目名，构建后给出「新增/消失」的具体清单
STORE_ENTRIES_BEFORE="$(mktemp)"
if [ "$SHARED_STORE" = true ] && [ -d "$STORE" ]; then
    ( find "$STORE" -maxdepth 1 -mindepth 1 -exec basename {} \; 2>/dev/null \
        | grep -v '^\.' | sort > "$STORE_ENTRIES_BEFORE" ) || true
fi

mkdir -p "$OUT" "$BIN_DIR" "$HERMES_HOME_DIR"
printf '%s\n' '{"schema":"hermes-build-local-v1"}' > "$MARKER"

export HERMES_HOME="$HERMES_HOME_DIR"
export HERMES_RUNTIME_DIR="$STORE"

printf "\n${C_CYAN}☤ Hermes 本地开发构建${C_NC}\n"
dim "  仓库         $REPO"
dim "  输出         $OUT"
dim "  HERMES_HOME  $HERMES_HOME"
dim "  工具 store   $STORE"
dim "  目标平台     $TARGET"
if [ -d "$PROD_HOME" ]; then
    dim "  生产安装     ${PROD_HOME}（已检测到，本脚本不会写入）"
    if [ "$SHARED_STORE" = true ]; then
        dim "               ↳ 工具 store 为共享复用；用 --isolated-store 可完全隔离"
    fi
else
    dim "  生产安装     未检测到（全新机器；本次会完整下载工具链与依赖）"
    dim "               ↳ 工具 store 落在 ${STORE}，全部产物都在 ${OUT} 内"
fi

# -----------------------------------------------------------------------------
# pm/lock.json pin 解析（在 Python 可用之前用 awk 完成）
# -----------------------------------------------------------------------------
pin() { # $1 = url|sha256|version, $2 = 包名（默认 uv）
    awk -F '"' -v package="${2:-uv}" -v target="$TARGET" -v field="$1" '
        /^[[:space:]]*("[^"]+"[[:space:]]*:[[:space:]]*)?\{[[:space:]]*$/ { path[++depth] = $2; next }
        /^[[:space:]]*}[[:space:]]*,?[[:space:]]*$/                        { delete path[depth--]; next }
        path[2] == "packages" && path[3] == package && $2 == field &&
          ((field == "version" && depth == 3) ||
           (depth == 5 && path[4] == "artifacts" && path[5] == target)) { print $4; exit }
    ' "$LOCK"
}

MIRROR_ORIGIN="$(awk -F '"' '$2 == "origin" { print $4; exit }' "$MIRROR_FILE" 2>/dev/null || true)"
MIRROR_PREFIX="$(awk -F '"' '$2 == "prefix" { print $4; exit }' "$MIRROR_FILE" 2>/dev/null || true)"

# -----------------------------------------------------------------------------
# 步骤 1：引导 Python 运行时（uv → CPython → PM 依赖同步）
# -----------------------------------------------------------------------------
BOOT_PY=""
for candidate in "$STORE"/python-*/bin/python3; do
    [ -x "$candidate" ] && { BOOT_PY="$candidate"; break; }
done

stage_uv() {
    local uv_version uv_entry url sha archive got tmp inner inner_count child mirror
    uv_version="$(pin version)"
    [ -n "$uv_version" ] || die "pm/lock.json 中缺少 uv 的版本 pin"
    uv_entry="$STORE/uv-$uv_version-$TARGET"

    if [ -x "$uv_entry/uv" ]; then
        UV="$uv_entry/uv"
        ok "复用已安装的 uv（$("$UV" --version 2>/dev/null || echo "uv $uv_version")）"
        return 0
    fi

    url="$(pin url)"; sha="$(pin sha256)"
    [ -n "$url" ] && [ -n "$sha" ] || die "pm/lock.json 中缺少 $TARGET 的 uv 制品"

    info "下载 pinned uv ${uv_version}（${TARGET}）..."
    mkdir -p "$STORE"
    tmp="$(mktemp -d "$STORE/.bootstrap-XXXXXX")"
    archive="$tmp/${url##*/}"

    if ! curl -fsSL -o "$archive" "$url"; then
        if [ -n "$MIRROR_ORIGIN" ] && [ -n "$MIRROR_PREFIX" ]; then
            mirror="$MIRROR_ORIGIN/$MIRROR_PREFIX$sha"
            info "主源不可用，回退镜像..."
            curl -fsSL -o "$archive" "$mirror" \
                || { rm -rf "$tmp"; die "uv 下载失败（主源与镜像均不可用）"; }
        else
            rm -rf "$tmp"; die "uv 下载失败：$url"
        fi
    fi

    got="$( (sha256sum "$archive" 2>/dev/null || shasum -a 256 "$archive") | cut -d' ' -f1 | tr -d '\\')"
    [ "$got" = "$sha" ] || { rm -rf "$tmp"; die "uv sha256 校验失败：期望 ${sha}，实际 ${got}"; }

    mkdir -p "$tmp/tree"
    case "$archive" in
        *.zip) unzip -q "$archive" -d "$tmp/tree" ;;
        *)     tar -xzf "$archive" -C "$tmp/tree" ;;
    esac
    inner=""; inner_count=0
    for child in "$tmp/tree"/* "$tmp/tree"/.[!.]* "$tmp/tree"/..?*; do
        [ -e "$child" ] || [ -L "$child" ] || continue
        inner="$child"; inner_count=$((inner_count + 1))
    done
    if [ "$inner_count" = 1 ] && [ -d "$inner" ]; then mv "$inner" "$tmp/entry"
    else mv "$tmp/tree" "$tmp/entry"; fi
    rm -rf "$uv_entry"
    mv "$tmp/entry" "$uv_entry"
    rm -rf "$tmp"
    UV="$uv_entry/uv"
    ok "uv 已就绪（$("$UV" --version 2>/dev/null || echo "uv $uv_version")）"
}

# -----------------------------------------------------------------------------
# 镜像加速：把 uv.lock 的下载 URL 改写为镜像
# -----------------------------------------------------------------------------
# 为什么只能这么做：PM 在无插件成员时走 `uv sync --frozen`，而 uv 的 --frozen
# 严格按 uv.lock 里记录的绝对 URL 下载（实测：无论怎么配 UV_INDEX_URL /
# UV_DEFAULT_INDEX，下载主机都仍是 files.pythonhosted.org）。索引配置只影响
# 「解析」和 sdist 构建。各镜像的 /packages/ 路径与 PyPI 完全一致、字节相同
# （探测时用锁内 sha256 校验），所以改写既不改动锁定内容，也不影响 uv 校验。
MIRROR_BASE=""
MIRROR_LABEL="直连"
MIRROR_DETAIL=""
NPM_REGISTRY="${NPM_REGISTRY_OPT:-https://registry.npmmirror.com}"
MIRROR_PROBE_PATH=""    # 测速样本路径（≥512KB 中最小的一个，用于量吞吐）
MIRROR_PROBE_HASH=""    # 校验样本的 sha256（最小文件，用于证明镜像字节一致）
MIRROR_VERIFY_PATH=""   # 校验样本路径
# auto 模式的判据是「吞吐」而不是「耗时」：早期版本用最小文件（实测只有 2KB 的
# types-certifi）下载耗时 ≤1.5s 就判定直连健康，而 2KB 在 0.6s 内下完量的其实是
# 延迟、不是带宽——结果判出「直连健康」而跳过镜像，真实依赖（如 uvloop 1.3MiB）
# 仍以 49 KB/s 直连，单文件就要 27.7 秒。现在改为：
#   直连吞吐 ≥ DIRECT_MIN_MBPS  → 直连够用，完全不动 uv.lock；
#   否则实测各镜像，最优者须 ≥ MIRROR_MIN_MBPS 且 ≥ MIRROR_MIN_RATIO × 直连
#   才改写（避免为微小差距去动锁文件），改写前仍做 sha256 一致性校验。
DIRECT_MIN_MBPS="${DIRECT_MIN_MBPS:-2}"
MIRROR_MIN_MBPS="${MIRROR_MIN_MBPS:-1}"
MIRROR_MIN_RATIO="${MIRROR_MIN_RATIO:-1.5}"

mirror_base_for() {
    case "$1" in
        aliyun)  printf '%s' 'https://mirrors.aliyun.com/pypi' ;;
        tencent) printf '%s' 'https://mirrors.cloud.tencent.com/pypi' ;;
        tuna)    printf '%s' 'https://pypi.tuna.tsinghua.edu.cn' ;;
        huawei)  printf '%s' 'https://repo.huaweicloud.com/repository/pypi' ;;
        ustc)    printf '%s' 'https://mirrors.ustc.edu.cn/pypi' ;;
    esac
}

# 从 uv.lock 里挑两个探测样本（同一份文件在所有镜像上都存在）：
#   VERIFY —— 体积最小的发行包，用于完整下载 + sha256 比对，证明「镜像与 PyPI
#             是同一份字节」。只有校验通过的基址才会被写进 uv.lock。
#   SPEED  —— ≥512KB 中最小的一个，用于量吞吐。样本必须够大才反映带宽：早期
#             版本拿最小文件（实测只有 2KB 的 types-certifi）量耗时，而 2KB 在
#             0.6s 内下完量到的是握手延迟、不是吞吐，于是把 49 KB/s 的链路判成
#             「直连健康」并跳过镜像。没有 ≥512KB 的制品时退回最大者。
mirror_probe_sample() {
    awk '
      match($0, /url = "https:\/\/files\.pythonhosted\.org\/[^"]+"/) {
        u = substr($0, RSTART + 7, RLENGTH - 8)
        p = u; sub(/^.*\/packages\//, "/packages/", p)
        match($0, /size = [0-9]+/)
        if (RSTART == 0) next
        s = substr($0, RSTART + 7, RLENGTH - 7) + 0
        if (s <= 0) next
        if (vs == 0 || s < vs) {
            vs = s; vp = p; vh = ""
            match($0, /hash = "sha256:[a-f0-9]+"/)
            if (RSTART > 0) vh = substr($0, RSTART + 15, RLENGTH - 16)
        }
        if (s >= 524288 && (ss == 0 || s < ss)) { ss = s; sp = p }
        if (s > bs) { bs = s; bp = p }
      }
      END {
        if (vp != "") printf "VERIFY\t%s\t%s\n", vp, vh
        printf "SPEED\t%s\n", (sp != "" ? sp : bp)
      }
    ' "$UVLOCK"
}

# 量吞吐（MB/s）：Range 只取前 1MB，--max-time 6 兜住慢链路。
# 超时（curl 退出码 28）不算失败——「已收字节 ÷ 已用时间」正是要的吞吐估计，
# 所以只看响应码与收到的字节数。任何异常都输出 0 并返回非零。
mirror_probe_speed() {
    local base="$1" out code got secs
    out="$(curl -sSL -r 0-1048575 -o /dev/null --max-time 6 \
        -w '%{http_code} %{size_download} %{time_total}' \
        "${base}${MIRROR_PROBE_PATH}" 2>/dev/null || true)"
    code="${out%% *}"
    case "$code" in 200|206) ;; *) printf '0'; return 1 ;; esac
    got="$(printf '%s' "$out" | awk '{print $2}')"
    secs="$(printf '%s' "$out" | awk '{print $3}')"
    [ "${got:-0}" -gt 0 ] 2>/dev/null || { printf '0'; return 1; }
    awk -v b="$got" -v t="${secs:-0}" 'BEGIN{ if (t<=0) t=0.001; printf "%.2f", (b/1048576)/t }'
}

# 完整下载 VERIFY 样本并比对 sha256，通过则输出耗时（秒）。
# 这是「镜像与 PyPI 同一份字节」的唯一证据，也是改写 uv.lock 的前提。
mirror_probe_verify() {
    local base="$1" dest="$2" out code elapsed
    out="$(curl -sSL -o "$dest" -w '%{http_code} %{time_total}' --max-time 20 \
        "${base}${MIRROR_VERIFY_PATH}" 2>/dev/null)" || return 1
    code="${out%% *}"; elapsed="${out##* }"
    [ "$code" = "200" ] || return 1
    [ "$(hash_of "$dest")" = "$MIRROR_PROBE_HASH" ] || return 1
    printf '%s' "$elapsed"
}

apply_mirror_lock() {
    mkdir -p "$PRISTINE_DIR"
    cp -p "$UVLOCK" "$UVLOCK_BACKUP"
    printf '%s' "$UVLOCK_SHA_BEFORE" > "$PRISTINE_DIR/uv.lock.sha256"
    awk -v base="$MIRROR_BASE" \
        '{ gsub(/https:\/\/files\.pythonhosted\.org\/packages\//, base "/packages/"); print }' \
        "$UVLOCK_BACKUP" > "$UVLOCK"
    # 把 mtime 拨回原值：PM 的激活戳（pm/environments.py 的 ACTIVATION_INPUTS）
    # 按 mtime 比对，一旦 uv.lock 的 mtime 变了，下次 `hermes` 启动会误判依赖变更
    # 而多做一次重新激活。cp -p 已把原 mtime 存进备份，这里直接沿用。
    touch -r "$UVLOCK_BACKUP" "$UVLOCK" 2>/dev/null || true
    LOCK_REWRITTEN=true
    trap 'restore_lock || true' EXIT INT TERM HUP
}

# 工具链镜像：改写 pm/lock.json 里的整段 URL 前缀。
#
# 收录标准刻意收得很紧——**只放经「PM 真实安装」验证跑通的规则**，理由见下：
#   候选 https://github.com/astral-sh/python-build-standalone/releases/download/
#        → https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/
#   单独用 curl -L 探测一切正常（200 / 26.7MB / sha256 逐位一致），但放进真实
#   构建后 PM 报：
#     ✗ python: install failed: HTTP Error 302: The HTTP server returned a redirect
#       error that would lead to an infinite loop.
#   PM 的 pm/downloader.py 用标准 urllib HTTPRedirectHandler，遇到「指回自身」的
#   302 就判为无限循环并硬失败（curl 会跟，urllib 不会）。NJU 的 CDN 会出现这种
#   瞬时自指跳转，事后复测又正常——即「平时能用、偶尔炸」。安装脚本炸掉比慢
#   几分钟严重得多，所以这条不收录。
#   （uv 托管的那份 CPython 不受影响：走 uv 自己的 HTTP 客户端，
#     --python-mirror 默认就指向 NJU，实测 25.3MiB/1.92s。）
# 收录的这条有真实安装背书：20260901 冷 store 构建中 PM 用它下载 node 28.8MiB 成功。
TOOL_MIRROR_RULES="
https://nodejs.org/dist/|https://npmmirror.com/mirrors/node/
"

# 探测必须走 PM 的同一条代码路径：PM 用 urllib 的 HTTPRedirectHandler，
# curl 会跟随而 urllib 会把「自指 302」判成无限循环。用 curl 探测会得出
# 「镜像可用」的错误结论（这正是 NJU/python 那条规则当初漏网的原因）。
tool_mirror_probe() { # $1=镜像基址前缀 $2=相对路径 → 能取到该路径即通过
    BLL_URL="${1}${2}" "$BOOT_PY" - <<'PY' 2>/dev/null
import os, urllib.request
try:
    with urllib.request.urlopen(os.environ["BLL_URL"], timeout=15) as response:
        if response.status != 200:
            raise SystemExit(1)
        response.read(262144)
except Exception:
    raise SystemExit(1)
PY
}

apply_tool_mirror_lock() {
    local src dst rel applied=0 rules_tmp picked
    rules_tmp="$(mktemp)"
    printf '%s\n' "$TOOL_MIRROR_RULES" | while IFS='|' read -r s d; do
        [ -n "${s:-}" ] || continue
        printf '%s|%s\n' "$s" "$d"
    done > "$rules_tmp" 2>/dev/null || true

    picked="$(mktemp)"
    while IFS='|' read -r src dst; do
        [ -n "${src:-}" ] || continue
        # 只有 lock 里确实存在该前缀、且镜像上能取到同一相对路径时才改写。
        # lock.json 是每行一个 "url": "…" 的 JSON，所以取出前缀之后必须截掉
        # 结尾的引号（以及可能跟随的逗号），否则探测 URL 尾部会多一个 " 而 404。
        rel="$(awk -v p="$src" -v t="$TARGET" '
            {
                i = index($0, p)
                if (i == 0) next
                tail = substr($0, i + length(p))
                j = index(tail, "\"")
                if (j > 0) tail = substr(tail, 1, j - 1)
                if (first == "") first = tail
                # lock 里同一前缀下有全平台的制品；优先挑当前平台的那条来探测
                if (best == "" && index(tail, t) > 0) best = tail
            }
            END { print (best != "" ? best : first) }
        ' "$LOCK")"
        [ -n "$rel" ] || continue
        if tool_mirror_probe "$dst" "$rel"; then
            printf '%s|%s\n' "$src" "$dst" >> "$picked"
        else
            warn "工具镜像探测失败，跳过该规则：${dst}"
        fi
    done < "$rules_tmp"
    rm -f "$rules_tmp"

    if [ ! -s "$picked" ]; then rm -f "$picked"; return 0; fi

    mkdir -p "$PRISTINE_DIR"
    cp -p "$LOCK" "$TOOLLOCK_BACKUP"
    printf '%s' "$TOOLLOCK_SHA_BEFORE" > "$PRISTINE_DIR/pm-lock.json.sha256"

    # 用 Python 的 str.replace 做字面替换（不经正则），只动 URL 子串，其余原样保留
    if ! BLL_LOCK="$LOCK" BLL_RULES="$picked" "$BOOT_PY" - <<'PY' 2>/dev/null
import os
path = os.environ["BLL_LOCK"]
data = open(path, encoding="utf-8").read()
with open(os.environ["BLL_RULES"], encoding="utf-8") as handle:
    for line in handle:
        line = line.rstrip("\n")
        if not line or "|" not in line:
            continue
        src, dst = line.split("|", 1)
        data = data.replace(src, dst)
open(path, "w", encoding="utf-8").write(data)
PY
    then
        warn "pm/lock.json 改写失败，回滚"
        restore_one_lock "$TOOLLOCK_BACKUP" "$LOCK" "pm/lock.json" "$TOOLLOCK_SHA_BEFORE"
        rm -f "$picked"
        return 0
    fi

    touch -r "$TOOLLOCK_BACKUP" "$LOCK" 2>/dev/null || true
    while IFS='|' read -r src dst; do
        [ -n "${src:-}" ] || continue
        applied=$((applied + 1))
        dim "  ${src}  →  ${dst}"
    done < "$picked"
    rm -f "$picked"
    TOOLLOCK_REWRITTEN=true
    trap 'restore_lock || true' EXIT INT TERM HUP
    ok "pm/lock.json 中 ${applied} 条工具地址已临时改写（原文件已备份，结束后自动还原）"
    dim "  未列入规则的制品（chromium / ffmpeg / cua-driver 等）没有可用国内镜像，仍走原始地址"
}

# -----------------------------------------------------------------------------
# 可选工具的裁剪（--without / --minimal / --with-browser）
# -----------------------------------------------------------------------------
# PM 把「可选默认工具」（agent-browser、cua-driver）与必需集分开装，并且把用户的
# 裁剪选择持久化在 <HERMES_HOME>/installs/<key>/declined-packages.json。
# 关键点：直接用 pm.cli install --without NAME 会让 PM 走「点名安装」分支
# （full_closure=False），从而不再推进激活戳。所以这里改为**先把裁剪记录写好**，
# 再跑一次不带参数的完整 install —— 这样既裁掉了工具，又保住了完整的依赖闭包。
decline_sync() {
    [ -n "$WITHOUT_OPT" ] || [ "$WITH_BROWSER" = true ] || return 0
    BLL_REPO="$REPO" BLL_WITHOUT="$WITHOUT_OPT" BLL_WITH_BROWSER="$WITH_BROWSER" \
    PYTHONHOME= PYTHONPATH="$REPO" "$BOOT_PY" - <<'PY' || return 0
import os, sys
sys.path.insert(0, os.environ["BLL_REPO"])
from pm.defaults import record_declined
add = [n for n in os.environ.get("BLL_WITHOUT", "").split() if n]
remove = ["agent-browser", "cua-driver"] if os.environ.get("BLL_WITH_BROWSER") == "true" else []
before = record_declined(add=add, remove=remove)
print("declined\t%s" % ",".join(sorted(before)))
PY
}

# -----------------------------------------------------------------------------
# 工具链下载预检
# -----------------------------------------------------------------------------
# 工具制品不在 PyPI 上，--mirror 的地址改写管不到它们；而 github.com /
# nodejs.org / cdn.playwright.dev 在国内常常只有 0.1~0.3 MB/s。全新机器首次
# 安装要拉 6~7 个制品、几百 MB，很容易让人以为卡死了。这里在开下载前把
# 「装什么、从哪来、多大、多久」摊开。全部失败即静默跳过，绝不阻断构建。
tool_plan() { # 输出 TSV：kind<TAB>name<TAB>url
    # 把本次将要写下的裁剪（--without / --minimal / --with-browser）一并算进来，
    # 否则 --minimal 已经把 Chromium 裁掉了，预检还在吓唬用户「162MB 要下 28 分钟」。
    BLL_REPO="$REPO" PYTHONHOME= PYTHONPATH="$REPO" \
    BLL_PLAN_WITHOUT="$WITHOUT_OPT" BLL_PLAN_WITH_BROWSER="$WITH_BROWSER" \
    "$BOOT_PY" - <<'PY' 2>/dev/null
import os, sys
sys.path.insert(0, os.environ["BLL_REPO"])
from pm.index_config import npm_registry_url
from pm.lock import Lockfile
from pm.paths import lockfile_path
from pm.registry import tool_roots, walk
from pm.defaults import default_packages, declined
from pm.store import current_target

lock = Lockfile(lockfile_path())
names = lock.names()
target = current_target()

refused = set(declined()) | {n for n in os.environ.get("BLL_PLAN_WITHOUT", "").split() if n}
if os.environ.get("BLL_PLAN_WITH_BROWSER") == "true":
    refused -= {"agent-browser", "cua-driver"}

plan = [("REQ", [p.name for p in walk(tool_roots(names))]),
        ("OPT", [p.name for p in walk(default_packages(names, declined_names=frozenset(refused)))])]
for kind, group in plan:
    for name in group:
        for artifact in lock.artifacts(name, target):
            url = artifact.get("url")
            if not url:
                continue
            # 与 PM 的下载走同一条重写规则：registry.npmjs.org 会被换成用户配置的
            # npm 镜像（本脚本默认 npmmirror）。不这样做的话预检会把 npm 系制品
            # 误判成走 registry.npmjs.org 的慢速直连。
            print("%s\t%s\t%s" % (kind, name, npm_registry_url(url, os.environ)))
            break
PY
}

tool_preflight() {
    [ "$TOOL_PREFLIGHT" = true ] || return 0
    [ -n "$BOOT_PY" ] || return 0

    _pf_plan="$(mktemp)"; _pf_miss="$(mktemp)"
    tool_plan > "$_pf_plan" 2>/dev/null || true
    if [ ! -s "$_pf_plan" ]; then rm -f "$_pf_plan" "$_pf_miss"; return 0; fi

    _pf_store="$(ls "$STORE" 2>/dev/null || true)"
    _pf_total=0; _pf_missing=0
    while IFS="$(printf '\t')" read -r kind name url; do
        [ -n "${name:-}" ] || continue
        case "$_pf_store" in *"$name-"*) continue ;; esac
        _pf_missing=$((_pf_missing + 1))
        printf '%s\t%s\t%s\n' "$kind" "$name" "$url" >> "$_pf_miss"
    done < "$_pf_plan"

    if [ "$_pf_missing" = 0 ]; then
        rm -f "$_pf_plan" "$_pf_miss"
        return 0
    fi

    info "工具链预检：store 内缺 $_pf_missing 个制品，正在并行测量各来源主机…"
    _pf_dir="$(mktemp -d)"
    _pf_rows="$_pf_dir/rows"; _pf_hosts="$_pf_dir/hosts"; _pf_speed="$_pf_dir/speed"
    : > "$_pf_rows"; : > "$_pf_hosts"; : > "$_pf_speed"

    # 第一轮：并行取各制品的 Content-Length（-I -L 跟随跳转，取最后一段的值）
    _pf_i=0
    while IFS="$(printf '\t')" read -r kind name url; do
        [ -n "${name:-}" ] || continue
        _pf_i=$((_pf_i + 1))
        (
            host="${url#*://}"; host="${host%%/*}"
            bytes="$(curl -sSIL --max-time 12 "$url" 2>/dev/null \
                     | awk 'BEGIN{IGNORECASE=1} /^content-length:/{v=$2} END{gsub(/\r/,"",v); print v+0}')"
            [ -n "$bytes" ] || bytes=0
            printf '%s\t%s\t%s\t%s\n' "$host" "$kind:$name" "$bytes" "$url" > "$_pf_dir/row.$_pf_i"
            printf '%s\n' "$host" > "$_pf_dir/host.$_pf_i"
        ) &
    done < "$_pf_miss"
    wait
    cat "$_pf_dir"/row.* > "$_pf_rows" 2>/dev/null || true
    cat "$_pf_dir"/host.* > "$_pf_hosts" 2>/dev/null || true
    sort -u "$_pf_hosts" > "$_pf_hosts.u" 2>/dev/null && mv "$_pf_hosts.u" "$_pf_hosts"

    # 第二轮：每个主机只测一次，取该主机任一制品的 256KB 采样算吞吐；同样并行
    _pf_i=0
    while read -r host; do
        [ -n "${host:-}" ] || continue
        _pf_i=$((_pf_i + 1))
        (
            sample="$(awk -F'\t' -v h="$host" '$1==h {print $4; exit}' "$_pf_rows")"
            out="$(curl -sSL -r 0-262143 -o /dev/null --max-time 10 \
                   -w '%{http_code} %{size_download} %{time_total}' "$sample" 2>/dev/null || true)"
            code="${out%% *}"; rest="${out#* }"; sd="${rest%% *}"; tt="${rest##* }"
            case "$code" in
                200|206) awk -v h="$host" -v s="$sd" -v t="$tt" \
                             'BEGIN{ if(t<=0)t=0.001; printf "%s\t%.3f\n", h, (s/1048576)/t }' \
                             > "$_pf_dir/sp.$_pf_i" ;;
                *)       printf '%s\t0\n' "$host" > "$_pf_dir/sp.$_pf_i" ;;
            esac
        ) &
    done < "$_pf_hosts"
    wait
    cat "$_pf_dir"/sp.* > "$_pf_speed" 2>/dev/null || true

    printf '\n'
    printf '  %-24s %-13s %-30s %-10s %s\n' '来源主机' '实测吞吐' '缺失制品' '体积' '估算'
    printf '  %s\n' '────────────────────────────────────────────────────────────────────────────────────'
    awk -F'\t' '
        FILENAME == ARGV[1] { speed[$1] = $2; next }
        {
            host = $1
            names[host] = names[host] (names[host] ? ", " : "") $2
            bytes[host] += $3
            order[host] = 1
        }
        END {
            total_mb = 0; total_s = 0; unreachable = 0; sized = 0
            for (h in order) {
                if (bytes[h] > 0) {
                    mb = bytes[h] / 1048576
                    size_txt = sprintf("%.1f MB", mb)
                    sized = 1
                } else {
                    mb = 0; size_txt = "大小未知"
                }
                sp = (h in speed) ? speed[h] : 0
                if (sp > 0 && mb > 0) {
                    secs = mb / sp
                    est = sprintf("%.1f 分钟", secs / 60)
                    total_mb += mb; total_s += secs
                } else if (sp > 0) {
                    est = "—"
                } else {
                    est = "—"; unreachable = 1
                }
                spd = (sp > 0) ? sprintf("%.2f MB/s", sp) : "不可达"
                printf "  %-24s %-13s %-30s %-10s %s\n", h, spd, names[h], size_txt, est
            }
            printf "  %s\n", "────────────────────────────────────────────────────────────────────────────────────"
            if (total_mb > 0)
                printf "  可估算部分合计 %.1f MB，预计 %.1f 分钟%s\n", total_mb, total_s / 60, \
                       (unreachable ? "（另有主机不可达，实际更久）" : "")
            else if (sized == 0)
                printf "  体积与吞吐均无法测定，跳过耗时估算\n"
        }
    ' "$_pf_speed" "$_pf_rows"

    _pf_slow="$(awk -F'\t' '
        FILENAME == ARGV[1] { sp[$1] = $2; next }
        { b[$1] += $3 }
        END {
            best = 0
            for (h in b) {
                if (!(h in sp) || sp[h] <= 0) continue
                secs = (b[h] / 1048576) / sp[h]
                if (secs > best) { best = secs; worst = h }
            }
            printf "%s\t%.0f", worst, best
        }
    ' "$_pf_speed" "$_pf_rows")"
    _pf_slowest="${_pf_slow%%	*}"
    _pf_slowest_secs="${_pf_slow##*	}"
    _pf_dead="$(awk -F'\t' '$2 <= 0 {print $1}' "$_pf_speed" | tr '\n' ' ' | sed 's/ *$//')"
    if [ -n "$_pf_dead" ]; then
        warn "以下来源当前不可达（连接超时，连不上而非仅慢）：${_pf_dead}"
        dim "  设代理后重跑：export HTTPS_PROXY=http://127.0.0.1:7890"
    fi
    # 只在真的慢（预计超过 3 分钟）时才提示——最慢的那台是本脚本已选用的镜像时，
    # 再说「没有国内镜像」既不准确也没意义。
    if [ -n "$_pf_slowest" ] \
       && awk -v t="${_pf_slowest_secs:-0}" 'BEGIN{exit !(t > 180)}'; then
        warn "最慢的来源是 ${_pf_slowest}（预计约 $(( _pf_slowest_secs / 60 )) 分钟）"
        dim "  该来源国内无更快镜像；如需进一步提速可设 HTTPS_PROXY 后重跑"
    fi
    if ! grep -q '^REQ' "$_pf_miss" && grep -q '^OPT' "$_pf_miss"; then
        dim "  提示：缺的都是可选工具，其中 Chromium（约 150MB，cdn.playwright.dev 约 0.1 MB/s）"
        dim "       只被 agent-browser 需要——不需要浏览器自动化时加 --minimal 可直接跳过。"
    fi
    rm -f "$_pf_plan" "$_pf_miss"
    rm -rf "$_pf_dir" 2>/dev/null || true
}

if [ "$MIRROR_MODE" != none ]; then
    if [ -n "$NPM_REGISTRY_OPT" ] || [ -n "$MIRROR_BASE_OPT" ] || [ "$SKIP_RUNTIME" != true ]; then
        export npm_config_registry="$NPM_REGISTRY"
        # PM 在 FORWARDED_UV_SETTINGS 里会把这个值转发给 uv（其余 UV_* 会被剥离）
        export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-300}"
    fi
fi

if [ "$MIRROR_MODE" != none ] && [ "$SKIP_RUNTIME" != true ]; then
    step "网络探测与镜像选择（--mirror ${MIRROR_MODE}）"

    PROBE_SAMPLE="$(mirror_probe_sample)"
    MIRROR_VERIFY_PATH="$(printf '%s\n' "$PROBE_SAMPLE" | awk -F'\t' '$1=="VERIFY"{print $2}')"
    MIRROR_PROBE_HASH="$(printf '%s\n' "$PROBE_SAMPLE" | awk -F'\t' '$1=="VERIFY"{print $3}')"
    MIRROR_PROBE_PATH="$(printf '%s\n' "$PROBE_SAMPLE" | awk -F'\t' '$1=="SPEED"{print $2}')"

    if [ -z "$MIRROR_PROBE_PATH" ] || [ -z "$MIRROR_VERIFY_PATH" ] || [ -z "$MIRROR_PROBE_HASH" ]; then
        warn "无法从 uv.lock 提取探测样本，跳过镜像改写"
    else
        PROBE_TMP="$(mktemp)"
        if [ -n "$MIRROR_BASE_OPT" ]; then
            if elapsed="$(mirror_probe_verify "$MIRROR_BASE_OPT" "$PROBE_TMP")"; then
                MIRROR_BASE="$MIRROR_BASE_OPT"
                MIRROR_LABEL="${MIRROR_BASE_OPT}"
                MIRROR_DETAIL="指定基址，样本 sha256 校验通过（${elapsed}s）"
            else
                rm -f "$PROBE_TMP"
                die "指定的 --mirror-base 校验失败（下载或 sha256 不符）：${MIRROR_BASE_OPT}"
            fi
        elif [ "$MIRROR_MODE" = auto ]; then
            direct_mbps="$(mirror_probe_speed 'https://files.pythonhosted.org' || true)"
            if awk -v s="$direct_mbps" -v min="$DIRECT_MIN_MBPS" 'BEGIN{exit !(s >= min)}'; then
                MIRROR_LABEL="直连"
                MIRROR_DETAIL="直连吞吐 ${direct_mbps} MB/s ≥ ${DIRECT_MIN_MBPS} MB/s，无需改写"
            else
                dim "  直连 files.pythonhosted.org 实测 ${direct_mbps} MB/s（< ${DIRECT_MIN_MBPS} MB/s），改测国内镜像"
                best_mbps=0; best_base=""; best_name=""
                for name in aliyun tencent tuna huawei ustc; do
                    base="$(mirror_base_for "$name")"
                    [ -n "$base" ] || continue
                    mbps="$(mirror_probe_speed "$base" || true)"
                    dim "  ${name} ${mbps} MB/s"
                    if awk -v a="$mbps" -v b="$best_mbps" 'BEGIN{exit !(a > b)}'; then
                        best_mbps="$mbps"; best_base="$base"; best_name="$name"
                    fi
                done
                if [ -n "$best_base" ] \
                   && awk -v s="$best_mbps" -v min="$MIRROR_MIN_MBPS" 'BEGIN{exit !(s >= min)}' \
                   && awk -v s="$best_mbps" -v d="$direct_mbps" -v r="$MIRROR_MIN_RATIO" 'BEGIN{exit !(s >= d * r)}'; then
                    # 只有 sha256 校验通过才改写：这一步保证镜像与 PyPI 是同一份字节
                    if elapsed="$(mirror_probe_verify "$best_base" "$PROBE_TMP")"; then
                        speedup="$(awk -v a="$best_mbps" -v b="$direct_mbps" \
                            'BEGIN{ if (b <= 0) b = 0.01; printf "%.0f", a / b }')"
                        MIRROR_BASE="$best_base"
                        MIRROR_LABEL="$best_name"
                        MIRROR_DETAIL="直连 ${direct_mbps} MB/s → ${best_name} ${best_mbps} MB/s，约 ${speedup} 倍"
                    else
                        warn "${best_name} 样本 sha256 校验失败，保持直连（不改写 uv.lock）"
                        MIRROR_DETAIL="最优镜像校验失败，保持直连"
                    fi
                else
                    warn "镜像未显著快于直连（最优 ${best_mbps} MB/s vs 直连 ${direct_mbps} MB/s），保持直连"
                    MIRROR_DETAIL="镜像未显著更快，保持直连"
                fi
            fi
        else
            base="$(mirror_base_for "$MIRROR_MODE")"
            if elapsed="$(mirror_probe_verify "$base" "$PROBE_TMP")"; then
                MIRROR_BASE="$base"
                MIRROR_LABEL="$MIRROR_MODE"
                MIRROR_DETAIL="指定镜像，样本 sha256 校验通过（${elapsed}s）"
            else
                rm -f "$PROBE_TMP"
                die "镜像 ${MIRROR_MODE}（${base}）校验失败：下载失败或 sha256 不一致"
            fi
        fi
        rm -f "$PROBE_TMP"

        if [ -n "$MIRROR_BASE" ]; then
            ok "使用镜像 ${MIRROR_LABEL}：${MIRROR_BASE}"
            dim "  ${MIRROR_DETAIL}"
            dim "  探测样本 sha256 与 uv.lock 记录一致（镜像是同一份字节）"
            apply_mirror_lock
            ok "uv.lock 下载地址已临时改写（原文件已备份，结束后自动还原）"
        else
            dim "  ${MIRROR_DETAIL}"
        fi
    fi
fi

if [ "$MIRROR_MODE" != none ] && [ "$NPM_REGISTRY_OPT" != "" ]; then
    dim "  npm registry → ${NPM_REGISTRY}"
fi

# --mirror-index：把「解析索引」也指向镜像。注意这与上面的下载地址改写是两件事：
#   下载（wheel/sdist 本体）由 uv.lock 里的绝对 URL 决定 —— 只能靠改写；
#   解析（build-isolation 的构建后端依赖、sdist 构建时的依赖）才走索引。
# PM 的 pm/index_config.py 会把 UV_INDEX_URL / UV_DEFAULT_INDEX / PIP_INDEX_URL
# 转发给 uv，其余 UV_* 会被剥离。
# 默认关闭：本仓库 pyproject.toml 设了 exclude-newer（按上传时间过滤），而部分
# 国内镜像未提供完整的 upload-time 元数据，一旦触发重新解析会报
# 「has no publish time」。所以只在明确需要时打开。
if [ "$MIRROR_INDEX" = true ]; then
    if [ -n "$MIRROR_BASE" ]; then
        MIRROR_SIMPLE="${MIRROR_BASE%/}/simple"
        export UV_INDEX_URL="$MIRROR_SIMPLE"
        export UV_DEFAULT_INDEX="$MIRROR_SIMPLE"
        export PIP_INDEX_URL="$MIRROR_SIMPLE"
        ok "解析索引已指向 ${MIRROR_SIMPLE}"
        dim "  只影响解析与 sdist 构建依赖；--frozen 的 wheel 仍走改写后的下载地址"
    else
        warn "--mirror-index 已忽略：当前是直连，没有可用的镜像索引"
    fi
fi

# -----------------------------------------------------------------------------
# 步骤 1：引导 Python 运行时（uv → CPython → PM 依赖同步）
# -----------------------------------------------------------------------------
if [ "$SKIP_RUNTIME" = true ]; then
    step "跳过运行时引导（--skip-runtime）"
    [ -n "$BOOT_PY" ] || die "工具 store 中没有可用 Python，无法跳过运行时引导"
    ok "复用 store Python：$BOOT_PY"
else
    step "引导 Python 运行时（uv → CPython → PM 依赖同步）"
    UV=""
    if [ -n "$BOOT_PY" ]; then
        ok "复用 store Python：$BOOT_PY"
    else
        stage_uv
        PY_REQUEST="$(pin version python | cut -d+ -f1 | cut -d. -f1,2)"
        [ -n "$PY_REQUEST" ] || die "pm/lock.json 中缺少 python 的版本 pin"
        if [ -n "$PYTHON_MIRROR_OPT" ]; then
            export UV_PYTHON_INSTALL_MIRROR="$PYTHON_MIRROR_OPT"
            dim "  CPython 下载镜像：${UV_PYTHON_INSTALL_MIRROR}"
        fi
        info "安装 CPython $PY_REQUEST ..."
        "$UV" python install --no-bin --no-registry "$PY_REQUEST"
        BOOT_PY="$("$UV" python find --managed-python "$PY_REQUEST")"
        BOOT_PY="${BOOT_PY%$'\r'}"
        [ -n "$BOOT_PY" ] && [ -x "$BOOT_PY" ] || die "无法定位被托管的 Python 解释器"
        ok "引导 Python：$BOOT_PY"
    fi

    if [ "$SHARED_STORE" = true ]; then
        dim "  工具 store 为共享复用：pm 只按「版本+平台」新增条目，不会删除既有条目"
        dim "  构建结束会对比 store 条目增删，任何写入都会明确列出"
    fi

    step "工具链预检与可选工具裁剪"

    # 先应用工具镜像改写，再预检：这样预检测到的是**实际会用的地址**，
    # 否则会把已被改写掉的慢源（比如 nodejs.org）当成真实来源报出来。
    if [ "$TOOL_MIRROR" != none ]; then
        apply_tool_mirror_lock
    fi

    tool_preflight

    DECLINED_NOW="$(decline_sync | awk -F'\t' '$1=="declined"{print $2}')"
    if [ -n "$DECLINED_NOW" ]; then
        ok "本次跳过可选工具：${DECLINED_NOW}"
        dim "  该选择被 PM 记在 <HERMES_HOME>/installs/<key>/declined-packages.json，后续 install/update 沿用"
        dim "  想装回来：--with-browser（或删掉上面那个文件）"
    elif [ "$WITH_BROWSER" = true ]; then
        ok "已清除可选用具的裁剪记录，将完整安装"
    fi

    info "同步依赖（python -m pm.cli install，首次可能耗时 1-5 分钟）..."
    pm_args=(install)
    [ "$WITH_TEST_ENV" = true ] && pm_args+=(--test-environment)
    # 在仓库根执行：`python -m` 依赖 CWD 找到 pm 包（同 setup-hermes.sh）
    ( cd "$REPO" && PYTHONHOME= PYTHONPATH= "$BOOT_PY" -m pm.cli "${pm_args[@]}" ) \
        || die "PM 安装失败（见上方输出）"
    ok "依赖已同步"

    # 下载已结束，立刻把被改写的锁文件还原，缩短改写窗口（trap 仍是兜底）
    if [ "$LOCK_REWRITTEN" = true ] || [ "$TOOLLOCK_REWRITTEN" = true ]; then
        restore_lock
    fi
fi

# -----------------------------------------------------------------------------
# 步骤 2：加载 PM 组合环境（node / npm / ripgrep / ffmpeg / 工具 PATH）
# -----------------------------------------------------------------------------
step "读取 PM 组合环境"

PM_ENV_SH="$(PYTHONHOME= PYTHONPATH="$REPO" "$BOOT_PY" -m pm.environments | PYTHONHOME= PYTHONPATH="$REPO" "$BOOT_PY" -c '
import json, re, shlex, sys
for key, value in json.load(sys.stdin).items():
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        continue
    print("export %s=%s" % (key, shlex.quote(str(value))))
')" || die "无法读取 PM 组合环境（pm.environments 失败）"
[ -n "$PM_ENV_SH" ] || die "PM 组合环境为空"
eval "$PM_ENV_SH"
ok "PM 环境已加载"

# 解析 PM 选定的应用解释器：核心依赖装在它下面，Pillow / resvg-py 只在它里面
APP_PY="$(PYTHONHOME= PYTHONPATH="$REPO" "$BOOT_PY" -c '
import sys
from pathlib import Path
from pm.environments import committed_venv, venv_bin_dir
venv = committed_venv(Path(sys.argv[1]))
if venv is None:
    sys.exit(1)
bin_dir = venv_bin_dir(venv)
for name in ("python3", "python"):
    if (bin_dir / name).exists():
        print(bin_dir / name)
        break
else:
    sys.exit(1)
' "$REPO" 2>/dev/null || true)"

if [ -n "$APP_PY" ] && [ -x "$APP_PY" ]; then
    ok "应用解释器：$APP_PY"
else
    APP_PY="${PYTHON:-python3}"
    warn "未能解析 PM 选定的解释器，回退到：$APP_PY"
fi

if ! "$APP_PY" -c 'import httpx, pydantic, fastapi' 2>/dev/null; then
    warn "核心依赖导入校验未通过（httpx / pydantic / fastapi），请检查上方 PM 输出"
fi

HAS_NODE=false
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    HAS_NODE=true
    ok "Node $(node --version) / npm $(npm --version)"
else
    warn "PATH 中找不到 node/npm，前端编译将跳过"
fi

# -----------------------------------------------------------------------------
# 步骤 3-7：JS 依赖 + 前端编译
# -----------------------------------------------------------------------------
BUILT_TUI=false
BUILT_WEB=false
BUILT_DESKTOP=false

if [ "$SKIP_JS" = true ]; then
    step "跳过前端编译（--skip-js）"
elif [ "$HAS_NODE" = false ]; then
    step "跳过前端编译（缺少 node/npm）"
else
    step "安装 JS 依赖（根目录 npm ci，覆盖全部 workspace）"
    # --- 二进制 postinstall 的国内镜像 ---
    # npm ci 的包体已经走 registry 镜像，但有些包的 postinstall 会**绕开 registry**
    # 直接去 GitHub Releases 拉预编译二进制，registry 配置对它们完全无效：
    #   electron@40.10.2 → github.com/electron/electron/releases/…darwin-arm64.zip
    #     109 MiB，本机实测 connect ETIMEDOUT (20.205.243.166:443)
    #   node-gyp（node-pty 回退到本地编译时用）→ nodejs.org 的头文件，0.04 MB/s
    #
    # 桌面端编译本来就是 opt-in（--with-desktop），默认构建用不到 Electron 的运行时
    # 二进制，所以默认直接跳过下载：electron/install.js 第 14 行的
    # ELECTRON_SKIP_BINARY_DOWNLOAD 会在任何网络动作之前 exit 0。只有 --with-desktop
    # 才真正下载，并改走 npmmirror（实测 1.76 MB/s，109 MiB 约 1 分钟；
    # 校验用的是包内自带的 checksums.json，不依赖镜像提供的 SHASUMS256.txt）。
    export NODEJS_ORG_MIRROR="${NODEJS_ORG_MIRROR:-https://npmmirror.com/mirrors/node/}"
    if [ "$WITH_DESKTOP" = true ]; then
        # 置空而非 unset：Node 读到的空串是 falsy，install.js 会照常下载
        export ELECTRON_SKIP_BINARY_DOWNLOAD=""
        export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}"
        if [ "$ELECTRON_MIRROR_OPT" = none ]; then
            dim "  Electron 二进制：GitHub 官方源（--electron-mirror none）"
        else
            export ELECTRON_MIRROR="${ELECTRON_MIRROR_OPT:-${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}}"
            dim "  Electron 二进制：${ELECTRON_MIRROR}（约 109 MiB）"
        fi
    else
        export ELECTRON_SKIP_BINARY_DOWNLOAD="${ELECTRON_SKIP_BINARY_DOWNLOAD:-1}"
        dim "  跳过 Electron 运行时二进制（默认不编译桌面端；要装加 --with-desktop）"
    fi

    ( cd "$REPO" && npm ci ) || die "npm ci 失败"
    ok "JS 依赖已安装"

    step "生成图标（scripts/generate_icons.py）"
    rm -rf "$ICONS_DIR"
    mkdir -p "$ICONS_DIR"
    "$APP_PY" "$REPO/scripts/generate_icons.py" --source "$REPO" --out "$ICONS_DIR" \
        || die "图标生成失败"
    ok "图标 → $ICONS_DIR"

    step "编译 TUI（ui-tui → products/tui）"
    node "$REPO/scripts/build/tui.mjs" --source "$REPO" --out "$PRODUCTS_DIR/tui" \
        || die "TUI 编译失败"
    [ -f "$PRODUCTS_DIR/tui/dist/entry.js" ] || die "TUI 产物缺少 dist/entry.js"
    ok "TUI → $PRODUCTS_DIR/tui/dist/entry.js"
    BUILT_TUI=true

    step "编译 Dashboard（web → products/web）"
    node "$REPO/scripts/build/web.mjs" --source "$REPO" \
        --out "$PRODUCTS_DIR/web" --icons "$ICONS_DIR" \
        || die "Dashboard 编译失败"
    [ -f "$PRODUCTS_DIR/web/index.html" ] || die "Dashboard 产物缺少 index.html"
    ok "Dashboard → $PRODUCTS_DIR/web/index.html"
    BUILT_WEB=true

    if [ "$WITH_DESKTOP" = true ]; then
        step "编译桌面端（Electron，耗时最长）"
        dim "  会编译 node-pty 等原生模块，并写入 apps/desktop/{dist,build}"
        if [ ! -f "$REPO/apps/desktop/node_modules/electron/path.txt" ]; then
            warn "未检测到 Electron 运行时二进制（apps/desktop/node_modules/electron/path.txt）"
            dim "  打包时会缺 Electron；修复：cd $REPO && npm run install:desktop"
        fi
        ( cd "$REPO" && npm run build --workspace apps/desktop ) || die "桌面端编译失败"
        rm -rf "$PRODUCTS_DIR/desktop"
        mkdir -p "$PRODUCTS_DIR/desktop"
        cp -R "$REPO/apps/desktop/dist/." "$PRODUCTS_DIR/desktop/"
        ok "桌面端 → $PRODUCTS_DIR/desktop"
        BUILT_DESKTOP=true
    fi
fi

# -----------------------------------------------------------------------------
# 步骤 8：同步内置 skills（尽力而为）
# -----------------------------------------------------------------------------
if [ -d "$REPO/skills" ]; then
    step "同步内置 skills 到 $HERMES_HOME/skills"
    if "$APP_PY" -m tools.skills_sync >/dev/null 2>&1; then
        ok "skills 已同步"
    else
        mkdir -p "$HERMES_HOME/skills"
        cp -Rn "$REPO/skills/." "$HERMES_HOME/skills/" 2>/dev/null || true
        warn "tools.skills_sync 未成功，已回退为直接复制"
    fi
fi

# -----------------------------------------------------------------------------
# 步骤 9：.env
# -----------------------------------------------------------------------------
if [ "$COPY_ENV" = true ] && [ -f "$HOME/.hermes/.env" ]; then
    step "复制密钥到开发 HERMES_HOME（--copy-env）"
    cp "$HOME/.hermes/.env" "$HERMES_HOME/.env"
    chmod 600 "$HERMES_HOME/.env" 2>/dev/null || true
    ok "$HERMES_HOME/.env"
elif [ "$COPY_ENV" = true ]; then
    warn "未找到 $HOME/.hermes/.env（本机没有可复制的正式安装），改从 .env.example 生成模板"
    if [ ! -f "$HERMES_HOME/.env" ] && [ -f "$REPO/.env.example" ]; then
        cp "$REPO/.env.example" "$HERMES_HOME/.env"
        chmod 600 "$HERMES_HOME/.env" 2>/dev/null || true
        ok "已生成 $HERMES_HOME/.env，请填入 API key（或稍后运行 hermes setup）"
    fi
elif [ ! -f "$HERMES_HOME/.env" ] && [ -f "$REPO/.env.example" ]; then
    cp "$REPO/.env.example" "$HERMES_HOME/.env"
    chmod 600 "$HERMES_HOME/.env" 2>/dev/null || true
    ok "已从 .env.example 生成模板：$HERMES_HOME/.env（需自行填 key，或用 --copy-env）"
fi

# -----------------------------------------------------------------------------
# 步骤 10：发布启动器 + 生成 activate.sh
# -----------------------------------------------------------------------------
step "发布启动器到 $BIN_DIR"

PYTHONHOME= PYTHONPATH="$REPO" \
HERMES_HOME="$HERMES_HOME" HERMES_RUNTIME_DIR="$STORE" \
BLL_REPO="$REPO" BLL_BIN="$BIN_DIR" BLL_PRODUCTS="$PRODUCTS_DIR" \
"$BOOT_PY" - <<'PY' || die "启动器发布失败"
import os, stat, sys
from pathlib import Path

repo = Path(os.environ["BLL_REPO"])
bin_dir = Path(os.environ["BLL_BIN"])
products = Path(os.environ["BLL_PRODUCTS"])

sys.path.insert(0, str(repo))
from hermes_cli._launchers import stage_launcher

extra = {
    "HERMES_HOME": os.environ["HERMES_HOME"],
    "HERMES_RUNTIME_DIR": os.environ["HERMES_RUNTIME_DIR"],
}
if (products / "tui" / "dist" / "entry.js").is_file():
    extra["HERMES_TUI_DIR"] = str(products / "tui")
if (products / "web" / "index.html").is_file():
    extra["HERMES_WEB_DIST"] = str(products / "web")

import shlex

for name in ("hermes", "hermes-acp"):
    path = stage_launcher(name, repo, bin_dir)
    if path is None:
        sys.exit("launcher publication failed: %s" % name)
    # 上游启动器是多行 exec：shlex.join 把整段 Python bootstrap 包成单个
    # 引号参数，末尾一行才带上 '"$@"'。只能整段搬运，取单行会把脚本截断成
    # 未闭合的引号（unexpected EOF）。
    text = Path(path).read_text(encoding="utf-8").splitlines()
    if text and text[0].startswith("#!"):
        text = text[1:]
    while text and not text[0].strip():
        text.pop(0)
    if not text or not text[0].startswith("exec "):
        sys.exit("unexpected launcher shape: %s" % path)
    header = ["#!/bin/sh", "# 由 build-local.sh 生成 —— 绑定本 checkout 的开发环境"]
    header += ["export %s=%s" % (k, shlex.quote(v)) for k, v in extra.items()]
    Path(path).write_text("\n".join([*header, *text]) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("  " + str(path))
PY
ok "启动器已发布"

step "生成 $OUT/activate.sh"
PYTHONHOME= \
BLL_OUT="$OUT" BLL_REPO="$REPO" BLL_BOOT_PY="$BOOT_PY" \
BLL_HOME="$HERMES_HOME" BLL_STORE="$STORE" \
BLL_BUILT_TUI="$BUILT_TUI" BLL_BUILT_WEB="$BUILT_WEB" \
BLL_TUI_DIR="$PRODUCTS_DIR/tui" BLL_WEB_DIST="$PRODUCTS_DIR/web" \
"$BOOT_PY" - <<'PY' || die "activate.sh 生成失败"
import os, shlex
from pathlib import Path

out = Path(os.environ["BLL_OUT"])
repo = Path(os.environ["BLL_REPO"])
boot_py = os.environ["BLL_BOOT_PY"]
optional = ""

if os.environ["BLL_BUILT_TUI"] == "true":
    optional += "export HERMES_TUI_DIR=%s\n" % shlex.quote(os.environ["BLL_TUI_DIR"])
if os.environ["BLL_BUILT_WEB"] == "true":
    optional += "export HERMES_WEB_DIST=%s\n" % shlex.quote(os.environ["BLL_WEB_DIST"])

body = """#!/bin/sh
# Hermes 本地开发环境 — 由 build-local.sh 生成，请勿手工编辑
# 用法:  source {out}/activate.sh      离开:  deactivate
#
# 激活只是「借用」这个 shell：HERMES_HOME 一旦泄漏到 deactivate 之后，生产版本的
# 启动器（hermes_cli 的 shim）会读它，于是拿「开发 home + 生产 checkout 的 install
# key」去找依赖环境；找不到就重新下载 Python 依赖。所以先按变量名快照旧值，
# deactivate 时整段还原（含 PATH 与 pm.environments 导出的 PYTHONPATH 等）。
__hermes_build_out={out_q}
__hermes_build_repo={repo_q}
__hermes_boot_py={boot_q}
__hermes_saved_path="$PATH"

unset PYTHONHOME VIRTUAL_ENV 2>/dev/null || true
__hermes_activation=""
if [ -x "$__hermes_boot_py" ]; then
    __hermes_activation="$(PYTHONHOME= PYTHONPATH="$__hermes_build_repo" "$__hermes_boot_py" -m pm.environments 2>/dev/null | "$__hermes_boot_py" -c '
import json, re, shlex, sys
for key, value in json.load(sys.stdin).items():
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        print("export %s=%s" % (key, shlex.quote(str(value))))
')"
fi

# 重复 source 时保留第一次的快照，deactivate 回到真正干净的环境。
if [ -z "${{__hermes_restore:-}}" ]; then
    __hermes_restore="$(printf '%s\\n' "$__hermes_activation" | "$__hermes_boot_py" -c '
import os, re, shlex, sys
names = []
for line in sys.stdin:
    found = re.match(r"export ([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
    if found:
        names.append(found.group(1))
seen = []
for name in [*names, "PATH", "HERMES_HOME", "HERMES_RUNTIME_DIR",
             "HERMES_TUI_DIR", "HERMES_WEB_DIST"]:
    if name not in seen:
        seen.append(name)
for name in seen:
    if name in os.environ:
        print("export %s=%s" % (name, shlex.quote(os.environ[name])))
    else:
        print("unset %s" % name)
' 2>/dev/null)"
fi

if [ -n "$__hermes_activation" ]; then
    eval "$__hermes_activation"
fi

export HERMES_HOME={home_q}
export HERMES_RUNTIME_DIR={store_q}
{optional}export PATH="$__hermes_build_out/bin:$PATH"

deactivate() {{
    if [ -n "${{__hermes_restore:-}}" ]; then
        eval "$__hermes_restore"
    else
        export PATH="$__hermes_saved_path"
        unset HERMES_HOME HERMES_RUNTIME_DIR HERMES_TUI_DIR HERMES_WEB_DIST
    fi
    unset __hermes_activation __hermes_restore
    unset __hermes_build_out __hermes_build_repo __hermes_boot_py __hermes_saved_path
    unset -f deactivate
    echo "hermes: 已退出本地开发环境（HERMES_HOME / PATH 已还原）"
}}

echo "hermes: 本地开发环境已激活 → {out}"
echo "        hermes / hermes --tui / hermes dashboard / hermes setup"
""".format(
    out=out,
    out_q=shlex.quote(str(out)),
    repo_q=shlex.quote(str(repo)),
    boot_q=shlex.quote(boot_py),
    home_q=shlex.quote(os.environ["BLL_HOME"]),
    store_q=shlex.quote(os.environ["BLL_STORE"]),
    optional=optional,
)

target = out / "activate.sh"
target.write_text(body, encoding="utf-8")
target.chmod(0o755)
print("  " + str(target))
PY
ok "激活脚本：source $OUT/activate.sh"

# -----------------------------------------------------------------------------
# 步骤 11：可选校验
# -----------------------------------------------------------------------------
if [ "$DO_VERIFY" = true ]; then
    step "校验安装状态（python -m pm.cli doctor）"
    ( cd "$REPO" && PYTHONHOME= PYTHONPATH= "$BOOT_PY" -m pm.cli doctor ) \
        || warn "pm doctor 报告了问题（见上方输出）"
fi

# -----------------------------------------------------------------------------
# 步骤 12：构建清单
# -----------------------------------------------------------------------------
GIT_HEAD="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
GIT_BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

PYTHONHOME= \
BLL_OUT="$OUT" BLL_REPO="$REPO" BLL_PRODUCTS="$PRODUCTS_DIR" \
BLL_HEAD="$GIT_HEAD" BLL_BRANCH="$GIT_BRANCH" BLL_TARGET="$TARGET" \
BLL_HOME="$HERMES_HOME" BLL_STORE="$STORE" \
BLL_MIRROR_MODE="$MIRROR_MODE" BLL_MIRROR_LABEL="$MIRROR_LABEL" \
BLL_MIRROR_BASE="$MIRROR_BASE" BLL_NPM_REGISTRY="$NPM_REGISTRY" \
BLL_LOCK_REWRITTEN="$LOCK_REWRITTEN" BLL_MIRROR_INDEX="$MIRROR_INDEX" \
BLL_DECLINED="$DECLINED_NOW" \
BLL_BOOT_PY="$BOOT_PY" BLL_APP_PY="$APP_PY" \
BLL_BUILT_TUI="$BUILT_TUI" BLL_BUILT_WEB="$BUILT_WEB" BLL_BUILT_DESKTOP="$BUILT_DESKTOP" \
"$BOOT_PY" - <<'PY' || warn "构建清单写入失败"
import json, os, time
from pathlib import Path

out = Path(os.environ["BLL_OUT"])
products = Path(os.environ["BLL_PRODUCTS"])
truthy = lambda name: os.environ.get(name) == "true"

def product(name, marker):
    artifact = products / name / marker
    return {"path": str(products / name), "artifact": str(artifact)} if artifact.is_file() else None

manifest = {
    "schema": "hermes-build-local-v1",
    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "repo": os.environ["BLL_REPO"],
    "git": {"head": os.environ["BLL_HEAD"], "branch": os.environ["BLL_BRANCH"]},
    "target": os.environ["BLL_TARGET"],
    "hermesHome": os.environ["BLL_HOME"],
    "runtimeDir": os.environ["BLL_STORE"],
    "mirror": {
        "mode": os.environ["BLL_MIRROR_MODE"],
        "label": os.environ["BLL_MIRROR_LABEL"],
        "base": os.environ["BLL_MIRROR_BASE"],
        "npmRegistry": os.environ["BLL_NPM_REGISTRY"],
        "lockRewritten": os.environ["BLL_LOCK_REWRITTEN"] == "true",
        "indexMirror": (os.environ["BLL_MIRROR_BASE"].rstrip("/") + "/simple")
                       if os.environ.get("BLL_MIRROR_INDEX") == "true" and os.environ["BLL_MIRROR_BASE"] else None,
    },
    "tools": {
        "declined": [n for n in os.environ.get("BLL_DECLINED", "").split(",") if n],
    },
    "python": {"bootstrap": os.environ["BLL_BOOT_PY"], "application": os.environ["BLL_APP_PY"]},
    "products": {
        "tui": product("tui", "dist/entry.js") if truthy("BLL_BUILT_TUI") else None,
        "web": product("web", "index.html") if truthy("BLL_BUILT_WEB") else None,
        "desktop": {"path": str(products / "desktop")} if truthy("BLL_BUILT_DESKTOP") else None,
    },
    "icons": str(out / "icons"),
    "launchers": {"binDir": str(out / "bin"), "names": ["hermes", "hermes-acp"]},
    "activate": str(out / "activate.sh"),
}
(out / "build-manifest.json").write_text(
    json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
ok "构建清单：$OUT/build-manifest.json"

# -----------------------------------------------------------------------------
# 步骤 13：核对生产安装未被改动
# -----------------------------------------------------------------------------
PROD_CHANGED=false
PROD_DIFF=""

if [ -d "$PROD_HOME" ] && [ -n "${PROD_BEFORE:-}" ] && [ -f "$PROD_BEFORE" ]; then
    step "核对生产安装（${PROD_HOME}）未被改动"
    PROD_AFTER="$(mktemp)"
    production_fingerprint > "$PROD_AFTER"
    if diff -u "$PROD_BEFORE" "$PROD_AFTER" > /dev/null 2>&1; then
        ok "生产安装指纹一致，未被本次构建改动"
    else
        PROD_CHANGED=true
        PROD_DIFF="$(diff -u "$PROD_BEFORE" "$PROD_AFTER" | sed -n '3,40p')"
        warn "生产安装指纹发生变化："
        printf '%s\n' "$PROD_DIFF" | sed 's/^/    /' >&2
        warn "请逐项确认上述差异是否可接受（共享工具 store 的 facts.json 重写属正常）"
    fi
    rm -f "$PROD_AFTER"

    # 共享 store 的具体增删（比哈希更可读，直接告诉你多了/少了哪个工具）
    if [ "$SHARED_STORE" = true ] && [ -s "$STORE_ENTRIES_BEFORE" ]; then
        STORE_ENTRIES_AFTER="$(mktemp)"
        ( find "$STORE" -maxdepth 1 -mindepth 1 -exec basename {} \; 2>/dev/null \
            | grep -v '^\.' | sort > "$STORE_ENTRIES_AFTER" ) || true
        STORE_ADDED="$(comm -13 "$STORE_ENTRIES_BEFORE" "$STORE_ENTRIES_AFTER" || true)"
        STORE_REMOVED="$(comm -23 "$STORE_ENTRIES_BEFORE" "$STORE_ENTRIES_AFTER" || true)"
        if [ -n "$STORE_ADDED" ]; then
            warn "共享工具 store 新增条目（仅新增，不影响既有安装）："
            printf '%s\n' "$STORE_ADDED" | sed 's/^/    + /' >&2
        fi
        if [ -n "$STORE_REMOVED" ]; then
            warn "共享工具 store 条目减少（需关注）："
            printf '%s\n' "$STORE_REMOVED" | sed 's/^/    - /' >&2
            PROD_CHANGED=true
        fi
        [ -z "${STORE_ADDED}${STORE_REMOVED}" ] && ok "共享工具 store 条目无增无减"
        rm -f "$STORE_ENTRIES_AFTER"
    fi
fi
[ -n "${PROD_BEFORE:-}" ] && rm -f "$PROD_BEFORE"
[ -n "${STORE_ENTRIES_BEFORE:-}" ] && rm -f "$STORE_ENTRIES_BEFORE"

# -----------------------------------------------------------------------------
# 摘要
# -----------------------------------------------------------------------------
printf "\n${C_GREEN}✓ 本地开发版构建完成${C_NC}\n\n"
printf "  产物根目录        %s\n" "$OUT"
printf "  启动器            %s/hermes\n" "$BIN_DIR"
printf "  开发 HERMES_HOME  %s\n" "$HERMES_HOME"
printf "  工具 store        %s\n" "$STORE"
if [ "$MIRROR_MODE" != none ]; then
    if [ -n "$MIRROR_BASE" ]; then
        printf "  依赖下载源        %s（%s）\n" "$MIRROR_LABEL" "$MIRROR_BASE"
    else
        printf "  依赖下载源        直连（未改写 uv.lock）\n"
    fi
    printf "  npm registry      %s\n" "$NPM_REGISTRY"
    if [ "$MIRROR_INDEX" = true ] && [ -n "$MIRROR_BASE" ]; then
        printf "  解析索引          %s\n" "${MIRROR_BASE%/}/simple"
    fi
    if [ "$LOCK_REWRITTEN" = true ]; then
        printf "  uv.lock           %s\n" "仍处于改写状态（异常，请检查）"
    else
        printf "  uv.lock           已还原，未被改动\n"
    fi
fi
if [ -n "$DECLINED_NOW" ]; then
    printf "  可选工具          已跳过 %s（--with-browser 可装回）\n" "$DECLINED_NOW"
else
    printf "  可选工具          全部安装\n"
fi
[ "$BUILT_TUI" = true ]     && printf "  TUI               %s/tui/dist/entry.js\n" "$PRODUCTS_DIR"
[ "$BUILT_WEB" = true ]     && printf "  Dashboard         %s/web/index.html\n" "$PRODUCTS_DIR"
[ "$BUILT_DESKTOP" = true ] && printf "  桌面端            %s/desktop\n" "$PRODUCTS_DIR"
if [ "${ELECTRON_SKIP_BINARY_DOWNLOAD:-}" = "1" ]; then
    printf "  Electron 运行时   未下载（默认不编译桌面端；加 --with-desktop 会从 npmmirror 拉取）\n"
fi
if [ "$GIT_HEAD" = unknown ]; then
    printf "  源码来源          就地使用当前工作区（非 git 检出，全程无 clone）\n"
else
    printf "  源码来源          就地使用当前工作区 %s @ %s（全程无 clone）\n" "$GIT_BRANCH" "${GIT_HEAD:0:12}"
fi

if [ -d "$PROD_HOME" ]; then
    printf "\n  与生产安装的隔离\n\n"
    printf "    生产家目录        %s\n" "$PROD_HOME"
    printf "    生产启动器        %s/hermes（未改动）\n" "$PROD_BIN"
    printf "    依赖代 key        开发 %s ／ 生产 %s\n" "$DEV_INSTALL_KEY" "$PROD_INSTALL_KEY"
    printf "    launchd 服务名    开发 ai.hermes.gateway-%s ／ 生产 ai.hermes.gateway\n" "$DEV_INSTALL_KEY"
    printf "    生产指纹          %s\n" "$([ "$PROD_CHANGED" = true ] && echo "有差异（见上方告警）" || echo "一致")"
fi

printf "\n  下一步\n\n"
printf "    source %s/activate.sh\n" "$OUT"
printf "    hermes --version\n"
printf "    hermes setup          # 首次配置模型 / API key（或重跑加 --copy-env）\n"
printf "    hermes                # 开始对话\n"
printf "    deactivate            # 退出，并把 HERMES_HOME / PATH 还原成进入前的样子\n"
printf "                          # （必须执行：HERMES_HOME 残留会让生产版 hermes 去\n"
printf "                          #   开发 home 里找依赖环境，从而重新下载 Python 依赖）\n\n"
if [ -d "$PROD_HOME" ]; then
    printf "  ${C_YELLOW}⚠ 在生产安装共存时，以下命令不要在本开发环境执行：${C_NC}\n\n"
    printf "    hermes update         # 其 launchd 机群重启会命中裸名 ai.hermes.gateway（即生产服务）\n"
    printf "    hermes gateway ...    # 生命周期命令请只在生产环境或卸载生产后使用\n\n"
fi
dim "  提示：.build/ 与 .venv/ 均在 .gitignore 中，不会污染 git 工作区。"
dim "  提示：无需再 source ./activate，本脚本的 activate.sh 指向同一 PM 环境。"
printf "\n"
