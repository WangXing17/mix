#!/usr/bin/env python3
"""Minimal model/provider/CLI launcher."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import Request, urlopen
from mix_config import APP_NAME, DEFAULT_CONFIG, load_config_file

__version__ = "0.1.0"
USER_HOME = Path.home()
APP_ROOT = Path(os.environ.get("MIX_HOME", USER_HOME / ".config" / APP_NAME))
CONFIG_PATH = Path(os.environ.get("MIX_CONFIG", APP_ROOT / "config.json"))
MODELS_CACHE_PATH = Path(os.environ.get("MIX_MODELS_CACHE", APP_ROOT / "models-cache.json"))
MODELS_CACHE_MAX_PROVIDERS = 20
MODELS_CACHE_MAX_MODELS_PER_PROVIDER = 200
MODELS_CACHE_MAX_AGE_DAYS = 30
SESSIONS_ROOT = Path(os.environ.get("MIX_SESSIONS", APP_ROOT / "sessions"))
TRUST_STORE_PATH = Path(os.environ.get("MIX_TRUST_STORE", APP_ROOT / "trusted-projects.json"))

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class MixError(Exception):
    pass


def validate_base_url(base_url: str, *, allow_insecure: bool = False) -> str:
    cleaned = base_url.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme == "https":
        return cleaned
    if parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS:
        return cleaned
    if allow_insecure:
        return cleaned
    raise MixError(f"Insecure provider URL blocked: {cleaned}")


SENSITIVE_KEY_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GLM_API_KEY",
    "DASHSCOPE_API_KEY",
    "MMS_API_KEY",
}


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _chmod_private_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _sanitized_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in SENSITIVE_KEY_ENV_NAMES:
        env.pop(key, None)
    return env


def _proxy_env(provider: Choice, model: Choice, port: int, token: str) -> dict[str, str]:
    env = _sanitized_env()
    api_key_env = str(provider.raw.get("api_key_env") or "").strip()
    api_key = provider_api_key(provider)
    if api_key_env and api_key:
        env[api_key_env] = api_key
    env["MIX_CONFIG"] = str(CONFIG_PATH)
    env["MIX_PROVIDER"] = provider.id
    env["MIX_MODEL"] = model.id
    env["MIX_PROXY_PORT"] = str(port)
    env["MIX_PROXY_TOKEN"] = token
    env["MIX_PROXY_LOG"] = str(SESSIONS_ROOT / "proxy.log")
    env.setdefault("MIX_PROXY_DEBUG_TOOLS", "1")
    return env




@dataclass(frozen=True)
class Choice:
    id: str
    name: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Session:
    id: str
    path: Path
    metadata: dict[str, Any]


def load_config() -> dict[str, Any]:
    try:
        return load_config_file(CONFIG_PATH)
    except json.JSONDecodeError as exc:
        raise MixError(f"Config parse failed: {CONFIG_PATH}: {exc}") from exc


def normalize_choices(items: list[Any], kind: str) -> list[Choice]:
    choices: list[Choice] = []
    for item in items:
        if isinstance(item, str):
            item = {"id": item, "name": item}
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("command") or "").strip()
        if not item_id:
            continue
        name = str(item.get("name") or item_id).strip()
        choices.append(Choice(item_id, name, item))
    if not choices:
        raise MixError(f"No {kind} configured")
    return choices


def _config_permissions_private() -> bool:
    try:
        mode = CONFIG_PATH.stat().st_mode
    except OSError:
        return False
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def provider_api_key(provider: Choice) -> str:
    explicit = str(provider.raw.get("api_key") or "").strip()
    if explicit:
        if not _config_permissions_private():
            raise MixError(f"Refusing plaintext api_key unless config is private: chmod 600 {CONFIG_PATH}")
        print(f"Warning: plaintext api_key in config is deprecated; use api_key_env for provider {provider.id}", file=sys.stderr)
        return explicit
    api_key_env = str(provider.raw.get("api_key_env") or "").strip()
    return os.environ.get(api_key_env, "") if api_key_env else ""


def provider_api_key_status(provider: Choice) -> str:
    if str(provider.raw.get("api_key") or "").strip():
        return "plaintext api_key deprecated"
    api_key_env = str(provider.raw.get("api_key_env") or "").strip()
    return "key ready" if api_key_env and os.environ.get(api_key_env) else f"missing ${api_key_env}"


def provider_static_models(provider: Choice) -> list[Choice]:
    return normalize_choices(list(provider.raw.get("models") or []), "model")


def _read_models_cache() -> dict[str, Any]:
    try:
        return json.loads(MODELS_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_models_cache(cache: dict[str, Any]) -> None:
    now = time.time()
    max_age = MODELS_CACHE_MAX_AGE_DAYS * 24 * 3600
    stale = [k for k, v in cache.items() if isinstance(v, dict) and now - float(v.get("fetched_at", 0)) > max_age]
    for k in stale:
        del cache[k]
    if len(cache) > MODELS_CACHE_MAX_PROVIDERS:
        by_age = sorted(cache.items(), key=lambda kv: float(kv[1].get("fetched_at", 0)) if isinstance(kv[1], dict) else 0)
        for k, _ in by_age[: len(cache) - MODELS_CACHE_MAX_PROVIDERS]:
            del cache[k]
    for v in cache.values():
        if isinstance(v, dict) and len(v.get("models") or []) > MODELS_CACHE_MAX_MODELS_PER_PROVIDER:
            v["models"] = v["models"][:MODELS_CACHE_MAX_MODELS_PER_PROVIDER]
    MODELS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(MODELS_CACHE_PATH.parent)
    MODELS_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(MODELS_CACHE_PATH)


def _provider_openai_base_url(provider: Choice) -> str:
    explicit = str(provider.raw.get("openai_base_url") or "").strip().rstrip("/")
    allow_insecure = bool(provider.raw.get("allow_insecure_base_url"))
    if explicit:
        return validate_base_url(explicit, allow_insecure=allow_insecure)
    return validate_base_url(str(provider.raw.get("base_url") or "").strip().rstrip("/"), allow_insecure=allow_insecure)


def fetch_provider_models(provider: Choice, *, force: bool = False) -> list[Choice]:
    endpoint = str(provider.raw.get("models_endpoint") or "/models").strip()
    if endpoint.lower() == "manual":
        return provider_static_models(provider)
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    cache = _read_models_cache()
    cached = cache.get(provider.id)
    if cached and not force and time.time() - float(cached.get("fetched_at", 0)) < 24 * 3600:
        models = cached.get("models") or []
        if models:
            return normalize_choices(models, "model")

    api_key = provider_api_key(provider)
    base_url = _provider_openai_base_url(provider)
    if not api_key or not base_url:
        return provider_static_models(provider)

    request = Request(
        f"{base_url}{endpoint}",
        headers={"authorization": f"Bearer {api_key}", "accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return normalize_choices(cached.get("models") or [], "model") if cached and cached.get("models") else provider_static_models(provider)

    models = []
    for item in data.get("data") or []:
        if isinstance(item, dict) and item.get("id"):
            models.append({"id": str(item["id"]), "name": str(item.get("id"))})
    models.sort(key=lambda item: item["id"].lower())
    if models:
        if len(models) > MODELS_CACHE_MAX_MODELS_PER_PROVIDER:
            models = models[:MODELS_CACHE_MAX_MODELS_PER_PROVIDER]
        cache[provider.id] = {"fetched_at": time.time(), "models": models}
        _write_models_cache(cache)
        return normalize_choices(models, "model")
    return provider_static_models(provider)


def compatible_providers(_cli: Choice, providers: list[Choice]) -> list[Choice]:
    return providers


def find_choice(choices: list[Choice], value: str | None, kind: str) -> Choice:
    if value:
        needle = value.strip().lower()
        for choice in choices:
            if choice.id.lower() == needle or choice.name.lower() == needle:
                return choice
        raise MixError(f"Unknown {kind}: {value}")
    return prompt_choice(choices, kind)


def prompt_choice(choices: list[Choice], kind: str) -> Choice:
    print(f"Select {kind}:")
    for index, choice in enumerate(choices, 1):
        suffix = ""
        if kind == "cli":
            suffix = " ✓" if shutil.which(str(choice.raw.get("command") or choice.id)) else " missing"
        if kind == "provider":
            suffix = " ✓" if provider_api_key(choice) else f" {provider_api_key_status(choice)}"
        print(f"  {index}. {choice.name} ({choice.id}){suffix}")
    while True:
        answer = input("> ").strip()
        if not answer:
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        for choice in choices:
            if answer.lower() in {choice.id.lower(), choice.name.lower()}:
                return choice
        print("Invalid selection")


def _cli_status(choice: Choice) -> str:
    command = str(choice.raw.get("command") or choice.id)
    return "ready" if shutil.which(command) else "missing"


def _provider_status(choice: Choice) -> str:
    return provider_api_key_status(choice)


def run_tui(clis: list[Choice], providers: list[Choice]) -> tuple[Choice, Choice, Choice] | None:
    try:
        import curses
    except ImportError as exc:
        raise MixError("TUI is unavailable on this platform; use --no-tui") from exc

    def _safe_addstr(stdscr: Any, y: int, x: int, text: str, attr: int = 0, max_width: int = 999) -> None:
        try:
            stdscr.addstr(y, x, text[:max_width], attr)
        except curses.error:
            pass

    def _draw_pane(
        stdscr: Any,
        title: str,
        items: list[Choice],
        selected: int,
        active: bool,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        show_status: str = "",
        filter_text: str = "",
    ) -> None:
        inner_w = max(1, width - 2)
        border_attr = curses.A_BOLD if active else curses.A_DIM

        label = f" {title} "
        pad_len = max(0, inner_w - len(label))
        top = "┌" + label + "─" * pad_len + "┐"
        top = top[:width]
        _safe_addstr(stdscr, y, x, top, border_attr, width)

        visible = max(1, height - 2)
        total = len(items)
        start = max(0, min(selected - visible + 1, total - visible))
        for row in range(visible):
            y_row = y + row + 1
            item_idx = start + row
            if item_idx < total:
                item = items[item_idx]
                marker = ">" if item_idx == selected else " "
                suffix = ""
                status_color = 0
                if title == "CLI":
                    st = _cli_status(item)
                    suffix = f" [{st}]"
                    status_color = curses.color_pair(4) if st == "ready" else curses.color_pair(3)
                elif title == "PROVIDER":
                    st = _provider_status(item)
                    suffix = f" [{st}]"
                    status_color = curses.color_pair(4) if st != "missing" else curses.color_pair(3)
                item_text = f"{marker} {item.name} ({item.id}){suffix}"
                if len(item_text) > inner_w:
                    item_text = item_text[: inner_w - 1] + "…"

                # draw border char + content
                _safe_addstr(stdscr, y_row, x, "│", border_attr)
                content_x = x + 1

                if item_idx == selected and active:
                    _safe_addstr(stdscr, y_row, content_x, item_text.ljust(inner_w), curses.A_REVERSE, inner_w)
                elif item_idx == selected:
                    _safe_addstr(stdscr, y_row, content_x, item_text.ljust(inner_w), curses.A_BOLD, inner_w)
                else:
                    if filter_text and active:
                        # draw with filter highlight
                        _draw_filtered_line(stdscr, y_row, content_x, item_text, inner_w, filter_text, status_color, suffix)
                    else:
                        line_attr = curses.A_DIM if not active else curses.A_NORMAL
                        _safe_addstr(stdscr, y_row, content_x, item_text.ljust(inner_w), line_attr, inner_w)

                _safe_addstr(stdscr, y_row, x + width - 1, "│", border_attr)
            else:
                # empty row
                _safe_addstr(stdscr, y_row, x, "│", border_attr)
                _safe_addstr(stdscr, y_row, x + width - 1, "│", border_attr)

        # bottom border: └── showing N/M ──┘
        if show_status:
            status_label = f" {show_status} "
            pad_len = max(0, inner_w - len(status_label))
            bottom = "└" + status_label + "─" * pad_len + "┘"
        else:
            bottom = "└" + "─" * inner_w + "┘"
        bottom = bottom[:width]
        _safe_addstr(stdscr, y + height - 1, x, bottom, border_attr, width)

    def _draw_filtered_line(
        stdscr: Any, y: int, x: int, text: str, max_w: int, ftext: str, status_color: int, suffix: str
    ) -> None:
        lower = ftext.lower()
        lower_text = text.lower()
        pos = 0
        cx = x
        while pos < len(text) and cx - x < max_w:
            match_start = lower_text.find(lower, pos)
            if match_start < 0:
                _safe_addstr(stdscr, y, cx, text[pos : pos + (max_w - (cx - x))], curses.A_NORMAL, max_w - (cx - x))
                break
            # draw before match
            if match_start > pos:
                seg = text[pos:match_start]
                _safe_addstr(stdscr, y, cx, seg, curses.A_NORMAL, max_w - (cx - x))
                cx += len(seg)
            # draw match highlight
            match_end = match_start + len(lower)
            seg = text[match_start:match_end]
            _safe_addstr(stdscr, y, cx, seg, curses.A_BOLD | curses.color_pair(2), max_w - (cx - x))
            cx += len(seg)
            pos = match_end
        if cx - x < max_w:
            _safe_addstr(stdscr, y, cx, " " * (max_w - (cx - x)), curses.A_NORMAL)

    def inner(stdscr: Any) -> tuple[Choice, Choice, Choice] | None:
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
        try:
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_WHITE)
        except curses.error:
            curses.init_pair(5, curses.COLOR_WHITE, -1)
        try:
            curses.init_pair(6, 8, -1)  # gray for dim
        except curses.error:
            curses.init_pair(6, curses.COLOR_WHITE, -1)
        selected = [0, 0, 0]
        active_pane = 0
        filter_text = ""
        filter_active = False

        def _filtered(items: list[Choice]) -> list[Choice]:
            if not filter_text:
                return items
            lower = filter_text.lower()
            return [item for item in items if lower in item.name.lower() or lower in item.id.lower()]

        while True:
            active_cli = clis[selected[0]]
            available_providers = compatible_providers(active_cli, providers)
            selected[1] = min(selected[1], len(available_providers) - 1)
            active_provider = available_providers[selected[1]]
            models = fetch_provider_models(active_provider)

            all_panes = [clis, available_providers, models]
            filtered_panes = [_filtered(p) for p in all_panes]
            for i in range(3):
                pane = filtered_panes[i] if filter_active else all_panes[i]
                selected[i] = min(selected[i], max(0, len(pane) - 1))

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if height < 14 or width < 80:
                stdscr.addstr(0, 0, "Terminal too small. Need 80x14.")
                stdscr.refresh()
                key = stdscr.getch()
                if key in (ord("q"), 27):
                    return None
                continue

            # layout — center within screen, cap max width
            max_content_width = 90
            max_content_height = 28
            content_width = min(width, max_content_width)
            content_height = min(height, max_content_height)
            ox = (width - content_width) // 2  # horizontal offset for centering
            oy = (height - content_height) // 2  # vertical offset

            margin = 2
            pane_width = (content_width - margin * 2) // 3
            pane_height = content_height - 6  # title + help + status + 3 border rows
            pane_xs = [ox + margin, ox + margin + pane_width, ox + margin + pane_width * 2]

            # y=0: title bar
            title = f" Mix v{__version__} "
            _safe_addstr(stdscr, oy, ox + 2, title, curses.A_BOLD | curses.color_pair(2))
            help_text = "↑/↓ PgUp/PgDn  ←/→/Tab switch  Enter launch  / filter  q quit"
            if filter_active:
                help_text = f"Filter: {filter_text}_  Enter/Esc done  ↑/↓ move"
            _safe_addstr(stdscr, oy, ox + 2 + len(title) + 2, help_text[: content_width - len(title) - 6], curses.A_DIM)

            # y=1: status bar — item counts
            pane_names = ["CLI", "PROVIDER", "MODEL"]
            for i, (name, pane_items) in enumerate(zip(pane_names, all_panes)):
                disp_items = filtered_panes[i] if filter_active else pane_items
                count = len(disp_items)
                total = len(pane_items)
                if filter_active and count != total:
                    label = f"{name} ({count}/{total})"
                else:
                    label = f"{name} ({count})"
                if name == "PROVIDER" and available_providers:
                    key_ok = _provider_status(available_providers[selected[1]]) if selected[1] < len(available_providers) else ""
                    if key_ok:
                        label += f"  {key_ok}"
                attr = curses.A_BOLD if i == active_pane else curses.A_DIM
                _safe_addstr(stdscr, oy + 1, pane_xs[i], label, attr, pane_width)

            # y=2..y+pane_height+1: panes with borders
            pane_y = oy + 2
            display_panes = [filtered_panes[i] if filter_active else all_panes[i] for i in range(3)]
            for i in range(3):
                pane_items = display_panes[i]
                total = len(pane_items)
                visible = max(1, pane_height - 2)
                scroll_status = ""
                if total > visible:
                    sel = selected[i]
                    end = min(sel + 1, total)
                    start_display = max(0, min(sel - visible + 1, total - visible))
                    scroll_status = f"{start_display + 1}-{start_display + visible}/{total}"
                _draw_pane(
                    stdscr,
                    pane_names[i],
                    pane_items,
                    selected[i],
                    i == active_pane,
                    pane_xs[i],
                    pane_y,
                    pane_width,
                    pane_height,
                    show_status=scroll_status,
                    filter_text=filter_text if (i == active_pane and filter_active) else "",
                )

            # footer
            pane_items = filtered_panes[active_pane] if filter_active else all_panes[active_pane]
            sel_idx = min(selected[active_pane], len(pane_items) - 1)
            sel_item = pane_items[sel_idx] if pane_items else active_cli
            footer = f" Selected: {active_cli.id} + {active_provider.id} + {sel_item.id} "
            _safe_addstr(stdscr, oy + content_height - 1, ox + 2, footer[: content_width - 3], curses.A_DIM)
            stdscr.refresh()

            # key handling
            key = stdscr.getch()
            if filter_active:
                if key == 27 or key in (10, 13, curses.KEY_ENTER):
                    filter_active = False
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    filter_text = filter_text[:-1]
                    for i in range(3):
                        pane = filtered_panes[i] if filter_active else all_panes[i]
                        selected[i] = min(selected[i], max(0, len(pane) - 1))
                elif 32 <= key <= 126:
                    filter_text += chr(key)
                    selected[active_pane] = 0
                elif key == curses.KEY_UP:
                    selected[active_pane] = max(0, selected[active_pane] - 1)
                elif key == curses.KEY_DOWN:
                    pane_items = _filtered(all_panes[active_pane])
                    selected[active_pane] = min(len(pane_items) - 1, selected[active_pane] + 1)
                continue
            if key in (ord("q"), 27):
                return None
            if key == ord("/"):
                filter_active = True
                filter_text = ""
                continue
            if key in (curses.KEY_LEFT, 353):
                active_pane = (active_pane - 1) % 3
            elif key in (curses.KEY_RIGHT, 9):
                active_pane = (active_pane + 1) % 3
            elif key == curses.KEY_UP:
                selected[active_pane] = max(0, selected[active_pane] - 1)
            elif key == curses.KEY_DOWN:
                pane_items = filtered_panes[active_pane] if filter_active else all_panes[active_pane]
                max_index = len(pane_items) - 1
                selected[active_pane] = min(max_index, selected[active_pane] + 1)
            elif key == curses.KEY_PPAGE:
                visible = max(1, pane_height - 2)
                selected[active_pane] = max(0, selected[active_pane] - visible)
            elif key == curses.KEY_NPAGE:
                visible = max(1, pane_height - 2)
                pane_items = filtered_panes[active_pane] if filter_active else all_panes[active_pane]
                selected[active_pane] = min(len(pane_items) - 1, selected[active_pane] + visible)
            elif key == curses.KEY_HOME:
                selected[active_pane] = 0
            elif key == curses.KEY_END:
                pane_items = filtered_panes[active_pane] if filter_active else all_panes[active_pane]
                selected[active_pane] = max(0, len(pane_items) - 1)
            elif key in (10, 13, curses.KEY_ENTER):
                pane_items = filtered_panes[2] if filter_active else models
                sel_idx = min(selected[2], len(pane_items) - 1)
                if pane_items:
                    return active_cli, active_provider, pane_items[sel_idx]

    return curses.wrapper(inner)


def select_runtime(
    clis: list[Choice],
    providers: list[Choice],
    cli_value: str | None,
    provider_value: str | None,
    model_value: str | None,
) -> tuple[Choice, Choice, Choice] | None:
    if cli_value or provider_value or model_value or not sys.stdin.isatty():
        cli = find_choice(clis, cli_value, "cli")
        available_providers = compatible_providers(cli, providers)
        provider = find_choice(available_providers, provider_value, "provider")
        model = find_choice(fetch_provider_models(provider), model_value, "model")
        return cli, provider, model
    return run_tui(clis, providers)


def _new_session_id() -> str:
    return uuid.uuid7().hex if hasattr(uuid, "uuid7") else uuid.uuid4().hex


def _session_path(session_id: str) -> Path:
    return SESSIONS_ROOT / session_id


def load_session(session_id: str) -> Session:
    path = _session_path(session_id)
    metadata_path = path / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MixError(f"Session not found: {session_id}") from exc
    except json.JSONDecodeError as exc:
        raise MixError(f"Session metadata parse failed: {metadata_path}: {exc}") from exc
    return Session(session_id, path, metadata)


def _remove_sensitive_env_values(data: dict[str, Any]) -> None:
    env = data.get("env")
    if not isinstance(env, dict):
        return
    for key in SENSITIVE_KEY_ENV_NAMES:
        env.pop(key, None)


def _safe_json_read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(path)


def _trusted_project_key(path: Path) -> str:
    return str(path.resolve())


def _read_trust_store() -> dict[str, Any]:
    data = _safe_json_read(TRUST_STORE_PATH)
    return data if isinstance(data, dict) else {}


def _trust_store_paths(cli_id: str) -> set[str]:
    values = _read_trust_store().get(cli_id)
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values}


def is_workspace_trusted(cli_id: str, workspace: Path) -> bool:
    return _trusted_project_key(workspace) in _trust_store_paths(cli_id)


def remember_workspace_trust(cli_id: str, workspace: Path) -> None:
    store = _read_trust_store()
    trusted = store.get(cli_id)
    if not isinstance(trusted, list):
        trusted = []
    key = _trusted_project_key(workspace)
    if key not in trusted:
        trusted.append(key)
    store[cli_id] = sorted(trusted)
    _write_json(TRUST_STORE_PATH, store)


def _claude_template_root() -> Path:
    return APP_ROOT / "templates" / "claude"


def _codex_template_root() -> Path:
    return APP_ROOT / "templates" / "codex"


def _claude_key_fingerprint(api_key: str) -> str:
    return api_key[-20:]


def _approve_claude_proxy_token(state: dict[str, Any], proxy_token: str) -> None:
    state.setdefault("hasCompletedOnboarding", True)
    state["installMethod"] = "mix"
    responses = state.get("customApiKeyResponses")
    if not isinstance(responses, dict):
        responses = {}
    approved = responses.get("approved")
    if not isinstance(approved, list):
        approved = []
    fingerprint = _claude_key_fingerprint(proxy_token)
    if fingerprint not in approved:
        approved.append(fingerprint)
    responses["approved"] = approved
    responses.setdefault("rejected", [])
    state["customApiKeyResponses"] = responses


def _trust_claude_workspace(state: dict[str, Any], workspace: Path) -> None:
    projects = state.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    project = projects.get(str(workspace))
    if not isinstance(project, dict):
        project = {}
    project["hasTrustDialogAccepted"] = True
    project["hasCompletedProjectOnboarding"] = True
    project.setdefault("allowedTools", [])
    project.setdefault("mcpContextUris", [])
    project.setdefault("enabledMcpjsonServers", [])
    project.setdefault("disabledMcpjsonServers", [])
    project.setdefault("mcpServers", {})
    projects[str(workspace)] = project
    state["projects"] = projects


def ensure_claude_runtime_state(session: Session, proxy_token: str) -> None:
    trusted = is_workspace_trusted("claude", Path.cwd())
    for state_path in (session.path / "home" / ".claude.json", session.path / "config" / ".claude.json"):
        state = _safe_json_read(state_path)
        _approve_claude_proxy_token(state, proxy_token)
        if trusted:
            _trust_claude_workspace(state, Path.cwd())
        _write_json(state_path, state)


def _claude_session_trusted_workspace(session: Session, workspace: Path) -> bool:
    for state_path in (session.path / "config" / ".claude.json", session.path / "home" / ".claude.json"):
        state = _safe_json_read(state_path)
        projects = state.get("projects")
        if not isinstance(projects, dict):
            continue
        project = projects.get(_trusted_project_key(workspace)) or projects.get(str(workspace))
        if isinstance(project, dict) and project.get("hasTrustDialogAccepted") is True:
            return True
    return False


def _codex_session_trusted_workspace(session: Session, workspace: Path) -> bool:
    config_path = session.path / "config" / "config.toml"
    if tomllib is not None:
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        projects = data.get("projects") if isinstance(data, dict) else None
        project = projects.get(_trusted_project_key(workspace)) if isinstance(projects, dict) else None
        if isinstance(project, dict):
            return project.get("trust_level") == "trusted"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    section = f"[projects.{_toml_string(_trusted_project_key(workspace))}]"
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == section
            continue
        if in_section and stripped == 'trust_level = "trusted"':
            return True
    return False


def collect_session_trust(cli: Choice, session: Session, workspace: Path) -> None:
    if cli.id == "claude" and _claude_session_trusted_workspace(session, workspace):
        remember_workspace_trust("claude", workspace)
    if cli.id == "codex" and _codex_session_trusted_workspace(session, workspace):
        remember_workspace_trust("codex", workspace)


def ensure_claude_runtime_settings(session: Session) -> None:
    settings_path = session.path / "config" / "settings.json"
    settings = _safe_json_read(settings_path)
    _remove_sensitive_env_values(settings)
    _write_json(settings_path, settings)


def ensure_claude_template() -> Path:
    template_root = _claude_template_root()
    template_settings = template_root / "settings.json"
    template_state = template_root / ".claude.json"
    if template_settings.exists() and template_state.exists():
        return template_root

    real_state = _safe_json_read(USER_HOME / ".claude.json")
    state_keys = [
        "autoUpdates",
        "customApiKeyResponses",
        "tipsHistory",
        "hasCompletedOnboarding",
        "lastOnboardingVersion",
        "lastReleaseNotesSeen",
        "opusProMigrationComplete",
        "sonnet1m45MigrationComplete",
    ]
    template_state_data = {key: real_state[key] for key in state_keys if key in real_state}
    template_state_data.setdefault("hasCompletedOnboarding", True)
    template_state_data.setdefault("customApiKeyResponses", {"approved": ["mix-local"], "rejected": []})
    _write_json(template_state, template_state_data)

    real_settings = _safe_json_read(USER_HOME / ".claude" / "settings.json")
    settings_keys = ["theme", "includeCoAuthoredBy", "cleanupPeriodDays", "permissions", "hooks", "statusLine"]
    template_settings_data = {key: real_settings[key] for key in settings_keys if key in real_settings}
    _remove_sensitive_env_values(template_settings_data)
    template_settings_data.setdefault("theme", "dark")
    _write_json(template_settings, template_settings_data)
    return template_root


def ensure_codex_template() -> Path:
    template_root = _codex_template_root()
    template_config = template_root / "config.toml"
    template_auth = template_root / "auth.json"
    if template_config.exists() and template_auth.exists():
        return template_root

    template_root.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(template_root)
    real_config = USER_HOME / ".codex" / "config.toml"
    if real_config.exists() and not template_config.exists():
        shutil.copy2(real_config, template_config)
        _chmod_private(template_config)
    elif not template_config.exists():
        template_config.write_text('disable_response_storage = true\n', encoding="utf-8")
        _chmod_private(template_config)
    if not template_auth.exists():
        _write_json(template_auth, {"OPENAI_API_KEY": "mix-local"})
    return template_root


def initialize_cli_session_config(cli: Choice, session: Session) -> None:
    if cli.id == "claude":
        template_root = ensure_claude_template()
        session_config = session.path / "config"
        for filename in ("settings.json", ".claude.json"):
            source = template_root / filename
            target = session_config / filename
            if source.exists() and not target.exists():
                shutil.copy2(source, target)
                _chmod_private(target)
        return

    if cli.id == "codex":
        template_root = ensure_codex_template()
        session_config = session.path / "config"
        for filename in ("config.toml", "auth.json"):
            source = template_root / filename
            target = session_config / filename
            if source.exists() and not target.exists():
                shutil.copy2(source, target)
                _chmod_private(target)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _codex_config_parts_without_mix_overrides(text: str, trusted_project: Path | None = None) -> tuple[list[str], list[str]]:
    root_lines: list[str] = []
    section_lines: list[str] = []
    in_section = False
    skip_section = False
    in_multiline_string = False
    trusted_project_section = f"[projects.{_toml_string(_trusted_project_key(trusted_project))}]" if trusted_project else ""
    for line in text.splitlines():
        stripped = line.strip()
        quote_count = stripped.count('"""') + stripped.count("'''")
        if not in_multiline_string and stripped.startswith("[") and stripped.endswith("]"):
            in_section = True
            skip_section = stripped in {"[model_providers.mix]", trusted_project_section}
            if skip_section:
                continue
        if quote_count % 2 == 1:
            in_multiline_string = not in_multiline_string
        if skip_section:
            continue
        if not in_section and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in {"model_provider", "model"}:
                continue
        if in_section:
            section_lines.append(line)
        else:
            root_lines.append(line)
    return root_lines, section_lines


