#!/bin/bash
# Mix 一键安装脚本
# 用法: curl -fsSL <url>/install.sh | bash -s --
# 或:   bash install.sh [--write-shell-rc] [--run-setup] [--install-cli claude,codex]
# 或:   bash install.sh --ref v0.1.0
set -e
set -o pipefail

# ── 配置 ──
REPO_OWNER="${MIX_REPO_OWNER:-CtriXin}"
REPO_NAME="${MIX_REPO_NAME:-mix}"
SCRIPT_SOURCE_PATH="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
case "$SCRIPT_SOURCE_PATH" in
""|stdin|/dev/fd/*|/proc/*/fd/*)
  SCRIPT_DIR=""
  ;;
*)
  if [ -f "$SCRIPT_SOURCE_PATH" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE_PATH")" 2>/dev/null && pwd 2>/dev/null || echo "")"
  fi
  ;;
esac

SOURCE_DIR=""
SOURCE_TMP_DIR=""
INSTALL_REF=""
RESOLVED_INSTALL_REF=""
INSTALL_CHANNEL="latest-tag"
LATEST_TAG_CACHE=""
DEFAULT_INSTALL_FALLBACK_TAG="${MIX_INSTALL_FALLBACK_TAG:-v0.1.0}"

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=9

INSTALL_LANG="zh"
INSTALL_LANG_EXPLICIT=0
WRITE_SHELL_RC=0
RUN_SETUP=0
INSTALL_CLI_LIST=""
INSTALL_CLI_EXPLICIT=0
CHECK_ONLY=0
PRINT_ONLY_VERSION=0
DRY_RUN=0

CLAUDE_CLI_PACKAGE_SPEC="${CLAUDE_CLI_PACKAGE_SPEC:-@anthropic-ai/claude-code@latest}"
CODEX_CLI_PACKAGE_SPEC="${CODEX_CLI_PACKAGE_SPEC:-@openai/codex@latest}"

REAL_HOME_CANDIDATE="${REAL_HOME:-${MIX_REAL_HOME:-${ORIGINAL_HOME:-}}}"
REAL_HOME="${REAL_HOME_CANDIDATE:-$HOME}"
if [[ "$REAL_HOME" == */.config/mix/* ]]; then
  REAL_HOME="${REAL_HOME%%/.config/mix/*}"
fi

MIX_HOME="$REAL_HOME/.config/mix"
BIN_DIR="$REAL_HOME/.local/bin"
VENV_DIR="$MIX_HOME/.venv"
CONFIG_PATH="$MIX_HOME/config.json"
VERSION_META_PATH="$MIX_HOME/version.json"

# ── 清理 ──
cleanup() {
  if [ -n "$SOURCE_TMP_DIR" ] && [ -d "$SOURCE_TMP_DIR" ]; then
    rm -rf "$SOURCE_TMP_DIR"
  fi
}
trap cleanup EXIT

# ── 双语 ──
t() {
  local zh="$1" en="$2"
  if [ "$INSTALL_LANG" = "en" ]; then printf "%s" "$en"
  else printf "%s" "$zh"; fi
}

# ── 工具函数 ──
normalize_install_ref() {
  local ref="$1"
  ref="${ref#refs/tags/}"
  ref="${ref#refs/heads/}"
  ref="${ref%^{}}"
  ref="${ref%\}}"
  ref="${ref#\{}"
  printf "%s" "$ref"
}

is_local_source_install() {
  [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/install.sh" ] && [ -f "$SCRIPT_DIR/mix.py" ]
}

resolve_local_source_ref() {
  if ! is_local_source_install; then return 1; fi
  if command -v git >/dev/null 2>&1 && git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$SCRIPT_DIR" describe --tags --always --dirty 2>/dev/null || true
    return 0
  fi
  echo "local-source"
}

can_prompt_interactively() {
  [ -r /dev/tty ] && [ -w /dev/tty ] || return 1
  { : < /dev/tty > /dev/tty; } 2>/dev/null
}

read_from_tty() {
  local prompt="$1" value=""
  if ! can_prompt_interactively; then return 1; fi
  printf "%s" "$prompt" > /dev/tty
  IFS= read -r value < /dev/tty || return 1
  printf "%s" "$value"
}

confirm_from_tty() {
  local prompt="$1" default_value="$2" answer="" normalized=""
  answer="$(read_from_tty "$prompt")" || return 1
  normalized="$(printf "%s" "$answer" | tr '[:upper:]' '[:lower:]' | xargs)"
  [ -z "$normalized" ] && normalized="$default_value"
  case "$normalized" in y|yes) return 0 ;; *) return 1 ;; esac
}

fetch_url_stdout() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl --retry 3 --retry-delay 2 --connect-timeout 10 -fsSL "$url"
    return $?
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO- "$url"
    return $?
  fi
  return 1
}

download_url_to_file() {
  local url="$1" output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl --retry 3 --retry-delay 2 --connect-timeout 10 -fsSL "$url" -o "$output"
    return $?
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO "$output" "$url"
    return $?
  fi
  return 1
}

_python_bin() {
  if [ -n "$PYTHON_CMD" ]; then printf "%s\n" "$PYTHON_CMD"
  else printf "%s\n" "python3"; fi
}

_python_candidate_works() {
  local candidate="$1"
  [ -n "$candidate" ] || return 1
  if [[ "$candidate" == */* ]]; then
    [ -x "$candidate" ] || return 1
  else
    candidate="$(command -v "$candidate" 2>/dev/null || true)"
    [ -n "$candidate" ] && [ -x "$candidate" ] || return 1
  fi
  local ver major minor
  ver="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)" || return 1
  [ -n "$ver" ] || return 1
  major="${ver%%.*}"; minor="${ver#*.}"
  [ -n "$major" ] && [ -n "$minor" ] || return 1
  [ "$major" -ge "$MIN_PYTHON_MAJOR" ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]
}

