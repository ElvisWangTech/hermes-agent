#!/usr/bin/env bash
# =============================================================================
# build-local.sh — build and install a Hermes dev checkout in place
# =============================================================================
# Unlike scripts/install.sh (which clones into ~/.hermes/hermes-agent), this
# script uses the current workspace and writes every artifact under .build/:
#
#   .build/
#   ├── bin/                       hermes / hermes-acp launchers (this checkout)
#   ├── hermes-home/               isolated HERMES_HOME (config / sessions / installs)
#   ├── products/tui/              TUI build            —— dist/entry.js
#   ├── products/web/              dashboard build      —— index.html + assets
#   ├── products/desktop/          desktop build        —— only with --with-desktop
#   ├── icons/                     generated icons      —— web/desktop build input
#   ├── activate.sh                source this to enter the dev environment
#   ├── build-manifest.json        manifest for this build
#   └── .hermes-build-local.json   ownership marker (required by --clean)
#
# Design:
#   1. Python is not compiled. "Build" means PM bootstraps a runtime from
#      pm/lock.json + uv.lock. setup.py disables wheel/sdist, so
#      `pip install .` / `uv build` fail on purpose.
#   2. The only compiled pieces are the JS frontends: TUI, Dashboard, and
#      (optional) the Electron desktop app.
#   3. HERMES_HOME and HERMES_RUNTIME_DIR both live under .build/. The tool
#      store is always <out>/tools. CPython and the rest of the toolchain
#      install there. This script does not read or write ~/.hermes.
#   4. Frontends are picked up via HERMES_TUI_DIR / HERMES_WEB_DIST, so the
#      script never writes ui-tui/dist or hermes_cli/web_dist back into the
#      checkout.
#   5. No clone. The script uses the current workspace and never runs
#      git clone / git fetch. git is used only to record HEAD and the branch
#      in the manifest, and a failure becomes "unknown" (a tree without .git
#      still builds). Dependencies are not fetched via git either: the tools
#      in pm/lock.json are url + sha256 artifacts. The two git-sourced deps
#      in uv.lock do not apply here (psutil is Android-only; misaki is an
#      optional kittentts extra and is not installed by default). The macOS
#      pin set has no git tool package (that artifact is Windows-only);
#      system git is an optional prerequisite.
#      Contrast: scripts/install.sh must clone (or git fetch + stash +
#      reset --hard on an existing checkout) and has no offline switch.
#
# Isolation from a production install (~/.hermes):
#   The script is valid whether or not a production install already exists.
#     · Production present: hard guards plus a post-build check. Nothing is
#       written into ~/.hermes, and its Python / tool store is not reused.
#       Do not run `hermes update` / `hermes gateway ...` from this dev env.
#     · No production install: just run it. The tool store is <out>/tools.
#       CPython, Node, and dependencies all download under <out>.
#   Guards (always on):
#     · Refuse --out / --home inside ~/.hermes, and refuse launchers in
#       ~/.local/bin.
#     · Mutable state (config / state.db / sessions / skills / cron / logs /
#       cache / installs) lives in $OUT/hermes-home. The install-generation
#       key is the first 16 hex chars of the checkout path, so it differs
#       from any existing install.
#     · Launchers go only to $OUT/bin. The launchd label gets a hash suffix
#       and does not take the bare name ai.hermes.gateway.
#     · If a production install is detected, the script fingerprints it
#       before and after (.env / config.yaml / install-stamp / tool store /
#       launcher) and lists every change.
#   The tool store and the install generation both live under .build/ and
#   share no directory with ~/.hermes.
#
# Network and mirrors:
#   Symptom: `uv sync` stalls on `Failed to download pydantic==…` because
#   files.pythonhosted.org times out.
#   Cause: with no plugin members, PM runs `uv sync --frozen`, and uv's
#   --frozen downloads the absolute URLs recorded in uv.lock. Setting an
#   index (UV_INDEX_URL / PIP_INDEX_URL / pip.conf index-url; PM forwards
#   all of them to uv) only speeds resolution and sdist builds. It does not
#   change the wheel host. Measured: all three index setups still download
#   from files.pythonhosted.org.
#   What --mirror does: rewrite
#       https://files.pythonhosted.org/packages/…
#   to
#       <mirror>/packages/…
#   Mirror /packages/ paths match PyPI, so the bytes and sha256 stay the
#   same. The lock contents and uv's hash check are unchanged.
#   Safety: the original uv.lock is copied to <out>/.lock-pristine/ before
#   the rewrite and restored (sha256 checked) on exit, interrupt, or error.
#   A hard kill is repaired on the next run.
#   --mirror auto (the default) judges throughput, not elapsed time. It
#   range-samples a uv.lock artifact of at least 512KB. Direct >= 2 MB/s
#   leaves uv.lock untouched. Otherwise it measures domestic mirrors; the
#   winner must be >= 1 MB/s and >= 1.5x direct, and must pass a sha256
#   check, before the lock is rewritten.
#   (An earlier version treated "smallest artifact downloads in <= 1.5s" as
#   healthy. That sample was 2KB, so it measured handshake latency. Direct
#   PythonHosted was 0.10 MB/s here: uvloop 1.3MiB took 27.7s, while the
#   Huawei mirror at 3.96 MB/s took 0.11s, but the 2KB sample finished in
#   0.3s and the mirror was skipped. Override with DIRECT_MIN_MBPS /
#   MIRROR_MIN_MBPS / MIRROR_MIN_RATIO.)
#
#   Python deps are only part of a first install. PM also downloads the
#   toolchain from pm/lock.json. Those artifacts are not on PyPI, so
#   --mirror does not cover them. Measured on this machine, 2026-10:
#     npmmirror (Tencent CDN)        3.65 MB/s   <- npm / node
#     pypi.tuna.tsinghua.edu.cn       probe 0.26s; PyPI wheels go here
#     nodejs.org                      0.35 MB/s
#     ffmpeg.martin-riedl.de          0.18 MB/s
#     github.com (most tools)         0.09-0.18 MB/s
#     cdn.playwright.dev              0.09 MB/s
#   Ten GitHub proxies (ghfast/gh-proxy/ghproxy.net/gh.llkk.cc/gitmirror/…)
#   all landed at 0.09-0.17 MB/s. The nousresearch fallback CDN was
#   0.35 MB/s. None of them really helped. The NJU mirror covers
#   python-build-standalone, not llama.cpp / bitwarden / etc.
#
#   The useful lever is downloading less. A fresh machine's tool closure
#   is small:
#     required: ffmpeg, node, npm, python, ripgrep
#     optional: agent-browser (pulls chromium), cua-driver
#   Chromium is about 150MB and only comes from cdn.playwright.dev
#   (0.09 MB/s), so that one artifact is ~28 minutes. Pass --minimal when
#   you do not need browser automation. --without NAME / --with-browser
#   adjust that choice later.
#   Two byte-identical shortcuts are on by default:
#     · PyPI deps: rewrite uv.lock (--mirror)
#     · node artifacts: rewrite pm/lock.json (--tool-mirror)
#       nodejs.org 0.04 MB/s -> npmmirror 10.4 MB/s; 30MB drops from
#       10.7 minutes to 3 seconds
#     · uv-managed CPython: --python-mirror defaults to NJU (25.3MiB / 1.92s)
#   Other mappable hosts in pm/lock.json (ffmpeg / chromium / cua-driver /
#   ripgrep / llama.cpp / bws / uv itself) have no usable domestic mirror
#   and need a proxy.
#   The script does not guess. Before downloading it runs a toolchain
#   preflight: missing artifacts, hosts, measured throughput, and an
#   estimate. Unreachable or slow hosts get an HTTPS_PROXY hint.
#   Preflight only reports "unreachable / slow". Real failures are still
#   caught by PM's sha256 check.
#
# Frontend binary postinstalls (the npm registry does not cover these):
#   npm ci package bodies already use the registry mirror, but some
#   postinstall scripts fetch prebuilt binaries straight from GitHub
#   Releases. Registry settings do not apply to them. Measured:
#     electron@40.10.2 -> github.com/electron/electron/releases/…darwin-arm64.zip
#       109 MiB, connect ETIMEDOUT (20.205.243.166:443); npm ci fails
#     node-gyp (when node-pty falls back to a local compile) -> nodejs.org
#       headers, 0.04 MB/s
#   Handling:
#     · Desktop is opt-in. The default build does not need the Electron
#       runtime, so ELECTRON_SKIP_BINARY_DOWNLOAD=1 skips the download
#       (electron's install.js exits 0 at line 14, before any network)
#       and npm ci no longer stalls.
#     · --with-desktop downloads it from npmmirror (1.76 MB/s, ~1 minute
#       for 109 MiB). Verification uses the package's own checksums.json,
#       not the mirror's SHASUMS256.txt. Pass --electron-mirror none for
#       GitHub.
#     · node-gyp headers use NODEJS_ORG_MIRROR=npmmirror (9.8 MiB).
#
# Usage:
#   ./build-local.sh [options]
#
# Common:
#   ./build-local.sh                  # full build (runtime + TUI + dashboard)
#   ./build-local.sh --with-desktop   # also build the desktop app (slowest)
#   ./build-local.sh --skip-runtime   # rebuild the frontend only
#   ./build-local.sh --minimal        # first install: skip Chromium
#   ./build-local.sh --clean          # wipe .build, then rebuild
#   source .build/activate.sh         # enter the dev environment
# =============================================================================

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$REPO/pm/lock.json"
UVLOCK="$REPO/uv.lock"
MIRROR_FILE="$REPO/pm/artifact-mirror.json"

# -----------------------------------------------------------------------------
# Options
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
ELECTRON_MIRROR_OPT=""   # --electron-mirror URL|none; used only with --with-desktop
PYTHON_MIRROR_OPT=""
WITHOUT_OPT=""      # space-separated tool names, each passed as pm --without
MINIMAL=false
WITH_BROWSER=false
TOOL_PREFLIGHT=true
TOOL_MIRROR="auto"   # toolchain URL rewrite: auto | none
DECLINED_NOW=""     # optional tools actually skipped this run (from the PM opt-out file)

