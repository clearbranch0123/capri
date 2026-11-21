"""
CAPRI // Model Context Protocol (MCP) Stdio JSON-RPC 2.0 Client & Manager (v3.0 — Wave 6).

Zero-extra-dependency, spec-compliant MCP stdio client (`protocolVersion: 2024-11-05`):
- Spawns configured MCP servers over stdio (`stdin`/`stdout` newline-delimited JSON-RPC 2.0)
- Uses event-loop-independent `subprocess.Popen` + `threading.Lock` + daemon reader threads
  so connections opened via `/mcp connect` remain usable across arbitrary `asyncio` event
  loops (e.g., separate `AgentWorker` threads/loops and shutdown paths) without ever hitting
  `RuntimeError: Event loop is closed` or `Future attached to a different loop`
- Performs `initialize` + `notifications/initialized` handshake
- Discovers server tools via `tools/list` and registers them dynamically in `ToolRegistry`
  under `mcp__<server>__<tool>`
- Executes `tools/call` with strict timeout and untrusted-content wrapping
- Always governed fail-closed by `ToolPolicyEngine` (denied in `safe`, explicit user approval in `ask`/`trusted`)
"""

from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_MCP_FRAME_BYTES = 1024 * 1024  # 1 MiB max per JSON-RPC stdout frame (prevents OOM DoS)
MAX_MCP_STDERR_LINE_BYTES = 4096
_OVERSIZED_FRAME_MARKER = b"__CAPRI_MCP_OVERSIZED_FRAME__"


def _escape_mcp_output_tags(text: str) -> str:
    """Prevent malicious MCP servers from breaking out of `<mcp_tool_output untrusted="true">`."""
    if not text:
        return ""
    safe = re.sub(r"<\s*/\s*mcp_tool_output\b[^>]*>", "[/mcp_tool_output]", text, flags=re.IGNORECASE)
    safe = re.sub(r"<\s*mcp_tool_output\b[^>]*>", "[mcp_tool_output]", safe, flags=re.IGNORECASE)
    return safe


def _redact_mcp_text(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    from .doctor import redact_sensitive_text
    return redact_sensitive_text(text, extra_secrets=extra_secrets)


def _extract_mcp_env_secrets(env_map: dict[str, str]) -> tuple[str, ...]:
    """Collect literal and `${VAR}`-expanded env values declared for an MCP server for secret redaction."""
    if not env_map:
        return ()
    expanded = expand_mcp_env(env_map)
    vals: list[str] = []
    for k, raw_v in env_map.items():
        if isinstance(raw_v, str):
            raw_s = raw_v.strip()
            if len(raw_s) >= 6 and not (raw_s.startswith("${") and raw_s.endswith("}")):
                vals.append(raw_s)
        exp_v = expanded.get(str(k), "")
        if isinstance(exp_v, str) and len(exp_v.strip()) >= 6:
            vals.append(exp_v.strip())
    return tuple(dict.fromkeys(vals))


def sanitize_mcp_identifier(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (raw or "").strip()).strip("_")
    return cleaned or "mcp"


def expand_mcp_env(env_map: dict[str, str]) -> dict[str, str]:
    """Build subprocess environment inheriting OS env and expanding `${VAR}` placeholders."""
    merged = dict(os.environ)
    for k, v in (env_map or {}).items():
        val_str = str(v)
        expanded = _ENV_REF_RE.sub(lambda m: os.environ.get(m.group(1), ""), val_str)
        merged[str(k)] = expanded
    return merged


@dataclass(frozen=True)
class MCPToolSpec:
    server_name: str
    remote_name: str
    qualified_name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}, "required": []})


def format_mcp_call_result(result: Any, max_chars: int = 4000) -> str:
    """Extract clean readable text from an MCP `tools/call` response object."""
    if not isinstance(result, dict):
        return str(result)
    is_err = bool(result.get("isError", False))
    blocks = result.get("content")
    parts: list[str] = []
    if isinstance(blocks, list):
        for blk in blocks:
            if not isinstance(blk, dict):
                parts.append(str(blk))
                continue
            btype = blk.get("type", "text")
            if btype == "text":
                parts.append(str(blk.get("text", "")))
            elif btype == "resource":
                res = blk.get("resource") or {}
                txt = res.get("text") or res.get("uri") or str(res)
                parts.append(str(txt))
            elif btype == "image":
                mime = blk.get("mimeType", "image")
                parts.append(f"[MCP image attachment: {mime}]")
            else:
                parts.append(json.dumps(blk, ensure_ascii=False))
    elif blocks is not None:
        parts.append(str(blocks))

    body = "\n".join(p for p in parts if p).strip() or "(пустой ответ MCP)"
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n... [обрезано]"
    body = _escape_mcp_output_tags(body)
    prefix = "[MCP ERROR]\n" if is_err else ""
    return f'{prefix}<mcp_tool_output untrusted="true">\n{body}\n</mcp_tool_output>'