find_supported_python() {
  local candidate=""
  for candidate in \
    "$PYTHON_CMD" \
    python3.13 python3.12 python3.11 python3.10 python3.9 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    python3; do
    if _python_candidate_works "$candidate"; then
      if [[ "$candidate" == */* ]]; then printf "%s\n" "$candidate"
      else command -v "$candidate" 2>/dev/null; fi
      return 0
    fi
  done
  return 1
}

ensure_supported_python() {
  local resolved_python=""
  resolved_python="$(find_supported_python || true)"
  if [ -n "$resolved_python" ]; then
    PYTHON_CMD="$resolved_python"
    local _pver
    _pver="$("$PYTHON_CMD" --version 2>/dev/null)" || true
    echo "✓ Python: $_pver ($PYTHON_CMD)"
    return
  fi
  echo "❌ $(t "未检测到 Python 3.9+，请安装后重试" "Python 3.9+ not found; please install and retry")"
  exit 1
}

find_cli_binary() {
  local command_name="$1" candidate="" dir=""
  [ -z "$command_name" ] && return 1
  candidate="$(command -v "$command_name" 2>/dev/null || true)"
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    printf "%s\n" "$candidate"
    return 0
  fi
  for dir in \
    "$BIN_DIR" \
    /opt/homebrew/bin \
    /usr/local/bin \
    "$REAL_HOME/.npm-global/bin" \
    "$REAL_HOME/.bun/bin" \
    "$REAL_HOME/.nvm/versions/node/"*/bin \
    /usr/bin /bin; do
    [ -d "$dir" ] || continue
    candidate="$dir/$command_name"
    if [ -x "$candidate" ]; then
      printf "%s\n" "$candidate"
      return 0
    fi
  done
  return 1
}

shell_name() { basename "${SHELL:-}" 2>/dev/null || true; }

# ── 版本解析 ──
resolve_latest_tag() {
  if [ -n "${MIX_INSTALL_LATEST_TAG_OVERRIDE:-}" ]; then
    normalize_install_ref "$MIX_INSTALL_LATEST_TAG_OVERRIDE"
    return 0
  fi
  if [ -n "$LATEST_TAG_CACHE" ]; then
    printf "%s" "$LATEST_TAG_CACHE"
    return 0
  fi
  local resolved_tag=""
  if command -v python3 >/dev/null 2>&1; then
    resolved_tag="$(python3 - "$REPO_OWNER" "$REPO_NAME" <<'PY'
import json, re, sys
from urllib.request import Request, urlopen
owner, repo = sys.argv[1], sys.argv[2]
url = f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=100"
req = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "mix-install"})
try:
    with urlopen(req, timeout=15) as resp: data = json.load(resp)
except Exception: sys.exit(1)
semver = []
for item in data:
    if not isinstance(item, dict): continue
    tag = str(item.get("name") or "").strip()
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    if m: semver.append(((int(m.group(1)), int(m.group(2)), int(m.group(3))), tag))
if not semver: sys.exit(1)
semver.sort(reverse=True)
print(semver[0][1])
PY
)" || true
  fi
  if [ -z "$resolved_tag" ]; then
    resolved_tag="$(git ls-remote --tags "https://github.com/${REPO_OWNER}/${REPO_NAME}.git" 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null \
      | sed 's#refs/tags/##; s#\^{}##' \
      | awk '{print $2}' \
      | sort -u \
      | awk -F'[v.]' '{ printf "%09d %09d %09d %s\n", $2, $3, $4, $0 }' \
      | sort -r \
      | head -n 1 \
      | awk '{print $4}'
    )" || true
  fi
  if [ -z "$resolved_tag" ] && [ -n "$DEFAULT_INSTALL_FALLBACK_TAG" ]; then
    resolved_tag="$DEFAULT_INSTALL_FALLBACK_TAG"
  fi
  resolved_tag="$(normalize_install_ref "$resolved_tag")"
  [ -n "$resolved_tag" ] || return 1
  LATEST_TAG_CACHE="$resolved_tag"
  printf "%s" "$resolved_tag"
}