usage() {
    sed -n '2,/^set -euo pipefail$/p' "$REPO/build-local.sh" | sed -e '$d' -e 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --out DIR           output root (default <repo>/.build)
  --home DIR          dev HERMES_HOME (default <out>/hermes-home)
  --store DIR         PM tool store (default <out>/tools; must not be under ~/.hermes)
  --isolated-store    kept for old command lines. The tool store is already <out>/tools; this flag does nothing extra
  --with-test-env     also build the isolated test environment (dev/test groups; slower)
  --skip-runtime      skip the Python runtime bootstrap (rebuild the frontend only)
  --skip-js           skip npm and frontend builds (bootstrap the runtime only)
  --with-desktop      also build the desktop app (Electron; slowest)
  --copy-env          copy secrets from ~/.hermes/.env into the dev HERMES_HOME
  --verify            run pm doctor after the build
  --clean             wipe the output directory first (only a directory this script created)
  --mirror MODE       PyPI mirror: auto (default) | aliyun | tencent | tuna | huawei | ustc | none
                      auto = measure throughput with a >=512KB uv.lock artifact. Direct >= 2 MB/s
                      leaves uv.lock alone; otherwise rewrite it to the fastest mirror after a sha256 check
                      thresholds: DIRECT_MIN_MBPS / MIRROR_MIN_MBPS / MIRROR_MIN_RATIO
  --mirror-base URL   mirror base URL (for example https://mirrors.aliyun.com/pypi)
  --mirror-index      also point dependency resolution at the mirror (default: download URLs only)
  --npm-registry URL  npm registry (default https://registry.npmmirror.com)
  --electron-mirror URL|none
                      Electron runtime mirror (default npmmirror). Used only with --with-desktop.
                      none = GitHub. The default build does not download that binary.
  --python-mirror URL  CPython download mirror (uv UV_PYTHON_INSTALL_MIRROR)
                      default is the NJU mirror (uv's own client).
                      Pass --tool-mirror none to disable it.
  --tool-mirror MODE  toolchain URL rewrite: auto (default) | none
                      auto currently rewrites one mapping that a real install verified:
                        nodejs.org/dist  -> npmmirror (measured 0.04 -> 10.4 MB/s)
                      other hosts stay on their original URLs
  --without NAME      skip an optional default tool; repeatable (agent-browser, cua-driver)
  --minimal           same as --without agent-browser --without cua-driver
                      skips Chromium (~150MB from cdn.playwright.dev)
  --with-browser      clear that opt-out and install the optional tools
  --no-tool-preflight skip the toolchain download preflight
  -h, --help          show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --out)            OUT="${2:?--out requires a value}"; shift 2 ;;
        --out=*)          OUT="${1#*=}"; shift ;;
        --home)           HERMES_HOME_OPT="${2:?--home requires a value}"; shift 2 ;;
        --home=*)         HERMES_HOME_OPT="${1#*=}"; shift ;;
        --store)          STORE_OPT="${2:?--store requires a value}"; shift 2 ;;
        --store=*)        STORE_OPT="${1#*=}"; shift ;;
        --isolated-store) ISOLATED_STORE=true; shift ;;
        --with-test-env)  WITH_TEST_ENV=true; shift ;;
        --skip-runtime)   SKIP_RUNTIME=true; shift ;;
        --skip-js)        SKIP_JS=true; shift ;;
        --with-desktop)   WITH_DESKTOP=true; shift ;;
        --copy-env)       COPY_ENV=true; shift ;;
        --verify)         DO_VERIFY=true; shift ;;
        --clean)          DO_CLEAN=true; shift ;;
        --mirror)         MIRROR_MODE="${2:?--mirror requires a value}"; shift 2 ;;
        --mirror=*)       MIRROR_MODE="${1#*=}"; shift ;;
        --mirror-base)    MIRROR_BASE_OPT="${2:?--mirror-base requires a value}"; shift 2 ;;
        --mirror-base=*)  MIRROR_BASE_OPT="${1#*=}"; shift ;;
        --mirror-index)   MIRROR_INDEX=true; shift ;;
        --npm-registry)   NPM_REGISTRY_OPT="${2:?--npm-registry requires a value}"; shift 2 ;;
        --npm-registry=*) NPM_REGISTRY_OPT="${1#*=}"; shift ;;
        --electron-mirror)   ELECTRON_MIRROR_OPT="${2:?--electron-mirror requires a value}"; shift 2 ;;
        --electron-mirror=*) ELECTRON_MIRROR_OPT="${1#*=}"; shift ;;
        --python-mirror)  PYTHON_MIRROR_OPT="${2:?--python-mirror requires a value}"; shift 2 ;;
        --python-mirror=*) PYTHON_MIRROR_OPT="${1#*=}"; shift ;;
        --without)        WITHOUT_OPT="${WITHOUT_OPT} ${2:?--without requires a value}"; shift 2 ;;
        --without=*)      WITHOUT_OPT="${WITHOUT_OPT} ${1#*=}"; shift ;;
        --minimal)        MINIMAL=true; shift ;;
        --with-browser)   WITH_BROWSER=true; shift ;;
        --no-tool-preflight) TOOL_PREFLIGHT=false; shift ;;
        --tool-mirror)    TOOL_MIRROR="${2:?--tool-mirror requires a value}"; shift 2 ;;
        --tool-mirror=*)  TOOL_MIRROR="${1#*=}"; shift ;;
        -h|--help)        usage; exit 0 ;;
        *)                printf 'unknown option: %s (see --help)\n' "$1" >&2; exit 2 ;;
    esac
done

if [ "$MINIMAL" = true ] && [ "$WITH_BROWSER" = true ]; then
    printf '✗ --minimal and --with-browser conflict\n' >&2
    exit 2
fi
case "$TOOL_MIRROR" in
    auto|none) ;;
    *) printf '✗ unknown --tool-mirror value: %s (auto | none)\n' "$TOOL_MIRROR" >&2; exit 2 ;;
esac
# The toolchain mirror also covers uv-managed CPython (NJU mirrors
# python-build-standalone). An explicit --python-mirror wins.
if [ "$TOOL_MIRROR" != none ] && [ -z "$PYTHON_MIRROR_OPT" ]; then
    PYTHON_MIRROR_OPT="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
fi
if [ "$MINIMAL" = true ]; then
    WITHOUT_OPT="${WITHOUT_OPT} agent-browser cua-driver"
fi
# drop duplicates and surrounding whitespace; keep a stable order
if [ -n "$WITHOUT_OPT" ]; then
    WITHOUT_OPT="$(printf '%s\n' $WITHOUT_OPT | awk 'NF && !seen[$0]++' | tr '\n' ' ')"
    for _w in $WITHOUT_OPT; do
        case "$_w" in
            agent-browser|cua-driver) ;;
            *) printf '✗ cannot omit %s (only agent-browser and cua-driver are optional;\n' "$_w" >&2
               printf '  ffmpeg/node/npm/python/ripgrep are always installed)\n' >&2
               exit 2 ;;
        esac
    done
fi

case "$MIRROR_MODE" in
    auto|aliyun|tencent|tuna|huawei|ustc|none) ;;
    *) printf 'unknown --mirror value: %s (auto|aliyun|tencent|tuna|huawei|ustc|none)\n' "$MIRROR_MODE" >&2; exit 2 ;;
esac

# -----------------------------------------------------------------------------
# Logging
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
# Production-install isolation
# -----------------------------------------------------------------------------
# A production install may already exist (~/.hermes + ~/.local/bin/hermes).
# Any combination that would write the dev build into that tree is refused.
PROD_HOME="$HOME/.hermes"
PROD_BIN="$HOME/.local/bin"

