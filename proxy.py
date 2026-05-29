#!/usr/bin/env python3
"""Mix local protocol proxy."""

from __future__ import annotations

import argparse
import copy
import http.client
import json
import os
import signal
import stat
import threading
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from mix_config import APP_NAME, load_config_file

USER_HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("MIX_CONFIG", USER_HOME / ".config" / APP_NAME / "config.json"))
MAX_BODY_SIZE = int(os.environ.get("MIX_MAX_BODY_SIZE", str(10 * 1024 * 1024)))
DOMESTIC_MODEL_PREFIXES = ("glm", "kimi", "k2.6", "mimo", "qwen", "minimax", "deepseek")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_shutdown_event = threading.Event()
_connection_cache_lock = threading.Lock()
_connection_cache: dict[str, tuple[Any, float]] = {}


def _signal_handler(signum: int, frame: Any) -> None:
    _shutdown_event.set()


def load_config() -> dict[str, Any]:
    return load_config_file(CONFIG_PATH)



def debug_log(message: str) -> None:
    path = os.environ.get("MIX_PROXY_LOG")
    if not path:
        return
    try:
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")
        os.chmod(log_path, 0o600)
    except OSError:
        pass


def response_log(status: int, path: str, body: bytes, *, stream: Any = None) -> str:
    message = f"status={status} path={path} bytes={len(body)}"
    if stream is not None:
        message += f" stream={stream}"
    if os.environ.get("MIX_PROXY_DEBUG_BODY") == "1":
        message += f" body={body[:500].decode('utf-8', errors='replace')}"
    return message


def validate_base_url(base_url: str, *, allow_insecure: bool = False) -> str:
    cleaned = base_url.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme == "https":
        return cleaned
    if parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS:
        return cleaned
    if allow_insecure or os.environ.get("MIX_ALLOW_INSECURE_BASE_URL") == "1":
        if parsed.scheme in {"http", "https"}:
            return cleaned
    raise ValueError(f"insecure base_url blocked: {cleaned}")


def provider_by_id(config: dict[str, Any], provider_id: str) -> dict[str, Any]:
    for provider in config.get("providers", []):
        if str(provider.get("id")) == provider_id:
            return provider
    raise KeyError(provider_id)


