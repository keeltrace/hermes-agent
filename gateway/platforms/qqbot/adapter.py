"""QQ Bot platform adapter (Official QQ Bot API v2): WebSocket gateway for inbound
events, REST (``api.sgroup.qq.com``) for outbound messages and media uploads.

config.yaml ``platforms.qq.extra``: app_id / client_secret (or QQ_APP_ID /
QQ_CLIENT_SECRET), markdown_support, dm_policy + allow_from, group_policy +
group_allow_from (open | allowlist | disabled | pairing), and optional ``stt``
{provider, baseUrl, apiKey, model} (or QQ_STT_* env vars). Voice transcription
tries QQ's free ``asr_refer_text`` first, then the configured STT provider.
"""

from __future__ import annotations

from pm import install_hint
import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    gateway_trust_env, BasePlatformAdapter, ExecApprovalPrompt, SendResult,
    _ssrf_redirect_guard, cache_document_from_bytes_async, cache_image_from_bytes_async,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import strip_markdown
from gateway.platforms.helpers import MessageDeduplicator, cancel_task
from gateway.platforms.access_policy_mixin import OwnAccessPolicyMixin
from gateway.platforms.media_cache import ext_for_mime

logger = logging.getLogger(__name__)


class QQCloseError(Exception):
    """Raised when the QQ WebSocket closes; carries code + reason for the reconnect loop."""

    def __init__(self, code, reason=""):
        self.code = int(code) if code else None
        self.reason = str(reason) if reason else ""
        super().__init__(f"WebSocket closed (code={self.code}, reason={self.reason})")


from gateway.platforms.qqbot.constants import (
    API_BASE, TOKEN_URL, GATEWAY_URL_PATH, DEFAULT_API_TIMEOUT, FILE_UPLOAD_TIMEOUT,
    CONNECT_TIMEOUT_SECONDS, RECONNECT_BACKOFF, MAX_RECONNECT_ATTEMPTS, RATE_LIMIT_DELAY,
    QUICK_DISCONNECT_THRESHOLD, MAX_QUICK_DISCONNECT_COUNT, MAX_MESSAGE_LENGTH,
    DEDUP_WINDOW_SECONDS, DEDUP_MAX_SIZE, MSG_TYPE_TEXT, MSG_TYPE_MARKDOWN, MSG_TYPE_MEDIA,
    MSG_TYPE_INPUT_NOTIFY, MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO, MEDIA_TYPE_VOICE, MEDIA_TYPE_FILE)
from gateway.platforms.qqbot.utils import coerce_list as _coerce_list, build_user_agent
from gateway.platforms.qqbot.chunked_upload import (
    ChunkedUploader, UploadDailyLimitExceededError, UploadFileTooLargeError)
from gateway.platforms.qqbot.keyboards import (
    ApprovalRequest, InlineKeyboard, InteractionEvent, build_approval_keyboard,
    build_update_prompt_keyboard, parse_approval_button_data, parse_interaction_event,
    parse_update_prompt_button_data)
from gateway.platforms._shared import get_scoped_secret as _resolve_qq_secret


def check_qq_requirements() -> bool:
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