ensure_install_ref_resolved() {
  if is_local_source_install; then
    INSTALL_CHANNEL="local-source"
    RESOLVED_INSTALL_REF="$(resolve_local_source_ref || true)"
    RESOLVED_INSTALL_REF="${RESOLVED_INSTALL_REF:-local-source}"
    return
  fi
  if [ -z "$RESOLVED_INSTALL_REF" ]; then
    resolve_requested_ref
  fi
}

resolve_requested_ref() {
  local ref="$INSTALL_REF"
  if [ -z "$ref" ]; then
    ref="$(resolve_latest_tag || true)"
    if [ -n "$ref" ]; then
      echo "✓ latest tag: $ref"
    else
      echo "⚠ $(t "获取最新 tag 失败，回退到 main" "Failed to fetch latest tag, falling back to main")"
      ref="main"
    fi
  fi
  ref="$(normalize_install_ref "$ref")"
  RESOLVED_INSTALL_REF="$ref"
}

# ── 源码下载 ──
download_remote_source() {
  local ref="$1" archive_url="" tarball="$SOURCE_TMP_DIR/source.tar.gz"
  [ -z "$ref" ] && return 1
  ref="$(normalize_install_ref "$ref")"
  if [ "$ref" = "main" ]; then
    archive_url="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/main.tar.gz"
  else
    archive_url="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/tags/${ref}.tar.gz"
  fi
  echo "$(t "正在下载源码归档" "Downloading source archive"): $archive_url"
  if ! download_url_to_file "$archive_url" "$tarball"; then
    echo "❌ $(t "下载源码归档失败，请检查网络后重试" "Failed to download source archive; check network and retry")"
    return 1
  fi
  if ! tar -xzf "$tarball" -C "$SOURCE_TMP_DIR"; then
    echo "❌ $(t "源码归档解压失败" "Failed to extract source archive")"
    return 1
  fi
  SOURCE_DIR="$(find "$SOURCE_TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [ -z "$SOURCE_DIR" ] || [ ! -f "$SOURCE_DIR/mix.py" ]; then
    echo "❌ $(t "远程源码解压失败" "Failed to extract downloaded source")"
    return 1
  fi
  echo "✓ $(t "已获取源码" "Source prepared"): $SOURCE_DIR"
}

prepare_source_dir() {
  ensure_install_ref_resolved
  if is_local_source_install; then
    SOURCE_DIR="$SCRIPT_DIR"
    echo "✓ $(t "使用本地源码" "Using local source tree"): $SOURCE_DIR"
    return
  fi
  SOURCE_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mix-install.XXXXXX")"
  download_remote_source "$RESOLVED_INSTALL_REF"
}