def _config_permissions_private() -> bool:
    try:
        mode = CONFIG_PATH.stat().st_mode
    except OSError:
        return False
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def provider_api_key(provider: dict[str, Any]) -> str:
    explicit = str(provider.get("api_key") or "").strip()
    if explicit:
        if not _config_permissions_private():
            raise ValueError("plaintext api_key requires private config permissions")
        debug_log(f"plaintext api_key deprecated provider={provider.get('id')}")
        return explicit
    api_key_env = str(provider.get("api_key_env") or "").strip()
    return os.environ.get(api_key_env, "") if api_key_env else ""


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text_from_content_block(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    if item_type in {"text", "input_text", "output_text"}:
        return str(item.get("text") or "")
    if item_type == "tool_result":
        tool_use_id = str(item.get("tool_use_id") or "")
        content = text_from_content(item.get("content"))
        return f"Tool result {tool_use_id}:\n{content}" if tool_use_id else f"Tool result:\n{content}"
    if item_type == "tool_use":
        name = str(item.get("name") or "tool")
        tool_input = item.get("input")
        return f"Tool call {name}: {_compact_json(tool_input)}"
    if item_type in {"thinking", "redacted_thinking"}:
        return ""
    if "text" in item:
        return str(item.get("text") or "")
    if "content" in item:
        return text_from_content(item.get("content"))
    return ""


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(_text_from_content_block(item))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return _text_from_content_block(content)
    return str(content or "")


def _openai_content_part_from_anthropic(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = str(item.get("type") or "")
    if item_type in {"text", "input_text", "output_text"}:
        return {"type": "text", "text": str(item.get("text") or "")}
    if item_type == "image":
        source = item.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            media_type = str(source.get("media_type") or "application/octet-stream")
            data = str(source.get("data") or "")
            return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
        if isinstance(source, dict) and source.get("url"):
            return {"type": "image_url", "image_url": {"url": str(source.get("url"))}}
    if item_type in {"thinking", "redacted_thinking"}:
        return None
    text = _text_from_content_block(item)
    return {"type": "text", "text": text} if text else None


def _openai_content_from_anthropic(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append({"type": "text", "text": item})
            elif isinstance(item, dict):
                part = _openai_content_part_from_anthropic(item)
                if part:
                    parts.append(part)
        if not parts:
            return ""
        if all(part.get("type") == "text" for part in parts):
            return "\n".join(str(part.get("text") or "") for part in parts if part.get("text"))
        return parts
    return text_from_content(content)


def _anthropic_tool_results_to_openai_messages(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    has_tool_result = any(isinstance(item, dict) and item.get("type") == "tool_result" for item in content)
    if not has_tool_result:
        return []
    out: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            text = str(item).strip() if isinstance(item, str) else ""
            if text:
                out.append({"role": "user", "content": text})
            continue
        item_type = str(item.get("type") or "")
        if item_type == "tool_result":
            tool_call_id = str(item.get("tool_use_id") or "")
            result_content = text_from_content(item.get("content"))
            if tool_call_id:
                out.append({"role": "tool", "tool_call_id": tool_call_id, "content": result_content})
            else:
                out.append({"role": "user", "content": result_content})
        elif item_type in {"thinking", "redacted_thinking"}:
            continue
        else:
            text = _text_from_content_block(item)
            if text:
                out.append({"role": "user", "content": text})
    return out


def _openai_tool_calls_from_anthropic(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    tool_calls = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        tool_calls.append(
            {
                "id": str(item.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or "tool"),
                    "arguments": _compact_json(item.get("input") or {}),
                },
            }
        )
    return tool_calls


def _anthropic_tool_blocks_from_openai(message: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        arguments = function.get("arguments") or "{}"
        try:
            parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            parsed_arguments = {"arguments": arguments}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(tool_call.get("id") or ""),
                "name": str(function.get("name") or "tool"),
                "input": parsed_arguments if isinstance(parsed_arguments, dict) else {"value": parsed_arguments},
            }
        )
    return blocks


def _openai_tools_from_anthropic(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            out.append(tool)
            continue
        name = str(tool.get("name") or "")
        if not name:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _openai_tool_choice_from_anthropic(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return tool_choice
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool":
        return {"type": "function", "function": {"name": str(tool_choice.get("name") or "")}}
    return tool_choice


def _tool_name_from_openai_tool(tool: dict[str, Any]) -> str:
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(function.get("name") or tool.get("name") or "")


def _tool_names_from_anthropic(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    return [str(tool.get("name") or "") for tool in tools if isinstance(tool, dict) and str(tool.get("name") or "")]


def _tool_names_from_openai(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    names = []
    for tool in tools:
        if isinstance(tool, dict):
            name = _tool_name_from_openai_tool(tool)
            if name:
                names.append(name)
    return names


def _content_block_type_counts(messages: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(messages, list):
        return counts
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                block_type = str(item.get("type") or type(item).__name__) if isinstance(item, dict) else type(item).__name__
                counts[block_type] = counts.get(block_type, 0) + 1
        elif content is not None:
            block_type = type(content).__name__
            counts[block_type] = counts.get(block_type, 0) + 1
    return counts


def _openai_tool_call_names(messages: Any) -> list[str]:
    names = []
    if not isinstance(messages, list):
        return names
    for message in messages:
        if not isinstance(message, dict):
            continue
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            name = str(function.get("name") or "")
            if name:
                names.append(name)
    return names


def _openai_tool_result_count(messages: Any) -> int:
    if not isinstance(messages, list):
        return 0
    return sum(1 for message in messages if isinstance(message, dict) and message.get("role") == "tool")


def _responses_item_counts(items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(items, list):
        return counts
    for item in items:
        item_type = str(item.get("type") or type(item).__name__) if isinstance(item, dict) else type(item).__name__
        counts[item_type] = counts.get(item_type, 0) + 1
    return counts


def log_tool_diagnostics(stage: str, payload: dict[str, Any]) -> None:
    if os.environ.get("MIX_PROXY_DEBUG_TOOLS") != "1":
        return
    parts = [f"tooldiag stage={stage}"]
    if "tools" in payload:
        names = _tool_names_from_anthropic(payload.get("tools")) or _tool_names_from_openai(payload.get("tools"))
        parts.append(f"tools={len(payload.get('tools') or [])}")
        parts.append(f"tool_names={names[:20]}")
    if payload.get("tool_choice") is not None:
        parts.append(f"tool_choice={payload.get('tool_choice')!r}")
    if "messages" in payload:
        parts.append(f"message_blocks={_content_block_type_counts(payload.get('messages'))}")
        parts.append(f"tool_calls={_openai_tool_call_names(payload.get('messages'))[:20]}")
        parts.append(f"tool_results={_openai_tool_result_count(payload.get('messages'))}")
    if "input" in payload:
        parts.append(f"responses_items={_responses_item_counts(payload.get('input'))}")
    debug_log(" ".join(parts))


def anthropic_to_openai(payload: dict[str, Any], model: str) -> dict[str, Any]:
    messages = []
    system = payload.get("system")
    if system:
        system_text = text_from_content(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        tool_messages = _anthropic_tool_results_to_openai_messages(message)
        if tool_messages:
            messages.extend(tool_messages)
            continue
        role = message.get("role") or "user"
        if role not in {"user", "assistant", "system"}:
            role = "user"
        content = message.get("content")
        out_message: dict[str, Any] = {"role": role, "content": _openai_content_from_anthropic(content)}
        tool_calls = _openai_tool_calls_from_anthropic(content)
        if tool_calls and role == "assistant":
            out_message["tool_calls"] = tool_calls
            if not out_message["content"]:
                out_message["content"] = None
        if out_message.get("content") or out_message.get("tool_calls"):
            messages.append(out_message)
    out: dict[str, Any] = {
        "model": model or payload.get("model"),
        "messages": messages or [{"role": "user", "content": ""}],
        "stream": bool(payload.get("stream", False)),
    }
    if "max_tokens" in payload:
        out["max_tokens"] = payload["max_tokens"]
    if "temperature" in payload:
        out["temperature"] = payload["temperature"]
    tools = _openai_tools_from_anthropic(payload.get("tools"))
    if tools:
        out["tools"] = tools
    if payload.get("tool_choice"):
        out["tool_choice"] = _openai_tool_choice_from_anthropic(payload.get("tool_choice"))
    return out


def _openai_tools_from_responses(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            out.append(tool)
            continue
        if tool.get("type") != "function":
            continue
        name = str(tool.get("name") or "")
        if not name:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _openai_tool_choice_from_responses(tool_choice: Any) -> Any:
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return tool_choice
    if tool_choice.get("type") == "function":
        return {"type": "function", "function": {"name": str(tool_choice.get("name") or "")}}
    return tool_choice


def responses_to_openai(payload: dict[str, Any], model: str) -> dict[str, Any]:
    messages = []
    instructions = payload.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": text_from_content(instructions)})
    input_value = payload.get("input")
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        for item in input_value:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            if role not in {"user", "assistant", "system"}:
                role = "user"
            if item_type == "function_call_output":
                call_id = str(item.get("call_id") or item.get("id") or "")
                if call_id:
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": text_from_content(item.get("output"))})
                else:
                    messages.append({"role": "user", "content": text_from_content(item.get("output"))})
                continue
            if item_type == "function_call":
                call_id = str(item.get("call_id") or item.get("id") or "")
                name = str(item.get("name") or "tool")
                arguments = item.get("arguments") or item.get("input") or "{}"
                if not isinstance(arguments, str):
                    arguments = _compact_json(arguments)
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}],
                })
                continue
            content = text_from_content(item.get("content") if "content" in item else item.get("text"))
            if content:
                messages.append({"role": role, "content": content})
    out: dict[str, Any] = {
        "model": model or payload.get("model"),
        "messages": messages or [{"role": "user", "content": ""}],
        "stream": bool(payload.get("stream", False)),
    }
    if "max_output_tokens" in payload:
        out["max_tokens"] = payload["max_output_tokens"]
    if "temperature" in payload:
        out["temperature"] = payload["temperature"]
    tools = _openai_tools_from_responses(payload.get("tools"))
    if tools:
        out["tools"] = tools
    if payload.get("tool_choice"):
        out["tool_choice"] = _openai_tool_choice_from_responses(payload.get("tool_choice"))
    return out


def openai_to_responses(payload: dict[str, Any], model: str) -> dict[str, Any]:
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    usage = payload.get("usage") or {}
    output: list[dict[str, Any]] = []
    output_text = content
    if content:
        output.append(
            {
                "id": "msg_mix",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        )
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        output.append(
            {
                "id": str(tool_call.get("id") or "fc_mix"),
                "type": "function_call",
                "status": "completed",
                "call_id": str(tool_call.get("id") or ""),
                "name": str(function.get("name") or "tool"),
                "arguments": str(function.get("arguments") or "{}"),
            }
        )
    if not output:
        output.append(
            {
                "id": "msg_mix",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            }
        )
    return {
        "id": payload.get("id", "resp_mix"),
        "object": "response",
        "created_at": payload.get("created") or int(time.time()),
        "status": "completed",
        "error": None,
        "model": model or payload.get("model", ""),
        "output": output,
        "output_text": output_text,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def openai_to_anthropic(payload: dict[str, Any], model: str) -> dict[str, Any]:
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    text = message.get("content") or ""
    if text:
        content.append({"type": "text", "text": text})
    content.extend(_anthropic_tool_blocks_from_openai(message))
    stop_reason = "tool_use" if _anthropic_tool_blocks_from_openai(message) else "end_turn"
    return {
        "id": payload.get("id", "msg_mix"),
        "type": "message",
        "role": "assistant",
        "model": model or payload.get("model", ""),
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": (payload.get("usage") or {}).get("prompt_tokens", 0),
            "output_tokens": (payload.get("usage") or {}).get("completion_tokens", 0),
        },
    }


ASCII_REPLACEMENTS = str.maketrans(
    {
        "→": "->",
        "←": "<-",
        "↑": "^",
        "↓": "v",
        "✓": "yes",
        "✗": "no",
        "—": "-",
        "–": "-",
        "‑": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "…": "...",
        "·": "-",
        "•": "-",
    }
)


def _ascii_safe(value: Any) -> Any:
    if isinstance(value, str):
        normalized = value.translate(ASCII_REPLACEMENTS)
        return normalized.encode("ascii", "ignore").decode("ascii")
    if isinstance(value, list):
        return [_ascii_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _ascii_safe(item) for key, item in value.items()}
    return value


def ascii_safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload)
    safe["messages"] = _ascii_safe(safe.get("messages") or [])
    if "system" in safe:
        safe["system"] = _ascii_safe(safe["system"])
    return safe


def strip_cache_control(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_cache_control(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [strip_cache_control(item) for item in value]
    return value


def _strip_cache_control(payload: dict[str, Any]) -> None:
    cleaned = strip_cache_control(payload)
    payload.clear()
    payload.update(cleaned)


def _set_nested_path(payload: dict[str, Any], path: str, value: Any) -> None:
    current: dict[str, Any] = payload
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def patch_domestic_anthropic_payload(payload: dict[str, Any], model: str) -> None:
    if not _is_domestic_model(model):
        return
    _strip_cache_control(payload)
    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        current_type = str(thinking.get("type") or "").strip().lower()
        if current_type not in {"enabled", "disabled"}:
            thinking["type"] = "enabled"
    elif thinking is not None:
        payload["thinking"] = {"type": "enabled"}


def non_ascii_summary(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    chars = sorted({char for char in text if ord(char) > 127})
    return "".join(chars[:40]) or "none"


def payload_size_summary(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    message_count = len(messages) if isinstance(messages, list) else 0
    message_bytes = [len(json.dumps(message, ensure_ascii=False).encode("utf-8")) for message in messages[:5]] if isinstance(messages, list) else []
    roles = [str(message.get("role")) for message in messages[:8] if isinstance(message, dict)] if isinstance(messages, list) else []
    content_chars = [len(str(message.get("content") or "")) for message in messages[:5] if isinstance(message, dict)] if isinstance(messages, list) else []
    return (
        f"bytes={len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))} "
        f"non_ascii={non_ascii_summary(payload)} keys={sorted(payload.keys())} "
        f"messages={message_count} roles={roles} content_chars={content_chars} "
        f"first_message_bytes={message_bytes} max_tokens={payload.get('max_tokens')}"
    )


def anthropic_payload_shape(payload: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for message in payload.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for item in content:
                item_type = str(item.get("type") or "unknown") if isinstance(item, dict) else type(item).__name__
                counts[item_type] = counts.get(item_type, 0) + 1
        else:
            item_type = type(content).__name__
            counts[item_type] = counts.get(item_type, 0) + 1
    return f"anthropic_blocks={counts} system_type={type(payload.get('system')).__name__}"


def is_ascii_codec_error(body: bytes) -> bool:
    text = body.decode("utf-8", errors="replace")
    return "ascii" in text and "codec can't encode character" in text


def forward_json(url: str, api_key: str, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "authorization": f"Bearer {api_key}",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except URLError as exc:
        body = json.dumps({"error": str(exc)}).encode("utf-8")
        return 502, {"content-type": "application/json"}, body


def forward_stream(url: str, api_key: str, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(payload).encode("utf-8")
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "authorization": f"Bearer {api_key}",
        "content-length": str(len(data)),
    }
    if extra_headers:
        headers.update(extra_headers)
    connection = None
    try:
        connection = _get_connection(parsed)
        connection.request("POST", path, body=data, headers=headers)
        response = connection.getresponse()
        headers_out = dict(response.getheaders())
        if response.status >= 400:
            body = response.read()
            connection.close()
            _invalidate_connection(parsed.netloc)
            return response.status, headers_out, body
        response._mix_connection = connection
        return response.status, headers_out, response
    except OSError as exc:
        if connection:
            connection.close()
            _invalidate_connection(parsed.netloc)
        body = json.dumps({"error": str(exc)}).encode("utf-8")
        return 502, {"content-type": "application/json"}, body


def _get_connection(parsed: Any) -> http.client.HTTPConnection:
    netloc = parsed.netloc
    now = time.time()
    with _connection_cache_lock:
        if netloc in _connection_cache:
            conn, last_used = _connection_cache[netloc]
            if now - last_used < 60:
                _connection_cache[netloc] = (conn, now)
                return conn
            _close_connection(conn)
            del _connection_cache[netloc]
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = connection_class(netloc, timeout=120)
    with _connection_cache_lock:
        _connection_cache[netloc] = (conn, now)
    return conn


def _close_connection(conn: Any) -> None:
    try:
        conn.close()
    except OSError:
        pass


def _invalidate_connection(netloc: str) -> None:
    with _connection_cache_lock:
        _connection_cache.pop(netloc, None)


def glm51_compat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe = ascii_safe_payload(payload)
    max_tokens = int(safe.get("max_tokens") or 0)
    if max_tokens > 4096:
        safe["max_tokens"] = 4096
    return safe


def glm51_no_max_tokens_payload(payload: dict[str, Any]) -> dict[str, Any]:
    retry = copy.deepcopy(payload)
    retry.pop("max_tokens", None)
    return retry


def _mostly_cjk(text: str) -> bool:
    cjk = sum(1 for char in text if "一" <= char <= "鿿")
    letters = sum(1 for char in text if char.isalpha())
    return cjk > 0 and cjk >= letters * 0.2


def _language_instruction(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and _mostly_cjk(message.get("content") or ""):
            return "请使用中文回答，除非用户明确要求使用其他语言。"
    return ""


def glm51_user_only_payload(payload: dict[str, Any]) -> dict[str, Any]:
    retry = glm51_no_max_tokens_payload(payload)
    system_parts = []
    user_messages = []
    for message in retry.get("messages") or []:
        role = message.get("role") if isinstance(message, dict) else "user"
        content = str(message.get("content") or "") if isinstance(message, dict) else str(message or "")
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            user_messages.append({"role": "assistant", "content": content})
        else:
            user_messages.append({"role": "user", "content": content})
    language_instruction = _language_instruction(user_messages)
    if system_parts and user_messages:
        prefix = language_instruction + "\n\n" if language_instruction else ""
        for message in reversed(user_messages):
            if message.get("role") == "user":
                message["content"] = message["content"] + "\n\nSystem context:\n" + prefix + "\n\n".join(system_parts)
                break
    elif system_parts:
        prefix = language_instruction + "\n\n" if language_instruction else ""
        user_messages.append({"role": "user", "content": prefix + "\n\n".join(system_parts)})
    retry["messages"] = user_messages or [{"role": "user", "content": ""}]
    return retry


def should_glm51_compat_retry(model: str, status: int, body: bytes) -> bool:
    if model != "glm-5.1" or status < 400:
        return False
    text = body.decode("utf-8", errors="replace")
    return "[400:" in text or "api_error" in text or is_ascii_codec_error(body)


def should_messages_direct_fallback(status: int, body: bytes) -> bool:
    if status in {404, 405, 410, 415, 422, 501}:
        return True
    if status != 400:
        return False
    text = body.decode("utf-8", errors="replace").lower()
    markers = (
        "unsupported",
        "not found",
        "invalid endpoint",
        "messages array is required",
        "field messages is required",
        "unknown path",
        "route not found",
        "unknown route",
        "no route matched",
    )
    return any(marker in text for marker in markers)


def openai_stream_text(body: bytes) -> tuple[str, str]:
    message_id = "msg_mix"
    parts = []
    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        message_id = str(chunk.get("id") or message_id)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        if text:
            parts.append(str(text))
    return message_id, "".join(parts)


def openai_stream_to_message(body: bytes, model: str) -> dict[str, Any]:
    message_id, text = openai_stream_text(body)
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def openai_stream_to_responses_sse(body: bytes, model: str) -> bytes:
    response_id = "resp_mix"
    parts = []
    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        response_id = str(chunk.get("id") or response_id)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        if text:
            parts.append(str(text))

    item_id = "msg_mix"
    text = "".join(parts)
    response_base = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    message_item = {
        "id": item_id,
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    content_part = {"type": "output_text", "text": "", "annotations": []}
    events = [
        {"type": "response.created", "response": response_base},
        {"type": "response.in_progress", "response": response_base},
        {"type": "response.output_item.added", "response_id": response_id, "output_index": 0, "item": message_item},
        {"type": "response.content_part.added", "response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "part": content_part},
    ]
    for part in parts:
        events.append({"type": "response.output_text.delta", "response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": part})
    done_part = {"type": "output_text", "text": text, "annotations": []}
    done_item = dict(message_item)
    done_item["status"] = "completed"
    done_item["content"] = [done_part]
    completed_response = dict(response_base)
    completed_response["status"] = "completed"
    completed_response["output"] = [done_item]
    completed_response["output_text"] = text
    completed_response["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    events.extend(
        [
            {"type": "response.output_text.done", "response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "text": text},
            {"type": "response.content_part.done", "response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "part": done_part},
            {"type": "response.output_item.done", "response_id": response_id, "output_index": 0, "item": done_item},
            {"type": "response.completed", "response": completed_response},
        ]
    )
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}")
        lines.append("data: " + json.dumps(event, ensure_ascii=False))
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def openai_stream_to_anthropic(body: bytes, model: str) -> bytes:
    message_id = "msg_mix"
    output = [
        {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ]
    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        message_id = str(chunk.get("id") or message_id)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        if text:
            output.append({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
    output.extend(
        [
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}},
            {"type": "message_stop"},
        ]
    )
    lines = []
    for event in output:
        lines.append(f"event: {event['type']}")
        lines.append("data: " + json.dumps(event, ensure_ascii=False))
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _sse_json_bytes(event: dict[str, Any], *, event_name: str | None = None) -> bytes:
    lines = []
    if event_name:
        lines.append(f"event: {event_name}")
    lines.append("data: " + json.dumps(event, ensure_ascii=False))
    lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def iter_openai_stream_chunks(response: Any) -> Any:
    while True:
        line = response.readline()
        if not line:
            break
        stripped = line.strip()
        if not stripped.startswith(b"data:"):
            continue
        data = stripped[len(b"data:") :].strip()
        if not data or data == b"[DONE]":
            break
        try:
            yield json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            continue


def _stream_tool_delta_index(tool_call: dict[str, Any]) -> int:
    try:
        return int(tool_call.get("index") or 0)
    except (TypeError, ValueError):
        return 0


def iter_openai_stream_to_anthropic(response: Any, model: str) -> Any:
    message_id = "msg_mix"
    content_index = 0
    started = False
    tool_indexes: dict[int, int] = {}
    yield _sse_json_bytes(
        {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}},
        event_name="message_start",
    )
    for chunk in iter_openai_stream_chunks(response):
        message_id = str(chunk.get("id") or message_id)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        if text:
            if not started:
                yield _sse_json_bytes({"type": "content_block_start", "index": content_index, "content_block": {"type": "text", "text": ""}}, event_name="content_block_start")
                started = True
            yield _sse_json_bytes({"type": "content_block_delta", "index": content_index, "delta": {"type": "text_delta", "text": str(text)}}, event_name="content_block_delta")
        for tool_call in delta.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            delta_index = _stream_tool_delta_index(tool_call)
            if delta_index not in tool_indexes:
                if started:
                    yield _sse_json_bytes({"type": "content_block_stop", "index": content_index}, event_name="content_block_stop")
                    content_index += 1
                    started = False
                tool_indexes[delta_index] = content_index
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                yield _sse_json_bytes(
                    {"type": "content_block_start", "index": content_index, "content_block": {"type": "tool_use", "id": str(tool_call.get("id") or ""), "name": str(function.get("name") or "tool"), "input": {}}},
                    event_name="content_block_start",
                )
                content_index += 1
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            arguments = function.get("arguments")
            if arguments:
                yield _sse_json_bytes({"type": "content_block_delta", "index": tool_indexes[delta_index], "delta": {"type": "input_json_delta", "partial_json": str(arguments)}}, event_name="content_block_delta")
    if started:
        yield _sse_json_bytes({"type": "content_block_stop", "index": content_index}, event_name="content_block_stop")
    for index in tool_indexes.values():
        yield _sse_json_bytes({"type": "content_block_stop", "index": index}, event_name="content_block_stop")
    stop_reason = "tool_use" if tool_indexes else "end_turn"
    yield _sse_json_bytes({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 0}}, event_name="message_delta")
    yield _sse_json_bytes({"type": "message_stop"}, event_name="message_stop")


def iter_openai_stream_to_responses_sse(response: Any, model: str) -> Any:
    response_id = "resp_mix"
    item_id = "msg_mix"
    response_base = {"id": response_id, "object": "response", "created_at": int(time.time()), "status": "in_progress", "model": model, "output": [], "parallel_tool_calls": False, "tool_choice": "auto", "tools": []}
    yield _sse_json_bytes({"type": "response.created", "response": response_base}, event_name="response.created")
    yield _sse_json_bytes({"type": "response.in_progress", "response": response_base}, event_name="response.in_progress")
    yield _sse_json_bytes({"type": "response.output_item.added", "response_id": response_id, "output_index": 0, "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}}, event_name="response.output_item.added")
    yield _sse_json_bytes({"type": "response.content_part.added", "response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}, event_name="response.content_part.added")
    parts: list[str] = []
    output_items: list[dict[str, Any]] = []
    tool_indexes: dict[int, int] = {}
    tool_state: dict[int, dict[str, str]] = {}
    output_index = 1
    for chunk in iter_openai_stream_chunks(response):
        response_id = str(chunk.get("id") or response_id)
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        if text:
            parts.append(str(text))
            yield _sse_json_bytes({"type": "response.output_text.delta", "response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "delta": str(text)}, event_name="response.output_text.delta")
        for tool_call in delta.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            delta_index = _stream_tool_delta_index(tool_call)
            function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            if delta_index not in tool_indexes:
                tool_indexes[delta_index] = output_index
                call_id = str(tool_call.get("id") or f"call_{delta_index}")
                name = str(function.get("name") or "tool")
                tool_state[delta_index] = {"call_id": call_id, "name": name, "arguments": ""}
                fc_item_id = f"fc_{delta_index}"
                yield _sse_json_bytes(
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": output_index,
                        "item": {"id": fc_item_id, "type": "function_call", "status": "in_progress", "call_id": call_id, "name": name, "arguments": ""},
                    },
                    event_name="response.output_item.added",
                )
                output_index += 1
            arguments = function.get("arguments")
            if arguments:
                tool_state[delta_index]["arguments"] += str(arguments)
                yield _sse_json_bytes(
                    {
                        "type": "response.function_call_arguments.delta",
                        "response_id": response_id,
                        "output_index": tool_indexes[delta_index],
                        "item_id": f"fc_{delta_index}",
                        "delta": str(arguments),
                    },
                    event_name="response.function_call_arguments.delta",
                )
    text = "".join(parts)
    done_part = {"type": "output_text", "text": text, "annotations": []}
    done_item = {"id": item_id, "type": "message", "status": "completed", "role": "assistant", "content": [done_part]}
    output_items = [done_item]
    for delta_index in sorted(tool_indexes):
        state = tool_state[delta_index]
        fc_item = {"id": f"fc_{delta_index}", "type": "function_call", "status": "completed", "call_id": state["call_id"], "name": state["name"], "arguments": state["arguments"]}
        output_items.append(fc_item)
        yield _sse_json_bytes(
            {"type": "response.output_item.done", "response_id": response_id, "output_index": tool_indexes[delta_index], "item": fc_item},
            event_name="response.output_item.done",
        )
    completed_response = dict(response_base)
    completed_response.update({"id": response_id, "status": "completed", "output": output_items, "output_text": text, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}})
    yield _sse_json_bytes({"type": "response.output_text.done", "response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "text": text}, event_name="response.output_text.done")
    yield _sse_json_bytes({"type": "response.content_part.done", "response_id": response_id, "item_id": item_id, "output_index": 0, "content_index": 0, "part": done_part}, event_name="response.content_part.done")
    yield _sse_json_bytes({"type": "response.output_item.done", "response_id": response_id, "output_index": 0, "item": done_item}, event_name="response.output_item.done")
    yield _sse_json_bytes({"type": "response.completed", "response": completed_response}, event_name="response.completed")
    yield b"data: [DONE]\n\n"


def _is_domestic_model(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized.startswith(DOMESTIC_MODEL_PREFIXES)


def anthropic_extra_headers(handler: BaseHTTPRequestHandler, api_key: str, model: str = "") -> dict[str, str]:
    headers = {"x-api-key": api_key, "anthropic-version": handler.headers.get("anthropic-version", "2023-06-01")}
    for key in ("User-Agent", "x-app", "anthropic-dangerous-direct-browser-access"):
        value = handler.headers.get(key)
        if value:
            headers[key] = value
    for key, value in handler.headers.items():
        if key.lower().startswith("x-stainless-") and value:
            headers[key] = value
    beta = handler.headers.get("anthropic-beta")
    if beta and not _is_domestic_model(model):
        headers["anthropic-beta"] = beta
    debug_log(
        "anthropic-extra-headers "
        f"beta={bool(headers.get('anthropic-beta'))} "
        f"beta_stripped={bool(beta and 'anthropic-beta' not in headers)} "
        f"direct_browser={bool(headers.get('anthropic-dangerous-direct-browser-access'))} "
        f"ua={bool(headers.get('User-Agent'))} x_app={bool(headers.get('x-app'))}"
    )
    return headers


def _upstream_path(base_path: str, request_path: str) -> str:
    query = urlsplit(request_path).query
    return base_path + (f"?{query}" if query else "")


def direct_response_summary(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    if not text:
        return "empty"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        events = []
        for line in text.splitlines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                if payload and payload != "[DONE]":
                    try:
                        item = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
                    if item_type:
                        events.append(item_type)
        if events:
            counts: dict[str, int] = {}
            for event in events:
                counts[event] = counts.get(event, 0) + 1
            return f"events={counts}"
        return f"text_prefix={text[:120]!r}"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return f"error_type={error.get('type')} error_message={str(error.get('message') or '')[:180]!r}"
    if isinstance(data, dict):
        return f"json_keys={sorted(data.keys())} type={data.get('type')} stop_reason={data.get('stop_reason')}"
    return f"json_type={type(data).__name__}"


def has_web_search_tool(payload: dict[str, Any]) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    return any(isinstance(tool, dict) and str(tool.get("name") or tool.get("type") or "") == "web_search" for tool in tools)


def prepare_web_search_payload(payload: dict[str, Any]) -> bool:
    if not has_web_search_tool(payload):
        return False
    payload.pop("tool_choice", None)
    return True


def web_search_retry_payloads(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if not has_web_search_tool(payload):
        return []
    no_tool_choice = copy.deepcopy(payload)
    no_tool_choice.pop("tool_choice", None)
    without_tools = copy.deepcopy(no_tool_choice)
    without_tools.pop("tools", None)
    return [("web-search-no-tool-choice", no_tool_choice), ("web-search-no-tools", without_tools)]


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "MixProxy/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("MIX_PROXY_DEBUG"):
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        request_path = urlsplit(self.path).path
        if request_path == "/health":
            self.send_json(200, {"ok": True})
            return
        self.send_json(404, {"error": "not found"})

    def _authorized(self) -> bool:
        token = os.environ.get("MIX_PROXY_TOKEN", "")
        if not token:
            return True
        auth = self.headers.get("authorization", "")
        x_api_key = self.headers.get("x-api-key", "")
        return auth == f"Bearer {token}" or x_api_key == token

    def _read_payload(self) -> dict[str, Any] | None:
        raw_length = self.headers.get("content-length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            self.send_json(400, {"error": "invalid content-length"})
            return None
        if length < 0:
            self.send_json(400, {"error": "invalid content-length"})
            return None
        if length > MAX_BODY_SIZE:
            self.send_json(413, {"error": "request body too large"})
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self.send_json(400, {"error": f"invalid json: {exc}"})
            return None

    def do_POST(self) -> None:
        if not self._authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        payload = self._read_payload()
        if payload is None:
            return

        request_url = urlsplit(self.path)
        request_path = request_url.path
        provider_id = os.environ.get("MIX_PROVIDER", "")
        model = os.environ.get("MIX_MODEL", "") or payload.get("model", "")
        debug_log(f"request path={self.path} request_path={request_path} provider={provider_id} model={model} payload_model={payload.get('model')} stream={payload.get('stream')}")
        log_tool_diagnostics(f"incoming:{request_path}", payload)
        try:
            provider = provider_by_id(self.server.config, provider_id)  # type: ignore[attr-defined]
        except KeyError:
            self.send_json(400, {"error": f"unknown provider: {provider_id}"})
            return

        provider_type = str(provider.get("type") or "").lower()
        try:
            allow_insecure = bool(provider.get("allow_insecure_base_url"))
            openai_base_url = validate_base_url(str(provider.get("openai_base_url") or provider.get("base_url") or ""), allow_insecure=allow_insecure)
            anthropic_base_url = validate_base_url(str(provider.get("anthropic_base_url") or provider.get("base_url") or ""), allow_insecure=allow_insecure)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        api_key_env = str(provider.get("api_key_env") or "")
        try:
            api_key = provider_api_key(provider)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        if not api_key:
            self.send_json(401, {"error": f"missing api_key or ${api_key_env}"})
            return

        if provider_type == "anthropic":
            if request_path in {"/v1/messages", "/messages"}:
                payload["model"] = model or payload.get("model")
                status, _headers, body = forward_json(
                    f"{anthropic_base_url}{_upstream_path('/v1/messages', self.path)}",
                    api_key,
                    payload,
                    anthropic_extra_headers(self, api_key, model),
                )
                self.send_raw(status, body)
                return
            self.send_json(400, {"error": "anthropic provider only supports /v1/messages"})
            return

        if provider_type != "openai":
            self.send_json(400, {"error": f"unsupported provider type: {provider_type}"})
            return

        if request_path in {"/v1/messages", "/messages"}:
            payload["model"] = model or payload.get("model")
            if anthropic_base_url:
                # Direct Anthropic passthrough: client and upstream both speak
                # the Anthropic messages protocol, so no conversion is needed.
                # The raw upstream response (including SSE for streaming) is
                # forwarded as-is.
                direct_payload = json.loads(json.dumps(payload))
                if model and not str(model).startswith("claude-"):
                    patch_domestic_anthropic_payload(direct_payload, model)
                if prepare_web_search_payload(direct_payload):
                    debug_log(f"anthropic-direct-web-search-prepared removed_tool_choice=True path={request_path}")
                debug_log(f"anthropic-direct path={request_path} url={anthropic_base_url}/v1/messages model={direct_payload.get('model')} stream={direct_payload.get('stream')}")
                status, _headers, body = forward_json(
                    f"{anthropic_base_url}{_upstream_path('/v1/messages', self.path)}",
                    api_key,
                    direct_payload,
                    anthropic_extra_headers(self, api_key, model),
                )
                debug_log(f"anthropic-direct {response_log(status, request_path, body)} summary={direct_response_summary(body)}")
                if status >= 400 and has_web_search_tool(direct_payload):
                    for retry_reason, retry_payload in web_search_retry_payloads(direct_payload):
                        debug_log(f"anthropic-direct-retry reason={retry_reason} path={request_path}")
                        status, _headers, body = forward_json(
                            f"{anthropic_base_url}{_upstream_path('/v1/messages', self.path)}",
                            api_key,
                            retry_payload,
                            anthropic_extra_headers(self, api_key, model),
                        )
                        debug_log(f"anthropic-direct-retry {retry_reason} {response_log(status, request_path, body)} summary={direct_response_summary(body)}")
                        if status < 400:
                            break
                if status < 400 or not should_messages_direct_fallback(status, body):
                    content_type = _headers.get("content-type") or _headers.get("Content-Type") or "application/json"
                    self.send_raw(status, body, content_type)
                    return
                debug_log(f"anthropic-direct-fallback-to-chat status={status} path={request_path}")

            debug_log(f"anthropic-shape path={request_path} {anthropic_payload_shape(payload)}")
            upstream_payload = anthropic_to_openai(payload, model)
            log_tool_diagnostics("anthropic_to_openai", upstream_payload)
            if model == "glm-5.1":
                upstream_payload = glm51_compat_payload(upstream_payload)
            if upstream_payload.get("stream"):
                status, _headers, stream_or_body = forward_stream(f"{openai_base_url}/chat/completions", api_key, upstream_payload)
                if status >= 400 or isinstance(stream_or_body, bytes):
                    body = stream_or_body if isinstance(stream_or_body, bytes) else stream_or_body.read()
                    debug_log(f"upstream {response_log(status, request_path, body, stream=True)}")
                    self.send_raw(status, body)
                    return
                debug_log(f"upstream-stream status={status} path={request_path}")
                self.send_sse_iter(200, iter_openai_stream_to_anthropic(stream_or_body, model), stream_or_body)
                return
            status, _headers, body = forward_json(f"{openai_base_url}/chat/completions", api_key, upstream_payload)
            debug_log(f"upstream {response_log(status, request_path, body, stream=upstream_payload.get('stream'))}")
            if status >= 500 and is_ascii_codec_error(body):
                retry_payload = ascii_safe_payload(upstream_payload)
                debug_log(f"retry-ascii-safe payload-summary path={request_path} {payload_size_summary(retry_payload)}")
                status, _headers, body = forward_json(f"{openai_base_url}/chat/completions", api_key, retry_payload)
                debug_log(f"retry-ascii-safe {response_log(status, request_path, body, stream=retry_payload.get('stream'))}")
            if should_glm51_compat_retry(model, status, body):
                retry_payload = glm51_no_max_tokens_payload(upstream_payload)
                debug_log(f"retry-glm51-no-max payload-summary path={request_path} {payload_size_summary(retry_payload)}")
                status, _headers, body = forward_json(f"{openai_base_url}/chat/completions", api_key, retry_payload)
                debug_log(f"retry-glm51-no-max {response_log(status, request_path, body, stream=retry_payload.get('stream'))}")
            if should_glm51_compat_retry(model, status, body):
                retry_payload = glm51_user_only_payload(upstream_payload)
                debug_log(f"retry-glm51-user-only payload-summary path={request_path} {payload_size_summary(retry_payload)}")
                status, _headers, body = forward_json(f"{openai_base_url}/chat/completions", api_key, retry_payload)
                debug_log(f"retry-glm51-user-only {response_log(status, request_path, body, stream=retry_payload.get('stream'))}")
            if status >= 400:
                try:
                    error_json = json.loads(body)
                except json.JSONDecodeError:
                    error_json = {}
                error_text = json.dumps(error_json, ensure_ascii=False)
                if not upstream_payload.get("stream") and "Stream must be set to true" in error_text:
                    retry_payload = dict(upstream_payload)
                    retry_payload["stream"] = True
                    status, _headers, body = forward_json(f"{openai_base_url}/chat/completions", api_key, retry_payload)
                    debug_log(f"retry-stream {response_log(status, request_path, body)}")
                    if status < 400:
                        self.send_json(200, openai_stream_to_message(body, model))
                        return
                self.send_raw(status, body)
                return
            if upstream_payload.get("stream"):
                self.send_sse(200, openai_stream_to_anthropic(body, model))
                return
            try:
                upstream_json = json.loads(body)
            except json.JSONDecodeError:
                self.send_raw(502, body)
                return
            log_tool_diagnostics("openai_response_for_anthropic", {"messages": [upstream_json.get("choices", [{}])[0].get("message") or {}]})
            self.send_json(200, openai_to_anthropic(upstream_json, model))
            return

        if request_path in {"/v1/responses", "/responses"}:
            upstream_payload = responses_to_openai(payload, model)
            log_tool_diagnostics("responses_to_openai", upstream_payload)
            if model == "glm-5.1":
                upstream_payload = glm51_compat_payload(upstream_payload)
            debug_log(f"payload-summary path={request_path} {payload_size_summary(upstream_payload)}")
            if upstream_payload.get("stream"):
                status, _headers, stream_or_body = forward_stream(f"{openai_base_url}/chat/completions", api_key, upstream_payload)
                if status >= 400 or isinstance(stream_or_body, bytes):
                    body = stream_or_body if isinstance(stream_or_body, bytes) else stream_or_body.read()
                    debug_log(f"upstream {response_log(status, request_path, body, stream=True)}")
                    self.send_raw(status, body)
                    return
                debug_log(f"upstream-stream status={status} path={request_path}")
                self.send_sse_iter(200, iter_openai_stream_to_responses_sse(stream_or_body, model), stream_or_body)
                return
            status, _headers, body = forward_json(f"{openai_base_url}/chat/completions", api_key, upstream_payload)
            debug_log(f"upstream {response_log(status, request_path, body, stream=upstream_payload.get('stream'))}")
            if should_glm51_compat_retry(model, status, body):
                retry_payload = dict(upstream_payload)
                retry_payload["max_tokens"] = 1024
                debug_log(f"retry-glm51-compact payload-summary path={request_path} {payload_size_summary(retry_payload)}")
                status, _headers, body = forward_json(f"{openai_base_url}/chat/completions", api_key, retry_payload)
                debug_log(f"retry-glm51-compact {response_log(status, request_path, body, stream=retry_payload.get('stream'))}")
            if status >= 400:
                self.send_raw(status, body)
                return
            if upstream_payload.get("stream"):
                response_body = openai_stream_to_responses_sse(body, model)
                debug_log(f"responses-sse-bytes path={request_path} bytes={len(response_body)}")
                self.send_sse(200, response_body)
                return
            try:
                upstream_json = json.loads(body)
            except json.JSONDecodeError:
                self.send_raw(502, body)
                return
            log_tool_diagnostics("openai_response_for_responses", {"messages": [upstream_json.get("choices", [{}])[0].get("message") or {}]})
            self.send_json(200, openai_to_responses(upstream_json, model))
            return

        if request_path in {"/v1/chat/completions", "/chat/completions"}:
            payload["model"] = model or payload.get("model")
            status, _headers, body = forward_json(f"{openai_base_url}/chat/completions", api_key, payload)
            self.send_raw(status, body)
            return

        self.send_json(404, {"error": "not found"})

    def send_raw(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_sse(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_sse_iter(self, status: int, chunks: Any, upstream_response: Any = None) -> None:
        self.send_response(status)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        try:
            for chunk in chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            if upstream_response is not None:
                connection = getattr(upstream_response, "_mix_connection", None)
                if connection is not None:
                    try:
                        upstream_response.close()
                    except OSError:
                        pass
                    _close_connection(connection)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_raw(status, body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mix local proxy")
    parser.add_argument("--host", default=os.environ.get("MIX_PROXY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MIX_PROXY_PORT", "8765")))
    parser.add_argument("--allow-remote", action="store_true", help="Allow binding to a non-loopback host")
    parser.add_argument("--check-upstream", action="store_true", help="Check upstream health at startup")
    args = parser.parse_args(argv or sys.argv[1:])

    if args.host not in LOOPBACK_HOSTS and not args.allow_remote:
        raise SystemExit("Refusing non-loopback proxy host without --allow-remote")

    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    server.config = load_config()  # type: ignore[attr-defined]

    if args.check_upstream:
        _check_upstream_health(server.config)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"Mix proxy listening on http://{args.host}:{args.port}", flush=True)

    _shutdown_event.wait()
    server.shutdown()
    server_thread.join(timeout=30)
    server.server_close()
    return 0


def _check_upstream_health(config: dict[str, Any]) -> None:
    providers = config.get("providers") or []
    if not providers:
        return
    provider = providers[0]
    api_key = provider_api_key(provider)
    base_url = str(provider.get("base_url") or "").strip().rstrip("/")
    if not base_url or not api_key:
        return
    try:
        req = Request(f"{base_url}/models", headers={"authorization": f"Bearer {api_key}"}, method="GET")
        with urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print(f"Upstream health check passed: {base_url}", flush=True)
            else:
                print(f"Upstream health check: status {resp.status}", flush=True)
    except Exception as exc:
        print(f"Upstream health check failed: {exc}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