class MCPStdioClient:
    """
    Event-loop-independent JSON-RPC 2.0 MCP client over stdio for a single server process.

    Internally manages `subprocess.Popen` pipes with `threading.Lock` and daemon reader
    threads so async methods (`connect`, `list_tools`, `call_tool`, `disconnect`) can be
    awaited from ANY `asyncio` event loop across the lifetime of the application.
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        timeout: float = 20.0,
    ):
        self.name = sanitize_mcp_identifier(name)
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self._extra_secrets: tuple[str, ...] = _extract_mcp_env_secrets(self.env)
        self.timeout = max(0.5, float(timeout))

        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._req_id: int = 0
        self._lock = threading.Lock()
        self._stdout_queue: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=256)
        self._stderr_tail: deque[str] = deque(maxlen=12)
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._closing: bool = False

        self.connected: bool = False
        self.server_info: dict[str, Any] = {}
        self.tools: list[MCPToolSpec] = []

    @staticmethod
    def _pump_stdout(stream, target_queue: queue.Queue[Optional[bytes]]) -> None:
        try:
            while True:
                chunk = stream.readline(MAX_MCP_FRAME_BYTES + 1)
                if not chunk:
                    break
                if len(chunk) > MAX_MCP_FRAME_BYTES and not chunk.endswith(b"\n"):
                    # Drain remainder of oversized line in bounded chunks to avoid OOM
                    while True:
                        tail = stream.readline(65536)
                        if not tail or tail.endswith(b"\n"):
                            break
                    try:
                        target_queue.put(_OVERSIZED_FRAME_MARKER, timeout=1.0)
                    except queue.Full:
                        pass
                    continue
                try:
                    target_queue.put(chunk, timeout=1.0)
                except queue.Full:
                    # Drop oldest queued frame if consumer is wedged, never OOM
                    try:
                        target_queue.get_nowait()
                        target_queue.put_nowait(chunk)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                target_queue.put_nowait(None)
            except Exception:
                pass

    @staticmethod
    def _pump_stderr(
        stream,
        tail_buf: deque[str],
        extra_secrets: tuple[str, ...] = (),
    ) -> None:
        try:
            while True:
                line = stream.readline(MAX_MCP_STDERR_LINE_BYTES)
                if not line:
                    break
                if len(line) >= MAX_MCP_STDERR_LINE_BYTES and not line.endswith(b"\n"):
                    while True:
                        tail = stream.readline(4096)
                        if not tail or tail.endswith(b"\n"):
                            break
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded:
                    tail_buf.append(_redact_mcp_text(decoded, extra_secrets=extra_secrets))
        except Exception:
            pass

    def connect_sync(self, cancel_event: Optional[threading.Event] = None) -> list[MCPToolSpec]:
        """Start the MCP server subprocess, run JSON-RPC handshake, and fetch tools."""
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError(f"MCP server '{self.name}' connection cancelled")
        if self.connected and self._proc is not None and self._proc.poll() is None:
            return list(self.tools)

        self.disconnect_sync()
        self._closing = False
        self._stdout_queue = queue.Queue(maxsize=256)
        self._stderr_tail.clear()

        proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=expand_mcp_env(self.env),
        )
        self._proc = proc
        self._stdout_thread = threading.Thread(
            target=self._pump_stdout,
            args=(proc.stdout, self._stdout_queue),
            name=f"capri-mcp-out-{self.name}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._pump_stderr,
            args=(proc.stderr, self._stderr_tail, self._extra_secrets),
            name=f"capri-mcp-err-{self.name}",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        try:
            init_res = self._request_sync(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "capri-ai-companion", "version": "3.1.0"},
                },
                cancel_event=cancel_event,
            )
            if isinstance(init_res, dict):
                self.server_info = init_res.get("serverInfo") or {}
            self._notify_sync("notifications/initialized", {})
            self.tools = self.list_tools_sync(cancel_event=cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError(f"MCP server '{self.name}' connection cancelled")
            self.connected = True
            return list(self.tools)
        except Exception:
            self.disconnect_sync()
            raise

    async def connect(self) -> list[MCPToolSpec]:
        return await asyncio.to_thread(self.connect_sync)

    def disconnect_sync(self) -> None:
        self.connected = False
        self._closing = True
        proc = self._proc
        self._proc = None
        stdout_t = self._stdout_thread
        stderr_t = self._stderr_thread
        self._stdout_thread = None
        self._stderr_thread = None
        try:
            self._stdout_queue.put_nowait(None)
        except Exception:
            pass

        if proc is not None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.5)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe and not pipe.closed:
                        pipe.close()
                except Exception:
                    pass

        for t in (stdout_t, stderr_t):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=0.5)

    async def disconnect(self) -> None:
        await asyncio.to_thread(self.disconnect_sync)

    def _send_line_sync(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise RuntimeError(f"MCP server '{self.name}' is not running")
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(raw)
            proc.stdin.flush()
        except BrokenPipeError as e:
            self.connected = False
            raise RuntimeError(f"MCP server '{self.name}' closed stdin unexpectedly") from e

    def _notify_sync(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            self._send_line_sync(msg)

    def _request_sync(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Any:
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdout is None or proc.stdin is None or proc.poll() is not None:
                self.connected = False
                raise RuntimeError(f"MCP server '{self.name}' is not connected")
            self._req_id += 1
            req_id = self._req_id
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                payload["params"] = params
            self._send_line_sync(payload)

            deadline = time.monotonic() + self.timeout
            while True:
                if self._closing or (cancel_event is not None and cancel_event.is_set()):
                    raise RuntimeError(f"MCP server '{self.name}' cancelled while waiting for '{method}'")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"MCP server '{self.name}' timed out waiting for '{method}'")
                try:
                    raw_line = self._stdout_queue.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    if proc.poll() is not None and self._stdout_queue.empty():
                        self.connected = False
                        err_hint = f" ({self._stderr_tail[-1]})" if self._stderr_tail else ""
                        raise RuntimeError(
                            f"MCP server '{self.name}' exited unexpectedly (code={proc.returncode}){err_hint}"
                        )
                    continue

                if raw_line is None:
                    self.connected = False
                    err_hint = f" ({self._stderr_tail[-1]})" if self._stderr_tail else ""
                    raise RuntimeError(f"MCP server '{self.name}' closed stdout unexpectedly{err_hint}")

                if raw_line == _OVERSIZED_FRAME_MARKER:
                    raise RuntimeError(
                        f"MCP server '{self.name}' exceeded max frame size ({MAX_MCP_FRAME_BYTES} bytes)"
                    )

                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    # Skip non-JSON log noise emitted on stdout
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") != req_id:
                    # Ignore async notifications / unrelated frames
                    continue
                if "error" in msg and msg["error"] is not None:
                    err_obj = msg["error"]
                    err_msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
                    raise RuntimeError(
                        f"MCP error ({method}): {_redact_mcp_text(err_msg, extra_secrets=self._extra_secrets)}"
                    )
                return msg.get("result")

    def list_tools_sync(self, cancel_event: Optional[threading.Event] = None) -> list[MCPToolSpec]:
        res = self._request_sync("tools/list", {}, cancel_event=cancel_event)
        raw_tools = res.get("tools", []) if isinstance(res, dict) else []
        specs: list[MCPToolSpec] = []
        for item in raw_tools:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            r_name = str(item["name"]).strip()
            q_name = f"mcp__{self.name}__{sanitize_mcp_identifier(r_name)}"
            desc = str(item.get("description") or f"MCP tool {r_name} from {self.name}").strip()
            schema = item.get("inputSchema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}, "required": []}
            specs.append(
                MCPToolSpec(
                    server_name=self.name,
                    remote_name=r_name,
                    qualified_name=q_name,
                    description=f"[MCP:{self.name}] {desc}",
                    input_schema=schema,
                )
            )
        return specs

    async def list_tools(self) -> list[MCPToolSpec]:
        return await asyncio.to_thread(self.list_tools_sync)

    def call_tool_sync(self, remote_name: str, arguments: Optional[dict[str, Any]] = None) -> str:
        res = self._request_sync(
            "tools/call",
            {"name": remote_name, "arguments": dict(arguments or {})},
        )
        return format_mcp_call_result(res)

    async def call_tool(self, remote_name: str, arguments: Optional[dict[str, Any]] = None) -> str:
        return await asyncio.to_thread(self.call_tool_sync, remote_name, arguments)


class MCPManager:
    """Manages multiple configured MCP stdio servers and binds their tools into Capri's `ToolRegistry`."""

    def __init__(self, tool_registry=None):
        self.tool_registry = tool_registry
        self.clients: dict[str, MCPStdioClient] = {}
        self.errors: dict[str, str] = {}
        self._state_lock = threading.Lock()
        self._connecting_client: Optional[MCPStdioClient] = None

    def cancel_inflight_connect(self) -> None:
        """Immediately disconnect any server currently in the middle of a connect handshake."""
        with self._state_lock:
            cl = self._connecting_client
        if cl is not None:
            try:
                cl.disconnect_sync()
            except Exception:
                pass

    def connect_from_config_sync(
        self,
        mcp_config,
        cancel_event: Optional[threading.Event] = None,
    ) -> dict[str, str]:
        """Connect all enabled servers from `MCPConfig` and register their tools (loop-independent & cancellable)."""
        self.shutdown_sync()
        self.errors.clear()
        summary: dict[str, str] = {}

        if not mcp_config or not getattr(mcp_config, "enabled", False):
            return summary

        servers = getattr(mcp_config, "servers", []) or []
        for srv in servers:
            if cancel_event is not None and cancel_event.is_set():
                break
            if not getattr(srv, "enabled", True):
                continue
            s_name = sanitize_mcp_identifier(getattr(srv, "name", "server"))
            cmd = (getattr(srv, "command", "") or "").strip()
            if not cmd:
                self.errors[s_name] = "не указана команда command"
                summary[s_name] = "ERR: пустой command"
                continue
            client = MCPStdioClient(
                name=s_name,
                command=cmd,
                args=list(getattr(srv, "args", []) or []),
                env=dict(getattr(srv, "env", {}) or {}),
                timeout=float(getattr(srv, "timeout", 20.0) or 20.0),
            )
            with self._state_lock:
                self._connecting_client = client
            try:
                specs = client.connect_sync(cancel_event=cancel_event)
                if cancel_event is not None and cancel_event.is_set():
                    client.disconnect_sync()
                    break
                with self._state_lock:
                    self.clients[s_name] = client
                self._register_client_tools(client, specs)
                summary[s_name] = f"OK ({len(specs)} tools)"
            except Exception as e:
                client.disconnect_sync()
                if cancel_event is not None and cancel_event.is_set():
                    break
                safe_err = _redact_mcp_text(str(e), extra_secrets=client._extra_secrets)
                self.errors[s_name] = safe_err
                summary[s_name] = f"ERR: {safe_err}"
            finally:
                with self._state_lock:
                    if self._connecting_client is client:
                        self._connecting_client = None

        if cancel_event is not None and cancel_event.is_set():
            self.shutdown_sync()
        return summary

    async def connect_from_config(
        self,
        mcp_config,
        cancel_event: Optional[threading.Event] = None,
    ) -> dict[str, str]:
        """Async wrapper around `connect_from_config_sync` safe across arbitrary event loops."""
        return await asyncio.to_thread(self.connect_from_config_sync, mcp_config, cancel_event)

    def _register_client_tools(self, client: MCPStdioClient, specs: list[MCPToolSpec]) -> None:
        if self.tool_registry is None:
            return
        for spec in specs:
            if not spec.qualified_name.startswith("mcp__"):
                logger.warning(f"Skipping invalid MCP tool name without mcp__ prefix: {spec.qualified_name}")
                continue
            def _make_invoker(bound_client: MCPStdioClient, bound_remote: str):
                async def _invoke(**kwargs) -> str:
                    return await bound_client.call_tool(bound_remote, kwargs)
                return _invoke

            self.tool_registry.register_external_tool(
                tool_name=spec.qualified_name,
                func=_make_invoker(client, spec.remote_name),
                description=spec.description,
                parameters_schema=spec.input_schema,
            )

    def list_registered_tools(self) -> list[MCPToolSpec]:
        out: list[MCPToolSpec] = []
        for client in self.clients.values():
            if client.connected:
                out.extend(client.tools)
        return out

    def shutdown_sync(self) -> None:
        self.cancel_inflight_connect()
        with self._state_lock:
            to_close = list(self.clients.values())
            self.clients.clear()
        for client in to_close:
            try:
                client.disconnect_sync()
            except Exception:
                pass
        if self.tool_registry is not None and hasattr(self.tool_registry, "unregister_prefix"):
            self.tool_registry.unregister_prefix("mcp__")

    async def shutdown(self) -> None:
        await asyncio.to_thread(self.shutdown_sync)

    def format_status_markdown(self, mcp_config=None) -> str:
        enabled = bool(getattr(mcp_config, "enabled", False)) if mcp_config else False
        configured = len(getattr(mcp_config, "servers", []) or []) if mcp_config else 0
        lines = [
            f"**CAPRI // MCP CLIENT** (в конфиге: `{'ON' if enabled else 'OFF'}`, серверов: `{configured}`, подключено: `{len(self.clients)}`)"
        ]
        for s_name, cl in self.clients.items():
            t_names = ", ".join(t.remote_name for t in cl.tools) or "нет инструментов"
            lines.append(f"- `● OK` **{s_name}:** {len(cl.tools)} тулов ({t_names})")
        for s_name, err in self.errors.items():
            lines.append(f"- `✕ ERR` **{s_name}:** {err}")
        if not self.clients and not self.errors:
            lines.append("- Подключённых MCP-серверов нет. Настрой секцию `mcp:` в `config.yaml` и выполни `/mcp connect`.")
        return "\n".join(lines)