# ── venv 与安装 ──
create_python_venv() {
  local venv_python="$VENV_DIR/bin/python"
  local broken_backup=""
  mkdir -p "$MIX_HOME"
  if [ -d "$VENV_DIR" ]; then
    if [ -x "$venv_python" ] && "$venv_python" -m pip --version >/dev/null 2>&1; then
      echo "✓ $(t "复用现有虚拟环境" "Reusing existing virtual environment"): $VENV_DIR"
      return
    fi
    broken_backup="${VENV_DIR}.broken-$(date -u +%Y%m%d%H%M%S)"
    mv "$VENV_DIR" "$broken_backup"
    echo "⚠ $(t "检测到损坏的虚拟环境，已备份后重建" "Detected broken venv; backed up and rebuilding"): $broken_backup"
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] $(t "创建虚拟环境" "Create venv"): $VENV_DIR"
    return
  fi
  if ! "$(_python_bin)" -m venv "$VENV_DIR"; then
    echo "❌ $(t "创建 Python 虚拟环境失败" "Failed to create Python virtual environment")"
    exit 1
  fi
  "$venv_python" -m pip install --quiet --upgrade pip
}

install_mix_package() {
  local venv_python="$VENV_DIR/bin/python"
  local venv_mix="$VENV_DIR/bin/mix"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] pip install $SOURCE_DIR"
    return
  fi
  echo "$(t "正在安装 Mix..." "Installing Mix...")"
  if ! "$venv_python" -m pip install --quiet "$SOURCE_DIR"; then
    echo "❌ $(t "安装 Mix 失败" "Failed to install Mix")"
    exit 1
  fi
  if [ ! -x "$venv_mix" ]; then
    echo "❌ $(t "安装后未找到 mix 入口" "mix entry point not found after install")"
    exit 1
  fi
  echo "✓ $(t "已安装 Mix" "Mix installed"): $venv_mix"
}

# ── 命令链接 ──
create_bin_symlink() {
  local venv_mix="$VENV_DIR/bin/mix"
  local target="$BIN_DIR/mix"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] ln -sf $venv_mix $target"
    return
  fi
  mkdir -p "$BIN_DIR"
  ln -sf "$venv_mix" "$target"
  echo "✓ $(t "命令已链接到" "Command linked to") $target"
}

# ── PATH ──
write_posix_path_rc() {
  local target="$1" marker="# Added by Mix"
  local path_line='export PATH="$HOME/.local/bin:$PATH"'
  [ -n "$target" ] || return 1
  mkdir -p "$(dirname "$target")"
  touch "$target"
  if ! grep -q "$marker" "$target" 2>/dev/null; then
    { echo ""; echo "$marker"; echo "$path_line"; } >> "$target"
    echo "✓ PATH $(t "已写入" "written to") $target"
  fi
}

write_fish_path_rc() {
  local target="$REAL_HOME/.config/fish/conf.d/mix.fish"
  local marker="# Added by Mix"
  local path_line='fish_add_path -g "$HOME/.local/bin"'
  mkdir -p "$(dirname "$target")"
  if ! grep -q "$marker" "$target" 2>/dev/null; then
    { echo "$marker"; echo "$path_line"; } >> "$target"
    echo "✓ PATH $(t "已写入" "written to") $target"
  fi
}

write_shell_path_config() {
  local shell_base
  shell_base="$(shell_name)"
  case "$shell_base" in
  fish) write_fish_path_rc ;;
  zsh)   write_posix_path_rc "$REAL_HOME/.zshrc" ;;
  bash)
    write_posix_path_rc "$REAL_HOME/.bashrc"
    if [ "$(uname -s 2>/dev/null || true)" = "Darwin" ]; then
      write_posix_path_rc "$REAL_HOME/.bash_profile"
    fi
    ;;
  *)
    if [ -f "$REAL_HOME/.zshrc" ]; then write_posix_path_rc "$REAL_HOME/.zshrc"
    elif [ -f "$REAL_HOME/.bashrc" ]; then write_posix_path_rc "$REAL_HOME/.bashrc"
    elif [ -d "$REAL_HOME/.config/fish" ]; then write_fish_path_rc
    else write_posix_path_rc "$REAL_HOME/.profile"; fi
    ;;
  esac
}

print_path_setup_hint() {
  echo "⚠ $(t "未修改 shell 配置" "Shell config was not modified")"
  echo "  $(t "可直接运行" "Run directly"): $BIN_DIR/mix"
  echo "  $(t "或添加 PATH" "Or add PATH"): export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo "  $(t "或重新执行" "Or rerun"): bash install.sh --write-shell-rc"
}