hash_of() { # file sha256, or "-" when missing
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

dir_listing_hash() { # sha256 of sorted immediate entry names, to spot added/removed entries
    local tmp out
    tmp="$(mktemp)"
    if [ -d "$1" ]; then
        find "$1" -maxdepth 1 -mindepth 1 -exec basename {} \; 2>/dev/null | sort > "$tmp" || true
    fi
    out="$(hash_of "$tmp")"
    rm -f "$tmp"
    printf '%s' "$out"
}

production_fingerprint() { # fingerprint of irreplaceable production files plus directory shape
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

assert_isolation() { # call after every target path is known; exit on failure
    local problem=""

    case "$HERMES_HOME_DIR/" in
        "$PROD_HOME"/*)
            problem="dev HERMES_HOME is inside the production home: ${HERMES_HOME_DIR}" ;;
    esac
    if [ -z "$problem" ]; then
        case "$OUT/" in
            "$PROD_HOME"/*)
                problem="output root is inside the production home: ${OUT}" ;;
        esac
    fi
    if [ -z "$problem" ]; then
        case "$BIN_DIR" in
            "$PROD_BIN"|"$PROD_BIN"/*)
                problem="launcher directory is the production launcher directory: ${BIN_DIR}" ;;
        esac
    fi
    if [ -n "$problem" ]; then
        printf "${C_RED}✗ isolation check failed: %s${C_NC}\n" "$problem" >&2
        printf '  refusing to run, so the install under %s is left alone.\n' "$PROD_HOME" >&2
        printf '  use the default output (<repo>/.build) or pass --out / --home.\n' >&2
        exit 1
    fi

    # The dev install key must differ from production, or they share one generation dir.
    DEV_INSTALL_KEY="$(printf '%s' "$REPO" | (shasum -a 256 2>/dev/null || sha256sum) | cut -c1-16)"
    PROD_INSTALL_KEY="$(printf '%s' "$PROD_HOME/hermes-agent" | (shasum -a 256 2>/dev/null || sha256sum) | cut -c1-16)"
    if [ "$DEV_INSTALL_KEY" = "$PROD_INSTALL_KEY" ]; then
        die "dev install key matches production (${DEV_INSTALL_KEY}); they would share one install generation"
    fi

    # launchd label: production owns the bare ai.hermes.gateway name; dev uses a hash suffix.
    if [ "$HERMES_HOME_DIR" = "$PROD_HOME" ]; then
        die "dev HERMES_HOME must not equal the production home"
    fi

    SHARED_STORE=false
    case "$STORE/" in
        "$PROD_HOME"|"$PROD_HOME"/*)
            printf "${C_RED}✗ isolation check failed: tool store is inside the production home: %s${C_NC}\n" "$STORE" >&2
            printf '  CPython and the toolchain stay in <out>/tools, not in %s.\n' "$PROD_HOME" >&2
            exit 1 ;;
    esac
}

# -----------------------------------------------------------------------------
# Prerequisites
# -----------------------------------------------------------------------------
for tool in git curl tar unzip; do
    command -v "$tool" >/dev/null 2>&1 || die "missing required command: $tool"
done
[ -f "$LOCK" ] || die "pm/lock.json not found; run this script from the Hermes repo root"

case "$(uname -s)" in
    Linux)  OS=linux ;;
    Darwin) OS=darwin ;;
    *)      die "unsupported OS: $(uname -s)" ;;
esac
case "$(uname -m)" in
    arm64|aarch64) ARCH=arm64 ;;
    x86_64|amd64)  ARCH=x64 ;;
    *)             die "unsupported architecture: $(uname -m)" ;;
esac
TARGET="$OS-$ARCH"

if [ "$OS" = darwin ]; then
    if ! command -v xcode-select >/dev/null 2>&1 \
       || [ ! -d "$(xcode-select -p 2>/dev/null || echo /nonexistent)" ]; then
        warn "Xcode Command Line Tools not found; native dependencies may fail to compile"
    fi
fi

# -----------------------------------------------------------------------------
# Output directory and the --clean guard
# -----------------------------------------------------------------------------
OUT="${OUT:-$REPO/.build}"
case "$OUT" in
    ""|/|"$HOME"|"$HOME"/|"$REPO"|"$REPO"/) die "refusing to use "${OUT}" as the output directory" ;;
esac
# Refuse paths inside the production home before mkdir -p can create them.
case "$OUT"/ in
    "$PROD_HOME"/*) die "refusing to write artifacts into the production home: ${OUT}" ;;
esac
mkdir -p "$(dirname "$OUT")"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
MARKER="$OUT/.hermes-build-local.json"

# -----------------------------------------------------------------------------
# Crash recovery for rewritten lockfiles
# -----------------------------------------------------------------------------
# Mirrors work by temporarily rewriting absolute download URLs in two files:
#   uv.lock      —— PyPI wheels / sdists (--mirror)
#   pm/lock.json —— toolchain artifacts (--tool-mirror, for example node)
# A previous SIGKILL is not caught by the trap, so this runs before --clean
# and restores those files before the backup directory can be deleted.
# The test is narrow: a backup exists AND the current file no longer
# contains the original host.
PRISTINE_DIR="$OUT/.lock-pristine"
UVLOCK_BACKUP="$PRISTINE_DIR/uv.lock"
TOOLLOCK_BACKUP="$PRISTINE_DIR/pm-lock.json"
LOCK_REWRITTEN=false
TOOLLOCK_REWRITTEN=false
UVLOCK_SHA_BEFORE="$(hash_of "$UVLOCK")"
TOOLLOCK_SHA_BEFORE="$(hash_of "$LOCK")"

# Restore one rewritten lockfile. $1=backup $2=target $3=label $4=original sha
# Do not delete PRISTINE_DIR here. uv.lock and pm/lock.json share that
# directory; deleting it after the first restore would make the second
# restore skip its backup. restore_lock removes the directory at the end.
restore_one_lock() {
    local backup="$1" target="$2" label="$3" want="$4"
    [ -f "$backup" ] || return 0
    cp -p "$backup" "$target"
    if [ "$(hash_of "$target")" = "$want" ]; then
        ok "${label} restored (sha256 matches the pre-rewrite file)"
    else
        warn "${label} sha256 mismatch after restore; check ${target}"
    fi
}

# EXIT/INT/TERM/HUP: restore every rewritten lockfile
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

# Repair a rewrite left by the previous run, before --clean can delete the backup
if [ -f "$UVLOCK_BACKUP" ] \
   && [ "$(hash_of "$UVLOCK")" != "$(cat "$PRISTINE_DIR/uv.lock.sha256" 2>/dev/null || echo '-')" ] \
   && ! grep -q 'files\.pythonhosted\.org/packages/' "$UVLOCK" 2>/dev/null; then
    warn "uv.lock was left rewritten by a previous run; restoring it"
    UVLOCK_SHA_BEFORE="$(cat "$PRISTINE_DIR/uv.lock.sha256")"
    LOCK_REWRITTEN=true
    restore_lock
fi
if [ -f "$TOOLLOCK_BACKUP" ] \
   && [ "$(hash_of "$LOCK")" != "$(cat "$PRISTINE_DIR/pm-lock.json.sha256" 2>/dev/null || echo '-')" ] \
   && ! grep -q 'nodejs\.org/dist/' "$LOCK" 2>/dev/null; then
    warn "pm/lock.json was left rewritten by a previous run; restoring it"
    TOOLLOCK_SHA_BEFORE="$(cat "$PRISTINE_DIR/pm-lock.json.sha256")"
    TOOLLOCK_REWRITTEN=true
    restore_lock
fi
rm -rf "$PRISTINE_DIR" 2>/dev/null || true

# Last-resort warning. The backup lives under --out. If that directory was
# deleted or moved, the self-repair above has nothing to restore and the
# lockfile stays rewritten.
# Trigger: uv.lock contains no files.pythonhosted.org URLs. A normal tree has thousands.
if [ -f "$UVLOCK" ] && ! grep -q 'files\.pythonhosted\.org/packages/' "$UVLOCK" 2>/dev/null; then
    warn "uv.lock has no files.pythonhosted.org URLs and no backup was found; it may still be rewritten"
    if [ -d "$REPO/.git" ]; then
        dim "  restore: git -C $REPO checkout -- uv.lock"
    else
        dim "  restore: this checkout has no .git; copy uv.lock back from an original tree"
    fi
fi

if [ "$DO_CLEAN" = true ] && [ -e "$OUT" ]; then
    if [ ! -d "$OUT" ]; then
        die "refusing to clean: "${OUT}" is not a directory"
    elif [ -n "$(ls -A "$OUT" 2>/dev/null || true)" ] && [ ! -f "$MARKER" ]; then
        die "refusing to clean: "${OUT}" is non-empty and has no ownership marker (${MARKER})"
    fi
    info "clearing output directory $OUT"
    rm -rf "$OUT"
fi

HERMES_HOME_DIR="${HERMES_HOME_OPT:-$OUT/hermes-home}"
BIN_DIR="$OUT/bin"
PRODUCTS_DIR="$OUT/products"
ICONS_DIR="$OUT/icons"

if [ -n "${HERMES_HOME:-}" ] && [ "$HERMES_HOME" != "$HERMES_HOME_DIR" ]; then
    warn "HERMES_HOME=$HERMES_HOME in this shell will be replaced with $HERMES_HOME_DIR"
fi

# The tool store is always inside this output directory. CPython, uv, and
# node resolve from here. ~/.hermes/tools is never probed or reused.
if [ -n "$STORE_OPT" ]; then
    STORE="$STORE_OPT"
else
    STORE="$OUT/tools"
fi

assert_isolation

# Fingerprint the production install before the build and compare after.
# Only irreplaceable small files and directory shape are covered, so
# "did this build touch production?" is a fact, not an assumption.
# Take it before creating any directory, or a failed isolation check
# would already have left a trace.
PROD_BEFORE="$(mktemp)"
production_fingerprint > "$PROD_BEFORE"

# On a shared store, record entry names so the end of the build can list additions and removals.
STORE_ENTRIES_BEFORE="$(mktemp)"
if [ "$SHARED_STORE" = true ] && [ -d "$STORE" ]; then
    ( find "$STORE" -maxdepth 1 -mindepth 1 -exec basename {} \; 2>/dev/null \
        | grep -v '^\.' | sort > "$STORE_ENTRIES_BEFORE" ) || true
fi

mkdir -p "$OUT" "$BIN_DIR" "$HERMES_HOME_DIR"
printf '%s\n' '{"schema":"hermes-build-local-v1"}' > "$MARKER"

export HERMES_HOME="$HERMES_HOME_DIR"
export HERMES_RUNTIME_DIR="$STORE"
# Keep build tools from consulting or updating per-user configuration and
# caches. HERMES_HOME isolates Hermes itself; these XDG/npm paths isolate the
# npm/uv/frontend subprocesses that otherwise default to $HOME.
export XDG_CONFIG_HOME="$OUT/cache/config"
export XDG_CACHE_HOME="$OUT/cache/xdg"
export npm_config_userconfig="$OUT/cache/npmrc"
export npm_config_cache="$OUT/cache/npm"
export ELECTRON_CACHE="$OUT/cache/electron"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$npm_config_cache" "$ELECTRON_CACHE"

printf "\n${C_CYAN}☤ Hermes local dev build${C_NC}\n"
dim "  repo         $REPO"
dim "  output       $OUT"
dim "  HERMES_HOME  $HERMES_HOME"
dim "  tool store   $STORE"
dim "  target       $TARGET"
if [ -d "$PROD_HOME" ]; then
    dim "  production   ${PROD_HOME} (present; this run still writes tools only to ${STORE})"
else
    dim "  production   not found"
    dim "               toolchain and artifacts stay under ${OUT}"
fi

# -----------------------------------------------------------------------------
# Parse pm/lock.json pins with awk, before a Python interpreter exists.
# -----------------------------------------------------------------------------
pin() { # $1 = url|sha256|version, $2 = package name (default uv)
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
# Step 1: bootstrap the Python runtime (uv -> CPython -> PM dependency sync)
# -----------------------------------------------------------------------------
# Prefer an interpreter already on disk. PM publishes
# python-<version>-<target>/bin/python3; uv publishes under uv-python/.
# A hit skips the CPython download on later runs.
export UV_PYTHON_INSTALL_DIR="$STORE/uv-python"
find_boot_py() {
    local candidate base
    BOOT_PY=""
    for candidate in "$STORE"/python-*/bin/python3 \
                     "$STORE"/uv-python/*/bin/python3 \
                     "$STORE"/uv-python/*/bin/python3.*; do
        [ -x "$candidate" ] && [ -f "$candidate" ] || continue
        base="$(basename "$candidate")"
        case "$base" in
            python3|python3.[0-9]|python3.[0-9][0-9])
                BOOT_PY="$candidate"
                return 0
                ;;
        esac
    done
    return 1
}
find_boot_py || true

# GitHub releases are unreachable on some networks. The official uv wheel
# lives on the same path as the PyPI index, so a domestic mirror serves the
# same binary. Verify with the simple-index sha256, not the GitHub tarball
# digest recorded in the lockfile.
uv_wheel_filename() { # $1 = uv version
    case "$TARGET" in
        darwin-arm64) printf 'uv-%s-py3-none-macosx_11_0_arm64.whl' "$1" ;;
        darwin-x64)   printf 'uv-%s-py3-none-macosx_10_12_x86_64.whl' "$1" ;;
        linux-arm64)  printf 'uv-%s-py3-none-manylinux_2_28_aarch64.whl' "$1" ;;
        linux-x64)    printf 'uv-%s-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl' "$1" ;;
        *) return 1 ;;
    esac
}