def write_codex_runtime_config(session: Session, model: Choice, proxy_root: str, proxy_token: str) -> None:
    session_config = session.path / "config"
    config_path = session_config / "config.toml"
    auth_path = session_config / "auth.json"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    trusted_project = Path.cwd()
    should_trust_project = is_workspace_trusted("codex", trusted_project)
    root_lines, section_lines = _codex_config_parts_without_mix_overrides(existing, trusted_project)
    lines = [
        f"model_provider = {_toml_string('mix')}",
        f"model = {_toml_string(model.id)}",
    ]
    root_text = "\n".join(line for line in root_lines if line.strip()).strip()
    if root_text:
        lines.extend(["", root_text])
    section_text = "\n".join(section_lines).strip()
    if section_text:
        lines.extend(["", section_text])
    if should_trust_project:
        lines.extend(
            [
                "",
                f"[projects.{_toml_string(_trusted_project_key(trusted_project))}]",
                f"trust_level = {_toml_string('trusted')}",
            ]
        )
    lines.extend(
        [
            "",
            "[model_providers.mix]",
            f"name = {_toml_string('Mix Local Proxy')}",
            f"wire_api = {_toml_string('responses')}",
            "requires_openai_auth = true",
            f"base_url = {_toml_string(proxy_root + '/v1')}",
        ]
    )
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _chmod_private(config_path)
    _write_json(auth_path, {"OPENAI_API_KEY": proxy_token})