# ── 版本元数据 ──
write_version_metadata() {
  ensure_install_ref_resolved
  mkdir -p "$(dirname "$VERSION_META_PATH")"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] $(t "写入版本元数据" "Write version metadata"): $VERSION_META_PATH"
    return
  fi
  "$(_python_bin)" - "$VERSION_META_PATH" "$RESOLVED_INSTALL_REF" "$INSTALL_CHANNEL" "$INSTALL_LANG" <<'PY'
import json, re, sys
from datetime import datetime, timezone
path, ref, channel, lang = sys.argv[1:5]
version = ref if re.fullmatch(r"v\d+\.\d+\.\d+", ref) else ""
lang = "en" if lang.lower().startswith("en") else "zh"
payload = {
    "installed_ref": ref,
    "installed_version": version,
    "install_channel": channel,
    "preferred_language": lang,
    "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source": "install.sh",
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  chmod 600 "$VERSION_META_PATH"
  echo "✓ $(t "已记录安装版本" "Recorded installed version"): $RESOLVED_INSTALL_REF"
}

current_installed_ref() {
  [ ! -f "$VERSION_META_PATH" ] && return 0
  "$(_python_bin)" - "$VERSION_META_PATH" <<'PY'
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f: data = json.load(f)
    print(str(data.get("installed_ref") or "").strip())
except Exception: pass
PY
}

# ── 可选 CLI 安装 ──
npm_global_install() {
  local label="$1" package="$2"
  if ! command -v npm >/dev/null 2>&1; then
    echo "⚠ $(t "未检测到 npm，跳过" "npm not found, skipping"): $label"
    return 1
  fi
  echo "→ $(t "正在安装" "Installing") $label..."
  set +e
  npm install -g "$package"
  local status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    echo "✓ $label"
    return 0
  fi
  echo "⚠ $(t "安装失败" "Install failed"): $label"
  return 1
}

install_named_cli() {
  local cli_name="$1" command_name="" label="" package_spec=""
  case "$cli_name" in
  claude) command_name="claude"; label="Claude Code"; package_spec="$CLAUDE_CLI_PACKAGE_SPEC" ;;
  codex)  command_name="codex";  label="Codex CLI";   package_spec="$CODEX_CLI_PACKAGE_SPEC" ;;
  *) echo "⚠ $(t "未知 CLI" "Unknown CLI"): $cli_name"; return 1 ;;
  esac
  if find_cli_binary "$command_name" >/dev/null 2>&1; then
    echo "✓ $label ($(find_cli_binary "$command_name"))"
    return 0
  fi
  npm_global_install "$label" "$package_spec" || true
  if find_cli_binary "$command_name" >/dev/null 2>&1; then
    echo "✓ $label ($(find_cli_binary "$command_name"))"
  else
    echo "⚠ $(t "$label 未安装成功，稍后可重试" "$label install incomplete; retry later")"
  fi
}

install_requested_clis() {
  [ -z "$INSTALL_CLI_LIST" ] && return 0
  echo ""
  echo "$(t "正在安装可选 CLI..." "Installing optional CLIs...")"
  local cli_name=""
  IFS=',' read -r -a _items <<< "$INSTALL_CLI_LIST"
  for cli_name in "${_items[@]}"; do
    install_named_cli "$cli_name" || true
  done
}

prompt_optional_cli_choices() {
  if ! can_prompt_interactively; then return 0; fi
  if [ "$INSTALL_CLI_EXPLICIT" -eq 1 ]; then return 0; fi

  local cli_name cli_command cli_label cli_path
  echo ""
  echo "$(t "可选 CLI 工具" "Optional CLI tools")"
  for cli_name in claude codex; do
    case "$cli_name" in
    claude) cli_command="claude"; cli_label="Claude Code" ;;
    codex)  cli_command="codex";  cli_label="Codex CLI" ;;
    esac
    if cli_path="$(find_cli_binary "$cli_command" 2>/dev/null)"; then
      echo " ✓ $(t "已检测到" "Detected"): $cli_label ($cli_path)"
      continue
    fi
    if [ "$INSTALL_LANG" = "en" ]; then
      if confirm_from_tty " ${cli_label} not found. Install now? [y/N]: " "n"; then
        INSTALL_CLI_LIST="${INSTALL_CLI_LIST:+$INSTALL_CLI_LIST,}$cli_name"
      fi
    else
      if confirm_from_tty " 未检测到 ${cli_label}，现在安装吗？[y/N]: " "n"; then
        INSTALL_CLI_LIST="${INSTALL_CLI_LIST:+$INSTALL_CLI_LIST,}$cli_name"
      fi
    fi
  done
}