stage_uv_from_wheel() { # $1 = version  $2 = install dir; sets UV on success
    local version="$1" entry="$2" name base page href path sha url tmp got bin seen
    name="$(uv_wheel_filename "$version")" || return 1
    seen=" "
    for base in ${MIRROR_BASE:+"$MIRROR_BASE"} \
        "https://repo.huaweicloud.com/repository/pypi" \
        "https://mirrors.aliyun.com/pypi" \
        "https://mirrors.cloud.tencent.com/pypi" \
        "https://pypi.tuna.tsinghua.edu.cn" \
        "https://mirrors.ustc.edu.cn/pypi"
    do
        case "$seen" in *" $base "*) continue ;; esac
        seen="$seen$base "
        page="$(mktemp)"
        if ! curl -fsSL --connect-timeout 8 --max-time 30 -o "$page" "${base%/}/simple/uv/"; then
            rm -f "$page"
            continue
        fi
        href="$(tr '"' '\n' < "$page" | grep -F "$name" | head -1 || true)"
        rm -f "$page"
        [ -n "$href" ] || continue
        sha="${href#*#sha256=}"
        path="${href%%#*}"
        case "$sha" in
            "$href"|""|*[!0-9a-f]*) continue ;;
        esac
        case "$path" in
            https://*) url="$path" ;;
            *)
                while [ "${path#../}" != "$path" ]; do path="${path#../}"; done
                url="${base%/}/${path#/}"
                ;;
        esac
        info "downloading the uv wheel from PyPI mirror ${base}..."
        tmp="$(mktemp -d "$STORE/.uvwheel-XXXXXX")"
        if ! curl -fsSL --connect-timeout 8 --max-time 120 -o "$tmp/$name" "$url"; then
            rm -rf "$tmp"
            continue
        fi
        got="$(hash_of "$tmp/$name")"
        if [ "$got" != "$sha" ]; then
            rm -rf "$tmp"
            warn "uv wheel sha256 mismatch; trying the next mirror"
            continue
        fi
        mkdir -p "$tmp/tree"
        unzip -q "$tmp/$name" -d "$tmp/tree"
        bin="$(find "$tmp/tree" -type f -path '*/scripts/uv' | head -1)"
        if [ -z "$bin" ] || [ ! -f "$bin" ]; then
            rm -rf "$tmp"
            continue
        fi
        rm -rf "$entry"
        mkdir -p "$entry"
        cp "$bin" "$entry/uv"
        if [ -f "$(dirname "$bin")/uvx" ]; then
            cp "$(dirname "$bin")/uvx" "$entry/uvx"
        fi
        chmod +x "$entry/uv" "$entry/uvx" 2>/dev/null || true
        rm -rf "$tmp"
        UV="$entry/uv"
        return 0
    done
    return 1
}

# Seed NJU's python-build-standalone into PM's fetch cache.
# Do not rewrite pm/lock.json: PM's urllib treats NJU's self-redirect 302
# as an infinite loop. curl downloads it, checks the lock sha256, and PM
# then hits the cache instead of GitHub.
seed_python_mirror() {
    local url sha rel file mirror dest tmp got
    [ -n "$PYTHON_MIRROR_OPT" ] || return 0
    url="$(pin url python)"
    sha="$(pin sha256 python)"
    [ -n "$url" ] && [ -n "$sha" ] || return 0
    case "$url" in
        https://github.com/astral-sh/python-build-standalone/releases/download/*)
            rel="${url#https://github.com/astral-sh/python-build-standalone/releases/download/}"
            ;;
        *) return 0 ;;
    esac
    file="${rel##*/}"
    mirror="${PYTHON_MIRROR_OPT%/}/$rel"
    dest="$STORE/fetch-${sha}/${file}"
    # The published store entry is enough. Do not re-fetch the tarball.
    if [ -n "$(find "$STORE" -maxdepth 1 -type d -name 'python-*' -print -quit 2>/dev/null)" ]; then
        ok "Reusing installed CPython in the tool store"
        return 0
    fi
    if [ -f "$dest" ] && [ "$(hash_of "$dest")" = "$sha" ]; then
        ok "Reusing cached CPython archive"
        return 0
    fi
    info "downloading the CPython archive from the domestic mirror..."
    dim "  ${mirror}"
    tmp="$(mktemp -d "$STORE/.cpython-XXXXXX")"
    if ! curl -fsSL --connect-timeout 8 --max-time 180 -o "$tmp/$file" "$mirror"; then
        rm -rf "$tmp"
        warn "CPython mirror download failed; PM will try the original URL"
        return 0
    fi
    got="$(hash_of "$tmp/$file")"
    if [ "$got" != "$sha" ]; then
        rm -rf "$tmp"
        warn "CPython mirror sha256 mismatch (expected ${sha}, got ${got}); not caching it"
        return 0
    fi
    mkdir -p "$STORE/fetch-${sha}"
    mv "$tmp/$file" "$dest"
    rm -rf "$tmp"
    ok "CPython archive cached from the domestic mirror"
}

stage_uv() {
    local uv_version uv_entry url sha archive got tmp inner inner_count child mirror
    uv_version="$(pin version)"
    [ -n "$uv_version" ] || die "pm/lock.json has no uv version pin"
    uv_entry="$STORE/uv-$uv_version-$TARGET"

    if [ -x "$uv_entry/uv" ]; then
        UV="$uv_entry/uv"
        ok "reusing installed uv ($("$UV" --version 2>/dev/null || echo "uv $uv_version"))"
        return 0
    fi

    url="$(pin url)"; sha="$(pin sha256)"
    [ -n "$url" ] && [ -n "$sha" ] || die "pm/lock.json has no uv artifact for $TARGET"

    info "downloading pinned uv ${uv_version} (${TARGET})..."
    mkdir -p "$STORE"
    tmp="$(mktemp -d "$STORE/.bootstrap-XXXXXX")"
    archive="$tmp/${url##*/}"

    if ! curl -fsSL --connect-timeout 8 --max-time 25 -o "$archive" "$url"; then
        rm -f "$archive"
        info "GitHub is unreachable; installing uv from a domestic PyPI mirror..."
        if stage_uv_from_wheel "$uv_version" "$uv_entry"; then
            rm -rf "$tmp"
            ok "uv is ready ($("$UV" --version 2>/dev/null || echo "uv $uv_version"))"
            return 0
        fi
        if [ -n "$MIRROR_ORIGIN" ] && [ -n "$MIRROR_PREFIX" ]; then
            mirror="$MIRROR_ORIGIN/$MIRROR_PREFIX$sha"
            info "domestic wheel unavailable; trying the official mirror..."
            curl -fsSL --connect-timeout 8 --max-time 60 -o "$archive" "$mirror" \
                || { rm -rf "$tmp"; die "uv download failed (GitHub, domestic PyPI, and the official mirror)"; }
        else
            rm -rf "$tmp"; die "uv download failed: $url"
        fi
    fi

    got="$( (sha256sum "$archive" 2>/dev/null || shasum -a 256 "$archive") | cut -d' ' -f1 | tr -d '\\')"
    [ "$got" = "$sha" ] || { rm -rf "$tmp"; die "uv sha256 mismatch: expected ${sha}, got ${got}"; }

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
    ok "uv is ready ($("$UV" --version 2>/dev/null || echo "uv $uv_version"))"
}

