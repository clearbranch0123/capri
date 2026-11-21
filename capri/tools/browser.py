"""
CAPRI // Isolated Browser Engine ("Her Own Browser", v3.0 — Wave 6).

Gives Capri her own dedicated browser profile & HTTP/headless-DOM session completely
isolated from the user's personal browser cookies, logins, tabs, and window focus.
Security guarantees:
- Strict URL validation (`http://` and `https://` only).
- Single-DNS-resolution IP pinning (`_SSRFGuardedNetworkBackend` for `httpx`) eliminating
  DNS rebinding TOCTOU windows while preserving native TLS SNI/certificate validation.
- Ephemeral local HTTP/CONNECT `_SSRFGuardProxy` enforced on Headless Chromium via
  `--proxy-server`, `--proxy-bypass-list=<-loopback>`, `--host-resolver-rules`, and
  `--disable-quic` so redirects and subresources (`<img>`, `<iframe>`, `fetch()`) CANNOT
  reach localhost, loopback, link-local/metadata, CGNAT, or RFC1918 private IPs.
- Isolated profile directory under `~/.config/capri/browser_profile/`.
- Extracted page text explicitly framed as untrusted external web content.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import shutil
import socket
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpcore
import httpx

from ..core.config import USER_CONFIG_DIR
from .registry import tool
from .web import USER_AGENT, _clean_html_text, escape_untrusted_tag

logger = logging.getLogger(__name__)

CAPRI_BROWSER_PROFILE_DIR = USER_CONFIG_DIR / "browser_profile"

_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\'#][^"\']*)["\'][^>]*>([\s\S]*?)</a>', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title\b[^>]*>([\s\S]*?)</title>", re.IGNORECASE)
_SHORTHAND_IPV4_RE = re.compile(r"^[0-9a-fxX.]+$")
_SSRF_PROXY_BLOCK_MARKER = "Blocked by SSRFGuard"


class SSRFBlockedError(RuntimeError):
    """Raised when an outgoing connection attempts to reach a forbidden/internal address."""


@dataclass(frozen=True)
class ResolvedHost:
    ok: bool
    ips: tuple[str, ...] = ()
    error: str = ""
    blocked: bool = False


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if an IP is non-global / loopback / private / link-local / metadata / CGNAT / mapped internal."""
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            return _is_forbidden_ip(ip_obj.ipv4_mapped)
        if ip_obj.sixtofour is not None and _is_forbidden_ip(ip_obj.sixtofour):
            return True
        if ip_obj.teredo is not None:
            srv, cli = ip_obj.teredo
            if _is_forbidden_ip(srv) or _is_forbidden_ip(cli):
                return True
    return bool(
        not ip_obj.is_global
        or ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _parse_ip_literal(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse standard IPv4/IPv6 literals (with optional brackets/zone) and shorthand numeric IPv4."""
    bare = (host or "").strip()
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1]
    if "%" in bare:
        bare = bare.split("%", 1)[0]
    if not bare:
        return None
    try:
        return ipaddress.ip_address(bare)
    except ValueError:
        pass
    # Catch obfuscated numeric/octal/hex/shorthand IPv4 literals (e.g. 2130706433, 0x7f000001, 127.1)
    if _SHORTHAND_IPV4_RE.match(bare):
        try:
            packed = socket.inet_aton(bare)
            return ipaddress.IPv4Address(packed)
        except OSError:
            pass
    return None


def validate_and_resolve_host(host: str, port: int = 80, resolve_dns: bool = True) -> ResolvedHost:
    """
    Validate a hostname/IP and resolve DNS ONCE, verifying EVERY returned IP is strictly global/public.

    Returns `ResolvedHost` with ordered deduplicated public IP literals (`ips`) suitable for direct
    pinned TCP connections (eliminating DNS rebinding TOCTOU).
    """
    raw_host = (host or "").strip().lower()
    if raw_host.startswith("[") and raw_host.endswith("]"):
        raw_host = raw_host[1:-1]
    clean_host = raw_host.rstrip(".")
    if not clean_host:
        return ResolvedHost(ok=False, error="Не указан хост в URL.", blocked=True)

    if clean_host in ("localhost", "localhost.localdomain") or clean_host.endswith(
        (".localhost", ".local", ".internal")
    ):
        return ResolvedHost(
            ok=False,
            error=f"Доступ к локальному хосту '{clean_host}' заблокирован политикой безопасности.",
            blocked=True,
        )

    ip_lit = _parse_ip_literal(clean_host)
    if ip_lit is not None:
        if _is_forbidden_ip(ip_lit):
            return ResolvedHost(
                ok=False,
                error=f"Доступ к приватному/локальному IP '{clean_host}' заблокирован политикой безопасности.",
                blocked=True,
            )
        return ResolvedHost(ok=True, ips=(str(ip_lit),))

    if not resolve_dns:
        return ResolvedHost(ok=True, ips=())

    try:
        infos = socket.getaddrinfo(clean_host, int(port), type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return ResolvedHost(ok=False, error=f"Не удалось разрешить DNS для '{clean_host}': {e}", blocked=False)
    except Exception as e:
        return ResolvedHost(ok=False, error=f"Ошибка DNS для '{clean_host}': {e}", blocked=False)

    if not infos:
        return ResolvedHost(ok=False, error=f"DNS не вернул адресов для '{clean_host}'.", blocked=False)

    v4_ips: list[str] = []
    v6_ips: list[str] = []
    seen: set[str] = set()
    for _fam, _type, _proto, _canon, sockaddr in infos:
        addr_raw = str(sockaddr[0])
        addr_clean = addr_raw.split("%", 1)[0]
        try:
            ip_resolved = ipaddress.ip_address(addr_clean)
        except ValueError:
            continue
        if _is_forbidden_ip(ip_resolved):
            return ResolvedHost(
                ok=False,
                error=f"Хост '{clean_host}' резолвится во внутренний IP '{addr_clean}' — доступ заблокирован.",
                blocked=True,
            )
        norm = str(ip_resolved)
        if norm in seen:
            continue
        seen.add(norm)
        if isinstance(ip_resolved, ipaddress.IPv4Address):
            v4_ips.append(norm)
        else:
            v6_ips.append(norm)

    ordered = tuple(v4_ips + v6_ips)
    if not ordered:
        return ResolvedHost(ok=False, error=f"Не удалось получить публичный IP для '{clean_host}'.", blocked=False)
    return ResolvedHost(ok=True, ips=ordered)


def validate_browser_url(url: str, resolve_dns: bool = True) -> tuple[bool, str]:
    """
    Validate a URL strictly before Capri's isolated browser visits it.
    Blocks non-HTTP(S) schemes and loopback/private/link-local addresses (SSRF protection).
    """
    raw = (url or "").strip()
    if not raw:
        return False, "Пустой URL."
    parsed = urllib.parse.urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"Запрещённая схема URL '{parsed.scheme}': разрешены только http:// и https://."
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "Не указан хост в URL."
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False, "Некорректный порт в URL."

    resolved = validate_and_resolve_host(host, port=port, resolve_dns=resolve_dns)
    if resolved.blocked:
        return False, resolved.error
    return True, raw


class _SSRFGuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """
    `httpcore` network backend that resolves DNS ONCE inside `connect_tcp`, blocks
    non-global/internal IPs, and connects directly to the pinned public IP literal.

    This eliminates DNS rebinding TOCTOU while keeping native HTTPS SNI, Host headers,
    and TLS certificate hostname validation intact.
    """

    def __init__(self, inner: Optional[httpcore.AsyncNetworkBackend] = None):
        if inner is None:
            from httpcore._backends.auto import AutoBackend

            inner = AutoBackend()
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options=None,
    ):
        resolved = await asyncio.to_thread(validate_and_resolve_host, host, port, True)
        if not resolved.ok:
            if resolved.blocked:
                raise SSRFBlockedError(resolved.error)
            raise httpcore.ConnectError(resolved.error)

        last_exc: Optional[Exception] = None
        for pinned_ip in resolved.ips:
            try:
                return await self._inner.connect_tcp(
                    pinned_ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise httpcore.ConnectError(f"Не удалось подключиться к '{host}:{port}'.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options=None,
    ):
        raise SSRFBlockedError(f"Подключение к локальному сокету '{path}' заблокировано политикой безопасности.")

    async def sleep(self, seconds: float) -> None:
        return await self._inner.sleep(seconds)


class _SSRFGuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """Isolated httpx transport backed by `_SSRFGuardedNetworkBackend` (no env proxy, no retries)."""

    def __init__(self, network_backend: Optional[httpcore.AsyncNetworkBackend] = None, **kwargs):
        kwargs["trust_env"] = False
        kwargs["retries"] = 0
        super().__init__(**kwargs)
        ssl_ctx = getattr(self._pool, "_ssl_context", None)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_ctx,
            http1=True,
            http2=False,
            retries=0,
            network_backend=network_backend or _SSRFGuardedNetworkBackend(),
        )


def _parse_connect_authority(target: str) -> tuple[str, int]:
    """Parse `host:port` or `[ipv6]:port` from an HTTP CONNECT request target."""
    t = (target or "").strip()
    if t.startswith("["):
        end_bracket = t.find("]")
        if end_bracket == -1:
            return "", 0
        host = t[1:end_bracket]
        rest = t[end_bracket + 1 :]
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
            except ValueError:
                return "", 0
        else:
            port = 443
        return host, port
    if ":" in t:
        host_part, port_part = t.rsplit(":", 1)
        try:
            port = int(port_part)
        except ValueError:
            return "", 0
        return host_part, port
    return t, 443


class _SSRFGuardProxy:
    """
    Ephemeral local HTTP/CONNECT proxy bound to `127.0.0.1:<ephemeral_port>` used to police
    EVERY network request made by Headless Chromium (navigation, redirects, subresources, fetch/XHR).

    Every request resolves DNS ONCE via `validate_and_resolve_host()`, rejects forbidden/internal
    destinations with `403 Forbidden`, and dials the pinned public IP directly.
    """

    def __init__(self, upstream_timeout: float = 8.0):
        self.upstream_timeout = upstream_timeout
        self.port: int = 0
        self.blocked_reasons: list[str] = []
        self.allowed_targets: list[tuple[str, int, str]] = []
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_tasks: set[asyncio.Task] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._on_client, host="127.0.0.1", port=0)
        sock = self._server.sockets[0]
        self.port = int(sock.getsockname()[1])
        return self.port

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        for task in list(self._active_tasks):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        self._active_tasks.clear()

    async def __aenter__(self) -> "_SSRFGuardProxy":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.stop()
        return False

    def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    @staticmethod
    async def _send_simple_response(writer: asyncio.StreamWriter, status: str, body: str = "") -> None:
        data = body.encode("utf-8", errors="replace")
        raw = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii") + data
        try:
            writer.write(raw)
            await writer.drain()
        except Exception:
            pass

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(16384)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except Exception:
            pass
        finally:
            try:
                if dst.can_write_eof():
                    dst.write_eof()
            except Exception:
                pass

    async def _open_pinned_upstream(self, host: str, port: int, ips: tuple[str, ...]):
        last_exc: Optional[Exception] = None
        for ip_str in ips:
            try:
                conn = await asyncio.wait_for(
                    asyncio.open_connection(ip_str, port),
                    timeout=self.upstream_timeout,
                )
                self.allowed_targets.append((host, port, ip_str))
                return conn
            except Exception as exc:
                last_exc = exc
        raise last_exc or OSError(f"Unable to connect to {host}:{port}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer: Optional[asyncio.StreamWriter] = None
        try:
            try:
                header_blob = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                return
            if len(header_blob) > 65536:
                await self._send_simple_response(writer, "431 Request Header Fields Too Large")
                return

            lines = header_blob.decode("iso-8859-1", errors="replace").split("\r\n")
            if not lines or not lines[0]:
                return
            req_parts = lines[0].split()
            if len(req_parts) < 3:
                await self._send_simple_response(writer, "400 Bad Request")
                return
            method, target, version = req_parts[0].upper(), req_parts[1], req_parts[2]
            raw_headers = [ln for ln in lines[1:] if ln and ":" in ln]

            if method == "CONNECT":
                host, port = _parse_connect_authority(target)
                if not host or not (1 <= port <= 65535):
                    await self._send_simple_response(writer, "400 Bad Request")
                    return
                resolved = await asyncio.to_thread(validate_and_resolve_host, host, port, True)
                if not resolved.ok:
                    if resolved.blocked:
                        self.blocked_reasons.append(resolved.error)
                        await self._send_simple_response(
                            writer, "403 Forbidden", f"{_SSRF_PROXY_BLOCK_MARKER}: {resolved.error}"
                        )
                    else:
                        await self._send_simple_response(writer, "502 Bad Gateway", resolved.error)
                    return

                try:
                    up_reader, upstream_writer = await self._open_pinned_upstream(host, port, resolved.ips)
                except Exception as exc:
                    await self._send_simple_response(writer, "502 Bad Gateway", str(exc))
                    return

                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(
                    self._pipe(reader, upstream_writer),
                    self._pipe(up_reader, writer),
                    return_exceptions=True,
                )
                return

            # Plain HTTP proxy request: `GET http://host[:port]/path HTTP/1.1`
            parsed = urllib.parse.urlparse(target)
            if (parsed.scheme or "").lower() != "http" or not parsed.hostname:
                reason = f"Запрещённая схема/цель прокси-запроса '{target}'."
                self.blocked_reasons.append(reason)
                await self._send_simple_response(writer, "403 Forbidden", f"{_SSRF_PROXY_BLOCK_MARKER}: {reason}")
                return

            host = parsed.hostname
            try:
                port = parsed.port or 80
            except ValueError:
                await self._send_simple_response(writer, "400 Bad Request")
                return
            if not (1 <= port <= 65535):
                await self._send_simple_response(writer, "400 Bad Request")
                return

            resolved = await asyncio.to_thread(validate_and_resolve_host, host, port, True)
            if not resolved.ok:
                if resolved.blocked:
                    self.blocked_reasons.append(resolved.error)
                    await self._send_simple_response(
                        writer, "403 Forbidden", f"{_SSRF_PROXY_BLOCK_MARKER}: {resolved.error}"
                    )
                else:
                    await self._send_simple_response(writer, "502 Bad Gateway", resolved.error)
                return

            content_length = 0
            filtered_headers: list[str] = []
            has_host_header = False
            for hline in raw_headers:
                hname, hval = hline.split(":", 1)
                hlow = hname.strip().lower()
                if hlow in ("proxy-connection", "proxy-authorization", "connection", "keep-alive"):
                    continue
                if hlow == "host":
                    has_host_header = True
                elif hlow == "content-length":
                    try:
                        content_length = max(0, int(hval.strip()))
                    except ValueError:
                        content_length = 0
                filtered_headers.append(f"{hname.strip()}: {hval.strip()}")

            if not has_host_header:
                host_hdr = parsed.netloc or (f"{host}:{port}" if port != 80 else host)
                filtered_headers.insert(0, f"Host: {host_hdr}")
            filtered_headers.append("Connection: close")

            body = b""
            if content_length > 2 * 1024 * 1024:
                await self._send_simple_response(writer, "413 Payload Too Large")
                return
            if content_length > 0:
                try:
                    body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)
                except Exception:
                    await self._send_simple_response(writer, "400 Bad Request")
                    return

            try:
                up_reader, upstream_writer = await self._open_pinned_upstream(host, port, resolved.ips)
            except Exception as exc:
                await self._send_simple_response(writer, "502 Bad Gateway", str(exc))
                return

            origin_path = parsed.path or "/"
            if parsed.query:
                origin_path = f"{origin_path}?{parsed.query}"
            req_out = (
                f"{method} {origin_path} {version}\r\n"
                + "\r\n".join(filtered_headers)
                + "\r\n\r\n"
            ).encode("iso-8859-1", errors="replace") + body
            upstream_writer.write(req_out)
            await upstream_writer.drain()
            await self._pipe(up_reader, writer)
        except Exception:
            pass
        finally:
            for w in (upstream_writer, writer):
                if w is not None:
                    try:
                        w.close()
                        await w.wait_closed()
                    except Exception:
                        pass


def _build_headless_chromium_cmd(chrome_bin: str, profile_dir: Path, proxy_port: int, url: str) -> list[str]:
    """Build hardened Headless Chromium argv forcing ALL DNS and HTTP(S) traffic through `_SSRFGuardProxy`."""
    return [
        chrome_bin,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=AsyncDns",
        f"--proxy-server=http://127.0.0.1:{int(proxy_port)}",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--dump-dom",
        url,
    ]


def extract_page_title_and_links(raw_html: str, base_url: str, max_links: int = 12) -> tuple[str, list[tuple[str, str]]]:
    """Extract clean page title and deduplicated absolute http(s) links from HTML."""
    title = ""
    tm = _TITLE_RE.search(raw_html or "")
    if tm:
        title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", tm.group(1))).split()).strip()

    links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for href_raw, inner_html in _LINK_RE.findall(raw_html or ""):
        href_clean = html.unescape(href_raw).strip()
        if not href_clean or href_clean.lower().startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        abs_url = urllib.parse.urljoin(base_url, href_clean)
        p = urllib.parse.urlparse(abs_url)
        if p.scheme.lower() not in ("http", "https"):
            continue
        if abs_url in seen_urls:
            continue
        label = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", inner_html)).split()).strip()
        if not label or len(label) < 2:
            continue
        if len(label) > 90:
            label = label[:87].rstrip() + "…"
        seen_urls.add(abs_url)
        links.append((label, abs_url))
        if len(links) >= max_links:
            break

    return title, links


def detect_headless_chromium() -> Optional[str]:
    """Return available Chromium-family binary for isolated headless DOM rendering, if installed."""
    for bin_name in ("chromium", "google-chrome", "google-chrome-stable", "brave", "vivaldi-stable"):
        found = shutil.which(bin_name)
        if found:
            return found
    return None


@dataclass
class IsolatedBrowserSession:
    """Stateful isolated browser session belonging solely to Capri."""

    profile_dir: Path = field(default_factory=lambda: CAPRI_BROWSER_PROFILE_DIR)
    current_url: str = ""
    current_title: str = ""
    current_links: list[tuple[str, str]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    cookies: dict[str, str] = field(default_factory=dict)
    last_backend: str = "httpx"

    def ensure_profile_dir(self) -> Path:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        return self.profile_dir

    async def _fetch_dom_headless(self, url: str, timeout: float = 12.0) -> tuple[Optional[str], Optional[str]]:
        """
        Render DOM using Headless Chromium strictly confined behind `_SSRFGuardProxy`.
        Returns `(dom_html, ssrf_block_error)`.
        """
        chrome = detect_headless_chromium()
        if not chrome:
            return None, None
        prof = self.ensure_profile_dir()
        proc: Optional[asyncio.subprocess.Process] = None
        async with _SSRFGuardProxy() as proxy:
            cmd = _build_headless_chromium_cmd(chrome, prof, proxy.port, url)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                decoded = out.decode("utf-8", errors="replace") if out else ""
                if proxy.blocked_reasons and (
                    not proxy.allowed_targets
                    or not decoded.strip()
                    or _SSRF_PROXY_BLOCK_MARKER in decoded
                    or "ERR_TUNNEL_CONNECTION_FAILED" in decoded
                    or "ERR_PROXY_CONNECTION_FAILED" in decoded
                    or "chrome-error://" in decoded
                ):
                    return None, proxy.blocked_reasons[0]
                if proc.returncode == 0 and decoded:
                    return decoded, None
            except asyncio.TimeoutError:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass
                if proxy.blocked_reasons:
                    return None, proxy.blocked_reasons[0]
                logger.debug(f"Headless Chromium timed out for {url}")
            except Exception as e:
                logger.debug(f"Headless Chromium fallback to httpx for {url}: {e}")
        return None, None

    async def _fetch_html_httpx(self, url: str, timeout: float = 10.0) -> tuple[Optional[str], str, Optional[str]]:
        """
        Fetch via isolated httpx client with Capri-only cookies, single-DNS pinned IP transport,
        and per-hop redirect SSRF validation.
        """
        headers = {"User-Agent": USER_AGENT}
        transport = _SSRFGuardedAsyncTransport()
        async with httpx.AsyncClient(
            headers=headers,
            cookies=self.cookies,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            timeout=timeout,
        ) as client:
            cur_url = url
            for _ in range(5):
                # Validate scheme/host/IP literal without pre-resolving DNS;
                # DNS resolution + IP pinning happens ONCE inside `_SSRFGuardedNetworkBackend`.
                ok, msg = validate_browser_url(cur_url, resolve_dns=False)
                if not ok:
                    return None, cur_url, msg
                try:
                    resp = await client.get(cur_url)
                except SSRFBlockedError as e:
                    return None, cur_url, str(e)
                except httpx.HTTPError as e:
                    return None, cur_url, f"Ошибка сети при загрузке {cur_url}: {e}"

                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        break
                    cur_url = urllib.parse.urljoin(cur_url, loc)
                    continue
                if resp.status_code != 200:
                    return None, cur_url, f"HTTP {resp.status_code} при загрузке {cur_url}."
                ctype = (resp.headers.get("content-type") or "").lower()
                if not any(t in ctype for t in ("text", "html", "json", "xml")):
                    return None, cur_url, f"Нетекстовый контент ({ctype})."
                for k, v in resp.cookies.items():
                    self.cookies[k] = v
                return resp.text, cur_url, None
        return None, url, "Слишком много перенаправлений."

    async def visit(self, url: str, max_chars: int = 3500, use_js: bool = False) -> str:
        # Fast syntax/scheme/localhost/literal-IP check (no extra pre-DNS lookup before connect)
        ok, checked = validate_browser_url(url, resolve_dns=False)
        if not ok:
            return f"Изолированный браузер отклонил URL: {checked}"

        raw_html: Optional[str] = None
        final_url = checked
        if use_js:
            raw_html, ssrf_err = await self._fetch_dom_headless(checked)
            if ssrf_err:
                return f"Изолированный браузер отклонил URL: {ssrf_err}"
            if raw_html is not None:
                self.last_backend = "headless-chromium"

        if raw_html is None:
            raw_html, final_url, err = await self._fetch_html_httpx(checked)
            self.last_backend = "httpx-isolated"
            if err or raw_html is None:
                return f"Изолированный браузер: {err or 'не удалось загрузить страницу.'}"

        title, links = extract_page_title_and_links(raw_html, final_url, max_links=10)
        clean_text = _clean_html_text(raw_html, max_chars=max(500, min(8000, int(max_chars))))
        safe_body = escape_untrusted_tag(clean_text or "(пустая страница)", "web_page_content")
        safe_title = escape_untrusted_tag(title or final_url, "web_page_content")

        self.current_url = final_url
        self.current_title = safe_title
        self.current_links = links
        if not self.history or self.history[-1] != final_url:
            self.history.append(final_url)
            self.history = self.history[-30:]

        links_block = ""
        if links:
            links_lines = [f"  [{i}] {escape_untrusted_tag(lbl, 'web_page_content')} -> {href}" for i, (lbl, href) in enumerate(links, 1)]
            links_block = "\n\nСсылки на странице (можно перейти через capri_browser_follow_link):\n" + "\n".join(links_lines)

        return (
            f"Изолированный браузер Capri ({self.last_backend})\n"
            f"Заголовок: {self.current_title}\n"
            f"URL: {final_url}\n\n"
            f"<web_page_content untrusted=\"true\">\n{safe_body}\n</web_page_content>"
            f"{links_block}"
        )

    async def follow_link(self, index: int, max_chars: int = 3500, use_js: bool = False) -> str:
        if not self.current_links:
            return "В изолированном браузере нет сохранённых ссылок. Сначала открой страницу через capri_browser_visit или capri_browser_search."
        idx = int(index)
        if idx < 1 or idx > len(self.current_links):
            return f"Неверный номер ссылки {idx}. Доступны номера 1..{len(self.current_links)}."
        _label, target_url = self.current_links[idx - 1]
        return await self.visit(target_url, max_chars=max_chars, use_js=use_js)

    def clear(self) -> str:
        self.current_url = ""
        self.current_title = ""
        self.current_links.clear()
        self.history.clear()
        self.cookies.clear()
        if self.profile_dir.exists():
            try:
                shutil.rmtree(self.profile_dir)
            except OSError as e:
                return f"История и куки в памяти очищены, но профиль на диске удалить не удалось: {e}"
        return "Изолированный браузер Capri полностью очищен (история, куки и отдельный профиль удалены; твой личный браузер не затронут)."

    def status_summary(self) -> str:
        headless = detect_headless_chromium()
        engine_note = f"headless ({Path(headless).name}) + isolated httpx" if headless else "isolated httpx"
        cur = f"{self.current_title} ({self.current_url})" if self.current_url else "страница не открыта"
        return (
            f"Изолированный браузер Capri («Her Own Browser»):\n"
            f"• Движок: {engine_note} (последний: {self.last_backend})\n"
            f"• Текущая страница: {cur}\n"
            f"• Ссылок в буфере: {len(self.current_links)} · История: {len(self.history)} · Куки: {len(self.cookies)}\n"
            f"• Изолированный профиль: `{self.profile_dir}` (личный браузер пользователя не используется)"
        )


_shared_browser = IsolatedBrowserSession()


def get_isolated_browser() -> IsolatedBrowserSession:
    return _shared_browser


@tool(
    description="Открывает веб-страницу в собственном изолированном браузере Capri (не трогает личный браузер, вкладки, куки и фокус мыши пользователя). url: адрес http(s), use_js: true для JS-рендера через headless Chromium."
)
async def capri_browser_visit(url: str, max_chars: int = 3500, use_js: bool = False) -> str:
    return await _shared_browser.visit(url, max_chars=max_chars, use_js=bool(use_js))


@tool(
    description="Поиск в сети через изолированную сессию Capri с сохранением пронумерованных ссылок для последующего перехода через capri_browser_follow_link."
)
async def capri_browser_search(query: str, max_results: int = 5) -> str:
    from .web import web_search

    raw = await web_search(query, max_results=max(1, min(10, int(max_results))))
    found_links: list[tuple[str, str]] = []
    for title, href in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", raw):
        found_links.append((title.strip(), href.strip()))
    _shared_browser.current_title = f"Поиск: {query.strip()}"
    _shared_browser.current_url = f"search://{urllib.parse.quote(query.strip())}"
    _shared_browser.current_links = found_links
    return raw + (
        "\n\n(Ссылки сохранены в изолированном браузере Capri — можно открыть любую через `capri_browser_follow_link(index)`.)"
        if found_links
        else ""
    )


@tool(
    description="Переходит по пронумерованной ссылке [index] из текущей страницы или результатов поиска в изолированном браузере Capri."
)
async def capri_browser_follow_link(index: int, max_chars: int = 3500, use_js: bool = False) -> str:
    return await _shared_browser.follow_link(index=index, max_chars=max_chars, use_js=bool(use_js))


@tool(description="Показывает состояние изолированного браузера Capri (текущий URL, история, движок, изолированный профиль).")
async def capri_browser_state() -> str:
    return _shared_browser.status_summary()


@tool(description="Очищает историю, куки и изолированный профиль собственного браузера Capri (не затрагивает личный браузер пользователя).")
async def capri_browser_clear() -> str:
    return _shared_browser.clear()