# ── 用法 ──
usage() {
  cat <<EOF
$(t "Mix 安装脚本" "Mix installer")

$(t "用法:" "Usage:"):
  bash install.sh [--lang zh|en] [--ref <tag>] [--main] [--write-shell-rc]
  bash install.sh [--install-cli name[,name]] [--run-setup]
  bash install.sh --check | --version | --dry-run | --help

$(t "说明:" "Notes"):
- $(t "默认远程安装使用最新 semver tag" "Default remote install uses the latest semver tag")
- --ref $(t "指定版本号或分支" "pin a version or branch")
- --check $(t "仅检查环境与安装状态" "check environment and install state only")
- --version $(t "仅显示将安装的版本" "print planned version without installing")
- --dry-run $(t "预览不执行" "preview without changes")
- --lang $(t "设置 UI 语言" "set UI language") (zh / en)
- --install-cli $(t "安装 CLI（逗号分隔）" "install CLIs, comma-separated"): claude, codex
- --write-shell-rc $(t "写入 PATH 到 shell 配置" "write PATH to shell config")
EOF
}

# ── 参数解析 ──
while [ $# -gt 0 ]; do
  case "$1" in
  --lang)         INSTALL_LANG="$2"; INSTALL_LANG_EXPLICIT=1; shift 2 ;;
  --ref)          INSTALL_REF="$2"; shift 2 ;;
  --main)         INSTALL_REF="main"; shift ;;
  --latest-tag)   INSTALL_CHANNEL="latest-tag"; shift ;;
  --write-shell-rc) WRITE_SHELL_RC=1; shift ;;
  --run-setup)    RUN_SETUP=1; shift ;;
  --install-cli)  INSTALL_CLI_LIST="$2"; INSTALL_CLI_EXPLICIT=1; shift 2 ;;
  --check)        CHECK_ONLY=1; shift ;;
  --version)      PRINT_ONLY_VERSION=1; shift ;;
  --dry-run)      DRY_RUN=1; shift ;;
  --help|-h)      usage; exit 0 ;;
  *) echo "⚠ $(t "未知参数" "Unknown argument"): $1"; usage; exit 1 ;;
  esac
done

# ── 语言提示 ──
prompt_install_language() {
  [ "$INSTALL_LANG_EXPLICIT" -eq 1 ] && return 0
  if ! can_prompt_interactively; then return 0; fi
  local answer normalized
  echo ""
  echo "Language / 语言"
  echo " 1) 中文"
  echo " 2) English"
  answer="$(read_from_tty 'Choose UI language [1/2, default 1]: ')" || return 0
  normalized="$(printf "%s" "$answer" | tr '[:upper:]' '[:lower:]' | xargs)"
  case "$normalized" in
  2|en|english) INSTALL_LANG="en" ;;
  *)            INSTALL_LANG="zh" ;;
  esac
}

