import json
import os
import tempfile
import time
import threading
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from unittest import mock

import mix
import proxy


class StreamingUpstreamHandler(proxy.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length") or "0")
        self.rfile.read(length)
        if self.path != "/chat/completions":
            body = b'{"error":"route not found"}'
            self.send_response(404)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for part in ("he", "llo"):
            chunk = f'data: {{"id":"chatcmpl_1","choices":[{{"delta":{{"content":"{part}"}}}}]}}\n\n'.encode("utf-8")
            self.wfile.write(chunk)
            self.wfile.flush()
            time.sleep(0.05)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class ProxySecurityTests(unittest.TestCase):
    def setUp(self):
        self.server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
        self.server.config = {"providers": []}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.env = mock.patch.dict(os.environ, {"MIX_PROXY_TOKEN": "test-token"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def post(self, body=b"{}", headers=None):
        request = urllib.request.Request(
            self.base + "/v1/messages",
            data=body,
            headers=headers or {"content-type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)

    def assert_http_error(self, expected_status, body=b"{}", headers=None):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(body, headers)
        self.assertEqual(ctx.exception.code, expected_status)

    def test_proxy_requires_token(self):
        self.assert_http_error(401)
        self.assert_http_error(401, headers={"authorization": "Bearer wrong"})
        self.assert_http_error(400, headers={"authorization": "Bearer test-token"})

    def test_proxy_accepts_x_api_key_token(self):
        self.assert_http_error(400, headers={"x-api-key": "test-token"})

    def test_content_length_validation(self):
        self.assert_http_error(400, headers={"authorization": "Bearer test-token", "content-length": "abc"})
        with mock.patch.object(proxy, "MAX_BODY_SIZE", 1):
            self.assert_http_error(413, body=b"{}", headers={"authorization": "Bearer test-token", "content-type": "application/json"})

    def test_health_does_not_require_token(self):
        with urllib.request.urlopen(self.base + "/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})


class HelperTests(unittest.TestCase):
    def test_url_validation(self):
        self.assertEqual(proxy.validate_base_url("https://example.com/v1/"), "https://example.com/v1")
        self.assertEqual(proxy.validate_base_url("http://127.0.0.1:8000"), "http://127.0.0.1:8000")
        with self.assertRaises(ValueError):
            proxy.validate_base_url("http://example.com")
        with self.assertRaises(mix.MixError):
            mix.validate_base_url("http://example.com")

    def test_glm_retry_payload_does_not_mutate_original(self):
        payload = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
        retry = proxy.glm51_no_max_tokens_payload(payload)
        retry["messages"][0]["content"] = "changed"
        self.assertEqual(payload["messages"][0]["content"], "hi")
        self.assertIn("max_tokens", payload)
        self.assertNotIn("max_tokens", retry)

    def test_fallback_marker_route_is_not_overbroad(self):
        self.assertFalse(proxy.should_messages_direct_fallback(400, b'rate limit route quota'))
        self.assertTrue(proxy.should_messages_direct_fallback(400, b'route not found'))

    def test_cli_env_sanitizes_real_keys_and_uses_proxy_token(self):
        cli = mix.Choice("claude", "Claude", {})
        provider = mix.Choice("openai", "OpenAI", {"api_key_env": "OPENAI_API_KEY"})
        model = mix.Choice("gpt-5", "GPT-5", {})
        with tempfile.TemporaryDirectory() as tempdir:
            session_path = Path(tempdir)
            session = mix.Session("s1", session_path, {})
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "real", "ANTHROPIC_API_KEY": "real2"}, clear=False):
                env = mix.build_env({}, cli, provider, model, session, 1234, "proxy-token")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "proxy-token")
        self.assertNotEqual(env.get("OPENAI_API_KEY"), "real")

    def test_proxy_env_only_keeps_current_provider_key(self):
        provider = mix.Choice("openai", "OpenAI", {"api_key_env": "OPENAI_API_KEY"})
        model = mix.Choice("gpt-5", "GPT-5", {})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "real", "ANTHROPIC_API_KEY": "other"}, clear=False):
            env = mix._proxy_env(provider, model, 1234, "proxy-token")
        self.assertEqual(env["OPENAI_API_KEY"], "real")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["MIX_PROXY_TOKEN"], "proxy-token")

    def test_proxy_plaintext_api_key_uses_config_path(self):
        provider = {"id": "p", "api_key": "secret"}
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            os.chmod(config_path, 0o600)
            with mock.patch.object(proxy, "CONFIG_PATH", config_path):
                self.assertEqual(proxy.provider_api_key(provider), "secret")

    def test_plaintext_api_key_requires_private_config(self):
        provider = mix.Choice("p", "Provider", {"api_key": "secret"})
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            os.chmod(config_path, 0o644)
            with mock.patch.object(mix, "CONFIG_PATH", config_path):
                with self.assertRaises(mix.MixError):
                    mix.provider_api_key(provider)
            os.chmod(config_path, 0o600)
            with mock.patch.object(mix, "CONFIG_PATH", config_path):
                self.assertEqual(mix.provider_api_key(provider), "secret")

    def test_start_proxy_retries_when_first_port_fails(self):
        provider = mix.Choice("openai", "OpenAI", {"api_key_env": "OPENAI_API_KEY"})
        model = mix.Choice("gpt-5", "GPT-5", {})
        first_port = 43210
        second_port = 43211
        launched_ports = []

        class FakeProcess:
            def __init__(self, port):
                self.port = port
                self.stderr = None
                self.terminated = False

            def poll(self):
                return 1 if self.port == first_port else None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.terminated = True

        class FakeSocket:
            def __init__(self, *args, **kwargs):
                self.port = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def settimeout(self, timeout):
                pass

            def close(self):
                pass

            def connect_ex(self, address):
                return 0 if address[1] == second_port else 1

            def bind(self, address):
                pass

            def getsockname(self):
                return ("127.0.0.1", second_port)

        def fake_popen(command, **kwargs):
            port = int(command[-1])
            launched_ports.append(port)
            return FakeProcess(port)

        with mock.patch.object(mix, "_free_port", side_effect=[first_port, second_port]):
            with mock.patch.object(mix.subprocess, "Popen", side_effect=fake_popen):
                with mock.patch.object(mix.socket, "socket", FakeSocket):
                    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "real"}, clear=False):
                        _process, port, token = mix.start_proxy(provider, model)
        self.assertEqual(port, second_port)
        self.assertTrue(token)
        self.assertEqual(launched_ports, [first_port, second_port])

    def test_anthropic_tool_use_maps_to_openai_tool_calls(self):
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "checking"},
                        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
                    ],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
            ],
            "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
        }
        out = proxy.anthropic_to_openai(payload, "model")
        self.assertEqual(out["messages"][0]["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(out["messages"][1], {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"})
        self.assertEqual(out["tools"][0]["type"], "function")

    def test_anthropic_image_maps_to_openai_content_part(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abcd"}},
                        {"type": "thinking", "thinking": "hidden"},
                    ],
                }
            ]
        }
        out = proxy.anthropic_to_openai(payload, "model")
        content = out["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "look"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,abcd")
        self.assertEqual(len(content), 2)

    def test_openai_tool_call_maps_to_anthropic_tool_use(self):
        payload = {
            "id": "chatcmpl_1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}
                        ],
                    }
                }
            ],
        }
        out = proxy.openai_to_anthropic(payload, "model")
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertEqual(out["content"][0], {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.txt"}})

    def test_streaming_anthropic_iterator_emits_incremental_chunks(self):
        class FakeResponse:
            def __init__(self):
                self.lines = iter(
                    [
                        b'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"he"}}]}\n',
                        b'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"llo"}}]}\n',
                        b'data: [DONE]\n',
                    ]
                )

            def readline(self):
                return next(self.lines, b"")

        chunks = list(proxy.iter_openai_stream_to_anthropic(FakeResponse(), "model"))
        text = b"".join(chunks).decode("utf-8")
        self.assertIn('"text": "he"', text)
        self.assertIn('"text": "llo"', text)
        self.assertLess(text.index('"text": "he"'), text.index('"text": "llo"'))

    def test_streaming_proxy_emits_first_chunk_before_upstream_done(self):
        upstream = proxy.ThreadingHTTPServer(("127.0.0.1", 0), StreamingUpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
        server.config = {
            "providers": [
                {
                    "id": "test-openai",
                    "type": "openai",
                    "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key_env": "TEST_OPENAI_KEY",
                }
            ]
        }
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            body = json.dumps({"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/messages",
                data=body,
                headers={"authorization": "Bearer test-token", "content-type": "application/json"},
                method="POST",
            )
            with mock.patch.dict(os.environ, {"MIX_PROXY_TOKEN": "test-token", "MIX_PROVIDER": "test-openai", "MIX_MODEL": "m", "TEST_OPENAI_KEY": "key"}, clear=False):
                start = time.monotonic()
                with urllib.request.urlopen(request, timeout=5) as response:
                    first = response.readline()
                    elapsed = time.monotonic() - start
                    rest = response.read().decode("utf-8")
            self.assertLess(elapsed, 0.04)
            self.assertIn(b"message_start", first)
            self.assertIn('"text": "he"', rest)
            self.assertIn('"text": "llo"', rest)
        finally:
            server.shutdown()
            upstream.shutdown()
            server_thread.join(timeout=2)
            upstream_thread.join(timeout=2)
            server.server_close()
            upstream.server_close()

    def test_streaming_tool_call_delta_emits_input_json_delta(self):
        class FakeResponse:
            def __init__(self):
                self.lines = iter(
                    [
                        b'data: {"id":"chatcmpl_1","choices":[{"delta":{"tool_calls":[{"index":"bad","id":"call_1","function":{"name":"read_file","arguments":"{\\"path\\":"}}]}}]}\n',
                        b'data: {"id":"chatcmpl_1","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"a.txt\\"}"}}]}}]}\n',
                        b'data: [DONE]\n',
                    ]
                )

            def readline(self):
                return next(self.lines, b"")

        chunks = list(proxy.iter_openai_stream_to_anthropic(FakeResponse(), "model"))
        text = b"".join(chunks).decode("utf-8")
        self.assertIn('"type": "tool_use"', text)
        self.assertIn('"id": "call_1"', text)
        self.assertIn('"partial_json": "{\\"path\\":"', text)
        self.assertIn('"partial_json": "\\"a.txt\\"}"', text)
        self.assertIn('"stop_reason": "tool_use"', text)

    def test_responses_function_call_maps_to_openai_tool_messages(self):
        payload = {
            "input": [
                {"type": "message", "role": "user", "content": "use tool"},
                {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": {"path": "a.txt"}},
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            ],
            "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "read_file"},
        }
        out = proxy.responses_to_openai(payload, "model")
        self.assertEqual(out["messages"][1]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(out["messages"][2], {"role": "tool", "tool_call_id": "call_1", "content": "ok"})
        self.assertEqual(out["tools"][0]["function"]["name"], "read_file")
        self.assertEqual(out["tool_choice"], {"type": "function", "function": {"name": "read_file"}})

    def test_openai_tool_calls_map_to_responses_function_call(self):
        payload = {
            "id": "chatcmpl_1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}
                        ],
                    }
                }
            ],
        }
        out = proxy.openai_to_responses(payload, "model")
        self.assertEqual(out["output"][0]["type"], "function_call")
        self.assertEqual(out["output"][0]["call_id"], "call_1")
        self.assertEqual(out["output_text"], "")

    def test_tool_diagnostics_logs_counts_without_arguments(self):
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"secret":"value"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "secret result"},
        ]
        with tempfile.TemporaryDirectory() as tempdir:
            log_path = Path(tempdir) / "proxy.log"
            with mock.patch.dict(os.environ, {"MIX_PROXY_DEBUG_TOOLS": "1", "MIX_PROXY_LOG": str(log_path)}, clear=False):
                proxy.log_tool_diagnostics("test", {"messages": messages, "tools": [{"type": "function", "function": {"name": "read_file"}}]})
            text = log_path.read_text(encoding="utf-8")
        self.assertIn("tooldiag stage=test", text)
        self.assertIn("tools=1", text)
        self.assertIn("read_file", text)
        self.assertIn("tool_results=1", text)
        self.assertNotIn("secret", text)

    def test_anthropic_extra_headers_match_domestic_model_strategy(self):
        class Handler:
            headers = {
                "User-Agent": "claude-cli",
                "x-app": "claude-code",
                "x-stainless-lang": "js",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "web-search-2025-03-05",
                "anthropic-dangerous-direct-browser-access": "true",
            }

        claude_headers = proxy.anthropic_extra_headers(Handler(), "key", "claude-sonnet-4-6")
        self.assertEqual(claude_headers["anthropic-beta"], "web-search-2025-03-05")
        self.assertEqual(claude_headers["User-Agent"], "claude-cli")
        self.assertEqual(claude_headers["x-app"], "claude-code")
        self.assertEqual(claude_headers["x-stainless-lang"], "js")
        self.assertEqual(claude_headers["anthropic-dangerous-direct-browser-access"], "true")

        glm_headers = proxy.anthropic_extra_headers(Handler(), "key", "glm-5.1")
        self.assertNotIn("anthropic-beta", glm_headers)
        self.assertEqual(glm_headers["anthropic-dangerous-direct-browser-access"], "true")
        self.assertEqual(proxy._upstream_path("/v1/messages", "/v1/messages?beta=true"), "/v1/messages?beta=true")

    def test_direct_response_summary_reports_error_and_sse_events(self):
        error = b'{"error":{"type":"invalid_request_error","message":"bad request secret text"}}'
        self.assertIn("error_type=invalid_request_error", proxy.direct_response_summary(error))
        sse = b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'
        summary = proxy.direct_response_summary(sse)
        self.assertIn("content_block_delta", summary)
        self.assertIn("message_stop", summary)

    def test_web_search_retry_payloads_remove_unsupported_fields(self):
        payload = {
            "tools": [{"type": "web_search", "name": "web_search"}],
            "tool_choice": {"type": "tool", "name": "web_search"},
            "messages": [{"role": "user", "content": "query"}],
        }
        retries = proxy.web_search_retry_payloads(payload)
        self.assertEqual(retries[0][0], "web-search-no-tool-choice")
        self.assertNotIn("tool_choice", retries[0][1])
        self.assertIn("tools", retries[0][1])
        self.assertEqual(retries[1][0], "web-search-no-tools")
        self.assertNotIn("tool_choice", retries[1][1])
        self.assertNotIn("tools", retries[1][1])

    def test_domestic_anthropic_patch_strips_cache_and_normalizes_thinking(self):
        payload = {
            "thinking": {"type": "auto"},
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}],
            "tools": [{"name": "x", "cache_control": {"type": "ephemeral"}}],
        }
        proxy.patch_domestic_anthropic_payload(payload, "glm-5.1")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertNotIn("cache_control", payload["messages"][0]["content"][0])
        self.assertNotIn("cache_control", payload["tools"][0])

    def test_claude_template_does_not_copy_install_method(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            app_root = root / "app"
            user_home = root / "home"
            user_home.mkdir()
            (user_home / ".claude.json").write_text(json.dumps({"installMethod": "native", "autoUpdates": True}), encoding="utf-8")
            with mock.patch.object(mix, "APP_ROOT", app_root), mock.patch.object(mix, "USER_HOME", user_home):
                template = mix.ensure_claude_template()
                state = json.loads((template / ".claude.json").read_text(encoding="utf-8"))
        self.assertNotIn("installMethod", state)
        self.assertTrue(state["autoUpdates"])

    def test_claude_runtime_state_syncs_only_remembered_trust(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "session"
            trust_store = Path(tempdir) / "trusted-projects.json"
            (root / "home").mkdir(parents=True)
            (root / "config").mkdir()
            session = mix.Session("s1", root, {})
            with mock.patch.object(mix, "TRUST_STORE_PATH", trust_store):
                mix.ensure_claude_runtime_state(session, "abcdefghijklmnopqrstuvwxyz78901234567890123456")
                state = json.loads((root / "config" / ".claude.json").read_text(encoding="utf-8"))
                self.assertIn("78901234567890123456", state["customApiKeyResponses"]["approved"])
                self.assertNotIn("projects", state)

                mix.remember_workspace_trust("claude", Path.cwd())
                mix.ensure_claude_runtime_state(session, "abcdefghijklmnopqrstuvwxyz78901234567890123456")
                state = json.loads((root / "config" / ".claude.json").read_text(encoding="utf-8"))
        self.assertTrue(state["projects"][str(Path.cwd())]["hasTrustDialogAccepted"])
        self.assertTrue(state["projects"][str(Path.cwd())]["hasCompletedProjectOnboarding"])

    def test_claude_uses_shell_model_for_cli_but_proxy_keeps_real_model(self):
        cli = mix.Choice("claude", "Claude", {"command": "claude", "model_args": ["--model", "{model}"]})
        provider = mix.Choice("glm", "GLM", {})
        model = mix.Choice("glm-5.1", "GLM", {})
        with tempfile.TemporaryDirectory() as tempdir:
            session = mix.Session("s1", Path(tempdir), {})
            with mock.patch.object(mix.shutil, "which", return_value="/bin/claude"):
                command = mix.build_command(cli, model, [])
            env = mix.build_env({}, cli, provider, model, session, 1234, "proxy-token")
        self.assertEqual(command, ["/bin/claude", "--model", "claude-sonnet-4-6"])
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-sonnet-4-6")
        self.assertEqual(env["MIX_MODEL"], "glm-5.1")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "session"
            trust_store = Path(tempdir) / "trusted-projects.json"
            (root / "config").mkdir(parents=True)
            session = mix.Session("s1", root, {})
            model = mix.Choice("glm-5.1", "GLM", {})
            section = f'[projects.{json.dumps(str(Path.cwd().resolve()), ensure_ascii=False)}]'
            with mock.patch.object(mix, "TRUST_STORE_PATH", trust_store):
                mix.write_codex_runtime_config(session, model, "http://127.0.0.1:1234", "proxy-token")
                text = (root / "config" / "config.toml").read_text(encoding="utf-8")
                self.assertNotIn(section, text)

                mix.remember_workspace_trust("codex", Path.cwd())
                mix.write_codex_runtime_config(session, model, "http://127.0.0.1:1234", "proxy-token")
                text = (root / "config" / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(text.count(section), 1)
        self.assertIn('trust_level = "trusted"', text)

    def test_codex_toml_trust_uses_tomllib_and_preserves_multiline_strings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "session"
            (root / "config").mkdir(parents=True)
            session = mix.Session("s1", root, {})
            workspace = Path.cwd().resolve()
            config = root / "config" / "config.toml"
            config.write_text(
                f'model = "old"\nnotes = """\n[not.a.section]\n"""\n\n[projects.{json.dumps(str(workspace), ensure_ascii=False)}]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            self.assertTrue(mix._codex_session_trusted_workspace(session, workspace))
            mix.write_codex_runtime_config(session, mix.Choice("m", "M", {}), "http://127.0.0.1:1234", "token")
            text = config.read_text(encoding="utf-8")
        self.assertIn('notes = """\n[not.a.section]\n"""', text)
        self.assertEqual(text.count("[model_providers.mix]"), 1)

    def test_collect_session_trust_persists_cli_specific_store(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "session"
            trust_store = Path(tempdir) / "trusted-projects.json"
            (root / "config").mkdir(parents=True)
            session = mix.Session("s1", root, {})
            state = {"projects": {str(Path.cwd().resolve()): {"hasTrustDialogAccepted": True}}}
            (root / "config" / ".claude.json").write_text(json.dumps(state), encoding="utf-8")
            with mock.patch.object(mix, "TRUST_STORE_PATH", trust_store):
                mix.collect_session_trust(mix.Choice("claude", "Claude", {}), session, Path.cwd())
                store = json.loads(trust_store.read_text(encoding="utf-8"))
        self.assertEqual(store["claude"], [str(Path.cwd().resolve())])

    def test_multiple_tool_results_split_into_separate_messages(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result_a"},
                        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "result_b"},
                    ],
                }
            ]
        }
        out = proxy.anthropic_to_openai(payload, "model")
        tool_msgs = [m for m in out["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertEqual(tool_msgs[0], {"role": "tool", "tool_call_id": "toolu_1", "content": "result_a"})
        self.assertEqual(tool_msgs[1], {"role": "tool", "tool_call_id": "toolu_2", "content": "result_b"})

    def test_mixed_tool_result_and_text_blocks(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "before"},
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                        {"type": "text", "text": "after"},
                    ],
                }
            ]
        }
        out = proxy.anthropic_to_openai(payload, "model")
        msgs = out["messages"]
        self.assertEqual(msgs[0], {"role": "user", "content": "before"})
        self.assertEqual(msgs[1], {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"})
        self.assertEqual(msgs[2], {"role": "user", "content": "after"})

    def test_tool_result_with_thinking_blocks_skipped(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "data"},
                        {"type": "thinking", "thinking": "internal"},
                    ],
                }
            ]
        }
        out = proxy.anthropic_to_openai(payload, "model")
        msgs = out["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0], {"role": "tool", "tool_call_id": "toolu_1", "content": "data"})

    def test_tool_result_without_tool_use_id_falls_back_to_user(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "content": "orphan"},
                    ],
                }
            ]
        }
        out = proxy.anthropic_to_openai(payload, "model")
        msgs = out["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "orphan")

    def test_no_tool_results_returns_empty(self):
        msg = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        result = proxy._anthropic_tool_results_to_openai_messages(msg)
        self.assertEqual(result, [])

    def test_responses_streaming_tool_call_deltas(self):
        chunks_data = [
            {"id": "chatcmpl_1", "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
            {"id": "chatcmpl_1", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]}}]},
        ]

        class FakeResponse:
            def __init__(self):
                self.lines = iter(
                    [f"data: {json.dumps(c)}\n".encode() for c in chunks_data] + [b"data: [DONE]\n"]
                )

            def readline(self):
                return next(self.lines, b"")

        chunks = list(proxy.iter_openai_stream_to_responses_sse(FakeResponse(), "model"))
        text = b"".join(chunks).decode("utf-8")
        self.assertIn("response.output_item.added", text)
        self.assertIn("function_call", text)
        self.assertIn("response.function_call_arguments.delta", text)
        self.assertIn('"name": "read_file"', text)
        completed_start = text.index("response.completed")
        completed_section = text[completed_start:]
        self.assertIn('"call_id": "call_1"', completed_section)
        self.assertIn('"name": "read_file"', completed_section)
        self.assertIn("arguments", completed_section)

    def test_responses_streaming_mixed_text_and_tool_call(self):
        chunks_data = [
            {"id": "chatcmpl_1", "choices": [{"delta": {"content": "thinking"}}]},
            {"id": "chatcmpl_1", "choices": [{"delta": {"content": "..."}}]},
            {"id": "chatcmpl_1", "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_42", "function": {"name": "search", "arguments": "{}"}}]}}]},
        ]

        class FakeResponse:
            def __init__(self):
                self.lines = iter(
                    [f"data: {json.dumps(c)}\n".encode() for c in chunks_data] + [b"data: [DONE]\n"]
                )

            def readline(self):
                return next(self.lines, b"")

        chunks = list(proxy.iter_openai_stream_to_responses_sse(FakeResponse(), "model"))
        text = b"".join(chunks).decode("utf-8")
        self.assertIn("response.output_text.delta", text)
        self.assertIn('"delta": "thinking"', text)
        self.assertIn("response.function_call_arguments.delta", text)
        completed_start = text.index("response.completed")
        completed_section = text[completed_start:]
        self.assertIn('"call_id": "call_42"', completed_section)
        self.assertIn('"name": "search"', completed_section)
        fc_start = text.index("function_call")
        self.assertIn('"delta": "thinking"', text[:fc_start])

    def test_responses_streaming_text_only_unchanged(self):
        chunks_data = [
            {"id": "chatcmpl_1", "choices": [{"delta": {"content": "hi"}}]},
            {"id": "chatcmpl_1", "choices": [{"delta": {"content": "!"}}]},
        ]

        class FakeResponse:
            def __init__(self):
                self.lines = iter(
                    [f"data: {json.dumps(c)}\n".encode() for c in chunks_data] + [b"data: [DONE]\n"]
                )

            def readline(self):
                return next(self.lines, b"")

        chunks = list(proxy.iter_openai_stream_to_responses_sse(FakeResponse(), "model"))
        text = b"".join(chunks).decode("utf-8")
        self.assertIn('"delta": "hi"', text)
        self.assertIn('"delta": "!"', text)
        self.assertIn("response.output_text.done", text)
        self.assertIn("response.completed", text)
        self.assertNotIn("function_call", text)

    def test_responses_streaming_multiple_tool_calls(self):
        chunks_data = [
            {"id": "chatcmpl_1", "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "f1", "arguments": "{"}}]}}]},
            {"id": "chatcmpl_1", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}}]},
            {"id": "chatcmpl_1", "choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_b", "function": {"name": "f2", "arguments": "[]"}}]}}]},
        ]

        class FakeResponse:
            def __init__(self):
                self.lines = iter(
                    [f"data: {json.dumps(c)}\n".encode() for c in chunks_data] + [b"data: [DONE]\n"]
                )

            def readline(self):
                return next(self.lines, b"")

        chunks = list(proxy.iter_openai_stream_to_responses_sse(FakeResponse(), "model"))
        text = b"".join(chunks).decode("utf-8")
        self.assertIn('"call_id": "call_a"', text)
        self.assertIn('"call_id": "call_b"', text)
        self.assertIn('"name": "f1"', text)
        self.assertIn('"name": "f2"', text)
        completed_start = text.index("response.completed")
        completed_section = text[completed_start:]
        self.assertIn("function_call", completed_section)