# -----------------------------------------------------------------------------
# Mirror acceleration: rewrite uv.lock download URLs
# -----------------------------------------------------------------------------
# Why rewrite the lock: with no plugin members PM runs `uv sync --frozen`,
# and uv's --frozen downloads the absolute URLs in uv.lock. Measured: no
# UV_INDEX_URL / UV_DEFAULT_INDEX setting changes the host away from
# files.pythonhosted.org. Index settings only affect resolution and sdist
# builds. Mirror /packages/ paths match PyPI byte for byte (the probe
# checks the lock sha256), so the rewrite does not change the lock
# contents or uv's hash check.
MIRROR_BASE=""
MIRROR_LABEL="direct"
MIRROR_DETAIL=""
MIRROR_CHOICE="$OUT/cache/pypi-mirror"
# Remember the last mirror decision so a later run does not re-download
# probe samples. Delete this file (or pass a different --mirror) to measure again.
save_mirror_choice() {
    mkdir -p "$OUT/cache"
    printf '%s\t%s\n' "${MIRROR_LABEL:-direct}" "${MIRROR_BASE:-}" > "$MIRROR_CHOICE"
}
load_mirror_choice() {
    [ -f "$MIRROR_CHOICE" ] || return 1
    IFS="$(printf '\t')" read -r MIRROR_LABEL MIRROR_BASE < "$MIRROR_CHOICE" || return 1
    [ -n "${MIRROR_LABEL:-}" ] || return 1
    return 0
}
NPM_REGISTRY="${NPM_REGISTRY_OPT:-https://registry.npmmirror.com}"
MIRROR_PROBE_PATH=""    # speed-sample path (smallest artifact >=512KB)
MIRROR_PROBE_HASH=""    # sha256 of the verify sample (smallest file, proves identical bytes)
MIRROR_VERIFY_PATH=""   # verify-sample path
# auto judges throughput, not elapsed time. An earlier version treated
# "smallest file (types-certifi, 2KB) downloads in <= 1.5s" as healthy.
# 2KB finishes in 0.6s, which measures latency, so a 49 KB/s link was
# called healthy and the mirror was skipped. A real dep such as uvloop
# (1.3MiB) then took 27.7s. Now:
#   direct >= DIRECT_MIN_MBPS  -> leave uv.lock alone;
#   otherwise measure mirrors; the winner must be >= MIRROR_MIN_MBPS and
#   >= MIRROR_MIN_RATIO times direct before rewriting (a tiny gap is not
#   worth touching the lock), and the sha256 check still runs first.
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

# Pick two probe samples from uv.lock (the same file exists on every mirror):
#   VERIFY —— smallest distribution. Full download + sha256, proving the
#             mirror bytes match PyPI. Only a base that passes is written
#             into uv.lock.
#   SPEED  —— smallest artifact >= 512KB, used to measure throughput. The
#             sample has to be large enough to reflect bandwidth: the 2KB
#             types-certifi sample finished in 0.6s and measured handshake
#             latency, so a 49 KB/s link was called healthy. Fall back to
#             the largest artifact when nothing is >= 512KB.
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

# Measure throughput (MB/s): a Range request of the first 1MB, --max-time 6.
# A timeout (curl exit 28) is not a failure: bytes received / time is the
# estimate we want, so only the status code and byte count matter. Any
# other error prints 0 and returns non-zero.
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

# Download the VERIFY sample and compare sha256. On success, print seconds.
# This is the only proof the mirror bytes match PyPI, and the precondition
# for rewriting uv.lock.
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
    # Restore the original mtime. PM's activation stamp
    # (ACTIVATION_INPUTS in pm/environments.py) compares mtimes, so a
    # touched uv.lock makes the next `hermes` start re-activate. cp -p
    # stored the original mtime in the backup; reuse it.
    touch -r "$UVLOCK_BACKUP" "$UVLOCK" 2>/dev/null || true
    LOCK_REWRITTEN=true
    trap 'restore_lock || true' EXIT INT TERM HUP
}

# Toolchain mirrors: rewrite whole URL prefixes in pm/lock.json.
#
# The allow-list is tight. A rule is included only after a real PM install
# succeeded with it. The rejected candidate:
#   https://github.com/astral-sh/python-build-standalone/releases/download/
#        -> https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/
#   curl -L alone was fine (200 / 26.7MB / sha256 matched), but a real
#   build made PM report:
#     x python: install failed: HTTP Error 302: The HTTP server returned a redirect
#       error that would lead to an infinite loop.
#   pm/downloader.py uses urllib's HTTPRedirectHandler, which treats a
#   redirect back to the same URL as an infinite loop and fails hard
#   (curl follows it; urllib does not). NJU's CDN does that briefly and
#   then looks fine again. A broken install is worse than a few extra
#   minutes, so that rule is not included.
#   uv-managed CPython is unaffected: it uses uv's own HTTP client, and
#   --python-mirror already points at NJU (measured 25.3MiB / 1.92s).
# The included rule has a real-install receipt: a cold-store build on
# 20260901 downloaded node (28.8MiB) through it.
TOOL_MIRROR_RULES="
https://nodejs.org/dist/|https://npmmirror.com/mirrors/node/
"

# The probe must use PM's code path. PM uses urllib's HTTPRedirectHandler.
# curl follows a self-redirect 302; urllib treats it as an infinite loop.
# A curl-only probe reports "mirror works" when it does not. That is how
# the NJU python rule slipped through.
tool_mirror_probe() { # $1=mirror prefix $2=relative path; pass when that path is fetchable
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
        # Rewrite only when the lock contains the prefix and the mirror
        # can serve the same relative path. lock.json is one "url": "..."
        # per line, so the trailing quote (and a following comma) must be
        # stripped or the probe URL 404s.
        rel="$(awk -v p="$src" -v t="$TARGET" '
            {
                i = index($0, p)
                if (i == 0) next
                tail = substr($0, i + length(p))
                j = index(tail, "\"")
                if (j > 0) tail = substr(tail, 1, j - 1)
                if (first == "") first = tail
                # the lock lists every platform under one prefix; probe the current platform
                if (best == "" && index(tail, t) > 0) best = tail
            }
            END { print (best != "" ? best : first) }
        ' "$LOCK")"
        [ -n "$rel" ] || continue
        if tool_mirror_probe "$dst" "$rel"; then
            printf '%s|%s\n' "$src" "$dst" >> "$picked"
        else
            warn "toolchain mirror probe failed, skipping ${dst}"
        fi
    done < "$rules_tmp"
    rm -f "$rules_tmp"

    if [ ! -s "$picked" ]; then rm -f "$picked"; return 0; fi

    mkdir -p "$PRISTINE_DIR"
    cp -p "$LOCK" "$TOOLLOCK_BACKUP"
    printf '%s' "$TOOLLOCK_SHA_BEFORE" > "$PRISTINE_DIR/pm-lock.json.sha256"

    # Literal str.replace (not a regex): only the URL substring changes.
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
        warn "pm/lock.json rewrite failed; rolled back"
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
    ok "rewrote ${applied} toolchain URL(s) in pm/lock.json (restored when this run ends)"
    dim "  artifacts outside the rewrite rules (chromium / ffmpeg / cua-driver, ...) keep their original URLs"
}

# -----------------------------------------------------------------------------
# Optional-tool opt-out (--without / --minimal / --with-browser)
# -----------------------------------------------------------------------------
# PM installs optional defaults (agent-browser, cua-driver) apart from the
# required set and persists the choice in
# <HERMES_HOME>/installs/<key>/declined-packages.json.
# pm.cli install --without NAME takes the named-install branch
# (full_closure=False) and does not advance the activation stamp. Write
# the opt-out file first, then run a full install with no arguments: the
# tools stay out and the dependency closure still completes.
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
# Toolchain download preflight
# -----------------------------------------------------------------------------
# Tool artifacts are not on PyPI, so --mirror does not cover them.
# github.com / nodejs.org / cdn.playwright.dev are often 0.1-0.3 MB/s.
# A first install pulls 6-7 artifacts and hundreds of MB, which looks
# stuck. Before downloading, print what, from where, how big, and how
# long. If every probe fails, skip quietly and do not block the build.
tool_plan() { # print TSV: kind<TAB>name<TAB>url
    # Include the opt-out this run will write (--without / --minimal /
    # --with-browser). Otherwise --minimal has already dropped Chromium
    # and the preflight still warns about a 162MB / 28 minute download.
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
            # Same rewrite PM uses for downloads: registry.npmjs.org becomes
            # the configured npm mirror (npmmirror by default). Otherwise
            # the preflight reports npm artifacts as a slow direct fetch.
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

    info "toolchain preflight: $_pf_missing artifact(s) missing from the store; measuring source hosts..."
    _pf_dir="$(mktemp -d)"
    _pf_rows="$_pf_dir/rows"; _pf_hosts="$_pf_dir/hosts"; _pf_speed="$_pf_dir/speed"
    : > "$_pf_rows"; : > "$_pf_hosts"; : > "$_pf_speed"

    # Round 1: fetch each artifact's Content-Length in parallel (-I -L, last hop).
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

    # Round 2: one 256KB sample per host, in parallel, to estimate throughput.
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
    printf '  %-24s %-13s %-30s %-10s %s\n' 'host' 'throughput' 'missing' 'size' 'estimate'
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
                    mb = 0; size_txt = "unknown"
                }
                sp = (h in speed) ? speed[h] : 0
                if (sp > 0 && mb > 0) {
                    secs = mb / sp
                    est = sprintf("%.1f min", secs / 60)
                    total_mb += mb; total_s += secs
                } else if (sp > 0) {
                    est = "—"
                } else {
                    est = "—"; unreachable = 1
                }
                spd = (sp > 0) ? sprintf("%.2f MB/s", sp) : "unreachable"
                printf "  %-24s %-13s %-30s %-10s %s\n", h, spd, names[h], size_txt, est
            }
            printf "  %s\n", "────────────────────────────────────────────────────────────────────────────────────"
            if (total_mb > 0)
                printf "  sized portion %.1f MB, about %.1f min%s\n", total_mb, total_s / 60, (unreachable ? " (some hosts unreachable)" : "")
            else if (sized == 0)
                printf "  size and throughput unknown; skipping the time estimate\n"
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
        warn "these hosts are unreachable (connection timeout): ${_pf_dead}"
        dim "  retry behind a proxy: export HTTPS_PROXY=http://127.0.0.1:7890"
    fi
    # Warn only when the estimate exceeds 3 minutes. If the slowest host is
    # already the mirror this script chose, "no domestic mirror" is wrong.
    if [ -n "$_pf_slowest" ] \
       && awk -v t="${_pf_slowest_secs:-0}" 'BEGIN{exit !(t > 180)}'; then
        warn "slowest host is ${_pf_slowest} (about $(( _pf_slowest_secs / 60 )) min)"
        dim "  no faster mirror is wired for that host; set HTTPS_PROXY and rerun to go faster"
    fi
    if ! grep -q '^REQ' "$_pf_miss" && grep -q '^OPT' "$_pf_miss"; then
        dim "  note: only optional tools are missing. Chromium (~150MB, cdn.playwright.dev)"
        dim "       is required only by agent-browser. Pass --minimal to skip it."
    fi
    rm -f "$_pf_plan" "$_pf_miss"
    rm -rf "$_pf_dir" 2>/dev/null || true
}