# ── 检查模式 ──
run_install_check() {
  local installed_ref stable_ref
  installed_ref="$(current_installed_ref || true)"
  stable_ref="$(resolve_latest_tag || true)"
  echo "$(t "版本概览" "Version overview")"
  echo "  $(t "当前已安装" "Currently installed"): ${installed_ref:-$(t "未安装" "none")}"
  echo "  $(t "线上最新" "Latest upstream"): ${stable_ref:-$(t "未获取" "unavailable")}"
  echo ""

  if find_supported_python >/dev/null 2>&1; then
    PYTHON_CMD="$(find_supported_python 2>/dev/null)"
    local _pver
    _pver="$("$PYTHON_CMD" --version 2>/dev/null)" || true
    echo "✓ $(t "Python 版本满足" "Python version OK"): $_pver"
  else
    echo "✗ $(t "未检测到 Python 3.9+" "Python 3.9+ not found")"
  fi

  local cli_name cli_path
  for cli_name in claude codex; do
    cli_path="$(find_cli_binary "$cli_name" 2>/dev/null || true)"
    if [ -n "$cli_path" ]; then
      echo "✓ $(t "已检测到 CLI" "CLI detected"): $cli_name ($cli_path)"
    else
      echo "• $(t "未检测到 CLI" "CLI not found"): $cli_name"
    fi
  done

  if [ -x "$VENV_DIR/bin/python" ]; then
    echo "✓ $(t "虚拟环境已存在" "Venv present"): $VENV_DIR"
  else
    echo "• $(t "虚拟环境未创建" "Venv not created"): $VENV_DIR"
  fi

  if [ -L "$BIN_DIR/mix" ]; then
    echo "✓ $(t "mix 命令链接已存在" "mix symlink present"): $BIN_DIR/mix"
    if [[ ":$PATH:" = *":$BIN_DIR:"* ]]; then
      echo "✓ $(t "~/.local/bin 已在 PATH" "~/.local/bin on PATH")"
    else
      echo "• $(t "~/.local/bin 不在 PATH" "~/.local/bin not on PATH")"
    fi
  else
    echo "• $(t "mix 命令链接未创建" "mix symlink not created"): $BIN_DIR/mix"
  fi
}

# ── 仅打印版本 ──
print_planned_version() {
  ensure_install_ref_resolved
  echo "$(t "计划安装版本" "Planned install ref"): ${RESOLVED_INSTALL_REF:-local-source}"
  echo "$(t "安装通道" "Install channel"): ${INSTALL_CHANNEL}"
}

# ══════════════════════════════════════
# 主流程
# ══════════════════════════════════════

echo ""
echo "═══════════════════════════════════"
echo "  $(t "Mix 安装脚本" "Mix Installer")"
echo "═══════════════════════════════════"
echo ""

# 0. 语言
prompt_install_language

# 1. 版本/检查模式
if [ "$PRINT_ONLY_VERSION" -eq 1 ]; then
  print_planned_version
  exit 0
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  PYTHON_CMD="${PYTHON_CMD:-$(find_supported_python || true)}"
  run_install_check
  exit 0
fi

# 2. Python
echo ""
echo "$(t "检查 Python 环境..." "Checking Python environment...")"
PYTHON_CMD="${PYTHON_CMD:-}"
ensure_supported_python

# 3. 源码
echo ""
prepare_source_dir

# 4. venv + 安装
echo ""
echo "$(t "准备安装环境..." "Preparing install environment...")"
create_python_venv
install_mix_package

# 5. 命令链接
echo ""
create_bin_symlink

# 6. 版本元数据
write_version_metadata

# 7. PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  if [ "$WRITE_SHELL_RC" -eq 1 ]; then
    write_shell_path_config
  else
    print_path_setup_hint
  fi
fi

# 8. 可选 CLI
prompt_optional_cli_choices
install_requested_clis

# 9. 配置初始化提示
if [ ! -f "$CONFIG_PATH" ] && [ "$DRY_RUN" -eq 0 ]; then
  echo ""
  echo "$(t "首次使用需要初始化配置:" "First-time config initialization:")"
  echo "  $BIN_DIR/mix --init-config"
fi

# 10. 完成
echo ""
if [ -x "$BIN_DIR/mix" ] || [ "$DRY_RUN" -eq 1 ]; then
  echo "===================================="
  echo " ✅ $(t "Mix 安装完成" "Mix install completed")"
  echo "===================================="
  echo ""
  echo "  $(t "运行" "Run") $BIN_DIR/mix $(t "开始使用" "to start")"
  if [[ ":$PATH:" = *":$BIN_DIR:"* ]]; then
    echo "  $(t "可直接运行" "Run directly"): mix"
  fi
  echo ""
  echo "  $(t "常用命令:" "Common commands:")"
  echo "    mix              $(t "打开 TUI" "open TUI")"
  echo "    mix --list       $(t "列出可用模型" "list models")"
  echo "    mix --init-config $(t "初始化配置" "init config")"
  echo "    mix --help       $(t "查看帮助" "show help")"
  echo ""

  if [ "$RUN_SETUP" -eq 1 ]; then
    echo "$(t "启动 Mix..." "Launching Mix...")"
    "$BIN_DIR/mix" || true
  fi
else
  echo "❌ $(t "安装失败，请检查错误信息" "Install failed; review errors above")"
  exit 1
fi
