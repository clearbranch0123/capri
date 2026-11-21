"""
Tests for Wave 6 (v3.0.0 — Living Presence 3.0 Release):
- Isolated Browser ("Her Own Browser"): strict URL/SSRF validation (including redirect hops),
  isolated profile & cookies, numbered link extraction & follow_link, untrusted content framing
- Join Voice (PipeWire virtual audio bus): load/idempotency, partial rollback, strict ownership
  isolation so `leave_voice()` NEVER touches foreign PipeWire sinks/sources/loopbacks
- MCP Stdio Client (JSON-RPC 2.0): real Python subprocess MCP server handshake, tool discovery,
  dynamic `ToolRegistry` registration/unregistration, fail-closed `ToolPolicyEngine` enforcement
- Cyberdeck `/browser`, `/call`, `/mcp` commands & `/doctor` MCP check
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import httpcore
import httpx
import pytest
from PySide6.QtWidgets import QApplication

from capri.core.config import AppConfig, MCPConfig, MCPServerConfig
from capri.core.doctor import run_diagnostics
from capri.core.mcp_client import MCPManager, format_mcp_call_result
from capri.core.policy import ToolPolicyEngine
from capri.tools.browser import (
    IsolatedBrowserSession,
    _SSRFGuardedAsyncTransport,
    _SSRFGuardedNetworkBackend,
    _SSRFGuardProxy,
    _build_headless_chromium_cmd,
    extract_page_title_and_links,
    validate_browser_url,
)
from capri.tools.registry import ToolRegistry
from capri.ui.cyberdeck import CapriCyberdeckWindow
from capri.voice import join_voice as jv_mod


def test_isolated_browser_ssrf_guards_and_stateful_navigation(tmp_path, monkeypatch):
    # 1. Forbidden schemes & private/loopback hosts blocked
    for bad_url in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "http://localhost:8080/admin",
        "http://127.0.0.1:9000/secret",
        "http://[::1]/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://router.local/",
    ):
        ok, _msg = validate_browser_url(bad_url, resolve_dns=False)
        assert ok is False, f"Expected SSRF/scheme guard to reject {bad_url}"

    # DNS resolving to loopback must also be rejected
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port, type=0: [(2, 1, 6, "", ("127.0.0.1", 80))],
    )
    ok_rebind, msg_rebind = validate_browser_url("https://rebind.example.test/", resolve_dns=True)
    assert ok_rebind is False
    assert "127.0.0.1" in msg_rebind

    # Public IP allowed
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port, type=0: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    ok_pub, _ = validate_browser_url("https://example.com/docs", resolve_dns=True)
    assert ok_pub is True

    # 2. Title & link extraction
    sample_html = (
        "<html><head><title>Capri Docs</title></head>"
        "<body><h1>Welcome</h1><p>Isolated test page.</p>"
        '<a href="/guide">Quick Guide</a>'
        '<a href="javascript:void(0)">Ignore JS</a>'
        '<a href="https://example.com/api">API Ref</a>'
        "</body></html>"
    )
    title, links = extract_page_title_and_links(sample_html, "https://example.com/")
    assert title == "Capri Docs"
    assert links == [
        ("Quick Guide", "https://example.com/guide"),
        ("API Ref", "https://example.com/api"),
    ]

    # 3. Stateful session visit, follow_link, redirect SSRF block, and isolated clear()
    sess = IsolatedBrowserSession(profile_dir=tmp_path / "isolated_prof")
    sess.ensure_profile_dir()
    (sess.profile_dir / "marker.txt").write_text("capri-only", encoding="utf-8")

    class _FakeResp:
        def __init__(self, status_code: int, text: str = "", headers: dict | None = None, cookies: dict | None = None):
            self.status_code = status_code
            self.text = text
            self.headers = headers or {"content-type": "text/html; charset=utf-8"}
            self.cookies = cookies or {}

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url: str):
            if url == "https://example.com/":
                return _FakeResp(200, sample_html, cookies={"capri_sid": "iso123"})
            if url == "https://example.com/guide":
                return _FakeResp(200, "<html><title>Guide</title><body><p>Guide body text.</p></body></html>")
            if url == "https://example.com/ssrf-redirect":
                return _FakeResp(302, "", headers={"location": "http://127.0.0.1:8080/internal"})
            return _FakeResp(404, "Not found")

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    out1 = asyncio.run(sess.visit("https://example.com/"))
    assert "Capri Docs" in out1
    assert '<web_page_content untrusted="true">' in out1
    assert sess.cookies.get("capri_sid") == "iso123"
    assert len(sess.current_links) == 2

    out2 = asyncio.run(sess.follow_link(1))
    assert "Guide body text." in out2
    assert sess.current_url == "https://example.com/guide"

    out_ssrf = asyncio.run(sess.visit("https://example.com/ssrf-redirect"))
    assert "заблокирован" in out_ssrf.lower()

    clr_msg = sess.clear()
    assert "очищен" in clr_msg.lower()
    assert sess.current_url == ""
    assert not sess.profile_dir.exists()


def test_join_voice_lifecycle_rollback_and_foreign_module_safety(monkeypatch):
    from capri.voice import pipewire as pw_mod

    jv_mod._CAPRI_VOICE_MODULE_IDS.clear()
    modules: list[tuple[str, str, str]] = [
        ("5", "module-null-sink", "sink_name=obs_audio_sink"),
        ("6", "module-loopback", "source=mic0 sink=obs_audio_sink"),
    ]
    next_id = [20]
    unloaded_calls: list[str] = []
    fail_remap = {"value": False}

    def fake_pactl(args, timeout=1.8):
        if args == ["info"]:
            return 0, "Server Name: PulseAudio (on PipeWire 1.6.10)\nDefault Source: mic0\n", ""
        if args == ["list", "short", "sources"]:
            lines = ["51\tmic0\tPipeWire\ts16le 1ch 48000Hz\tIDLE"]
            if any(m[1] == "module-remap-source" and jv_mod.CAPRI_VIRTUAL_MIC_SOURCE in m[2] for m in modules):
                lines.append(f"88\t{jv_mod.CAPRI_VIRTUAL_MIC_SOURCE}\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE")
            return 0, "\n".join(lines) + "\n", ""
        if args == ["list", "short", "modules"]:
            return 0, "\n".join(f"{mid}\t{mn}\t{ma}" for mid, mn, ma in modules) + "\n", ""
        if args and args[0] == "load-module":
            mod_name = args[1]
            mod_args = " ".join(args[2:])
            if mod_name == "module-remap-source" and fail_remap["value"]:
                return 1, "", "simulated remap error"
            mid = str(next_id[0])
            next_id[0] += 1
            modules.append((mid, mod_name, mod_args))
            return 0, mid + "\n", ""
        if args and args[0] == "unload-module":
            mid = args[1]
            unloaded_calls.append(mid)
            modules[:] = [m for m in modules if m[0] != mid]
            return 0, "", ""
        return 1, "", "unsupported"

    monkeypatch.setattr(pw_mod, "_run_pactl", fake_pactl)
    monkeypatch.setattr(jv_mod, "_run_pactl", fake_pactl)

    # 1. Partial failure rollback must unload the already-created null-sink
    fail_remap["value"] = True
    ok_fail, _ = jv_mod.join_voice(mix_user_mic=True)
    assert ok_fail is False
    assert len(modules) == 2  # only foreign modules 5 and 6 remain
    fail_remap["value"] = False

    # 2. Clean join creates bus + virtual mic + user mic loopback
    ok_join, msg_join = jv_mod.join_voice(mix_user_mic=True)
    assert ok_join is True
    assert jv_mod.CAPRI_VIRTUAL_MIC_SOURCE in msg_join
    st = jv_mod.get_join_voice_status()
    assert st.active is True
    assert st.mic_mixed is True
    assert len(st.owned_module_ids) == 3

    # 3. Second join is idempotent (no duplicate modules)
    ok_dup, msg_dup = jv_mod.join_voice(mix_user_mic=True)
    assert ok_dup is True and "уже активен" in msg_dup
    assert len(modules) == 5  # 2 foreign + 3 Capri

    # 4. Leave unloads ONLY Capri's 3 modules and NEVER touches foreign 5 or 6
    unloaded_calls.clear()
    ok_leave, _ = jv_mod.leave_voice()
    assert ok_leave is True
    assert "5" not in unloaded_calls
    assert "6" not in unloaded_calls
    assert [m[0] for m in modules] == ["5", "6"]
    assert jv_mod.get_join_voice_status().active is False


def test_mcp_stdio_client_real_subprocess_handshake_and_tool_execution():
    # Minimal real spec-compliant MCP stdio JSON-RPC 2.0 server run as an isolated child process
    server_script = r"""
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tiny-demo", "version": "1.0"},
            },
        }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "ping",
                        "description": "Echo ping message",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            },
        }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"pong: {args.get('text', '')}"}],
                "isError": False,
            },
        }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