class CacheTests(unittest.TestCase):
    def test_cache_evicts_stale_providers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            now = time.time()
            old_time = now - 31 * 24 * 3600
            cache = {
                "old_provider": {"fetched_at": old_time, "models": [{"id": "m1"}]},
                "fresh_provider": {"fetched_at": now, "models": [{"id": "m2"}]},
            }
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            with mock.patch.object(mix, "MODELS_CACHE_PATH", cache_path):
                mix._write_models_cache(cache)
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertNotIn("old_provider", result)
            self.assertIn("fresh_provider", result)

    def test_cache_caps_provider_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            cache = {f"prov_{i}": {"fetched_at": float(i), "models": [{"id": "m"}]} for i in range(25)}
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            with mock.patch.object(mix, "MODELS_CACHE_PATH", cache_path):
                mix._write_models_cache(cache)
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(result), mix.MODELS_CACHE_MAX_PROVIDERS)

    def test_cache_caps_models_per_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "cache.json"
            cache = {}
            models = [{"id": f"model_{i}"} for i in range(300)]
            with mock.patch.object(mix, "MODELS_CACHE_PATH", cache_path):
                cache["prov"] = {"fetched_at": time.time(), "models": models}
                mix._write_models_cache(cache)
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(result["prov"]["models"]), mix.MODELS_CACHE_MAX_MODELS_PER_PROVIDER)