if [ "$MIRROR_MODE" != none ]; then
    if [ -n "$NPM_REGISTRY_OPT" ] || [ -n "$MIRROR_BASE_OPT" ] || [ "$SKIP_RUNTIME" != true ]; then
        export npm_config_registry="$NPM_REGISTRY"
        # PM forwards this value to uv via FORWARDED_UV_SETTINGS; other UV_* are stripped.
        export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-300}"
    fi
fi

if [ "$MIRROR_MODE" != none ] && [ "$SKIP_RUNTIME" != true ] \
   && [ "$MIRROR_MODE" = auto ] && [ -z "$MIRROR_BASE_OPT" ] && load_mirror_choice; then
    step "PyPI mirror (--mirror auto)"
    if [ -n "$MIRROR_BASE" ]; then
        ok "Reusing cached PyPI mirror ${MIRROR_LABEL}: ${MIRROR_BASE}"
        dim "  skipping the speed probe; delete ${MIRROR_CHOICE} to measure again"
        apply_mirror_lock
        ok "uv.lock download URLs rewritten temporarily (restored when this run ends)"
    else
        ok "Reusing cached choice: direct PyPI (uv.lock left unchanged)"
        dim "  delete ${MIRROR_CHOICE} to measure mirrors again"
    fi
elif [ "$MIRROR_MODE" != none ] && [ "$SKIP_RUNTIME" != true ]; then
    step "probing the network and choosing a mirror (--mirror ${MIRROR_MODE})"

    PROBE_SAMPLE="$(mirror_probe_sample)"
    MIRROR_VERIFY_PATH="$(printf '%s\n' "$PROBE_SAMPLE" | awk -F'\t' '$1=="VERIFY"{print $2}')"
    MIRROR_PROBE_HASH="$(printf '%s\n' "$PROBE_SAMPLE" | awk -F'\t' '$1=="VERIFY"{print $3}')"
    MIRROR_PROBE_PATH="$(printf '%s\n' "$PROBE_SAMPLE" | awk -F'\t' '$1=="SPEED"{print $2}')"

    if [ -z "$MIRROR_PROBE_PATH" ] || [ -z "$MIRROR_VERIFY_PATH" ] || [ -z "$MIRROR_PROBE_HASH" ]; then
        warn "could not pick a probe sample from uv.lock; leaving download URLs unchanged"
    else
        PROBE_TMP="$(mktemp)"
        if [ -n "$MIRROR_BASE_OPT" ]; then
            if elapsed="$(mirror_probe_verify "$MIRROR_BASE_OPT" "$PROBE_TMP")"; then
                MIRROR_BASE="$MIRROR_BASE_OPT"
                MIRROR_LABEL="${MIRROR_BASE_OPT}"
                MIRROR_DETAIL="given base, sample sha256 matched (${elapsed}s)"
            else
                rm -f "$PROBE_TMP"
                die "given --mirror-base failed (download or sha256): ${MIRROR_BASE_OPT}"
            fi
        elif [ "$MIRROR_MODE" = auto ]; then
            direct_mbps="$(mirror_probe_speed 'https://files.pythonhosted.org' || true)"
            if awk -v s="$direct_mbps" -v min="$DIRECT_MIN_MBPS" 'BEGIN{exit !(s >= min)}'; then
                MIRROR_LABEL="direct"
                MIRROR_DETAIL="direct throughput ${direct_mbps} MB/s >= ${DIRECT_MIN_MBPS} MB/s; leaving uv.lock unchanged"
            else
                dim "  direct files.pythonhosted.org is ${direct_mbps} MB/s (< ${DIRECT_MIN_MBPS} MB/s); trying domestic mirrors"
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
                    # Rewrite only after sha256 matches: the mirror bytes equal PyPI.
                    if elapsed="$(mirror_probe_verify "$best_base" "$PROBE_TMP")"; then
                        speedup="$(awk -v a="$best_mbps" -v b="$direct_mbps" \
                            'BEGIN{ if (b <= 0) b = 0.01; printf "%.0f", a / b }')"
                        MIRROR_BASE="$best_base"
                        MIRROR_LABEL="$best_name"
                        MIRROR_DETAIL="direct ${direct_mbps} MB/s -> ${best_name} ${best_mbps} MB/s, about ${speedup}x"
                    else
                        warn "${best_name} sample sha256 failed; staying on direct PyPI"
                        MIRROR_DETAIL="fastest mirror failed verification; staying on direct PyPI"
                    fi
                else
                    warn "mirrors are not meaningfully faster (${best_mbps} MB/s vs direct ${direct_mbps} MB/s); staying direct"
                    MIRROR_DETAIL="mirrors are not meaningfully faster; staying direct"
                fi
            fi
        else
            base="$(mirror_base_for "$MIRROR_MODE")"
            if elapsed="$(mirror_probe_verify "$base" "$PROBE_TMP")"; then
                MIRROR_BASE="$base"
                MIRROR_LABEL="$MIRROR_MODE"
                MIRROR_DETAIL="selected mirror, sample sha256 matched (${elapsed}s)"
            else
                rm -f "$PROBE_TMP"
                die "mirror ${MIRROR_MODE} (${base}) failed: download error or sha256 mismatch"
            fi
        fi
        rm -f "$PROBE_TMP"

        if [ -n "$MIRROR_BASE" ]; then
            ok "using mirror ${MIRROR_LABEL}: ${MIRROR_BASE}"
            dim "  ${MIRROR_DETAIL}"
            dim "  probe sample sha256 matches uv.lock (same bytes as PyPI)"
            apply_mirror_lock
            ok "uv.lock download URLs rewritten temporarily (restored when this run ends)"
        else
            dim "  ${MIRROR_DETAIL}"
        fi
        save_mirror_choice
    fi
fi

if [ "$MIRROR_MODE" != none ] && [ "$NPM_REGISTRY_OPT" != "" ]; then
    dim "  npm registry → ${NPM_REGISTRY}"
fi

# --mirror-index also points resolution at the mirror. That is separate
# from the download-URL rewrite above:
#   downloads (wheel/sdist bytes) come from absolute URLs in uv.lock;
#   resolution (build-isolation backends, sdist build deps) uses the index.
# pm/index_config.py forwards UV_INDEX_URL / UV_DEFAULT_INDEX /
# PIP_INDEX_URL to uv and strips every other UV_*.
# Off by default: pyproject.toml sets exclude-newer (filter by upload
# time), and some domestic mirrors omit upload-time metadata. A re-resolve
# then fails with "has no publish time". Turn it on only when you need it.
if [ "$MIRROR_INDEX" = true ]; then
    if [ -n "$MIRROR_BASE" ]; then
        MIRROR_SIMPLE="${MIRROR_BASE%/}/simple"
        export UV_INDEX_URL="$MIRROR_SIMPLE"
        export UV_DEFAULT_INDEX="$MIRROR_SIMPLE"
        export PIP_INDEX_URL="$MIRROR_SIMPLE"
        ok "resolution index set to ${MIRROR_SIMPLE}"
        dim "  affects resolution and sdist build deps only; frozen wheels still use the rewritten URLs"
    else
        warn "--mirror-index ignored: staying on direct PyPI, so there is no mirror index"
    fi
fi

# -----------------------------------------------------------------------------
# Step 1: bootstrap the Python runtime (uv -> CPython -> PM install)
# -----------------------------------------------------------------------------
if [ "$SKIP_RUNTIME" = true ]; then
    step "skipping the runtime bootstrap (--skip-runtime)"
    [ -n "$BOOT_PY" ] || die "no Python in the tool store; cannot skip the runtime bootstrap"
    ok "reusing store Python: $BOOT_PY"
else
    step "bootstrapping the Python runtime (uv -> CPython -> PM install)"
    UV=""
    if [ -n "$BOOT_PY" ]; then
        ok "reusing store Python: $BOOT_PY"
    else
        stage_uv
        PY_REQUEST="$(pin version python | cut -d+ -f1 | cut -d. -f1,2)"
        [ -n "$PY_REQUEST" ] || die "pm/lock.json has no python version pin"
        mkdir -p "$UV_PYTHON_INSTALL_DIR"
        if [ -n "$PYTHON_MIRROR_OPT" ]; then
            export UV_PYTHON_INSTALL_MIRROR="$PYTHON_MIRROR_OPT"
            dim "  CPython download mirror: ${UV_PYTHON_INSTALL_MIRROR}"
        fi
        info "installing CPython $PY_REQUEST ..."
        "$UV" python install --no-bin --no-registry "$PY_REQUEST"
        BOOT_PY="$("$UV" python find --managed-python "$PY_REQUEST")"
        BOOT_PY="${BOOT_PY%$'\r'}"
        [ -n "$BOOT_PY" ] && [ -x "$BOOT_PY" ] || die "could not locate the managed Python interpreter"
        ok "bootstrap Python: $BOOT_PY"
    fi

    if [ "$SHARED_STORE" = true ]; then
        dim "  shared tool store: pm only adds version+platform entries and does not delete existing ones"
        dim "  the build compares store entries at the end and lists every write"
    fi

    step "toolchain preflight and optional-tool selection"

    # Apply the toolchain rewrite before the preflight, so the report
    # shows the URL that will actually be used (not nodejs.org after it
    # has already been rewritten).
    if [ "$TOOL_MIRROR" != none ]; then
        apply_tool_mirror_lock
    fi

    tool_preflight

    DECLINED_NOW="$(decline_sync | awk -F'\t' '$1=="declined"{print $2}')"
    if [ -n "$DECLINED_NOW" ]; then
        ok "skipping optional tools: ${DECLINED_NOW}"
        dim "  PM records that choice in <HERMES_HOME>/installs/<key>/declined-packages.json and reuses it"
        dim "  install them again with --with-browser (or delete that file)"
    elif [ "$WITH_BROWSER" = true ]; then
        ok "cleared the optional-tool opt-out; installing the full set"
    fi

    seed_python_mirror

    info "syncing dependencies (python -m pm.cli install; first run can take 1-5 minutes)..."
    pm_args=(install)
    [ "$WITH_TEST_ENV" = true ] && pm_args+=(--test-environment)
    # Run from the repo root: `python -m` finds the pm package via CWD (same as setup-hermes.sh).
    ( cd "$REPO" && PYTHONHOME= PYTHONPATH= "$BOOT_PY" -m pm.cli "${pm_args[@]}" ) \
        || die "PM install failed (see the output above)"
    ok "dependencies synced"

    # Downloads are done; restore the lockfiles now. The trap is the fallback.
    if [ "$LOCK_REWRITTEN" = true ] || [ "$TOOLLOCK_REWRITTEN" = true ]; then
        restore_lock
    fi