def prepare_cli_session_runtime_config(cli: Choice, session: Session, model: Choice, proxy_port: int, proxy_token: str) -> None:
    if cli.id == "claude":
        ensure_claude_runtime_state(session, proxy_token)
        ensure_claude_runtime_settings(session)
    if cli.id == "codex":
        write_codex_runtime_config(session, model, f"http://127.0.0.1:{proxy_port}", proxy_token)


def create_session(cli: Choice, provider: Choice, model: Choice, name: str | None = None) -> Session:
    session_id = _new_session_id()
    path = _session_path(session_id)
    metadata: dict[str, Any] = {
        "id": session_id,
        "cli": cli.id,
        "provider": provider.id,
        "model": model.id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "working_dir": str(Path.cwd()),
    }
    if name:
        metadata["name"] = name
    for child in ("home", "config", "workspace", "logs"):
        child_path = path / child
        child_path.mkdir(parents=True, exist_ok=False)
        _chmod_private_dir(child_path)
    _chmod_private_dir(path)
    metadata_path = path / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(metadata_path)
    session = Session(session_id, path, metadata)
    initialize_cli_session_config(cli, session)
    return session


def resolve_session(
    session_id: str | None,
    cli: Choice,
    provider: Choice,
    model: Choice,
    name: str | None = None,
) -> Session:
    if not session_id:
        return create_session(cli, provider, model, name)
    session = load_session(session_id)
    expected = {"cli": cli.id, "provider": provider.id, "model": model.id}
    actual = {key: str(session.metadata.get(key) or "") for key in expected}
    if actual != expected:
        raise MixError(
            "Session runtime mismatch: "
            f"session has {actual['cli']} / {actual['provider']} / {actual['model']}, "
            f"selected {cli.id} / {provider.id} / {model.id}"
        )
    return session