class ProxyLifecycleTests(unittest.TestCase):
    def test_proxy_sigterm_shuts_down_gracefully(self):
        server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
        server.config = {"providers": []}
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.1)
        self.assertTrue(server_thread.is_alive())
        proxy._shutdown_event.set()
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
        self.assertFalse(server_thread.is_alive())
        proxy._shutdown_event.clear()

    def test_connection_cache_reuses_connection(self):
        with mock.patch.object(proxy, "_connection_cache", {}):
            parsed = type("Parsed", (), {"scheme": "http", "netloc": "127.0.0.1:99999"})()
            conn1 = proxy._get_connection(parsed)
            conn2 = proxy._get_connection(parsed)
            self.assertIs(conn1, conn2)
            proxy._close_connection(conn1)

    def test_connection_cache_invalidates_stale(self):
        with mock.patch.object(proxy, "_connection_cache", {}):
            parsed = type("Parsed", (), {"scheme": "http", "netloc": "example.com:443"})()
            conn1 = proxy._get_connection(parsed)
            with mock.patch.object(proxy, "_connection_cache", {parsed.netloc: (conn1, time.time() - 120)}):
                conn2 = proxy._get_connection(parsed)
                self.assertIsNot(conn1, conn2)
                proxy._close_connection(conn2)

    def test_check_upstream_health_logs_result(self):
        config = {"providers": [{"id": "test", "base_url": "http://127.0.0.1:1", "api_key_env": "NO_KEY"}]}
        with mock.patch.dict(os.environ, {"NO_KEY": "test-key"}):
            with mock.patch("proxy.urlopen", side_effect=Exception("connection refused")):
                with mock.patch("builtins.print") as mock_print:
                    proxy._check_upstream_health(config)
                    mock_print.assert_called_once()
                    self.assertIn("Upstream health check", mock_print.call_args[0][0])