fi

# -----------------------------------------------------------------------------
# Step 2: load the PM environment (node / npm / ripgrep / ffmpeg / tool PATH)
# -----------------------------------------------------------------------------
step "loading the PM environment"

PM_ENV_SH="$(PYTHONHOME= PYTHONPATH="$REPO" "$BOOT_PY" -m pm.environments | PYTHONHOME= PYTHONPATH="$REPO" "$BOOT_PY" -c '
import json, re, shlex, sys
for key, value in json.load(sys.stdin).items():
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        continue
    print("export %s=%s" % (key, shlex.quote(str(value))))
')" || die "could not read the PM environment (pm.environments failed)"
[ -n "$PM_ENV_SH" ] || die "PM environment is empty"
eval "$PM_ENV_SH"
ok "PM environment loaded"

# Resolve the interpreter PM selected. Core deps, and Pillow / resvg-py, live there.
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
    ok "app interpreter: $APP_PY"
else
    APP_PY="${PYTHON:-python3}"
    warn "could not resolve the PM interpreter; falling back to: $APP_PY"
fi

if ! "$APP_PY" -c 'import httpx, pydantic, fastapi' 2>/dev/null; then
    warn "core import check failed (httpx / pydantic / fastapi); see the PM output above"
fi

HAS_NODE=false
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    HAS_NODE=true
    ok "Node $(node --version) / npm $(npm --version)"
else
    warn "node/npm not on PATH; skipping frontend builds"
fi

# -----------------------------------------------------------------------------
# Steps 3-7: JS dependencies and frontend builds
# -----------------------------------------------------------------------------
BUILT_TUI=false
BUILT_WEB=false
BUILT_DESKTOP=false

if [ "$SKIP_JS" = true ]; then
    step "skipping frontend builds (--skip-js)"
elif [ "$HAS_NODE" = false ]; then
    step "skipping frontend builds (node/npm missing)"
else
    step "Installing JS dependencies (workspace root)"
    export npm_config_cache="${npm_config_cache:-$OUT/cache/npm}"
    mkdir -p "$npm_config_cache"
    # --- domestic mirrors for binary postinstalls ---
    # npm ci package bodies already use the registry mirror, but some
    # postinstall scripts fetch prebuilt binaries from GitHub Releases.
    # Registry settings do not apply to them:
    #   electron@40.10.2 -> github.com/electron/electron/releases/...darwin-arm64.zip
    #     109 MiB, measured connect ETIMEDOUT (20.205.243.166:443)
    #   node-gyp (when node-pty falls back to a local compile) -> nodejs.org
    #     headers, 0.04 MB/s
    #
    # Desktop is opt-in (--with-desktop). The default build does not need
    # the Electron runtime, so the download is skipped: line 14 of
    # electron/install.js exits 0 on ELECTRON_SKIP_BINARY_DOWNLOAD before
    # any network. --with-desktop downloads from npmmirror (measured
    # 1.76 MB/s, ~1 minute for 109 MiB). Verification uses the package's
    # own checksums.json, not the mirror's SHASUMS256.txt.
    export NODEJS_ORG_MIRROR="${NODEJS_ORG_MIRROR:-https://npmmirror.com/mirrors/node/}"
    if [ "$WITH_DESKTOP" = true ]; then
        # Empty, not unset: Node treats "" as falsy and install.js downloads normally.
        export ELECTRON_SKIP_BINARY_DOWNLOAD=""
        export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}"
        if [ "$ELECTRON_MIRROR_OPT" = none ]; then
            dim "  Electron binary: GitHub (--electron-mirror none)"
        else
            export ELECTRON_MIRROR="${ELECTRON_MIRROR_OPT:-${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}}"
            dim "  Electron binary: ${ELECTRON_MIRROR} (~109 MiB)"
        fi
    else
        export ELECTRON_SKIP_BINARY_DOWNLOAD="${ELECTRON_SKIP_BINARY_DOWNLOAD:-1}"
        dim "  skipping the Electron runtime binary (desktop is off unless you pass --with-desktop)"
    fi

    # npm ci deletes node_modules. Skip it when the installed tree already
    # matches package-lock.json; the npm cache under <out>/cache/npm covers
    # the case where the tree is missing or the lock changed.
    if [ -d "$REPO/node_modules" ] \
       && [ -f "$REPO/package-lock.json" ] \
       && [ -f "$REPO/node_modules/.package-lock.json" ] \
       && [ "$(hash_of "$REPO/package-lock.json")" = "$(hash_of "$REPO/node_modules/.package-lock.json")" ]; then
        ok "Reusing node_modules (package-lock.json unchanged)"
    else
        ( cd "$REPO" && npm ci ) || die "npm ci failed"
        ok "JS dependencies installed"
    fi

    step "generating icons (scripts/generate_icons.py)"
    rm -rf "$ICONS_DIR"
    mkdir -p "$ICONS_DIR"
    "$APP_PY" "$REPO/scripts/generate_icons.py" --source "$REPO" --out "$ICONS_DIR" \
        || die "icon generation failed"
    ok "icons -> $ICONS_DIR"

    step "building the TUI (ui-tui -> products/tui)"
    node "$REPO/scripts/build/tui.mjs" --source "$REPO" --out "$PRODUCTS_DIR/tui" \
        || die "TUI build failed"
    [ -f "$PRODUCTS_DIR/tui/dist/entry.js" ] || die "TUI output is missing dist/entry.js"
    ok "TUI → $PRODUCTS_DIR/tui/dist/entry.js"
    BUILT_TUI=true

    step "building the dashboard (web -> products/web)"
    node "$REPO/scripts/build/web.mjs" --source "$REPO" \
        --out "$PRODUCTS_DIR/web" --icons "$ICONS_DIR" \
        || die "dashboard build failed"
    [ -f "$PRODUCTS_DIR/web/index.html" ] || die "dashboard output is missing index.html"
    ok "Dashboard → $PRODUCTS_DIR/web/index.html"
    BUILT_WEB=true

    if [ "$WITH_DESKTOP" = true ]; then
        step "building the desktop app (Electron; slowest step)"
        dim "  compiles native modules such as node-pty and writes apps/desktop/{dist,build}"
        if [ ! -f "$REPO/apps/desktop/node_modules/electron/path.txt" ]; then
            warn "Electron runtime binary not found (apps/desktop/node_modules/electron/path.txt)"
            dim "  packaging will miss Electron. Fix: cd $REPO && npm run install:desktop"
        fi
        ( cd "$REPO" && npm run build --workspace apps/desktop ) || die "desktop build failed"
        rm -rf "$PRODUCTS_DIR/desktop"
        mkdir -p "$PRODUCTS_DIR/desktop"
        cp -R "$REPO/apps/desktop/dist/." "$PRODUCTS_DIR/desktop/"
        ok "desktop -> $PRODUCTS_DIR/desktop"
        BUILT_DESKTOP=true
    fi
fi

# -----------------------------------------------------------------------------
# Step 8: sync built-in skills (best effort)
# -----------------------------------------------------------------------------
if [ -d "$REPO/skills" ]; then
    step "syncing built-in skills to $HERMES_HOME/skills"
    if "$APP_PY" -m tools.skills_sync >/dev/null 2>&1; then
        ok "skills synced"
    else
        mkdir -p "$HERMES_HOME/skills"
        cp -Rn "$REPO/skills/." "$HERMES_HOME/skills/" 2>/dev/null || true
        warn "tools.skills_sync failed; copied the skill tree instead"
    fi
fi

# -----------------------------------------------------------------------------
# Step 9: .env
# -----------------------------------------------------------------------------
if [ "$COPY_ENV" = true ] && [ -f "$HOME/.hermes/.env" ]; then
    step "copying secrets into the dev HERMES_HOME (--copy-env)"
    cp "$HOME/.hermes/.env" "$HERMES_HOME/.env"
    chmod 600 "$HERMES_HOME/.env" 2>/dev/null || true
    ok "$HERMES_HOME/.env"
elif [ "$COPY_ENV" = true ]; then
    warn "no $HOME/.hermes/.env to copy; generating a template from .env.example"
    if [ ! -f "$HERMES_HOME/.env" ] && [ -f "$REPO/.env.example" ]; then
        cp "$REPO/.env.example" "$HERMES_HOME/.env"
        chmod 600 "$HERMES_HOME/.env" 2>/dev/null || true
        ok "wrote $HERMES_HOME/.env; add an API key or run hermes setup"
    fi
elif [ ! -f "$HERMES_HOME/.env" ] && [ -f "$REPO/.env.example" ]; then
    cp "$REPO/.env.example" "$HERMES_HOME/.env"
    chmod 600 "$HERMES_HOME/.env" 2>/dev/null || true
    ok "wrote a template from .env.example: $HERMES_HOME/.env (fill in keys, or use --copy-env)"