"""

    async def _run():
        local_reg = ToolRegistry(default_policy_engine=ToolPolicyEngine(mode="ask"))
        mgr = MCPManager(tool_registry=local_reg)
        mcp_cfg = MCPConfig(
            enabled=True,
            servers=[MCPServerConfig(name="demo", command=sys.executable, args=["-u", "-c", server_script])],
        )
        try:
            summary = await mgr.connect_from_config(mcp_cfg)
            assert summary == {"demo": "OK (1 tools)"}
            assert "mcp__demo__ping" in local_reg._tools

            # Policy enforcement: SAFE denies, ASK without handler fails closed, ASK with approval succeeds
            safe_pol = ToolPolicyEngine(mode="safe")
            res_safe = await local_reg.execute("mcp__demo__ping", {"text": "hi"}, policy_engine=safe_pol)
            assert "заблокировано" in res_safe.lower()

            ask_no_handler = ToolPolicyEngine(mode="ask")
            res_no_h = await local_reg.execute("mcp__demo__ping", {"text": "hi"}, policy_engine=ask_no_handler)
            assert "заблокировано" in res_no_h.lower()

            approvals = []

            async def approve_once(req):
                approvals.append(req.tool_name)
                return True

            ask_approved = ToolPolicyEngine(mode="ask", approval_handler=approve_once)
            res_ok = await local_reg.execute("mcp__demo__ping", {"text": "hello"}, policy_engine=ask_approved)
            assert approvals == ["mcp__demo__ping"]
            assert '<mcp_tool_output untrusted="true">' in res_ok
            assert "pong: hello" in res_ok
        finally:
            await mgr.shutdown()

        # After shutdown, dynamic MCP tools are cleanly unregistered
        assert "mcp__demo__ping" not in local_reg._tools
        assert not any(s["function"]["name"].startswith("mcp__") for s in local_reg.get_schemas())

    asyncio.run(_run())
    assert "[MCP ERROR]" in format_mcp_call_result({"isError": True, "content": [{"type": "text", "text": "boom"}]})


def test_policy_classifications_for_wave6_tools_and_cyberdeck_commands():
    safe_pol = ToolPolicyEngine(mode="safe")
    ask_pol = ToolPolicyEngine(mode="ask")
    trusted_pol = ToolPolicyEngine(mode="trusted")

    # Isolated browser read tools are read_only; clear is companion; voice_call & mcp are mutating
    assert safe_pol.evaluate("capri_browser_visit", {"url": "https://example.com"}).action == "allow"
    assert safe_pol.evaluate("capri_browser_clear", {}).action == "deny"
    assert ask_pol.evaluate("capri_browser_clear", {}).action == "allow"
    assert safe_pol.evaluate("capri_voice_call", {"action": "join"}).action == "deny"
    assert ask_pol.evaluate("capri_voice_call", {"action": "join"}).action == "ask"
    assert trusted_pol.evaluate("capri_voice_call", {"action": "join"}).action == "ask"
    assert safe_pol.evaluate("mcp__github__list_issues", {}).action == "deny"
    assert trusted_pol.evaluate("mcp__github__list_issues", {}).action == "ask"

    # /doctor reports MCP-клиент check
    cfg = AppConfig()
    cfg.voice.wake_word_enabled = False
    cfg.ui.avatar_enabled = False
    diag_names = {d.name for d in run_diagnostics(cfg)}
    assert "MCP-клиент" in diag_names

    # Cyberdeck slash commands /browser, /call, /mcp execute cleanly
    app = QApplication.instance() or QApplication([])
    with patch("capri.ui.cyberdeck.notify_avatar", return_value=True):
        deck = CapriCyberdeckWindow(config=cfg)
        deck._handle_command("/browser status")
        deck._handle_command("/call status")
        deck._handle_command("/mcp status")
        deck.close()


def test_mcp_stdio_client_cross_event_loop_execution():
    """
    Regression test for MCP JSON-RPC error secret redaction:
    MCP servers connected in one event loop (or synchronously via `/mcp connect`)
    MUST execute tool calls cleanly in completely separate `asyncio` event loops
    (such as `AgentWorker`'s per-request event loops) without `Event loop is closed`
    or `Future attached to a different loop`.
    """
    server_script = r"""
import json, sys
counter = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cross-loop-demo", "version": "1.0"},
            },
        }) + "\n")
        sys.stdout.flush()
    elif method == "tools/list":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [{
                    "name": "ping",
                    "description": "Echo across loops",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }]
            },
        }) + "\n")
        sys.stdout.flush()
    elif method == "tools/call":
        counter += 1
        args = (msg.get("params") or {}).get("arguments") or {}
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"call#{counter}:{args.get('text', '')}"}],
                "isError": False,
            },
        }) + "\n")
        sys.stdout.flush()