def preview_session(session_id: str | None, cli: Choice, provider: Choice, model: Choice, name: str | None = None) -> Session:
    if session_id:
        return load_session(session_id)
    preview_id = "<new-session-id>"
    metadata: dict[str, Any] = {
        "id": preview_id,
        "cli": cli.id,
        "provider": provider.id,
        "model": model.id,
        "created_at": "<launch-time>",
        "working_dir": str(Path.cwd()),
    }
    if name:
        metadata["name"] = name
    return Session(preview_id, _session_path(preview_id), metadata)


def list_sessions() -> None:
    if not SESSIONS_ROOT.exists():
        return
    for metadata_path in sorted(SESSIONS_ROOT.glob("*/metadata.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        label = f"\t{metadata.get('name')}" if metadata.get("name") else ""
        print(
            f"{metadata.get('id')}\t{metadata.get('cli')}\t{metadata.get('provider')}\t"
            f"{metadata.get('model')}\t{metadata.get('created_at')}{label}"
        )

def cli_visible_model_id(cli: Choice, model: Choice) -> str:
    if cli.id != "claude":
        return model.id
    return str(model.raw.get("claude_shell_model") or cli.raw.get("claude_shell_model") or "claude-sonnet-4-6")


def build_command(cli: Choice, model: Choice, passthrough: list[str]) -> list[str]:
    command = str(cli.raw.get("command") or cli.id).strip()
    binary = shutil.which(command) if os.path.sep not in command else command
    if not binary:
        raise MixError(f"CLI not found in PATH: {command}")
    model_args = cli.raw.get("model_args", [])
    if not isinstance(model_args, list):
        raise MixError(f"Invalid model_args for CLI: {cli.id}")
    rendered_args = [str(arg).replace("{model}", cli_visible_model_id(cli, model)) for arg in model_args]
    return [binary, *rendered_args, *passthrough]


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_proxy(provider: Choice, model: Choice) -> tuple[subprocess.Popen[bytes], int, str]:
    proxy_path = Path(__file__).with_name("proxy.py")
    errors: list[str] = []
    for _attempt in range(5):
        port = _free_port()
        token = secrets.token_urlsafe(32)
        env = _proxy_env(provider, model, port, token)
        process = subprocess.Popen(
            [sys.executable, str(proxy_path), "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if process.poll() is not None:
                stderr = ""
                if process.stderr:
                    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
                errors.append(f"port {port}: {stderr or 'exited'}")
                break
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
                sock.settimeout(0.1)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return process, port, token
            time.sleep(0.05)
        else:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
            errors.append(f"port {port}: start timed out")
            continue

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
    detail = "; ".join(errors[-3:])
    raise MixError(f"Local proxy failed to start after retries: {detail}")


def build_env(config: dict[str, Any], cli: Choice, provider: Choice, model: Choice, session: Session, proxy_port: int, proxy_token: str) -> dict[str, str]:
    env = _sanitized_env()
    proxy_root = f"http://127.0.0.1:{proxy_port}"
    session_home = session.path / "home"
    session_config = session.path / "config"
    session_workspace = session.path / "workspace"
    env["HOME"] = str(session_home)
    env["MIX_SESSION_ID"] = session.id
    env["MIX_SESSION_DIR"] = str(session.path)
    env["MIX_CONFIG"] = str(CONFIG_PATH)
    env["MIX_PROVIDER"] = provider.id
    env["MIX_MODEL"] = model.id
    env["MIX_PROXY_URL"] = proxy_root

    if cli.id == "claude":
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env["CLAUDE_CONFIG_DIR"] = str(session_config)
        env["ANTHROPIC_BASE_URL"] = proxy_root
        env["ANTHROPIC_API_KEY"] = proxy_token
        env["ANTHROPIC_MODEL"] = cli_visible_model_id(cli, model)
    elif cli.id == "codex":
        env["CODEX_HOME"] = str(session_config)
        env["OPENAI_BASE_URL"] = f"{proxy_root}/v1"
        env["OPENAI_API_KEY"] = proxy_token
        env["OPENAI_MODEL"] = model.id
        env["CODEX_MODEL"] = model.id

    configured_env = dict(config.get("env") or {})
    configured_env.update(provider.raw.get("env") or {})
    configured_env.update(cli.raw.get("env") or {})
    replacements = {
        "model": model.id,
        "cli": cli.id,
        "provider": provider.id,
        "proxy_url": proxy_root,
        "session_id": session.id,
        "session_dir": str(session.path),
        "session_home": str(session_home),
        "session_config": str(session_config),
        "session_workspace": str(session_workspace),
    }
    for key, value in configured_env.items():
        rendered = str(value)
        for placeholder, replacement in replacements.items():
            rendered = rendered.replace("{" + placeholder + "}", replacement)
        if str(key) in SENSITIVE_KEY_ENV_NAMES:
            continue
        env[str(key)] = rendered
    return env




def write_default_config() -> None:
    if CONFIG_PATH.exists():
        raise MixError(f"Config already exists: {CONFIG_PATH}")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(CONFIG_PATH.parent)
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(CONFIG_PATH)
    print(f"Wrote {CONFIG_PATH}")


def list_items(config: dict[str, Any]) -> None:
    clis = normalize_choices(config.get("clis", []), "cli")
    providers = normalize_choices(config.get("providers", []), "provider")
    print("CLIs:")
    for cli in clis:
        command = str(cli.raw.get("command") or cli.id)
        status = shutil.which(command) or "missing"
        print(f"  {cli.id}\t{cli.name}\tlocal-proxy\t{status}")
    print("Providers:")
    for provider in providers:
        key_status = provider_api_key_status(provider)
        print(f"  {provider.id}\t{provider.name}\t{provider.raw.get('type')}\t{provider.raw.get('base_url')}\t{key_status}")
        for model in fetch_provider_models(provider):
            print(f"    - {model.id}\t{model.name}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal model/provider/CLI switcher")
    parser.add_argument("--cli", help="CLI id/name, e.g. claude/codex")
    parser.add_argument("--provider", help="Provider id/name, e.g. anthropic/openai/openrouter/qwen")
    parser.add_argument("--model", help="Model id/name")
    parser.add_argument("--session", help="Reuse an existing session id")
    parser.add_argument("--session-name", help="Optional name for a new session")
    parser.add_argument("--list", action="store_true", help="List configured CLIs, providers, and models")
    parser.add_argument("--list-sessions", action="store_true", help=f"List sessions in {SESSIONS_ROOT}")
    parser.add_argument("--init-config", action="store_true", help=f"Write default config to {CONFIG_PATH}")
    parser.add_argument("--no-tui", action="store_true", help="Use text prompts instead of TUI when values are missing")
    parser.add_argument("--dry-run", action="store_true", help="Print command instead of launching")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="Arguments after -- are passed to selected CLI")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130
    except MixError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.init_config:
        write_default_config()
        return 0

    if args.list_sessions:
        list_sessions()
        return 0

    config = load_config()
    if args.list:
        list_items(config)
        return 0

    clis = normalize_choices(config.get("clis", []), "cli")
    providers = normalize_choices(config.get("providers", []), "provider")
    if args.no_tui:
        cli = find_choice(clis, args.cli, "cli")
        provider = find_choice(compatible_providers(cli, providers), args.provider, "provider")
        model = find_choice(fetch_provider_models(provider), args.model, "model")
    else:
        selected_runtime = select_runtime(clis, providers, args.cli, args.provider, args.model)
        if selected_runtime is None:
            return 0
        cli, provider, model = selected_runtime

    passthrough = args.args[1:] if args.args[:1] == ["--"] else args.args
    command = build_command(cli, model, passthrough)
    if args.dry_run:
        session = preview_session(args.session, cli, provider, model, args.session_name)
        print(f"Session: {session.id} ({session.path})")
        print(f"Local proxy: would start for {provider.id} / {model.id}")
        print(" ".join(subprocess.list2cmdline([part]) for part in command))
        return 0

    workspace = Path.cwd()
    session = resolve_session(args.session, cli, provider, model, args.session_name)
    proxy_process, proxy_port, proxy_token = start_proxy(provider, model)
    prepare_cli_session_runtime_config(cli, session, model, proxy_port, proxy_token)
    env = build_env(config, cli, provider, model, session, proxy_port, proxy_token)
    try:
        return subprocess.call(command, env=env, cwd=workspace)
    finally:
        collect_session_trust(cli, session, workspace)
        proxy_process.terminate()
        try:
            proxy_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proxy_process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