fi

# -----------------------------------------------------------------------------
# Step 10: publish launchers and write activate.sh
# -----------------------------------------------------------------------------
step "publishing launchers to $BIN_DIR"

PYTHONHOME= PYTHONPATH="$REPO" \
HERMES_HOME="$HERMES_HOME" HERMES_RUNTIME_DIR="$STORE" \
BLL_REPO="$REPO" BLL_BIN="$BIN_DIR" BLL_PRODUCTS="$PRODUCTS_DIR" \
"$BOOT_PY" - <<'PY' || die "launcher publish failed"
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
    # The upstream launcher is a multi-line exec: shlex.join wraps the
    # whole Python bootstrap as one quoted argument, and only the last
    # line carries '"$@"'. Copy the whole block; one line truncates the
    # script into an unclosed quote (unexpected EOF).
    text = Path(path).read_text(encoding="utf-8").splitlines()
    if text and text[0].startswith("#!"):
        text = text[1:]
    while text and not text[0].strip():
        text.pop(0)
    if not text or not text[0].startswith("exec "):
        sys.exit("unexpected launcher shape: %s" % path)
    header = ["#!/bin/sh", "# generated by build-local.sh — binds this checkout's dev environment"]
    header += ["export %s=%s" % (k, shlex.quote(v)) for k, v in extra.items()]
    Path(path).write_text("\n".join([*header, *text]) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("  " + str(path))
PY
ok "launchers published"

step "writing $OUT/activate.sh"
PYTHONHOME= \
BLL_OUT="$OUT" BLL_REPO="$REPO" BLL_BOOT_PY="$BOOT_PY" \
BLL_HOME="$HERMES_HOME" BLL_STORE="$STORE" \
BLL_BUILT_TUI="$BUILT_TUI" BLL_BUILT_WEB="$BUILT_WEB" \
BLL_TUI_DIR="$PRODUCTS_DIR/tui" BLL_WEB_DIST="$PRODUCTS_DIR/web" \
"$BOOT_PY" - <<'PY' || die "activate.sh generation failed"
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
# Hermes local dev environment — generated by build-local.sh, do not edit
# usage:  source {out}/activate.sh      leave:  deactivate
#
# Activation only borrows this shell. If HERMES_HOME leaks past deactivate,
# the production launcher (the hermes_cli shim) reads it and looks up a
# dependency env with the dev home plus the production checkout's install
# key. A miss re-downloads Python dependencies. Snapshot the previous
# values by name and restore them in deactivate, including PATH and
# PYTHONPATH exported by pm.environments.
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

# A second source keeps the first snapshot, so deactivate returns to a clean shell.
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
    echo "hermes: left the local dev environment (HERMES_HOME / PATH restored)"
}}

echo "hermes: local dev environment active -> {out}"
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
ok "activate with: source $OUT/activate.sh"

# -----------------------------------------------------------------------------
# Step 11: optional verification
# -----------------------------------------------------------------------------
if [ "$DO_VERIFY" = true ]; then
    step "checking the install (python -m pm.cli doctor)"
    ( cd "$REPO" && PYTHONHOME= PYTHONPATH= "$BOOT_PY" -m pm.cli doctor ) \
        || warn "pm doctor reported problems (see the output above)"
fi

# -----------------------------------------------------------------------------
# Step 12: build manifest
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
"$BOOT_PY" - <<'PY' || warn "could not write the build manifest"
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
ok "manifest: $OUT/build-manifest.json"

# -----------------------------------------------------------------------------
# Step 13: confirm the production install was not modified
# -----------------------------------------------------------------------------
PROD_CHANGED=false
PROD_DIFF=""

if [ -d "$PROD_HOME" ] && [ -n "${PROD_BEFORE:-}" ] && [ -f "$PROD_BEFORE" ]; then
    step "checking that the production install (${PROD_HOME}) was not modified"
    PROD_AFTER="$(mktemp)"
    production_fingerprint > "$PROD_AFTER"
    if diff -u "$PROD_BEFORE" "$PROD_AFTER" > /dev/null 2>&1; then
        ok "production fingerprint unchanged"
    else
        PROD_CHANGED=true
        PROD_DIFF="$(diff -u "$PROD_BEFORE" "$PROD_AFTER" | sed -n '3,40p')"
        warn "production fingerprint changed:"
        printf '%s\n' "$PROD_DIFF" | sed 's/^/    /' >&2
        warn "review each difference above (a facts.json rewrite in a shared store is expected)"
    fi
    rm -f "$PROD_AFTER"

    # Concrete shared-store additions and removals (clearer than a hash).
    if [ "$SHARED_STORE" = true ] && [ -s "$STORE_ENTRIES_BEFORE" ]; then
        STORE_ENTRIES_AFTER="$(mktemp)"
        ( find "$STORE" -maxdepth 1 -mindepth 1 -exec basename {} \; 2>/dev/null \
            | grep -v '^\.' | sort > "$STORE_ENTRIES_AFTER" ) || true
        STORE_ADDED="$(comm -13 "$STORE_ENTRIES_BEFORE" "$STORE_ENTRIES_AFTER" || true)"
        STORE_REMOVED="$(comm -23 "$STORE_ENTRIES_BEFORE" "$STORE_ENTRIES_AFTER" || true)"
        if [ -n "$STORE_ADDED" ]; then
            warn "shared tool store gained entries (additions only):"
            printf '%s\n' "$STORE_ADDED" | sed 's/^/    + /' >&2
        fi
        if [ -n "$STORE_REMOVED" ]; then
            warn "shared tool store lost entries:"
            printf '%s\n' "$STORE_REMOVED" | sed 's/^/    - /' >&2
            PROD_CHANGED=true
        fi
        [ -z "${STORE_ADDED}${STORE_REMOVED}" ] && ok "shared tool store entries unchanged"
        rm -f "$STORE_ENTRIES_AFTER"
    fi
fi
[ -n "${PROD_BEFORE:-}" ] && rm -f "$PROD_BEFORE"
[ -n "${STORE_ENTRIES_BEFORE:-}" ] && rm -f "$STORE_ENTRIES_BEFORE"

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
printf "\n${C_GREEN}✓ local dev build finished${C_NC}\n\n"
printf "  output            %s\n" "$OUT"
printf "  launcher          %s/hermes\n" "$BIN_DIR"
printf "  dev HERMES_HOME   %s\n" "$HERMES_HOME"
printf "  tool store        %s\n" "$STORE"
if [ "$MIRROR_MODE" != none ]; then
    if [ -n "$MIRROR_BASE" ]; then
        printf "  download source   %s (%s)\n" "$MIRROR_LABEL" "$MIRROR_BASE"
    else
        printf "  download source   direct (uv.lock not rewritten)\n"
    fi
    printf "  npm registry      %s\n" "$NPM_REGISTRY"
    if [ "$MIRROR_INDEX" = true ] && [ -n "$MIRROR_BASE" ]; then
        printf "  resolution index  %s\n" "${MIRROR_BASE%/}/simple"
    fi
    if [ "$LOCK_REWRITTEN" = true ]; then
        printf "  uv.lock           %s\n" "still rewritten (unexpected; check the backup)"
    else
        printf "  uv.lock           restored\n"
    fi
fi
if [ -n "$DECLINED_NOW" ]; then
    printf "  optional tools    skipped %s (reinstall with --with-browser)\n" "$DECLINED_NOW"
else
    printf "  optional tools    all installed\n"
fi
[ "$BUILT_TUI" = true ]     && printf "  TUI               %s/tui/dist/entry.js\n" "$PRODUCTS_DIR"
[ "$BUILT_WEB" = true ]     && printf "  Dashboard         %s/web/index.html\n" "$PRODUCTS_DIR"
[ "$BUILT_DESKTOP" = true ] && printf "  desktop           %s/desktop\n" "$PRODUCTS_DIR"
if [ "${ELECTRON_SKIP_BINARY_DOWNLOAD:-}" = "1" ]; then
    printf "  Electron runtime  not downloaded (pass --with-desktop to fetch it from npmmirror)\n"
fi
if [ "$GIT_HEAD" = unknown ]; then
    printf "  source            this working tree (not a git checkout; no clone)\n"
else
    printf "  source            this working tree %s @ %s (no clone)\n" "$GIT_BRANCH" "${GIT_HEAD:0:12}"
fi

if [ -d "$PROD_HOME" ]; then
    printf "\n  isolation from the production install\n\n"
    printf "    production home   %s\n" "$PROD_HOME"
    printf "    production bin    %s/hermes (untouched)\n" "$PROD_BIN"
    printf "    install key       dev %s / production %s\n" "$DEV_INSTALL_KEY" "$PROD_INSTALL_KEY"
    printf "    launchd label     dev ai.hermes.gateway-%s / production ai.hermes.gateway\n" "$DEV_INSTALL_KEY"
    printf "    production hash   %s\n" "$([ "$PROD_CHANGED" = true ] && echo "changed (see the warning above)" || echo "unchanged")"
fi

printf "\n  next\n\n"
printf "    source %s/activate.sh\n" "$OUT"
printf "    hermes --version\n"
printf "    hermes setup          # first-time model / API key setup (or rerun with --copy-env)\n"
printf "    hermes                # start a chat\n"
printf "    deactivate            # leave and restore HERMES_HOME / PATH\n"
printf "                          # required: a leftover HERMES_HOME makes the production\n"
printf "                          # hermes look in the dev home and re-download Python deps)\n\n"
if [ -d "$PROD_HOME" ]; then
    printf "  ${C_YELLOW}⚠ with a production install present, do not run these here:${C_NC}\n\n"
    printf "    hermes update         # its launchd restart hits the bare ai.hermes.gateway label (production)\n"
    printf "    hermes gateway ...    # lifecycle commands belong in production, or after production is uninstalled\n\n"
fi
dim "  note: .build/ and .venv/ are gitignored."
dim "  note: do not also source ./activate; this activate.sh points at the same PM environment."
printf "\n"
