"""Base platform adapter interface; every platform adapter inherits from BasePlatformAdapter."""

import asyncio
import contextlib
import inspect
import ipaddress
import logging
import math
import os
import random
import re
import socket as _socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from utils import normalize_proxy_url
from agent.i18n import t
from agent.retry_utils import jittered_backoff
from agent.proxy_bypass import first_proxy_env_value, should_bypass_proxy as _should_bypass_proxy

logger = logging.getLogger(__name__)


def _consume_detached_handler_exception(task: "asyncio.Task") -> None:
    """Done-callback for a detached fatal-error handler task (carrier cancelled in
    ``_notify_fatal_error``): retrieve its exception so asyncio never logs "never retrieved"."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Detached fatal-error handler task failed: %s", exc, exc_info=exc)


# Audio exts for native audio delivery; Telegram's narrower sets stay separate (.m2a is audio to
# Hermes but not to sendAudio).
_AUDIO_MIME_TYPES = {
    ".ogg": "audio/ogg", ".opus": "audio/opus", ".mp3": "audio/mpeg", ".m2a": "audio/mpeg",
    ".wav": "audio/wav", ".m4a": "audio/m4a", ".flac": "audio/flac"}
_AUDIO_EXTS = frozenset(_AUDIO_MIME_TYPES)
# Outbound dispatch partition for MEDIA/local files (image batch vs send_video).
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"})
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
# Telegram sendAudio accepts only MP3 / M4A; others go via sendVoice (Opus/OGG) or as a document.
_TELEGRAM_AUDIO_ATTACHMENT_EXTS = frozenset({'.mp3', '.m4a'})
_TELEGRAM_VOICE_EXTS = frozenset({'.ogg', '.opus'})


def transcode_to_ogg_opus(path: str, *, bitrate: str = "32k", timeout: int = 60,
                          output_path: "str | None" = None) -> "str | None":
    """Best-effort ffmpeg transcode to Ogg/Opus (voip-tuned) for native voice bubbles: the written
    ``.ogg`` path (a NEW temp file unless ``output_path`` is given; caller cleans up), or None when
    ffmpeg is missing/fails. ``output_path`` may equal ``path`` (in-place container repair) — the
    encode goes through a sidecar so a failed run never truncates the source. Blocking (to_thread)."""
    import shutil as _shutil
    ffmpeg = _shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    if output_path is None:
        fd, ogg_path = tempfile.mkstemp(prefix="voice_transcode_", suffix=".ogg")
        os.close(fd)
    else:
        ogg_path = output_path
    in_place = os.path.abspath(str(path)) == os.path.abspath(ogg_path)
    work_path = ogg_path + ".tmp.ogg" if in_place else ogg_path
    try:
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", str(path),
             "-acodec", "libopus", "-ac", "1", "-b:a", bitrate, "-vbr", "on",
             "-application", "voip", "-compression_level", "10", "-f", "ogg", work_path],
            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if result.returncode == 0 and os.path.getsize(work_path) > 0:
            if in_place:
                os.replace(work_path, ogg_path)
            return ogg_path
        logger.warning("ffmpeg Ogg/Opus transcode of %s failed (returncode=%s): %s", path, result.returncode,
                       (result.stderr or b"").decode("utf-8", errors="replace")[:500])
    except Exception:
        logger.warning("voice transcode to Ogg/Opus failed for %s", path, exc_info=True)
    with contextlib.suppress(OSError):
        os.unlink(work_path)
    return None
_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS = 30.0
# History dedup is best-effort: stay well below the Discord heartbeat watchdog and fail open.
_HISTORY_MEDIA_LOOKUP_TIMEOUT_SECONDS = 5.0
# Timed-out reads can't be cancelled mid-SQLite: cap the isolated threads so wedged lookups
# can't exhaust the shared executor.
_HISTORY_MEDIA_LOOKUP_MAX_WORKERS = 2
_HISTORY_MEDIA_LOOKUP_ADMISSION = threading.BoundedSemaphore(_HISTORY_MEDIA_LOOKUP_MAX_WORKERS)


def _platform_name(platform) -> str:
    """Normalize a Platform enum / raw string into a lowercase name."""
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


def _or_default(thunk, default, exc=(TypeError, ValueError)):
    """``thunk()``, or ``default`` when it raises one of ``exc`` (numeric config/env coercion)."""
    try:
        return thunk()
    except exc:
        return default


DEFAULT_BUSY_TEXT_DEBOUNCE_SECONDS = 0.35
DEFAULT_BUSY_TEXT_HARD_CAP_SECONDS = 1.0


def _thread_metadata_for_source(source, reply_to_message_id: str | None = None) -> dict | None:
    """Platform-aware thread metadata for adapter sends. Telegram DM topics route with
    ``message_thread_id`` + a reply anchor; anchorless synthetic/resumed sends fall back to
    ``direct_messages_topic_id`` when supported."""
    thread_id = getattr(source, "thread_id", None)
    platform = _platform_name(getattr(source, "platform", None))
    metadata = {"thread_id": thread_id} if thread_id is not None else {}
    # Slack workspace identity is routing state: carry it so a multi-workspace Socket Mode
    # gateway never falls back to its primary WebClient.
    scope_id = getattr(source, "scope_id", None) if platform == "slack" else None
    if scope_id:
        metadata["slack_team_id"] = str(scope_id)
    if not metadata:
        return None
    if platform == "telegram" and getattr(source, "chat_type", None) == "dm":
        metadata["telegram_dm_topic_reply_fallback"] = True
        if str(thread_id) not in {"", "1"}:
            metadata["direct_messages_topic_id"] = str(thread_id)
        anchor = reply_to_message_id or getattr(source, "message_id", None)
        if anchor is not None:
            metadata["telegram_reply_to_message_id"] = str(anchor)
    # Routed profile (multiplex / profile_routes): outbound prune paths must not assume the
    # adapter's static profile stamp.
    profile = str(getattr(source, "profile", None) or "").strip()
    if profile:
        metadata["hermes_profile"] = profile
    return metadata


def _thread_metadata_for_event(event) -> dict | None:
    """``_thread_metadata_for_source`` for an event, anchored on its reply id."""
    return _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))


def _mark_notify_metadata(metadata: dict | None) -> dict:
    """Clone metadata and mark a user-visible reply as notify-worthy."""
    notify_metadata = dict(metadata) if metadata else {}
    notify_metadata["notify"] = True
    return notify_metadata


def _reply_anchor_for_event(event) -> str | None:
    """Return reply_to id for platforms that need reply semantics."""
    override = getattr(event, "reply_anchor_override", None)
    if override is not None:
        return override  # the turn was redirected onto another message (#115001)
    source = getattr(event, "source", None)
    platform = _platform_name(getattr(source, "platform", None))
    thread_id = getattr(source, "thread_id", None)
    raw_message = getattr(event, "raw_message", None)
    if (platform == "slack" and isinstance(raw_message, dict)
            and raw_message.get("_hermes_no_thread_response")):
        # Slack reaction handoff = new top-level message; a message_id anchor would make
        # _resolve_thread_ts() reply in a nonexistent thread.
        return None
    if platform == "telegram" and thread_id:
        # Forum topics route by topic metadata (no reply); DM-topic lanes reply to the triggering
        # message — replying to the topic seed/anchor can render outside the active lane.
        if getattr(source, "chat_type", None) != "dm":
            return None
        return getattr(event, "message_id", None) or getattr(event, "reply_to_message_id", None)
    if platform == "feishu" and thread_id and getattr(event, "reply_to_message_id", None):
        return getattr(event, "reply_to_message_id", None)
    return getattr(event, "message_id", None)


_MEDIA_KIND_KEYS = frozenset({"audio", "video", "file", "image"})


def _media_failure_text(kind: str, file_name: "str | None" = None) -> str:
    """User-facing "couldn't deliver" notice; ``file_name`` is the only name ever shown."""
    kind_label = t(f"gateway.notify.media_kind.{kind}") if kind in _MEDIA_KIND_KEYS else kind
    if file_name:
        return t("gateway.notify.media_delivery_failed_with_name", kind=kind_label, file_name=file_name)
    return t("gateway.notify.media_delivery_failed", kind=kind_label)


def should_send_media_as_audio(platform, ext: str, is_voice: bool = False) -> bool:
    """True when a media file should use the platform's audio sender. Telegram: explicit
    ``is_voice`` ([[audio_as_voice]]) routes ANY format to the voice sender (adapter transcodes
    non-Opus); otherwise only sendAudio's MP3/M4A qualify — a plain Opus/OGG attachment is never
    turned into a voice bubble, everything else → document. Other platforms: any audio ext."""
    normalized_ext = (ext or "").lower()
    if normalized_ext not in _AUDIO_EXTS:
        return False
    if _platform_name(platform) != "telegram":
        return True
    return is_voice or normalized_ext in _TELEGRAM_AUDIO_ATTACHMENT_EXTS


def build_auto_tts_output_path(platform) -> str:
    """Unique temp output path for gateway auto-TTS: ``.ogg`` for ``OPUS_VOICE_PLATFORMS``
    (the tool's ``_repair_ogg_container`` then guarantees real Opus bytes), else ``.mp3``.
    Platform-awareness lives HERE because ``_clear_session_env`` wipes the TTS tool's
    ``HERMES_SESSION_PLATFORM`` contextvar before the post-handler auto-TTS block runs.

    Platforms whose native voice bubbles require Ogg/Opus (``tools.tts_tool.OPUS_VOICE_PLATFORMS`` — the
    single source of truth) get an explicit ``.ogg`` path; the tool's central container repair
    (``_repair_ogg_container``) then guarantees real Ogg/Opus bytes for every provider, including MP3-only
    backends like Edge TTS. Everything else keeps the MP3 default. See #36685, #57049.
    """
    from tools.tts_tool import OPUS_VOICE_PLATFORMS
    ext = "ogg" if _platform_name(platform) in OPUS_VOICE_PLATFORMS else "mp3"
    audio_path = os.path.join(
        tempfile.gettempdir(), "hermes_voice", f"tts_reply_{uuid.uuid4().hex[:12]}.{ext}")
    os.makedirs(os.path.dirname(audio_path), exist_ok=True)
    return audio_path


def utf16_len(s: str) -> int:
    """UTF-16 code units in *s* — Telegram's 4 096 limit counts those, so astral chars
    (emoji, CJK Ext B) cost **two** units although Python's ``len()`` counts one.

    Ported from nearai/ironclaw#2304 which discovered the same discrepancy in Rust's ``chars().count()``.
    """
    return len(s.encode("utf-16-le")) // 2


def _custom_unit_to_cp(s: str, budget: int, len_fn) -> int:
    """Largest codepoint offset *n* with ``len_fn(s[:n]) <= budget`` (binary search)."""
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _prefix_within_utf16_limit(s: str, limit: int) -> str:
    """Longest prefix of *s* with UTF-16 length ≤ *limit*; never splits a surrogate pair."""
    return s[:_custom_unit_to_cp(s, limit, utf16_len)]


def is_network_accessible(host: str) -> bool:
    """True if *host* would expose the server beyond loopback (incl. IPv4-mapped
    ::ffff:127.0.0.1); hostnames are resolved and DNS failure fails closed (True)."""
    with contextlib.suppress(ValueError):  # ValueError: hostname — resolve below
        addr = ipaddress.ip_address(host)
        # ::ffff:127.0.0.1 reports is_loopback=False; check the mapped IPv4 explicitly.
        mapped = getattr(addr, "ipv4_mapped", None)
        return not (addr.is_loopback or (mapped and mapped.is_loopback))
    try:
        resolved = _socket.getaddrinfo(host, None, _socket.AF_UNSPEC, _socket.SOCK_STREAM)
        # Network-accessible if any resolved address is non-loopback.
        return any(not ipaddress.ip_address(sockaddr[0]).is_loopback for *_, sockaddr in resolved)
    except (_socket.gaierror, OSError):
        return True


# ``scutil --proxy`` is a fork+exec (~11 ms measured) and resolve_proxy_url runs it on the SEND path —
# per chunk of an outbound message and per media attachment, not once per adapter. The answer is an
# OS-level network setting that changes when someone edits Network Settings or joins a VPN, so it is
# cached briefly rather than per call. The TTL is the staleness a proxy change can suffer; a send that
# goes out on a stale answer fails and is retried, which is the same outcome as any transient proxy error.
# No lock: a race costs one extra fork and both answers are equally current.
_MACOS_PROXY_TTL_SECONDS = 60.0
_macos_proxy_cache: "tuple[float, str | None] | None" = None


def _detect_macos_system_proxy() -> str | None:
    """Read the macOS system HTTP(S) proxy via ``scutil --proxy``: ``http://host:port``
    when an HTTP(S) proxy is enabled, else None (non-macOS or any subprocess error).

    Memoised for ``_MACOS_PROXY_TTL_SECONDS``; call :func:`reset_macos_proxy_cache` to force a re-read.
    """
    global _macos_proxy_cache

    if sys.platform != "darwin":
        return None
    cached = _macos_proxy_cache
    now = time.monotonic()
    if cached is not None and (now - cached[0]) < _MACOS_PROXY_TTL_SECONDS:
        return cached[1]
    try:
        out = subprocess.check_output(["scutil", "--proxy"], timeout=3, text=True, encoding='utf-8',
                                      errors='replace', stderr=subprocess.DEVNULL)
    except Exception:
        # Cache the failure too: a broken/slow scutil must not re-fork on every chunk.
        _macos_proxy_cache = (now, None)
        return None
    props = {
        key.strip(): val.strip()
        for key, sep, val in (line.strip().partition(" : ") for line in out.splitlines()) if sep}
    # Prefer HTTPS, fall back to HTTP
    resolved = None
    for enable_key, host_key, port_key in (
        ("HTTPSEnable", "HTTPSProxy", "HTTPSPort"), ("HTTPEnable", "HTTPProxy", "HTTPPort")):
        if props.get(enable_key) == "1" and props.get(host_key) and props.get(port_key):
            resolved = f"http://{props[host_key]}:{props[port_key]}"
            break
    _macos_proxy_cache = (now, resolved)
    return resolved


def reset_macos_proxy_cache() -> None:
    """Drop the memoised ``scutil --proxy`` answer so the next call re-reads it."""
    global _macos_proxy_cache

    _macos_proxy_cache = None


def should_bypass_proxy(target_hosts: str | list[str] | tuple[str, ...] | set[str] | None) -> bool:
    """True when NO_PROXY/no_proxy matches at least one target host (exact hosts, domain /
    wildcard suffixes, IP literals, CIDR ranges, optional host:port entries, ``*``)."""
    return _should_bypass_proxy(target_hosts)


def resolve_proxy_url(
    platform_env_var: str | None = None, *,
    target_hosts: str | list[str] | tuple[str, ...] | set[str] | None = None,
    configured: str | None = None) -> str | None:
    """Proxy URL: *platform_env_var* (e.g. ``DISCORD_PROXY``) first, then the adapter's own YAML
    value *configured* (``telegram.proxy_url``), then HTTPS_PROXY / HTTP_PROXY / ALL_PROXY (any
    case), then the macOS system proxy — the latter two only when ``gateway.trust_env`` is true.
    None when nothing is found or NO_PROXY matches a target.

    *platform_env_var* is a per-adapter, per-profile-configurable setting (each proxy URL can
    embed credentials, e.g. ``http://user:pass@host``) so it is read scope-aware: under a
    secondary multiplex profile it comes from that profile's own ``.env``, not the shared
    process env another profile's ``TELEGRAM_PROXY``/``DISCORD_PROXY``/etc. may hold; the YAML
    value is the same profile's, so a secondary keeps its configured route without any env
    bridge (#108440). The generic ``HTTPS_PROXY``/``HTTP_PROXY``/``ALL_PROXY`` fallback stays a raw
    process-env read — those are OS/system-level network settings, not a per-profile Hermes concept."""
    from gateway.platforms._shared import get_scoped_secret as _get_scoped_proxy_var
    value = (_get_scoped_proxy_var(platform_env_var, "") or "").strip() if platform_env_var else ""
    if not value:
        value = str(configured or "").strip()
    if not value:
        if not gateway_trust_env():  # only the explicit per-platform var is honored
            return None
        value = first_proxy_env_value()
    proxy = normalize_proxy_url(value or _detect_macos_system_proxy())
    return None if proxy and should_bypass_proxy(target_hosts) else proxy


def _aiohttp_socks_connector(proxy_url: str):
    """``aiohttp_socks.ProxyConnector`` for ``proxy_url``, or None when aiohttp_socks is missing
    (SOCKS logs a warning; HTTP callers fall back to ``proxy=``). ``rdns=True`` forces remote DNS
    through the proxy — required by Shadowrocket/Clash-style SOCKS and against GFW DNS pollution."""
    try:
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(proxy_url, rdns=True)
    except ImportError:
        if proxy_url.lower().startswith("socks"):
            logger.warning("aiohttp_socks not installed — SOCKS proxy %s ignored. "
                           "Use an HTTP proxy instead.", proxy_url)
        return None


def proxy_kwargs_for_bot(proxy_url: str | None) -> dict:
    """Kwargs for ``commands.Bot()`` / ``discord.Client()``: SOCKS → ``{"connector"}``,
    HTTP → ``{"proxy": url}``, None → ``{}``."""
    if not proxy_url:
        return {}
    if proxy_url.lower().startswith("socks"):
        connector = _aiohttp_socks_connector(proxy_url)
        return {"connector": connector} if connector is not None else {}
    return {"proxy": proxy_url}


def _config_section(name: str) -> dict:
    """Read-only ``config.yaml`` section ``name``; ``{}`` when unreadable/missing/not a dict."""
    try:
        from hermes_cli.config import load_config_readonly as _load_config
        cfg = _load_config()  # read-only: .get() only, never mutated
    except Exception:
        return {}
    section = cfg.get(name) if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def gateway_trust_env() -> bool:
    """``gateway.trust_env`` from config.yaml (default True): whether gateway
    ``aiohttp.ClientSession``s honor HTTP(S)_PROXY / NO_PROXY / SSL_CERT_FILE. Set false
    when the gateway inherits a proxy env it must not use. Fail-open to default."""
    value = _config_section("gateway").get("trust_env", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value) if value is not None else True


def proxy_kwargs_for_aiohttp(proxy_url: str | None) -> tuple[dict, dict]:
    """``(session_kwargs, request_kwargs)`` for a standalone ``aiohttp.ClientSession``. With
    aiohttp-socks every scheme uses a connector (mautrix-style libs never forward per-request
    ``proxy=``); without it HTTP falls back to ``({}, {"proxy": url})`` and SOCKS is ignored."""
    if not proxy_url:
        return {}, {}
    connector = _aiohttp_socks_connector(proxy_url)
    if connector is not None:
        return {"connector": connector}, {}
    return ({}, {}) if proxy_url.lower().startswith("socks") else ({}, {"proxy": proxy_url})


def is_host_excluded_by_no_proxy(hostname: str, no_proxy_value: str | None = None) -> bool:
    """Return True when ``hostname`` matches a ``NO_PROXY`` entry (``no_proxy_value`` overrides the
    environment); same matcher as :func:`should_bypass_proxy`."""
    return _should_bypass_proxy(hostname, no_proxy_value=no_proxy_value)


import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Any, Callable, Awaitable, Tuple, Union

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import fence_state_after
from gateway.platforms.base_exec_approval import (
    approval_timeout_seconds, ea_action_labels, ea_default_reason_text, ea_header_text,
    ea_reason_label_text, ea_smart_deny_line_text, format_approval_deadline_line)
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.warning_notifications import diagnostic_wake_muted
from hermes_cli.observability.shared_metrics_gateway import records_delivery, stop_reply_clock
from gateway.session import SessionSource, build_session_key
from gateway.session_transcript import TranscriptReadError
from hermes_constants import get_default_hermes_root, get_hermes_dir, get_hermes_home

if TYPE_CHECKING:
    from agent.display import ToolPreview

@dataclass
# --------------------------------------------------------------------------- Streaming TTS format
# descriptor and handle (#60671) ---------------------------------------------------------------------------
class AudioFormat:
    """Declared PCM format for a streaming-TTS session: every ``write_streaming_tts``
    chunk must be raw little-endian PCM at this rate / channels / sample width."""
    sample_rate: int = 24000
    channels: int = 1
    sample_width: int = 2  # bytes per sample (int16 = 2)


@dataclass
class StreamingTTSHandle:
    """Opaque handle returned by ``begin_streaming_tts``; adapters may extend it with
    platform state. The base fields are consumer bookkeeping / cancellation."""
    chat_id: str = ""
    audio_format: AudioFormat = field(default_factory=AudioFormat)
    # True once the first PCM chunk is written: a later failure then ends cleanly instead of
    # falling back to whole-file TTS (don't replay already-audible output).
    audible: bool = False
    aborted: bool = False  # set by abort_streaming_tts; late chunks are dropped


def streaming_tts_turn_key(session_key: str | None, turn_marker: Any = None, *, event: Any = None) -> str | None:
    """Per-turn streaming-TTS suppression key — turn-scoped (not chat-scoped) so
    overlapping turns in one chat can't suppress each other's fallback paths.
    ``turn_marker`` is normally the run generation, else the event's message/update id."""
    if not session_key:
        return None
    if turn_marker is None and event is not None:
        turn_marker = getattr(event, "message_id", None) or getattr(event, "platform_update_id", None)
    return None if turn_marker is None else f"{session_key}:{turn_marker}"


def streaming_tts_should_skip_whole_file(completed_turns: set[str], session_key: str | None,
                                         turn_marker: Any = None, *, event: Any = None) -> bool:
    """Pure, turn-scoped auto-TTS suppression decision (testable without the adapter stack)."""
    turn_key = streaming_tts_turn_key(session_key, turn_marker, event=event)
    return bool(turn_key and turn_key in completed_turns)


GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE = (
    "Secure secret entry is not supported over messaging. "
    "Load this skill in the local CLI to be prompted, or add the key to ~/.hermes/.env manually.")

# One sentence for every "you may not press/run this" refusal on every platform (slash commands,
# approval buttons, pickers, prompts). ``{platform}`` is the ``Platform.value`` for the
# ``hermes pairing approve`` command (hermes_cli/subcommands/pairing.py) that lets the owner fix it.
# Kept under 200 chars: Telegram's answerCallbackQuery truncates longer text.
UNAUTHORIZED_ACTION_NOTICE = (
    "This bot is private and you're not on its allowed list. If you own it, run "
    "`hermes pairing approve {platform} <request-id>` on the host (`hermes pairing list` shows the id).")


def unauthorized_action_notice(platform: Any) -> str:
    """Localized ``UNAUTHORIZED_ACTION_NOTICE`` for a ``Platform`` member or its string name.
    Call it per refusal — never bind the result at import, or the language freezes."""
    name = getattr(platform, "value", platform)
    return t("gateway.unauthorized.action_notice", platform=str(name or "<platform>"))


def safe_url_for_log(url: str, max_len: int = 80) -> str:
    """Return a URL string safe for logs (no query/fragment/userinfo)."""
    raw = str(url) if max_len > 0 and url is not None else ""
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw[:max_len]
    safe = raw
    if parsed.scheme and parsed.netloc:
        # Strip potential embedded credentials (user:pass@host).
        path = parsed.path or ""
        basename = path.rsplit("/", 1)[-1]
        tail = "" if path in ("", "/") else f"/.../{basename}" if basename else "/..."
        safe = f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}{tail}"
    if len(safe) <= max_len:
        return safe
    return "." * max_len if max_len <= 3 else f"{safe[:max_len - 3]}..."


async def _ssrf_redirect_guard(response):
    """Re-validate each redirect target (a public URL 302-ing to http://169.254.169.254/ would
    bypass the pre-flight is_safe_url()). Async because httpx awaits response event hooks."""
    from tools.url_safety import is_safe_url, redirect_target_from_response
    redirect_url = redirect_target_from_response(response)
    if redirect_url and not is_safe_url(redirect_url):
        raise ValueError(f"Blocked redirect to private/internal address: {safe_url_for_log(redirect_url)}")


# Inbound images are cached locally for the vision tool (platform URLs are ephemeral).
# Import-time default; tests monkeypatch it, getters re-resolve per call.
IMAGE_CACHE_DIR = get_hermes_dir("cache/images", "image_cache")


# Inbound media cap (``gateway.max_inbound_media_bytes``): payloads are buffered fully in memory,
# so an uncapped upload (Discord Nitro: 500 MB) could OOM-kill the gateway.
# Inbound image / audio / video payloads are buffered fully into process memory before being written to the
# cache directory. With no cap, a single large upload (Discord Nitro allows 500 MB) — or a remote URL in an
# inbound message payload pointing at an arbitrarily large file — can spike RAM and OOM-kill the gateway.
# The ``cache_*_from_bytes`` helpers (the shared funnel every platform reaches eventually) and the
# ``cache_*_from_url`` downloaders enforce this cap, so the protection holds regardless of which platform
# adapter or code path produced the bytes. Configurable via ``gateway.max_inbound_media_bytes`` in
# config.yaml. ``0`` disables the cap. Default 128 MiB — generous enough for ordinary photos/voice
# notes/short clips while still bounding a hostile upload.
# --------------------------------------------------------------------------- See #13145.
DEFAULT_INBOUND_MEDIA_MAX_BYTES = 128 * 1024 * 1024


def get_inbound_media_max_bytes() -> int:
    """Max inbound media bytes held in memory (``gateway.max_inbound_media_bytes``);
    ``0`` / negative / unparseable disables the cap; unreadable config → default."""
    return _or_default(lambda: int(_config_section("gateway")["max_inbound_media_bytes"]),
                       DEFAULT_INBOUND_MEDIA_MAX_BYTES, (KeyError, TypeError, ValueError))


def validate_inbound_media_size(
    size: int, *, media_type: str = "media", max_bytes: Optional[int] = None) -> None:
    """Raise ``ValueError`` if an inbound payload exceeds the cap (``max_bytes`` of ``0``
    disables it; pass it explicitly to resolve the limit once across an incremental read)."""
    limit = get_inbound_media_max_bytes() if max_bytes is None else max_bytes
    if limit and size > limit:
        raise ValueError(f"Inbound {media_type} payload is too large ({size} bytes > {limit} bytes)")


async def _read_httpx_body_with_limit(response, *, media_type: str) -> bytes:
    """Read an httpx streaming body under the media cap: reject an oversized ``Content-Length``
    early, then re-check the running total per chunk (a lying/absent header can't smuggle more)."""
    max_bytes = get_inbound_media_max_bytes()
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            logger.debug("Ignoring invalid Content-Length for inbound %s: %r", media_type, content_length)
        else:
            validate_inbound_media_size(declared_size, media_type=media_type, max_bytes=max_bytes)
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        validate_inbound_media_size(total, media_type=media_type, max_bytes=max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def _cache_dir_accessors(kind: str, constant_name: str, new_subpath: str, old_name: str):
    """``(get_<kind>_cache_dir, cleanup_<kind>_cache)`` pair. The getter resolves fresh via
    get_hermes_dir (active profile) unless a test monkeypatched the module constant away from
    its import-time default, and creates the directory; ``cleanup(max_age_hours=24)`` deletes
    older files and returns the count."""
    def get_dir() -> Path:
        d = get_hermes_dir(new_subpath, old_name)
        current = globals().get(constant_name)
        default = _CACHE_DIR_IMPORT_DEFAULTS.get(constant_name)
        if current is not None and default is not None and current != default:
            d = Path(current)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cleanup(max_age_hours: int = 24) -> int:
        return _cleanup_cache_dir(get_dir(), max_age_hours)
    get_dir.__name__ = get_dir.__qualname__ = f"get_{kind}_cache_dir"
    cleanup.__name__ = cleanup.__qualname__ = f"cleanup_{kind}_cache"
    return get_dir, cleanup


get_image_cache_dir, cleanup_image_cache = _cache_dir_accessors(
    "image", "IMAGE_CACHE_DIR", "cache/images", "image_cache")


def _looks_like_image(data: bytes) -> bool:
    """Return True if *data* starts with a known image magic-byte sequence."""
    return len(data) >= 4 and (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
               or data[:6] in {b"GIF87a", b"GIF89a"} or data[:2] == b"BM"
               or (data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP"))


def _write_cache_file(cache_dir: Path, prefix: str, ext: str, data: bytes) -> str:
    """Write ``data`` to ``<cache_dir>/<prefix>_<uuid12><ext>``; return the path string."""
    filepath = cache_dir / f"{prefix}_{uuid.uuid4().hex[:12]}{ext}"
    filepath.write_bytes(data)
    return str(filepath)


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    """Save raw image bytes to the cache and return the absolute path; raises
    ValueError when *data* isn't an image (e.g. an upstream HTML error page)."""
    validate_inbound_media_size(len(data), media_type="image")
    if not _looks_like_image(data):
        snippet = data[:80].decode("utf-8", errors="replace")
        raise ValueError(f"Refusing to cache non-image data as {ext} (starts with: {snippet!r})")
    return _write_cache_file(get_image_cache_dir(), "img", ext, data)


async def cache_image_from_bytes_async(data: bytes, ext: str = ".jpg") -> str:
    """Cache image bytes without blocking the caller's event loop."""
    return await asyncio.to_thread(cache_image_from_bytes, data, ext)


async def _cache_media_from_url(url: str, ext: str, retries: int, *, media_type: str, accept: str,
                                cache_fn, log_label: str) -> str:
    """Shared downloader behind ``cache_*_from_url``: SSRF-checked (pre-flight + per-redirect;
    raises ValueError), size-capped, linear-backoff retries on timeouts / 429 / 5xx."""
    from tools.url_safety import create_ssrf_safe_async_client, is_safe_url
    import httpx
    if not is_safe_url(url):
        raise ValueError(f"Blocked unsafe URL (SSRF protection): {safe_url_for_log(url)}")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)", "Accept": accept}
    async with create_ssrf_safe_async_client(
        timeout=30.0, follow_redirects=True, event_hooks={"response": [_ssrf_redirect_guard]},
    ) as client:
        for attempt in range(retries + 1):
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    content = await _read_httpx_body_with_limit(response, media_type=media_type)
                return await asyncio.to_thread(cache_fn, content, ext)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                    raise
                if attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    logger.debug("%s cache retry %d/%d for %s (%.1fs): %s", log_label, attempt + 1,
                                 retries, safe_url_for_log(url), wait, exc)
                    await asyncio.sleep(wait)
                    continue
                raise


async def cache_image_from_url(url: str, ext: str = ".jpg", retries: int = 2) -> str:
    """Download an image URL into the image cache; return the absolute path."""
    return await _cache_media_from_url(
        url, ext, retries, media_type="image", accept="image/*,*/*;q=0.8",
        cache_fn=cache_image_from_bytes, log_label="Media")


def _cleanup_cache_dir(cache_dir: Path, max_age_hours: int) -> int:
    """Delete files in *cache_dir* older than *max_age_hours*; return the count removed."""
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            with contextlib.suppress(OSError):
                f.unlink()
                removed += 1
    return removed


# Audio cache utilities (same pattern as images; feeds the STT tool).
AUDIO_CACHE_DIR = get_hermes_dir("cache/audio", "audio_cache")
get_audio_cache_dir, cleanup_audio_cache = _cache_dir_accessors(
    "audio", "AUDIO_CACHE_DIR", "cache/audio", "audio_cache")


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    """Save raw audio bytes to the cache (container-sniffed ext); return the path."""
    # tools.audio_container is the ONE owner of container detection (outbound TTS repair + here).
    from tools.audio_container import sniff_audio_ext
    validate_inbound_media_size(len(data), media_type="audio")
    return _write_cache_file(get_audio_cache_dir(), "audio", sniff_audio_ext(data, ext), data)


async def cache_audio_from_bytes_async(data: bytes, ext: str = ".ogg") -> str:
    """Cache audio bytes without blocking the caller's event loop."""
    return await asyncio.to_thread(cache_audio_from_bytes, data, ext)


async def cache_audio_from_url(url: str, ext: str = ".ogg", retries: int = 2) -> str:
    """Download an audio URL into the audio cache; return the absolute path."""
    return await _cache_media_from_url(
        url, ext, retries, media_type="audio", accept="audio/*,*/*;q=0.8",
        cache_fn=cache_audio_from_bytes, log_label="Audio")


# Video cache utilities (same pattern; referenced by local path).
VIDEO_CACHE_DIR = get_hermes_dir("cache/videos", "video_cache")
get_video_cache_dir, cleanup_video_cache = _cache_dir_accessors(
    "video", "VIDEO_CACHE_DIR", "cache/videos", "video_cache")

SUPPORTED_VIDEO_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}


def cache_video_from_bytes(data: bytes, ext: str = ".mp4") -> str:
    """Save raw video bytes to the cache and return the absolute file path."""
    validate_inbound_media_size(len(data), media_type="video")
    return _write_cache_file(get_video_cache_dir(), "video", ext, data)


async def cache_video_from_bytes_async(data: bytes, ext: str = ".mp4") -> str:
    """Cache video bytes without blocking the caller's event loop."""
    return await asyncio.to_thread(cache_video_from_bytes, data, ext)


# Document / screenshot cache utilities (same pattern; referenced by local path).
DOCUMENT_CACHE_DIR = get_hermes_dir("cache/documents", "document_cache")
SCREENSHOT_CACHE_DIR = get_hermes_dir("cache/screenshots", "browser_screenshots")
get_document_cache_dir, cleanup_document_cache = _cache_dir_accessors(
    "document", "DOCUMENT_CACHE_DIR", "cache/documents", "document_cache")
get_screenshot_cache_dir, cleanup_screenshot_cache = _cache_dir_accessors(
    "screenshot", "SCREENSHOT_CACHE_DIR", "cache/screenshots", "browser_screenshots")

# Import-time defaults; _resolve_cache_dir compares against these to detect a test monkeypatch.
_CACHE_DIR_IMPORT_DEFAULTS = {
    "IMAGE_CACHE_DIR": IMAGE_CACHE_DIR, "AUDIO_CACHE_DIR": AUDIO_CACHE_DIR,
    "VIDEO_CACHE_DIR": VIDEO_CACHE_DIR, "DOCUMENT_CACHE_DIR": DOCUMENT_CACHE_DIR,
    "SCREENSHOT_CACHE_DIR": SCREENSHOT_CACHE_DIR}

# Launch-time homes: fine for the static ALLOW roots below (per-profile cache roots are
# enumerated at check time), never for the credential DENY side — see _credential_home_roots.
_HERMES_HOME = get_hermes_home()
_HERMES_ROOT = get_default_hermes_root()
MEDIA_DELIVERY_ALLOW_DIRS_ENV = "HERMES_MEDIA_ALLOW_DIRS"
MEDIA_DELIVERY_TRUST_RECENT_ENV = "HERMES_MEDIA_TRUST_RECENT_FILES"
MEDIA_DELIVERY_TRUST_RECENT_SECONDS_ENV = "HERMES_MEDIA_TRUST_RECENT_SECONDS"
# Strict mode = allowlist+recency validation; off by default (the denylist still blocks
# credential / system paths). Set true on public-facing gateways.
MEDIA_DELIVERY_STRICT_ENV = "HERMES_MEDIA_DELIVERY_STRICT"
# Canonical cache subdirs of deliverable artifacts; also enumerates per-profile cache roots.
_MEDIA_DELIVERY_CACHE_SUBDIRS = ("images", "audio", "videos", "documents", "screenshots")
MEDIA_DELIVERY_SAFE_ROOTS = (
    IMAGE_CACHE_DIR, AUDIO_CACHE_DIR, VIDEO_CACHE_DIR, DOCUMENT_CACHE_DIR, SCREENSHOT_CACHE_DIR,
    *(_HERMES_HOME / d for d in (
        "image_cache", "audio_cache", "video_cache", "document_cache", "browser_screenshots")),
    # Canonical cache layout, alongside the legacy *_cache dirs (installs may have both).
    *(_HERMES_HOME / "cache" / d for d in _MEDIA_DELIVERY_CACHE_SUBDIRS))

# Recency window (s) for trusting fresh files: artifacts land seconds before delivery,
# pre-existing host files (/etc/passwd, ~/.ssh/id_rsa) are days/months old.
_MEDIA_DELIVERY_TRUST_RECENT_DEFAULT_SECONDS = 600

# Hard denylist even for "recent" files (credentials, system state, /proc); the cache-dir
# allowlist still beats it.
_MEDIA_DELIVERY_DENIED_PREFIXES = (
    "/etc", "/proc", "/sys", "/dev", "/root", "/boot", "/var/log", "/var/lib", "/var/run")

# Credential / config dirs denied under $HOME (Library/Keychains = macOS), resolved at check time.
_MEDIA_DELIVERY_DENIED_HOME_SUBPATHS = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config", ".azure", ".gcloud",
    "Library/Keychains")

def _sqlite_files(name: str) -> tuple[str, ...]:
    """A SQLite store plus its WAL/SHM/rollback-journal sidecars."""
    return (name, f"{name}-wal", f"{name}-shm", f"{name}-journal")


# Credential stores at the HERMES_HOME root, denied per-file so skills/, logs/ and agent-written
# files stay deliverable (cache subdirs are allowlisted BEFORE this). A superset of the
# agent/file_safety.py read+write denies so exfil never trails the read guard. google_token.json's mtime bumps every turn (defeats the
# recency window); pairing/ and mcp-tokens/ (live OAuth tokens) are denied as whole trees.
_ROOT_CREDENTIAL_PATHS = (
    ".env", "auth.json", "auth.lock", "credentials", "config.yaml", ".anthropic_oauth.json",
    "google_token.json", "google_oauth_pending.json", os.path.join("auth", "google_oauth.json"),
    "webhook_subscriptions.json", os.path.join("cache", "bws_cache.json"),
    os.path.join("cache", "bws_cache.enc.json"), "pairing", "mcp-tokens",
    # Whole conversation history (every secret ever pasted into a chat) and the copied browser
    # cookie/login store; sessions/ is the legacy transcript dir. SQLite sidecars are listed
    # too: WAL mode touches state.db-wal on every write, so recency trust alone would leak them.
    "sessions", "browser-profile", *_sqlite_files("state.db"), *_sqlite_files("kanban.db"))


def _profile_cache_roots() -> List[Path]:
    """Per-profile cache roots ``<root>/profiles/<name>/cache/{images,...}`` (the static safe
    roots cover only the active HERMES_HOME). Enumerated at check time so profiles created after
    startup count and are allowlisted BEFORE the ``/root`` denylist (HERMES_HOME symlinked).

    ``HERMES_HOME=/opt/data``) while the model emits a profile-scoped path silently fails delivery.
    Enumerated dynamically at check time so profiles created after startup are covered, and so the resolved
    profile path is allowlisted *before* the ``/root`` system denylist is consulted (which otherwise wins
    when HERMES_HOME is symlinked under a denied prefix and $HOME is not that prefix). See issue #31733.
    """
    return [p / "cache" / subdir for p in _profile_dirs() for subdir in _MEDIA_DELIVERY_CACHE_SUBDIRS]


def _profile_dirs() -> List[Path]:
    """Every ``<root>/profiles/<name>`` directory, read at check time."""
    try:
        return [p for p in (_HERMES_ROOT / "profiles").iterdir() if p.is_dir()]
    except OSError:
        return []


def _credential_home_roots() -> List[Path]:
    """Every Hermes home whose credential stores the denylist must cover: the ACTIVE home
    (the per-turn HERMES_HOME override under ``gateway.multiplex_profiles``), the shared root
    and every ``<root>/profiles/*``. Enumerated at check time like ``_profile_cache_roots`` on
    the allow side — a denylist frozen at import covers only the launch profile, so a
    ``MEDIA:<root>/profiles/<other>/.env`` emitted in any profile's turn would upload it."""
    return list(dict.fromkeys((get_hermes_home(), _HERMES_ROOT, *_profile_dirs())))


def _kanban_root() -> Path:
    """Kanban is root-shared across profiles by design (``kanban_db.kanban_home``)."""
    return Path(os.environ.get("HERMES_KANBAN_HOME", "").strip() or _HERMES_ROOT).expanduser()


def _kanban_board_dirs() -> List[Path]:
    """Every directory under ``<root>/kanban/boards`` (lax on purpose: the DENY side must catch a
    board whatever its name; the allow side filters further)."""
    with contextlib.suppress(OSError):
        return [p for p in (_kanban_root() / "kanban" / "boards").iterdir() if p.is_dir()]
    return []


def _kanban_attachment_roots() -> List[Path]:
    """Return durable Kanban attachment roots without importing kanban_db."""
    override = os.environ.get("HERMES_KANBAN_ATTACHMENTS_ROOT", "").strip()
    if override:
        return [Path(override).expanduser()]
    roots = [_kanban_root() / "kanban" / "attachments"]
    roots.extend(path / "attachments" for path in _kanban_board_dirs()
                 if not path.is_symlink() and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", path.name)
                 and (path / "kanban.db").is_file())
    return roots


def _media_delivery_allowed_roots() -> List[Path]:
    """Return roots from which model-emitted local media may be delivered."""
    from gateway.media_policy import media_delivery_allow_dirs
    operator_roots = (
        root for chunk in media_delivery_allow_dirs().split(os.pathsep)
        for raw_root in chunk.split(",")
        if (root := Path(os.path.expanduser(raw_root.strip()))).is_absolute())
    return [*map(Path, MEDIA_DELIVERY_SAFE_ROOTS), *_profile_cache_roots(),
            *_kanban_attachment_roots(), *operator_roots]


def _media_delivery_recency_seconds() -> float:
    """Recency window (seconds) for trusting fresh files; 0 = pure-allowlist mode."""
    from gateway.media_policy import media_delivery_trust_recent, media_delivery_trust_recent_seconds
    if not media_delivery_trust_recent():
        return 0.0
    custom = media_delivery_trust_recent_seconds().strip()
    default = float(_MEDIA_DELIVERY_TRUST_RECENT_DEFAULT_SECONDS)
    return _or_default(lambda: max(0.0, float(custom)) if custom else default, default)


def _kanban_board_db_paths() -> List[Path]:
    """Named-board ``kanban.db`` stores (+ sidecars): they sit beside the ATTACHMENTS dir
    ``_kanban_attachment_roots`` allowlists and hold every task, comment and run transcript."""
    return [board / name for board in _kanban_board_dirs() for name in _sqlite_files("kanban.db")]


def _media_delivery_denied_paths() -> List[Path]:
    """Return absolute denylist paths under which delivery is never allowed."""
    home = Path(os.path.expanduser("~"))
    return [*map(Path, _MEDIA_DELIVERY_DENIED_PREFIXES),
            *(home / sub for sub in _MEDIA_DELIVERY_DENIED_HOME_SUBPATHS),
            *(r / rel for r in _credential_home_roots() for rel in _ROOT_CREDENTIAL_PATHS),
            *_kanban_board_db_paths()]


def _resolve_path(path: Path, *, strict: bool = False, expand: bool = False) -> Optional[Path]:
    """``path[.expanduser()].resolve(strict)`` or None when it fails (OSError / RuntimeError /
    ValueError — embedded NUL, symlink loop, undeterminable home, missing file under ``strict``)."""
    try:
        return (path.expanduser() if expand else path).resolve(strict=strict)
    except (OSError, RuntimeError, ValueError):
        return None


def _path_under_denied_prefix(resolved: Path) -> bool:
    """True if ``resolved`` lives under a deny-listed system path — except a denied prefix that
    IS the running user's own home: ``/root`` is listed so a non-root gateway can't deliver
    another user's home, but a root-run gateway's own deliverables live under ``$HOME=/root``.
    Credential sub-dirs (``~/.ssh``, ``~/.hermes/.env``) stay blocked (more-specific entries)."""
    home = _resolve_path(Path(os.path.expanduser("~")))
    for denied in _media_delivery_denied_paths():
        resolved_denied = _resolve_path(denied, expand=True)
        if resolved_denied is None:
            continue
        hit = resolved == resolved_denied or _path_is_within(resolved, resolved_denied)
        if hit and resolved_denied != home:
            return True
    return False


def _file_is_recently_produced(resolved: Path, window_seconds: float) -> bool:
    """True if mtime is within ``window_seconds`` — a session-scoped trust signal: agents
    produce artifacts seconds before sending; pre-existing host files are days/months old."""
    if window_seconds <= 0:
        return False
    try:
        return (time.time() - resolved.stat().st_mtime) <= window_seconds
    except OSError:
        return False


def _path_is_within(path: Path, root: Path) -> bool:
    with contextlib.suppress(ValueError):
        path.relative_to(root)
        return True
    return False


def _tenv(name: str, default: str = "") -> str:
    """Scope-aware TERMINAL_* read: the per-turn scope carries the ACTIVE profile's settings while
    os.getenv reads whatever a prior turn pinned into the process env. Only ImportError falls
    back — a refusal scope must raise rather than rebuild another profile's policy from the env."""
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.getenv(name, default)
    return terminal_env(name, default)


def _parse_docker_volume_mounts() -> List[Tuple[Path, Path]]:
    """Parse ``TERMINAL_DOCKER_VOLUMES`` (JSON list of ``host:container[:mode]``) into
    ``(host_path, container_path)``; named volumes / non-absolute hosts can't resolve here."""
    raw = _tenv("TERMINAL_DOCKER_VOLUMES", "").strip()
    try:
        import json as _json
        parsed = _json.loads(raw) if raw else []
    except Exception:
        return []
    mounts: List[Tuple[Path, Path]] = []
    for entry in parsed if isinstance(parsed, list) else ():
        spec = entry.strip() if isinstance(entry, str) else ""
        # Prefer the first ':/' so absolute container paths are unambiguous.
        sep = spec.find(":/")
        if sep <= 0:
            continue
        container_raw = spec[sep + 1:].split(":", 1)[0]  # starts with /
        # Skip named volumes (no absolute/drive host path).
        host_expanded = os.path.expanduser(spec[:sep])
        if not (host_expanded.startswith("/") or (len(host_expanded) > 1 and host_expanded[1] == ":")):
            continue
        host_path, container_path = _resolve_path(Path(host_expanded)), Path(container_raw)
        if host_path is not None and container_path.is_absolute():
            mounts.append((host_path, container_path))
    return mounts


def _docker_sandbox_dir_candidates(session_key: str = "") -> List[str]:
    """Candidate host sandbox dir names for the delivering session, best first. Mirrors
    ``_resolve_container_task_id`` (tools/terminal_tool.py): containers are PROFILE-scoped
    (``default``, else ``profile:<name>``); legacy ``session:<key>`` sandboxes stay as a fallback.
    The key is passed explicitly because delivery runs after the turn's contextvars were cleared.

    Takes the key explicitly because the delivery pipeline runs after ``_handle_message_with_agent`` cleared
    the turn's session contextvars (#93950) — an ambient lookup here would silently collapse onto
    ``default`` and miss the session's real sandbox.
    """
    try:
        from tools.environments.path_utils import sanitize_task_id_for_path
    except Exception:
        return ["default"]
    try:
        from hermes_cli.profiles import get_active_profile_name
        profile = get_active_profile_name() or "default"
    except Exception:
        profile = "default"
    candidates: List[str] = []
    # Explicit trusted-profiles opt-in: one shared container identity.
    if shared := _tenv("TERM