"""

    local_reg = ToolRegistry(default_policy_engine=ToolPolicyEngine(mode="ask"))
    mgr = MCPManager(tool_registry=local_reg)
    mcp_cfg = MCPConfig(
        enabled=True,
        servers=[MCPServerConfig(name="demo", command=sys.executable, args=["-u", "-c", server_script])],
    )

    async def approve_all(_req):
        return True

    ask_approved = ToolPolicyEngine(mode="ask", approval_handler=approve_all)

    # Path A: Connect in Event Loop #1 (which then closes), invoke in Loop #2 and Loop #3, shutdown in Loop #4
    try:
        summary = asyncio.run(mgr.connect_from_config(mcp_cfg))
        assert summary == {"demo": "OK (1 tools)"}
        assert "mcp__demo__ping" in local_reg._tools

        res_loop2 = asyncio.run(local_reg.execute("mcp__demo__ping", {"text": "loop2"}, policy_engine=ask_approved))
        assert "call#1:loop2" in res_loop2

        res_loop3 = asyncio.run(local_reg.execute("mcp__demo__ping", {"text": "loop3"}, policy_engine=ask_approved))
        assert "call#2:loop3" in res_loop3
    finally:
        asyncio.run(mgr.shutdown())

    assert "mcp__demo__ping" not in local_reg._tools

    # Path B: Synchronous `/mcp connect` -> separate async `AgentWorker` loop -> sync `closeEvent` shutdown
    try:
        summary_sync = mgr.connect_from_config_sync(mcp_cfg)
        assert summary_sync == {"demo": "OK (1 tools)"}
        res_worker = asyncio.run(local_reg.execute("mcp__demo__ping", {"text": "worker"}, policy_engine=ask_approved))
        assert "call#1:worker" in res_worker
    finally:
        mgr.shutdown_sync()

    assert "mcp__demo__ping" not in local_reg._tools


def test_isolated_browser_anti_dns_rebinding_single_resolution_in_httpx(tmp_path, monkeypatch):
    """
    Regression test for HTTPX DNS-rebinding SSRF guard:
    Proves `_SSRFGuardedNetworkBackend` resolves DNS EXACTLY ONCE per connection and connects
    strictly to the pinned public IP literal (never re-resolving on a second lookup that
    could rebind to 127.0.0.1), while also blocking obfuscated IP literals and redirect-target
    DNS rebinding.
    """
    # Obfuscated/non-global IP literals and trailing-dot localhost must be rejected even without DNS
    for sneaky_url in (
        "http://2130706433/",
        "http://0x7f000001/",
        "http://127.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://100.64.0.1/",
        "http://localhost./",
        "http://sub.localhost/",
    ):
        ok, _ = validate_browser_url(sneaky_url, resolve_dns=False)
        assert ok is False, f"Expected sneaky SSRF URL to be blocked: {sneaky_url}"

    dns_calls: list[str] = []

    def fake_getaddrinfo(host, port, type=0):
        dns_calls.append(host)
        if host == "rebind-target.example":
            # 1st lookup returns public IP; any hypothetical 2nd lookup rebinds to 127.0.0.1!
            if dns_calls.count("rebind-target.example") == 1:
                return [(2, 1, 6, "", ("93.184.216.34", port))]
            return [(2, 1, 6, "", ("127.0.0.1", port))]
        if host == "redirect-source.example":
            return [(2, 1, 6, "", ("93.184.216.35", port))]
        if host == "internal-rebind.example":
            return [(2, 1, 6, "", ("127.0.0.1", port))]
        raise OSError(f"unexpected DNS lookup: {host}")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    connected_targets: list[tuple[str, int]] = []
    sent_requests: list[bytes] = []

    class _FakeStream(httpcore.AsyncNetworkStream):
        def __init__(self, response_bytes: bytes):
            self._buf = response_bytes

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            chunk = self._buf[:max_bytes]
            self._buf = self._buf[max_bytes:]
            return chunk

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            sent_requests.append(bytes(buffer))

        async def aclose(self) -> None:
            pass

    class _FakeInnerBackend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            connected_targets.append((host, port))
            if host == "93.184.216.34":
                body = b"<html><title>Pinned OK</title><body><p>Single DNS safe page.</p></body></html>"
                raw = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + body
                )
                return _FakeStream(raw)
            if host == "93.184.216.35":
                raw = (
                    b"HTTP/1.1 302 Found\r\n"
                    b"Location: http://internal-rebind.example/admin\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                return _FakeStream(raw)
            raise AssertionError(f"Unexpected TCP connect to non-pinned/forbidden host: {host}:{port}")

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise AssertionError("Unix socket must never be dialed")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    import capri.tools.browser as browser_mod

    monkeypatch.setattr(
        browser_mod,
        "_SSRFGuardedAsyncTransport",
        lambda **kw: _SSRFGuardedAsyncTransport(network_backend=_SSRFGuardedNetworkBackend(inner=_FakeInnerBackend())),
    )

    sess = IsolatedBrowserSession(profile_dir=tmp_path / "rebind_prof")

    # 1. Visit `rebind-target.example`: DNS must be resolved ONCE, TCP must dial pinned `93.184.216.34`,
    #    and original `Host: rebind-target.example` header must be preserved.
    out_ok = asyncio.run(sess.visit("http://rebind-target.example/page", use_js=False))
    assert "Single DNS safe page." in out_ok
    assert dns_calls.count("rebind-target.example") == 1
    assert connected_targets == [("93.184.216.34", 80)]
    assert any(b"Host: rebind-target.example" in req for req in sent_requests)

    # 2. Redirect to `internal-rebind.example` (resolving to 127.0.0.1) MUST be blocked inside transport
    #    before any TCP socket is ever opened to 127.0.0.1.
    connected_targets.clear()
    out_redir = asyncio.run(sess.visit("http://redirect-source.example/go", use_js=False))
    assert "заблокирован" in out_redir.lower()
    assert "127.0.0.1" in out_redir
    assert connected_targets == [("93.184.216.35", 80)]


def test_isolated_browser_ssrf_guard_proxy_and_headless_chromium(tmp_path, monkeypatch):
    """
    Regression test for Headless Chromium JS-mode SSRF guard:
    Verifies `_SSRFGuardProxy` blocks plain HTTP, HTTPS CONNECT, IPv6 loopback, DNS-rebinded
    hostnames, and non-HTTP schemes against a real local secret server (0 requests reach it),
    verifies hardened Chromium proxy/DNS flags, and verifies `visit(..., use_js=True)` rejects
    when Chromium's navigation hits a proxy-blocked internal redirect.
    """
    import capri.tools.browser as browser_mod

    # 1. Verify hardened Chromium launch flags force ALL traffic & DNS through `_SSRFGuardProxy`
    cmd = _build_headless_chromium_cmd("/usr/bin/chromium", tmp_path / "prof", 43210, "https://example.com/")
    assert "--proxy-server=http://127.0.0.1:43210" in cmd
    assert "--proxy-bypass-list=<-loopback>" in cmd
    assert "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1" in cmd
    assert "--disable-quic" in cmd
    assert "--disable-component-update" in cmd

    # 2. Live `_SSRFGuardProxy` test against a real local 127.0.0.1 secret HTTP server
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port, type=0: [(2, 1, 6, "", ("127.0.0.1", port))]
        if host == "evil-rebind.example"
        else [(2, 1, 6, "", ("93.184.216.34", port))],
    )

    async def _run_proxy_checks():
        secret_hits: list[bytes] = []

        async def _secret_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            data = await reader.read(4096)
            secret_hits.append(data)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nTOP_SECRET")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        secret_srv = await asyncio.start_server(_secret_handler, "127.0.0.1", 0)
        secret_port = secret_srv.sockets[0].getsockname()[1]

        async def _send_via_proxy(proxy_port: int, raw_req: bytes) -> str:
            r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
            w.write(raw_req)
            await w.drain()
            resp = await asyncio.wait_for(r.read(4096), timeout=3.0)
            w.close()
            await w.wait_closed()
            return resp.decode("utf-8", errors="replace")

        try:
            async with _SSRFGuardProxy() as proxy:
                # (a) Plain HTTP to 127.0.0.1
                r_http = await _send_via_proxy(
                    proxy.port,
                    f"GET http://127.0.0.1:{secret_port}/secret HTTP/1.1\r\nHost: 127.0.0.1:{secret_port}\r\n\r\n".encode(),
                )
                assert "403 Forbidden" in r_http and "Blocked by SSRFGuard" in r_http

                # (b) HTTPS CONNECT tunnel to 127.0.0.1
                r_connect = await _send_via_proxy(
                    proxy.port,
                    f"CONNECT 127.0.0.1:{secret_port} HTTP/1.1\r\nHost: 127.0.0.1:{secret_port}\r\n\r\n".encode(),
                )
                assert "403 Forbidden" in r_connect and "Blocked by SSRFGuard" in r_connect

                # (c) Plain HTTP to hostname resolving to 127.0.0.1 (redirect / subresource SSRF)
                r_rebind = await _send_via_proxy(
                    proxy.port,
                    b"GET http://evil-rebind.example/secret HTTP/1.1\r\nHost: evil-rebind.example\r\n\r\n",
                )
                assert "403 Forbidden" in r_rebind and "127.0.0.1" in r_rebind

                # (d) HTTPS CONNECT to IPv6 loopback [::1]
                r_v6 = await _send_via_proxy(
                    proxy.port,
                    b"CONNECT [::1]:8443 HTTP/1.1\r\nHost: [::1]:8443\r\n\r\n",
                )
                assert "403 Forbidden" in r_v6

                # (e) Non-HTTP scheme over proxy
                r_file = await _send_via_proxy(
                    proxy.port,
                    b"GET file:///etc/passwd HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )
                assert "403 Forbidden" in r_file

                assert len(proxy.blocked_reasons) >= 5
                assert secret_hits == [], f"Local secret server must never be reached! Got: {secret_hits}"

            # 3. End-to-end `visit(..., use_js=True)`: simulated Chromium following a redirect to
            #    `http://127.0.0.1:<secret_port>/secret` through the real `_SSRFGuardProxy`
            monkeypatch.setattr(browser_mod, "detect_headless_chromium", lambda: "/usr/bin/fake-chromium")

            async def _fake_chromium_exec(*argv, **kwargs):
                proxy_flag = next(a for a in argv if a.startswith("--proxy-server=http://127.0.0.1:"))
                p_port = int(proxy_flag.rsplit(":", 1)[1])
                assert "--proxy-bypass-list=<-loopback>" in argv
                assert "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1" in argv

                proxy_resp = await _send_via_proxy(
                    p_port,
                    f"GET http://127.0.0.1:{secret_port}/secret HTTP/1.1\r\nHost: 127.0.0.1:{secret_port}\r\n\r\n".encode(),
                )
                body = proxy_resp.split("\r\n\r\n", 1)[-1]

                class _FakeProc:
                    returncode = 0

                    async def communicate(self):
                        return f"<html><body><pre>{body}</pre></body></html>".encode("utf-8"), b""

                return _FakeProc()

            monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_chromium_exec)

            sess = IsolatedBrowserSession(profile_dir=tmp_path / "js_ssrf_prof")
            out_js = await sess.visit("http://public-redirector.example/go", use_js=True)
            assert "отклонил url" in out_js.lower()
            assert "127.0.0.1" in out_js
            assert secret_hits == []
        finally:
            secret_srv.close()
            await secret_srv.wait_closed()

    asyncio.run(_run_proxy_checks())
