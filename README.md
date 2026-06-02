# Mix

轻量级多模型切换器 + 协议代理。一个 TUI 选择 CLI / Provider / Model，自动拉起本地代理，透明转发到上游 LLM 平台。

## 为什么选 Mix

**零依赖** — 纯 Python 标准库，`pip install` 不需要联网拉包。Python 3.9+ 即可运行。

**协议透明转换** — 一个代理同时处理三种 API 格式：
- Anthropic Messages (`/v1/messages`)
- OpenAI Chat Completions (`/v1/chat/completions`)
- OpenAI Responses (`/v1/responses`)

Claude Code、Codex CLI、任意 OpenAI-compatible 客户端都能直接对接任意 provider。

**会话隔离** — 每次启动创建独立 session：独立 HOME、独立 CLI 配置、独立工作区。不同模型/Provider 的凭证和状态互不污染。

**安全设计**：
- 代理只绑定 `127.0.0.1`，不暴露局域网
- 每次启动生成随机 proxy token，CLI 不接触真实 API Key
- Provider URL 强制 HTTPS（localhost 例外）
- 默认不记录响应 body

**一行安装**：
```bash
curl -fsSL https://raw.githubusercontent.com/USER/mix/main/install.sh | bash -s --
```

**轻量** — 三个源文件（`mix.py` / `proxy.py` / `mix_config.py`），无 Node.js / 无 npm / 无外部服务。

## 使用

```bash
mix
```

打开三栏 TUI：

```
CLI | PROVIDER | MODEL
↑/↓ move  ←/→/Tab switch pane  Enter launch  q quit
```

支持的 CLI：

- `claude` — Claude Code
- `codex` — Codex CLI

两个 CLI 都能选择所有 provider/model。

### 直接指定

```bash
mix --cli claude --provider glm --model glm-4.5
mix --cli codex --provider qwen --model qwen3.6-plus
mix --cli codex --provider openai --model gpt-5 -- --full-auto
```

### 其他命令

```bash
mix --list                        # 查看可用模型
mix --dry-run                     # 只看不启动
mix --no-tui                      # 文本选择模式
mix --init-config                 # 生成配置文件
```

配置路径：`~/.config/mix/config.json`，可用 `MIX_CONFIG=/path/config.json` 覆盖。

## API Key

不把 key 写进配置，只写环境变量名：

```bash
export MMS_API_KEY="sk-..."          # AdsConflux Gateway
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export OPENROUTER_API_KEY="sk-or-..."
export GLM_API_KEY="..."
export DASHSCOPE_API_KEY="sk-..."
```

TUI 会显示每个 provider 的 key 状态。

## 本地代理

启动 CLI 前自动拉起临时代理：

```
claude -> ANTHROPIC_BASE_URL=http://127.0.0.1:<port>
codex  -> OPENAI_BASE_URL=http://127.0.0.1:<port>/v1
```

代理支持的协议转换：

| 入口 | 上游 | 说明 |
|------|------|------|
| `POST /v1/messages` | Anthropic Messages | 优先直连；不支持时转 OpenAI `/chat/completions` |
| `POST /v1/chat/completions` | OpenAI-compatible | 直接转发 |
| `POST /v1/responses` | OpenAI Responses | 转换为 `/chat/completions` |
| `GET /health` | — | `{ "ok": true }` |

## 会话隔离

每次启动创建独立 session 目录 `~/.config/mix/sessions/<id>/`：

```
home/       # 独立 HOME
config/     # CLI 配置
workspace/  # 预留工作区
logs/       # 预留日志
```

- Claude Code：独立 `HOME`、`CLAUDE_CONFIG_DIR`、`.claude.json`、`settings.json`
- Codex CLI：独立 `HOME`、`CODEX_HOME`、`config.toml`、`auth.json`

## 目录信任

首次在新目录启动 Claude/Codex 时保留 CLI 原生信任确认。确认后 Mix 保存到 `~/.config/mix/trusted-projects.json`，后续同目录不同 provider/model/session 自动同步，不再重复弹确认。

## AdsConflux Gateway

默认 provider，开箱即用：

```
openai_base_url    = https://chat.adsconflux.xyz/openapi/v1
anthropic_base_url = https://chat.adsconflux.xyz/openapi
api_key_env        = MMS_API_KEY
```

Mix 会用 `GET .../models` 获取模型列表并缓存到 `~/.config/mix/models-cache.json`。

## 配置格式

```json
{
  "clis": [
    {"id": "claude", "name": "Claude Code", "command": "claude", "model_args": ["--model", "{model}"]},
    {"id": "codex", "name": "Codex CLI", "command": "codex", "model_args": ["--model", "{model}"]}
  ],
  "providers": [
    {
      "id": "adsgateway",
      "name": "AdsConflux Gateway",
      "type": "openai",
      "base_url": "https://chat.adsconflux.xyz/openapi/v1",
      "openai_base_url": "https://chat.adsconflux.xyz/openapi/v1",
      "anthropic_base_url": "https://chat.adsconflux.xyz/openapi",
      "api_key_env": "MMS_API_KEY",
      "models_endpoint": "/models",
      "models": [
        {"id": "glm-5", "name": "glm-5"},
        {"id": "qwen3.6-plus", "name": "qwen3.6-plus"}
      ]
    },
    {
      "id": "qwen",
      "name": "Qwen DashScope",
      "type": "openai",
      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "api_key_env": "DASHSCOPE_API_KEY",
      "models": [
        {"id": "qwen3.6-plus", "name": "Qwen3.6 Plus"}
      ]
    }
  ]
}
```

## 安装

### 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/USER/mix/main/install.sh | bash -s --
```

支持选项：

```bash
bash install.sh --lang en                     # 英文 UI
bash install.sh --ref v0.1.0                  # 指定版本
bash install.sh --install-cli claude,codex    # 同时安装 CLI
bash install.sh --write-shell-rc              # 自动写 PATH
bash install.sh --check                       # 检查环境
```

### 本地安装

```bash
cd mix && bash install.sh
```

### 从源码运行

```bash
pip install .
mix --list
```

## License

MIT