_VOICE_EXTENSIONS = (".silk", ".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".speex", ".flac")
_STT_PROVIDER_BASE_URLS = {
    "zai": "https://open.bigmodel.cn/api/coding/paas/v4",
    # Aliases that target direct REST APIs not modeled as first-class providers in PROVIDER_REGISTRY. Used
    # for ``auxiliary.<task>.provider`` so users can write the obvious name and have it resolve to a working
    # ``custom`` endpoint without needing to know our internal provider IDs. Why these specifically:
    # PROVIDER_REGISTRY has ``openai-codex`` (OAuth) and ``custom`` (manual base_url + OPENAI_API_KEY) but
    # no plain ``openai`` for direct API-key access. Users predictably type ``provider: openai`` and expect
    # it to use OPENAI_API_KEY against api.openai.com. Previously this silently fell back to the user's main
    # provider, sending OpenAI model names to e.g. DeepSeek and producing cryptic ``unknown variant
    # 'image_url'`` errors (issue #31179).
    "openai": "https://api.openai.com/v1",
    "glm": "https://open.bigmodel.cn/api/coding/paas/v4"}
_AUDIO_URL_EXTENSIONS = {".silk", ".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}


class QQAdapter(OwnAccessPolicyMixin, BasePlatformAdapter):
    """QQ Bot adapter backed by the official QQ Bot WebSocket Gateway + REST API."""

    # QQ Bot API does not support editing sent messages.
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    ALLOW_ALL_ENV_PREFIX = "QQ"
    _TYPING_INPUT_SECONDS = 60  # input_notify duration reported to QQ
    _TYPING_DEBOUNCE_SECONDS = 50  # refresh before it expires

    # WS close codes that are unrecoverable → stop reconnecting.
    _FATAL_CLOSE_CODES = {
        4001: "invalid opcode", 4002: "invalid payload", 4010: "invalid shard",
        4011: "sharding required", 4012: "invalid API version", 4013: "invalid intent",
        4014: "intent not authorized", 4914: "offline/sandbox-only", 4915: "banned"}
    # WS close codes that invalidate the session → clear it and re-identify on
    # the next Hello. 4009 (connection timeout) is deliberately absent: it is
    # resumable per the QQ protocol and must keep session state.
    _SESSION_INVALID_CLOSE_CODES = {4006, 4007} | set(range(4900, 4914))

    @property
    def _log_tag(self) -> str:
        """Log prefix including app_id for multi-instance disambiguation."""
        app_id = getattr(self, "_app_id", None)
        return f"QQBot:{app_id}" if app_id else "QQBot"

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending_responses.clear()

    def _mark_transport_disconnected(self) -> None:
        """Mark QQ WS down without stopping the reconnect loop (base's _running
        doubles as lifecycle flag; the listener must survive transient drops)."""
        if self.has_fatal_error:
            return
        self._write_runtime_status_safe(
            "disconnected", platform_state="disconnected", error_code=None, error_message=None)

    @property
    def is_connected(self) -> bool:
        """Return True only when the QQ WebSocket transport is usable."""
        return bool(self._running and self._ws and not self._ws.closed)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.QQBOT)

        extra = config.extra or {}
        self._app_id = str(extra.get("app_id") or _resolve_qq_secret("QQ_APP_ID", "")).strip()
        self._client_secret = str(extra.get("client_secret") or _resolve_qq_secret("QQ_CLIENT_SECRET", "")).strip()
        self._markdown_support = bool(extra.get("markdown_support", True))
        self._dm_policy = str(extra.get("dm_policy", "pairing")).strip().lower()
        self._allow_from = _coerce_list(extra.get("allow_from") or extra.get("allowFrom"))
        self._group_policy = str(extra.get("group_policy", "pairing")).strip().lower()
        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval: float = 30.0  # seconds, updated by Hello
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._chat_type_map: Dict[str, str] = {}  # chat_id → "c2c"|"group"|"guild"|"dm"
        self._pending_responses: Dict[str, asyncio.Future] = {}  # request/response correlation
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE, ttl_seconds=DEDUP_WINDOW_SECONDS)
        self._last_msg_id: Dict[str, str] = {}  # last inbound message ID per chat (send_typing)
        self._typing_sent_at: Dict[str, float] = {}  # typing debounce: chat_id → last send_typing ts
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        # Inline-keyboard interaction routing: invoked for every INTERACTION_CREATE
        # after the adapter ACKed it. Defaults to the approval/update-prompt
        # dispatcher; override via set_interaction_callback() (None drops clicks).
        self._interaction_callback: Optional[Callable[[InteractionEvent], Awaitable[None]]] = (
            self._default_interaction_dispatch)

    # ── Properties ──

    @property
    def name(self) -> str:
        return "QQBot"

    # ── Connection lifecycle ──

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Authenticate, obtain gateway URL, and open the WebSocket. ``is_reconnect``
        is accepted for interface conformance only (QQBot has no server-side update queue)."""
        for ok, code, what, hint in (
            (AIOHTTP_AVAILABLE, "qq_missing_dependency", "aiohttp not installed",
             f". Run: {install_hint('messaging')}"),
            (HTTPX_AVAILABLE, "qq_missing_dependency", "httpx not installed", ". Run: hermes pm repair"),
            (self._app_id and self._client_secret, "qq_missing_credentials",
             "QQ_APP_ID and QQ_CLIENT_SECRET are required", "")):
            if not ok:
                message = f"QQ startup failed: {what}"
                self._set_fatal_error(code, message, retryable=True)
                logger.warning("[%s] %s%s", self._log_tag, message, hint)
                return False

        if not self._acquire_platform_lock("qqbot-appid", self._app_id, "QQBot app ID"):
            return False

        try:
            # Tighter keepalive pool so idle CLOSE_WAIT sockets drain faster behind proxies.
            # See #18451.
            from gateway.platforms._http_client_limits import platform_httpx_limits
            from tools.url_safety import create_ssrf_safe_async_client
            self._http_client = create_ssrf_safe_async_client(
                timeout=30.0, follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]}, limits=platform_httpx_limits())

            await self._open_gateway_ws(log_url=True)
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._mark_connected()
            logger.info("[%s] Connected", self._log_tag)
            self._wire_plugin_handlers(None)
            return True
        except Exception as exc:
            message = f"QQ startup failed: {exc}"
            self._set_fatal_error("qq_connect_error", message, retryable=True)
            logger.error("[%s] %s", self._log_tag, message, exc_info=True)
            await self._cleanup()
            self._release_platform_lock()
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        await cancel_task(self._listen_task)
        await cancel_task(self._heartbeat_task)
        self._listen_task = self._heartbeat_task = None
        await self._cleanup()
        self._release_platform_lock()
        logger.info("[%s] Disconnected", self._log_tag)

    async def _close_ws(self) -> None:
        """Close the WebSocket + its aiohttp session (keeps _http_client alive)."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _cleanup(self) -> None:
        """Close WebSocket, HTTP session, and client; fail pending futures."""
        await self._close_ws()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._fail_pending("Disconnected")

    # ── Token management ──

    async def _fetch_json(self, what: str, request: Callable[[], Awaitable[Any]]) -> Dict[str, Any]:
        """Run an httpx request and return its JSON; any failure → RuntimeError."""
        try:
            resp = await request()
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise RuntimeError(f"Failed to get QQ Bot {what}: {exc}") from exc

    def _token_fresh(self) -> bool:
        return bool(self._access_token) and time.time() < self._token_expires_at - 60

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed (with singleflight)."""
        if self._token_fresh():
            return self._access_token
        async with self._token_lock:
            if self._token_fresh():  # double-check after acquiring lock
                return self._access_token
            data = await self._fetch_json("access token", lambda: self._http_client.post(
                TOKEN_URL, json={"appId": self._app_id, "clientSecret": self._client_secret},
                timeout=DEFAULT_API_TIMEOUT))
            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"QQ Bot token response missing access_token: {data}")
            expires_in = int(data.get("expires_in", 7200))
            self._access_token = token
            self._token_expires_at = time.time() + expires_in
            logger.info("[%s] Access token refreshed, expires in %ds", self._log_tag, expires_in)
            return self._access_token

    async def _get_gateway_url(self) -> str:
        token = await self._ensure_token()
        data = await self._fetch_json("gateway URL", lambda: self._http_client.get(
            f"{API_BASE}{GATEWAY_URL_PATH}",
            headers={"Authorization": f"QQBot {token}", "User-Agent": build_user_agent()},
            timeout=DEFAULT_API_TIMEOUT))
        url = data.get("url")
        if not url:
            raise RuntimeError(f"QQ Bot gateway response missing url: {data}")
        return url

    # ── WebSocket lifecycle ──

    async def _open_gateway_ws(self, *, log_url: bool = False) -> None:
        """Token → gateway URL → WebSocket (shared by connect and _reconnect)."""
        await self._ensure_token()
        gateway_url = await self._get_gateway_url()
        if log_url:
            logger.info("[%s] Gateway URL: %s", self._log_tag, gateway_url)
        await self._open_ws(gateway_url)

    async def _open_ws(self, gateway_url: str) -> None:
        await self._close_ws()
        # Honor proxy env vars for the WebSocket (WSL setups need this).
        self._session = aiohttp.ClientSession(trust_env=gateway_trust_env())
        proxy_vars = ("WSS_PROXY", "wss_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
        ws_proxy = next((v for v in map(os.getenv, proxy_vars) if v), None)
        self._ws = await self._session.ws_connect(
            gateway_url, headers={"User-Agent": build_user_agent()}, timeout=CONNECT_TIMEOUT_SECONDS, proxy=ws_proxy,
        )
        logger.info("[%s] WebSocket connected to %s", self._log_tag, gateway_url)

    async def _listen_loop(self) -> None:
        """Read WebSocket events and reconnect on errors. Close codes: 4004 → refresh
        token; 4006/4007/49xx → clear session and re-identify; 4008 → rate limited,
        back off; _FATAL_CLOSE_CODES → stop."""
        backoff_idx = 0
        connect_time = 0.0
        quick_disconnect_count = 0

        async def reconnect() -> None:
            nonlocal backoff_idx, quick_disconnect_count
            if await self._reconnect(backoff_idx):
                backoff_idx = quick_disconnect_count = 0
            else:
                backoff_idx += 1

        while self._running:
            try:
                connect_time = time.monotonic()
                await self._read_events()
                backoff_idx = quick_disconnect_count = 0
            except asyncio.CancelledError:
                return
            except QQCloseError as exc:
                if not self._running:
                    return
                code = exc.code
                logger.warning("[%s] WebSocket closed: code=%s reason=%s", self._log_tag, code, exc.reason)

                # Quick disconnect detection (permission issues, misconfiguration)
                duration = time.monotonic() - connect_time
                if duration < QUICK_DISCONNECT_THRESHOLD and connect_time > 0:
                    quick_disconnect_count += 1
                    logger.info(
                        "[%s] Quick disconnect (%.1fs), count: %d", self._log_tag, duration, quick_disconnect_count
                    )
                    if quick_disconnect_count >= MAX_QUICK_DISCONNECT_COUNT:
                        logger.error(
                            "[%s] Too many quick disconnects. "
                            "Check: 1) AppID/Secret correct 2) Bot permissions on QQ Open Platform",
                            self._log_tag)
                        self._set_fatal_error(
                            "qq_quick_disconnect", "Too many quick disconnects — check bot permissions", retryable=True
                        )
                        return
                else:
                    quick_disconnect_count = 0

                self._mark_transport_disconnected()
                self._fail_pending("Connection closed")

                desc = self._FATAL_CLOSE_CODES.get(code)
                if desc:
                    logger.error("[%s] Bot is %s. Check QQ Open Platform.", self._log_tag, desc)
                    self._set_fatal_error(f"qq_{desc}", f"Bot is {desc}", retryable=False)
                    return

                if code == 4008:
                    logger.info("[%s] Rate limited (4008), waiting %ds", self._log_tag, RATE_LIMIT_DELAY)
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        self._mark_disconnected()
                        return
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                    await reconnect()
                    continue

                if code == 4004:
                    logger.info("[%s] Invalid token (4004), will refresh and reconnect", self._log_tag)
                    self._access_token = None
                    self._token_expires_at = 0.0

                if code in self._SESSION_INVALID_CLOSE_CODES:
                    logger.info("[%s] Session error (%d), clearing session for re-identify", self._log_tag, code)
                    self._session_id = None
                    self._last_seq = None

                await reconnect()
                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("[%s] Max reconnect attempts reached (QQCloseError)", self._log_tag)
                    self._mark_disconnected()
                    return

            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[%s] WebSocket error: %s", self._log_tag, exc)
                self._mark_transport_disconnected()
                self._fail_pending("Connection interrupted")

                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("[%s] Max reconnect attempts reached", self._log_tag)
                    self._mark_disconnected()
                    return
                await reconnect()

    async def _reconnect(self, backoff_idx: int) -> bool:
        delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
        logger.info("[%s] Reconnecting in %ds (attempt %d)...", self._log_tag, delay, backoff_idx + 1)
        await asyncio.sleep(delay)

        self._heartbeat_interval = 30.0  # reset until Hello
        try:
            await self._open_gateway_ws()
            self._mark_connected()
            logger.info("[%s] Reconnected", self._log_tag)
            return True
        except Exception as exc:
            logger.warning("[%s] Reconnect failed: %s", self._log_tag, exc)
            return False

    async def _read_events(self) -> None:
        if not self._ws:
            raise RuntimeError("WebSocket not connected")
        if self._ws.closed:
            # Returning normally here would make _listen_loop treat it as a clean
            # read and retry with backoff reset → 100% CPU spin. Raise instead.
            raise RuntimeError("WebSocket closed")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    self._dispatch_payload(payload)
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                raise QQCloseError(msg.data, msg.extra)
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WebSocket closed")

    async def _heartbeat_loop(self) -> None:
        """Send op 1 heartbeats with the latest seq at 80% of the Hello interval."""
        with contextlib.suppress(asyncio.CancelledError):
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    await self._ws.send_json({"op": 1, "d": self._last_seq})
                except Exception as exc:
                    logger.debug("[%s] Heartbeat failed: %s", self._log_tag, exc)

    async def _send_ws_auth(self, name: str, payload: Dict[str, Any], sent_msg: str, *log_args) -> bool:
        """Send an Identify/Resume payload; returns False if the send raised."""
        try:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(payload)
                logger.info("[%s] " + sent_msg, self._log_tag, *log_args)
            else:
                logger.warning("[%s] Cannot send %s: WebSocket not connected", self._log_tag, name)
        except Exception as exc:
            logger.error("[%s] Failed to send %s: %s", self._log_tag, name, exc)
            return False
        return True

    async def _send_identify(self) -> None:
        """Send op 2 Identify (reply to Hello); server answers with READY. Intents:
        C2C_GROUP_AT_MESSAGES | PUBLIC_GUILD_MESSAGES | DIRECT_MESSAGE | INTERACTION."""
        token = await self._ensure_token()
        payload = {"op": 2, "d": {
            "token": f"QQBot {token}",
            "intents": (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26),
            "shard": [0, 1],
            "properties": {"$os": "macOS", "$browser": "hermes-agent", "$device": "hermes-agent"}}}
        await self._send_ws_auth("Identify", payload, "Identify sent")

    async def _send_resume(self) -> None:
        """Send op 6 Resume after a reconnect; on failure clear session → Identify next Hello."""
        token = await self._ensure_token()
        payload = {"op": 6, "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._last_seq}}
        if not await self._send_ws_auth(
            "Resume", payload, "Resume sent (session_id=%s, seq=%s)", self._session_id, self._last_seq
        ):
            self._session_id = None
            self._last_seq = None

    @staticmethod
    def _create_task(coro):
        """Schedule a coroutine; returns None (no error) when no loop is running
        (tests call _dispatch_payload synchronously)."""
        try:
            return asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            return None

    def _close_ws_soon(self) -> None:
        """Close the WS so _read_events raises and _listen_loop reconnects (with Resume)."""
        if self._ws and not self._ws.closed:
            self._create_task(self._ws.close())

    def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """Route inbound WebSocket payloads (dispatch synchronously, spawn async handlers)."""
        op, t, s, d = payload.get("op"), payload.get("t"), payload.get("s"), payload.get("d")
        if isinstance(s, int) and (self._last_seq is None or s > self._last_seq):
            self._last_seq = s

        if op == 10:  # Hello — reply with Resume (have session) or Identify
            interval_ms = (d if isinstance(d, dict) else {}).get("heartbeat_interval", 30000)
            self._heartbeat_interval = interval_ms / 1000.0 * 0.8  # 80% of server interval
            logger.debug(
                "[%s] Hello received, heartbeat_interval=%dms (sending every %.1fs)",
                self._log_tag, interval_ms, self._heartbeat_interval)
            resume = self._session_id and self._last_seq is not None
            self._create_task(self._send_resume() if resume else self._send_identify())
        elif op == 0 and t:  # Dispatch
            if t == "READY":
                if isinstance(d, dict):  # store session_id for resume
                    self._session_id = d.get("session_id")
                    logger.info("[%s] Ready, session_id=%s", self._log_tag, self._session_id)
            elif t == "RESUMED":
                logger.info("[%s] Session resumed", self._log_tag)
            elif t in self._INBOUND_HANDLERS:
                asyncio.create_task(self._on_message(t, d))
            elif t == "INTERACTION_CREATE":
                self._create_task(self._on_interaction(d))
            else:
                logger.debug("[%s] Unhandled dispatch: %s", self._log_tag, t)
        elif op == 11:  # Heartbeat ACK
            pass
        elif op == 7:  # Server Reconnect
            logger.info("[%s] Server requested reconnect (op 7)", self._log_tag)
            self._close_ws_soon()
        elif op == 9:  # Invalid Session — d=True resumable, d=False re-identify from scratch
            if d is not None and bool(d):
                logger.info("[%s] Invalid session (op 9, resumable)", self._log_tag)
            else:
                logger.info("[%s] Invalid session (op 9, not resumable), clearing session", self._log_tag)
                self._session_id = None
                self._last_seq = None
            self._close_ws_soon()
        else:
            logger.debug("[%s] Unknown op: %s", self._log_tag, op)

    # ── JSON helpers ──

    @staticmethod
    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.warning("[QQBot] Failed to parse JSON: %r", raw)
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _next_msg_seq(msg_id: str) -> int:
        """Generate a message sequence number in 0..65535 range."""
        time_part = int(time.time()) % 100000000
        rand = int(uuid.uuid4().hex[:4], 16)
        return (time_part ^ rand) % 65536

    # ── Inbound message handling ──

    async def handle_message(self, event: MessageEvent) -> None:
        """Cache the last message ID per chat, then delegate to base."""
        if event.message_id and event.source.chat_id:
            self._last_msg_id[event.source.chat_id] = event.message_id
        await super().handle_message(event)

    async def _on_message(self, event_type: str, d: Any) -> None:
        if not isinstance(d, dict):
            return
        msg_id = str(d.get("id", ""))
        if not msg_id or self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate or missing message id: %s", self._log_tag, msg_id)
            return
        handler = self._INBOUND_HANDLERS.get(event_type)
        if handler:
            author = d.get("author") if isinstance(d.get("author"), dict) else {}
            await getattr(self, handler)(
                d, msg_id, str(d.get("content", "")).strip(), author, str(d.get("timestamp", "")))

    # ── Inline-keyboard interactions (INTERACTION_CREATE) ──

    def set_interaction_callback(self, callback: Optional[Callable[[InteractionEvent], Awaitable[None]]]) -> None:
        """Register (or clear) the callback invoked per ACKed INTERACTION_CREATE."""
        self._interaction_callback = callback

    async def _on_interaction(self, d: Any) -> None:
        """Parse INTERACTION_CREATE, ACK it promptly (else the client shows an error
        icon on the button), then dispatch to the registered callback."""
        if not isinstance(d, dict):
            return
        try:
            event = parse_interaction_event(d)
        except Exception as exc:
            logger.warning("[%s] Failed to parse INTERACTION_CREATE: %s", self._log_tag, exc)
            return
        if not event.id:
            logger.warning("[%s] INTERACTION_CREATE missing id, skipping ACK", self._log_tag)
            return

        try:
            await self._acknowledge_interaction(event.id)
        except Exception as exc:
            logger.warning("[%s] Failed to ACK interaction %s: %s", self._log_tag, event.id, exc)

        logger.info(
            "[%s] Interaction: scene=%s button_data=%r operator=%s",
            self._log_tag, event.scene, event.button_data, event.operator_openid)
        callback = self._interaction_callback
        if callback is None:
            logger.debug(
                "[%s] No interaction callback registered; dropping button click %r", self._log_tag, event.button_data
            )
            return
        try:
            await callback(event)
        except Exception as exc:
            logger.error("[%s] Interaction callback raised: %s", self._log_tag, exc, exc_info=True)

    async def _acknowledge_interaction(self, interaction_id: str, code: int = 0) -> None:
        """ACK a button interaction via ``PUT /interactions/{id}`` (code 0 = success)."""
        resp = await self._require_http_client().put(
            f"{API_BASE}/interactions/{interaction_id}",
            headers=await self._auth_headers(), json={"code": code}, timeout=DEFAULT_API_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"Interaction ACK failed [{resp.status_code}]: {resp.text[:200]}")

    # Button decision → ``choice`` for tools.approval.resolve_gateway_approval. The
    # 3-button layout folds "session" into "always"; ``/approve session`` still works.
    _APPROVAL_BUTTON_TO_CHOICE = {"allow-once": "once", "allow-always": "always", "deny": "deny"}

    @staticmethod
    def _parse_gateway_session_key(session_key: str) -> Optional[Dict[str, str]]:
        """Parse ``agent:<namespace>:<platform>:<chat_type>:<chat_id>[:<user_id>]``.

        The namespace slot carries the multiplex profile ("main" for the
        default profile — see ``gateway.session._session_key_namespace``);
        it takes no part in any authorization decision, so any non-empty
        value is accepted and every later slot keeps its position.
        """
        parts = str(session_key or "").split(":")
        if len(parts) < 5 or parts[0] != "agent" or not parts[1]:
            return None
        parsed = {"platform": parts[2], "chat_type": parts[3], "chat_id": parts[4]}
        if len(parts) > 5:
            parsed["user_id"] = parts[5]
        return parsed

    def _is_authorized_interaction_for_session(self, event: InteractionEvent, session_key: str) -> bool:
        """Authorize approval/update interactions against session + operator."""
        parsed = self._parse_gateway_session_key(session_key)
        operator = str(event.operator_openid or "").strip()
        if not parsed or parsed.get("platform") != "qqbot" or not operator:
            return False

        chat_type = parsed.get("chat_type", "")
        chat_id = parsed.get("chat_id", "")
        # Approval keys come from build_source (chat_type="dm"); update-prompt keys from event.scene ("c2c").
        if chat_type in {"c2c", "dm"}:
            return bool(chat_id) and operator == chat_id
        if chat_type in {"group", "guild"}:
            event_chat = str(event.group_openid or event.guild_id or "").strip()
            if not event_chat or event_chat != chat_id:
                return False
            session_user = str(parsed.get("user_id", "")).strip()
            return bool(session_user) and operator == session_user
        return False

    def _update_prompt_session_key(self, event: InteractionEvent, chat: str) -> str:
        """Session key an update-prompt click is authorized against, built by the ONE key builder so
        it carries the profile namespace (a hard-coded ``agent:main:`` prefix never matched a
        multiplexed secondary bot's lane). No participant: ``c2c`` authorizes on chat == operator."""
        return self._source_session_key(self.build_source(chat_id=chat, chat_type=event.scene))

    async def _default_interaction_dispatch(self, event: InteractionEvent) -> None:
        """Default interaction callback: ``approve:<session_key>:<decision>`` →
        tools.approval.resolve_gateway_approval; ``update_prompt:<answer>`` →
        ``~/.hermes/.update_response``; anything else is ignored at DEBUG."""
        button_data = event.button_data
        if not button_data:
            return

        approval = parse_approval_button_data(button_data)
        if approval is not None:
            session_key, decision, request_id = approval
            choice = self._APPROVAL_BUTTON_TO_CHOICE.get(decision)
            if choice is None:
                logger.warning("[%s] Unknown approval decision %r (session=%s)", self._log_tag, decision, session_key)
                return
            if not self._is_authorized_interaction_for_session(event, session_key):
                logger.warning(
                    "[%s] Rejected unauthorized approval click for session %s (operator=%s)",
                    self._log_tag, session_key, event.operator_openid)
                return
            try:
                from tools.approval import resolve_gateway_approval  # lazy: keep adapter light
                count = (
                    resolve_gateway_approval(session_key, choice, request_id=request_id)
                    if request_id else 0
                )
                logger.info(
                    "[%s] Button resolved %d approval(s) for session %s (choice=%s, operator=%s)",
                    self._log_tag, count, session_key, choice, event.operator_openid)
            except Exception as exc:
                logger.error("[%s] resolve_gateway_approval failed for session %s: %s", self._log_tag, session_key, exc)
            return

        update_answer = parse_update_prompt_button_data(button_data)
        if update_answer is not None:
            chat = event.group_openid or event.guild_id or event.user_openid
            if not self._is_authorized_interaction_for_session(event, self._update_prompt_session_key(event, chat)):
                logger.warning(
                    "[%s] Rejected unauthorized update prompt click (operator=%s)", self._log_tag, event.operator_openid
                )
                return
            self._write_update_response(update_answer, event.operator_openid)
            return

        logger.debug("[%s] Unrecognised button_data %r from interaction %s", self._log_tag, button_data, event.id)

    @staticmethod
    def _write_update_response(answer: str, operator: str = "") -> None:
        """Atomically (tmp + rename) write the update-prompt answer to
        ``.update_response``, polled by the detached ``hermes update --gateway`` watcher."""
        try:
            from hermes_constants import get_hermes_home
            response_path = get_hermes_home() / ".update_response"
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(answer, encoding="utf-8")
            tmp.replace(response_path)
            logger.info("QQ update prompt answered %r by %s", answer, operator or "(unknown)")
        except Exception as exc:
            logger.error("Failed to write update response: %s", exc)

    async def _handle_c2c_message(self, d, msg_id, content, author, timestamp) -> None:
        user_openid = str(author.get("user_openid", ""))
        if not user_openid or not self._is_dm_intake_allowed(user_openid):
            return

        attachments_raw = d.get("attachments")
        logger.info(
            "[%s] C2C message: id=%s content=%r attachments=%s",
            self._log_tag, msg_id, content[:50] if content else "",
            f"{len(attachments_raw) if isinstance(attachments_raw, list) else 0} items" if attachments_raw else "None",
        )
        if attachments_raw and isinstance(attachments_raw, list):
            for _i, _att in enumerate(attachments_raw):
                if isinstance(_att, dict):
                    logger.info(
                        "[%s] attachment[%d]: content_type=%s url=%s filename=%s",
                        self._log_tag, _i, _att.get("content_type", ""),
                        str(_att.get("url", ""))[:80], _att.get("filename", ""))

        await self._ingest(
            d, msg_id, content, attachments_raw, timestamp, verbose=True,
            chat_id=user_openid, qq_chat_type="c2c", user_id=user_openid, chat_type="dm")

    async def _handle_group_message(self, d, msg_id, content, author, timestamp) -> None:
        group_openid = str(d.get("group_openid", ""))
        member = str(author.get("member_openid", ""))
        if not group_openid or not self._is_group_allowed(group_openid, member):
            return
        await self._ingest(
            d, msg_id, self._strip_at_mention(content), d.get("attachments"), timestamp,
            chat_id=group_openid, qq_chat_type="group", user_id=member, chat_type="group")

    async def _handle_guild_message(self, d, msg_id, content, author, timestamp) -> None:
        channel_id = str(d.get("channel_id", ""))
        if not channel_id:
            return
        # group_policy ACL — guild channels are group-like; without it any guild
        # member could bypass the allowlist.
        guild_id = str(d.get("guild_id", ""))
        author_id = str(author.get("id", ""))
        if not self._is_group_allowed(guild_id or channel_id, author_id):
            logger.debug("[%s] Guild message blocked by ACL: channel=%s user=%s", self._log_tag, channel_id, author_id)
            return

        member = d.get("member") if isinstance(d.get("member"), dict) else {}
        nick = str(member.get("nick", "")) or str(author.get("username", ""))
        await self._ingest(
            d, msg_id, content, d.get("attachments"), timestamp,
            chat_id=channel_id, qq_chat_type="guild", user_id=author_id, user_name=nick or None, chat_type="group")

    async def _handle_dm_message(self, d, msg_id, content, author, timestamp) -> None:
        guild_id = str(d.get("guild_id", ""))
        if not guild_id:
            return
        # dm_policy ACL — without it any guild member could bypass the allowlist via DM.
        author_id = str(author.get("id", ""))
        if not self._is_dm_intake_allowed(author_id):
            logger.debug("[%s] Guild DM blocked by ACL: guild=%s user=%s", self._log_tag, guild_id, author_id)
            return
        await self._ingest(
            d, msg_id, content, d.get("attachments"), timestamp,
            chat_id=guild_id, qq_chat_type="dm", user_id=author_id, chat_type="dm")

    _INBOUND_HANDLERS = {
        "C2C_MESSAGE_CREATE": "_handle_c2c_message",
        "GROUP_AT_MESSAGE_CREATE": "_handle_group_message",
        "GUILD_MESSAGE_CREATE": "_handle_guild_message",
        "GUILD_AT_MESSAGE_CREATE": "_handle_guild_message",
        "DIRECT_MESSAGE_CREATE": "_handle_dm_message"}

    # ── Shared inbound pipeline (all four message kinds) ──

    @staticmethod
    def _append_block(text: str, block: str) -> str:
        """Append *block* to *text* after a blank line (or return block alone if text is blank)."""
        return (text + "\n\n" + block).strip() if text.strip() else block

    async def _ingest(
        self, d: Dict[str, Any], msg_id: str, content: str, attachments: Any, timestamp: str, *,
        chat_id: str, qq_chat_type: str, verbose: bool = False, **source_kwargs: Any) -> None:
        """Shared inbound tail: fold attachment transcripts/file info and quoted context
        into the text, drop empty events, remember the QQ chat kind and dispatch."""
        att = await self._process_attachments(attachments)
        text = content
        voice_transcripts = att["voice_transcripts"]
        if voice_transcripts:
            text = self._append_block(text, "\n".join(voice_transcripts))
        if att["attachment_info"]:
            text = self._append_block(text, att["attachment_info"])
        image_urls, image_media_types = att["image_urls"], att["image_media_types"]
        if verbose:
            logger.info("[%s] After processing: images=%d, voice=%d", self._log_tag, len(image_urls), len(voice_transcripts))

        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]
        if not text.strip() and not image_urls:
            return

        self._chat_type_map[chat_id] = qq_chat_type
        event = MessageEvent(
            source=self.build_source(chat_id=chat_id,** source_kwargs), text=text,
            message_type=self._detect_message_type(image_urls, image_media_types), raw_message=d,
            message_id=msg_id, media_urls=image_urls, media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    # ── Quoted-message handling ──

    async def _process_quoted_context(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """Process the quoted message a user is replying to (``message_type == 103``;
        referenced content + attachments live in ``msg_elements``). Quoted attachments
        go through _process_attachments so quoted voice gets STT and quoted images are
        cached identically. Returns ``{"quote_block", "image_urls", "image_media_types"}``;
        quote_block is "" when nothing is quoted."""
        empty = {"quote_block": "", "image_urls": [], "image_media_types": []}
        try:
            is_quote = int(d.get("message_type", 0) or 0) == 103
        except (TypeError, ValueError):
            is_quote = False
        elements = d.get("msg_elements")
        if not is_quote or not isinstance(elements, list) or not elements:
            return empty

        elements = [e for e in elements if isinstance(e, dict)]
        quoted_text_parts = [t for t in (str(e.get("content", "")).strip() for e in elements) if t]
        all_attachments = [
            a for e in elements if isinstance(e.get("attachments"), list) for a in e["attachments"] if isinstance(a, dict)]
        att_result = await self._process_attachments(all_attachments)
        quoted_images = att_result.get("image_urls") or []

        lines: List[str] = [" ".join(quoted_text_parts)] if quoted_text_parts else []
        lines.extend(att_result.get("voice_transcripts") or [])
        if att_result.get("attachment_info"):
            lines.append(att_result["attachment_info"])
        if not lines and not quoted_images:
            return empty
        # Images-only quote still gets a marker so the LLM knows context was referenced.
        return {
            "quote_block": "[Quoted message]:\n" + "\n".join(lines) if lines else "[Quoted message]: (image)",
            "image_urls": quoted_images,
            "image_media_types": att_result.get("image_media_types") or []}

    @staticmethod
    def _merge_quote_into(text: str, quote_block: str) -> str:
        """Prepend ``quote_block`` to *text*, separated by a blank line."""
        if not quote_block:
            return text
        return f"{quote_block}\n\n{text}".strip() if text.strip() else quote_block

    # ── Attachment processing ──

    @staticmethod
    def _detect_message_type(media_urls: list, media_types: list):
        if not media_urls:
            return MessageType.TEXT
        if not media_types:
            return MessageType.PHOTO
        first_type = media_types[0].lower()
        if "audio" in first_type or "voice" in first_type or "silk" in first_type:
            return MessageType.VOICE
        if "video" in first_type:
            return MessageType.VIDEO
        if "image" in first_type or "photo" in first_type:
            return MessageType.PHOTO
        logger.debug("Unknown media content_type '%s', defaulting to TEXT", first_type)
        return MessageType.TEXT

    async def _process_attachments(self, attachments: Any) -> Dict[str, Any]:
        """Process inbound attachments uniformly. Returns ``{"image_urls",
        "image_media_types", "voice_transcripts", "attachment_info"}`` (cached image
        paths + MIME types, "[Voice] ..." transcripts, text description of other files)."""
        image_urls: List[str] = []
        image_media_types: List[str] = []
        voice_transcripts: List[str] = []
        other_attachments: List[str] = []

        for att in attachments if isinstance(attachments, list) else ():
            if not isinstance(att, dict):
                continue
            ct = str(att.get("content_type", "")).strip().lower()
            url = str(att.get("url", "")).strip()
            filename = str(att.get("filename", ""))
            if not url:
                continue
            if url.startswith("//"):
                url = f"https:{url}"
            logger.debug(
                "[%s] Processing attachment: content_type=%s, url=%s, filename=%s",
                self._log_tag, ct, url[:80], filename)

            if self._is_voice_content_type(ct, filename):
                asr_refer, wav_url = (self._opt_str(att.get(k)) for k in ("asr_refer_text", "voice_wav_url"))
                transcript = await self._stt_voice_attachment(
                    url, ct, filename, asr_refer_text=asr_refer, voice_wav_url=wav_url)
                if transcript:
                    voice_transcripts.append(f"[Voice] {transcript}")
                    logger.debug("[%s] Voice transcript: %s", self._log_tag, transcript)
                else:
                    logger.warning("[%s] Voice STT failed for %s", self._log_tag, url[:60])
                    voice_transcripts.append("[Voice] [语音识别失败]")
                continue

            is_image = ct.startswith("image/")
            try:
                cached_path = await self._download_and_cache(url, ct, filename)
            except Exception as exc:
                logger.debug("[%s] Failed to cache %s: %s", self._log_tag, "image" if is_image else "attachment", exc)
                continue
            if not cached_path:
                continue
            if not is_image:
                label = "video" if ct.startswith("video/") else "file"
                other_attachments.append(f"[{label}: {filename or ct} ({cached_path})]")
            elif os.path.isfile(cached_path):
                image_urls.append(cached_path)
                image_media_types.append(ct or "image/jpeg")
            else:
                logger.warning("[%s] Cached image path does not exist: %s", self._log_tag, cached_path)

        return {
            "image_urls": image_urls,
            "image_media_types": image_media_types,
            "voice_transcripts": voice_transcripts,
            "attachment_info": "\n".join(other_attachments)}

    @staticmethod
    def _opt_str(value: Any) -> Optional[str]:
        return (value.strip() if isinstance(value, str) else "") or None

    async def _download_and_cache(self, url: str, content_type: str, original_name: str = "") -> Optional[str]:
        """Download a URL and cache it locally (``original_name`` falls back to the URL basename)."""
        from tools.url_safety import is_safe_url

        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL: {url[:80]}")
        if not self._http_client:
            return None
        try:
            resp = await self._http_client.get(url, timeout=30.0, headers=self._qq_media_headers())
            resp.raise_for_status()
            data = resp.content
        except Exception as exc:
            logger.debug("[%s] Download failed for %s: %s", self._log_tag, url[:80], exc)
            return None

        if content_type.startswith("image/"):
            # Historical qqbot mapping: trust mimetypes' guess (never the shared table), fall back to .jpg.
            ext = ext_for_mime(content_type, use_defaults=False, use_mimetypes=True, fallback=".jpg") or ".jpg"
            return await cache_image_from_bytes_async(data, ext)
        if content_type == "voice" or content_type.startswith("audio/"):
            # QQ voice is usually .amr/.silk — convert to .wav for STT engines.
            return await self._convert_audio_to_wav(data, url)
        filename = original_name or Path(urlparse(url).path).name or "qq_attachment"
        return await cache_document_from_bytes_async(data, filename)

    @staticmethod
    def _is_voice_content_type(content_type: str, filename: str) -> bool:
        ct = content_type.strip().lower()
        if ct == "voice" or ct.startswith("audio/"):
            return True
        # content_type="file" is an explicit upload: never route .wav/.mp3 files into STT.
        if ct == "file":
     