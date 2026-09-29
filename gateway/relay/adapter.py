"""RelayAdapter — one generic gateway adapter fronted by the connector. EXPERIMENTAL.

A single ``BasePlatformAdapter`` subclass that receives a ``CapabilityDescriptor`` at
handshake (which platform it fronts, which capabilities to advertise) and delegates
all wire I/O to an injected transport. There is NO per-platform gateway code: only
the connector knows "this chat_id maps to a Discord channel"; the gateway sees an
ordinary ``MessageEvent`` in and calls ``adapter.send`` out. Transport protocol and
descriptor schema may change without a deprecation cycle until >=2 Class-1 platforms
validate them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple, Union

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter, ExecApprovalPrompt, SendResult,
)
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from agent.i18n import t
from gateway.relay.descriptor import CapabilityDescriptor
from gateway.relay.egress import (
    EGRESS_DECLINE_CODE,
    decline_error,
    is_egress_decline,
    log_decline,
)
from gateway.relay.media import RelayMediaClient
from gateway.relay.transport import RelayTransport
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

# The drain-path going-idle ACK budget must stay strictly under the runner's
# default adapter disconnect timeout (5s) or cancellation fires before
# transport.disconnect() and leaves the websocket open. With transport teardown
# budgets of 1s each for supervisor, reader and ws.close, the drain stays <5s.
_RELAY_GO_IDLE_ON_DISCONNECT_TIMEOUT_S = 2.0
_RELAY_REVOCATION_MONITOR_TEARDOWN_TIMEOUT_S = 1.0

# Link detection for the fresh-final unfurl route: raw URLs, Slack mrkdwn links
# and markdown links. Permissive on purpose — a false positive costs one fresh
# (non-edited) final; a false negative silently loses the preview.
_URL_RE = re.compile(r"https?://|<https?:|\]\(https?:")

# Already-answered prompt ids to remember so a duplicate answer (double tap or
# connector redelivery) reads as a repeat, not a stale prompt.
_RESOLVED_PROMPT_MEMORY = 256

# Connector promptCodec.decodePromptCallback id alphabet ([A-Za-z0-9_.-], <=32).
_PROMPT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

_SLACK = Platform.SLACK.value

# Prompt option id -> in-channel ack label (the option set doubles as the choice allowlist).
_EXEC_APPROVAL_LABELS = {
    "once": "✅ Approved once",
    "session": "✅ Approved for session",
    "always": "✅ Approved permanently",
    "deny": "❌ Denied",
}
_SLASH_CONFIRM_LABELS = {"once": "✅ Approved once", "always": "🔒 Always approve", "cancel": "❌ Cancelled"}


def _utf16_len(text: str) -> int:
    """Count UTF-16 code units (Telegram's length unit)."""
    return len(text.encode("utf-16-le")) // 2


_LEN_FNS: Dict[str, Callable[[str], int]] = {"chars": len, "utf16": _utf16_len}


def _send_result(result: Dict[str, Any], **extra: Any) -> SendResult:
    """Project a connector ``outbound_result`` dict onto a SendResult."""
    return SendResult(
        success=bool(result.get("success")), message_id=result.get("message_id"),
        error=result.get("error"), **extra,
    )


def _event_ids(event) -> Tuple[Optional[str], Optional[str]]:
    """(message_id, chat_id) of an inbound event; message_id lives on the event, falls back to source."""
    message_id = getattr(event, "message_id", None) or getattr(event.source, "message_id", None)
    return message_id, getattr(event.source, "chat_id", None)


def _profile_from_session_key(session_key: str) -> Optional[str]:
    """Named profile encoded in an ``agent:<ns>:...`` session key; None for the legacy ``agent:main``
    namespace (single-profile gateway) so the wire frame stays byte-identical there."""
    parts = (session_key or "").split(":")
    if len(parts) < 2 or parts[0] != "agent" or not parts[1]:
        return None
    from gateway.session import profile_from_session_key_namespace
    profile = profile_from_session_key_namespace(parts[1])
    return None if profile == "default" else profile


class RelayAdapter(BasePlatformAdapter):
    """Generic relay adapter advertising a connector-negotiated capability profile."""

    def __init__(
        self,
        config: PlatformConfig,
        descriptor: CapabilityDescriptor,
        transport: Optional[RelayTransport] = None,
    ) -> None:
        # Fronts many platforms but presents to the runner as Platform.RELAY.
        super().__init__(config, Platform.RELAY)
        self._transport = transport
        self._apply_descriptor(descriptor)
        # Per-chat egress routing caches learned from inbound events (send() only
        # receives a chat_id). The connector's egress guard resolves the owning tenant
        # from OUTBOUND metadata.scope_id / user_id, so we re-attach what we saw
        # inbound (_capture_scope).
        self._scope_by_chat: Dict[str, str] = {}
        self._dm_user_by_chat: Dict[str, str] = {}
        # chat_id -> chat_type: reproduces native Slack's synthetic DM-thread
        # suppression (a raw reply_to becomes a thread_ts connector-side, so a plain
        # DM reply would thread under the user).
        self._chat_type_by_chat: Dict[str, str] = {}
        # chat_id -> last triggering Slack message ts (typing/status lane's
        # synthetic thread anchor in thread-per-message mode).
        self._last_inbound_ts_by_chat: Dict[str, str] = {}
        # chat_id -> UNDERLYING platform ("discord", ...): one adapter fronts N
        # platforms on one WS and a reply must egress through the platform the
        # inbound came from. Empty for a single-platform gateway (connector default).
        self._platform_by_chat: Dict[str, str] = {}
        # chat_id -> Hermes profile the connector routed the inbound to (multiplex mode). Echoed
        # on every outbound frame's metadata so the connector can stamp the SAME profile on the
        # next passthrough_forward for that chat; empty on a single-profile gateway.
        self._profile_by_chat: Dict[str, str] = {}
        # Chats the connector has refused (see the terminal-decline latch).
        # chat_id -> (thread_id, initial_name) of the auto-thread the CONNECTOR
        # created for our latest send; read by the semantic thread-rename lane.
        self._auto_thread_by_chat: Dict[str, Tuple[str, str]] = {}
        # chat_id -> event fired when the entry above lands (wait_for_auto_thread_info).
        self._auto_thread_waiters: Dict[str, asyncio.Event] = {}
        # Bounded FIFO seen-set for inbound replay dedupe (insertion-ordered dict).
        self._seen_inbound: Dict[str, None] = {}
        # Live cards: draft_key -> draft_id of the OPEN native stream. Armed by
        # send_draft; consumed by send() to convert the turn-final into
        # draft(final=true) instead of a duplicate post. Keyed by _draft_key (chat +
        # per-turn identity), NOT bare chat: parallel turns in one DM are distinct
        # streams (per-chat keying merged three concurrent turns).
        self._open_draft_by_chat: Dict[str, int] = {}
        # draft_key -> draft_id of the most recently SEALED stream (mirror of the
        # connector's sealed-key tombstone): post-seal stragglers must neither
        # re-arm interception nor re-open a stream.
        self._sealed_draft_by_chat: Dict[str, int] = {}
        # Draft keys whose post-seal swallow has been logged once (bounded FIFO).
        self._tombstone_swallow_logged: Dict[str, int] = {}
        # Strong refs for fire-and-forget lifecycle acks (asyncio holds tasks weakly).
        self._lifecycle_ack_tasks: set = set()
        # Stream-is-the-message marker read by the stream consumer to keep ONE draft
        # stream per turn instead of bumping draft_id at tool boundaries. SLACK-ONLY:
        # the base send_draft contract is Telegram-shaped (draft clears, final is a
        # separate real send); setting this for any "draft" connector intercepted the
        # turn-final into draft(final=true) and no history message was ever posted.
        # A future platform with this semantic should advertise it via the descriptor.
        self.draft_stream_is_message = str(getattr(descriptor, "platform", "") or "").lower() == "slack"
        # Watches the transport for a terminal auth revocation (4401 after a
        # successful handshake = operator opted this instance out) and surfaces a
        # clean non-retryable "relay disabled" fatal instead of a retry spin.
        self._revocation_monitor: Optional[asyncio.Task[None]] = None
        # Lazily built client for the connector's /relay/media routes; None when
        # dial URL or creds are absent (media lanes degrade to text fallbacks).
        self._media_client: Optional["RelayMediaClient"] = None
        # prompt_id -> pending-prompt state for the interactive `prompt` op; the
        # user's pick comes back as a prompt_response naming this id and resolves the
        # waiting primitive like native button callbacks. Expire lazily (_pop_prompt).
        self._pending_prompts: Dict[str, Dict[str, Any]] = {}
        # Per-process marker prefixed onto every prompt id we mint. WHY: button
        # presses ride the passthrough plane, which the connector fans out to EVERY
        # live gateway session of the tenant, while _pending_prompts is process-local.
        # Without the marker a sibling cannot tell "someone else owns this" from "my
        # prompt expired", and the id-shaped text ("/c1") falls through to chat
        # dispatch as "Unknown command" — once per sibling (common in a DM).
        self._prompt_owner_nonce: str = secrets.token_hex(3)
        # Prompt ids this process already resolved, newest last (repeat answers are
        # consumed silently instead of treated as stale).
        self._resolved_prompts: "OrderedDict[str, float]" = OrderedDict()

    # ── capability surface (from descriptor) ─────────────────────────────
    @property
    def authorization_is_upstream(self) -> bool:
        """The connector enforces authorization (owner-only author-binding before
        delivery), so relay users must not be default-denied for lack of a local
        ``RELAY_ALLOWED_USERS`` allowlist."""
        return True

    @property
    def message_len_fn(self) -> Callable[[str], int]:
        return _LEN_FNS.get(self.descriptor.len_unit, len)

    @property
    def supports_status_text(self) -> bool:  # type: ignore[override]
        """Whether the fronted platform renders a TEXT status line: Slack's typing
        surface is the assistant status line, so run.py's live-status lane may feed
        per-tool phrases; other platforms have textless bubbles and must NOT receive
        them. Reflects the PRIMARY identity, like the scalar ``descriptor``."""
        return self.descriptor.platform == _SLACK

    # ── per-chat capability resolution (multi-platform) ──────────────────
    def _negotiated_descriptor(self, platform: Optional[str]) -> Optional[CapabilityDescriptor]:
        """The transport's negotiated descriptor for ``platform``, or None (unknown
        platform, no transport, or a transport predating ``descriptor_for_platform``).
        Never raises — capability lookup must never break a send."""
        resolve = getattr(self._transport, "descriptor_for_platform", None) if platform else None
        if not callable(resolve):
            return None
        try:
            return resolve(platform)
        except Exception:  # noqa: BLE001
            return None

    def _chat_platform(self, chat_id: str) -> Optional[str]:
        """The chat's underlying platform as seen inbound, else the primary's."""
        return self._platform_by_chat.get(str(chat_id)) or self.descriptor.platform

    def _metrics_platform(self, chat_id: str) -> Optional[str]:
        """The platform a chat's shared metrics carry: the inbound's, else the primary's only when this
        socket fronts one platform (a multi-platform connector's unknown chat stays unlabelled)."""
        fronted = {p for p, _ in (getattr(self._transport, "_identities", None) or ())}
        return self._platform_by_chat.get(str(chat_id)) or (self.descriptor.platform if len(fronted) <= 1 else None)

    def warning_notifications_enabled(self, logical_platform=None, *, chat_id=None, metadata=None) -> bool:
        platform = (logical_platform or (metadata or {}).get("_relay_logical_platform")
                    or self._chat_platform(chat_id))
        return super().warning_notifications_enabled(platform)

    def _descriptor_for_chat(self, chat_id: str) -> CapabilityDescriptor:
        """The descriptor governing a specific chat. Platform caps genuinely differ
        (Discord 2000 / Telegram 4096 / Slack 39000), so the primary's scalar cap
        either fragments needlessly or over-sends into a platform 400. Falls back to
        the scalar when the chat's platform is unknown (never saw inbound)."""
        per_platform = self._negotiated_descriptor(self._platform_by_chat.get(str(chat_id)))
        return per_platform if per_platform is not None else self.descriptor

    def max_message_length_for_chat(self, chat_id: str) -> int:
        return self._descriptor_for_chat(chat_id).max_message_length

    def message_len_fn_for_chat(self, chat_id: str) -> Callable[[str], int]:
        return _LEN_FNS.get(self._descriptor_for_chat(chat_id).len_unit, len)

    def supports_draft_streaming(
        self,
        chat_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        # Needs BOTH the descriptor flag and an explicit "draft" op: supported_ops is
        # fail-open for legacy connectors, but "draft" did not exist pre-contract, so
        # it must NOT fail open. Resolved per chat when the caller names one (a
        # Telegram primary must not starve a Slack chat).
        desc = self._descriptor_for_chat(str(chat_id)) if chat_id is not None else self.descriptor
        if not (desc.supports_draft_streaming and "draft" in (desc.supported_ops or ())):
            return False
        # Slack chat.*Stream has no unfurl_links / unfurl_media; like native
        # SlackAdapter, refuse streaming when those knobs are set so chat.postMessage
        # can carry them.
        platform = self._chat_platform(chat_id) if chat_id is not None else desc.platform
        return not self._slack_unfurl_hints(platform)

    def prefers_fresh_final_streaming(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        """Deliver streamed finals as a FRESH send when Slack unfurl is forced on.

        Slack evaluates link previews exactly once, at ``chat.postMessage``; a
        ``chat.update`` that INTRODUCES the URL never unfurls. Edit-based streaming
        posts its first frame before any URL exists, so a configured ``unfurl_*: true``
        can only surface via a fresh final that ``send()`` stamps with the hints. ONLY
        when the hints contain an explicit True: false-only hints (fail-closed) ride
        the placeholder post fine. Only link-bearing finals qualify — the relay has no
        delete op in contract v1, so a linkless fresh final would just be a duplicate.
        """
        platform = self._platform_by_chat.get(str(chat_id)) if chat_id is not None else None
        # The stream consumer's hook passes (content, metadata=...) only.
        if platform is None and isinstance(metadata, dict):
            platform = metadata.get("platform")
        if platform is None:
            platform = self.descriptor.platform
        hints = self._slack_unfurl_hints(platform)
        return bool(hints) and any(v is True for v in hints.values()) and bool(_URL_RE.search(content or ""))

    def stream_is_message_for_chat(self, chat_id: str) -> bool:
        """Per-chat stream-is-the-message semantic (see ``draft_stream_is_message``).
        A Slack primary must not impose seal semantics on a Telegram chat (its
        turn-final would become draft(final=true) — no history message), nor a
        Telegram primary deny a Slack chat native streaming. Platform-name inference
        is deliberate; a descriptor field is the eventual contract."""
        return str(self._descriptor_for_chat(str(chat_id)).platform or "").lower() == "slack"

    # ── Live cards: native draft streaming + task cards ──────────────────
    # Additive relay ops within contract v1, emitted when the negotiated descriptor
    # advertises them; the connector owns the platform API mechanics and the
    # send+edit fallback. Semantic bridge: the base send_draft contract is
    # Telegram-shaped (draft clears, final is a separate send()); Slack native
    # streaming makes the stream THE message, so the adapter tracks the open draft
    # per turn and converts that turn's final send() into draft(final=true).

    def supports_native_task_cards(self) -> bool:
        """Explicit advertisement required — same no-fail-open rule as "draft"."""
        return "task_card" in (self.descriptor.supported_ops or ())

    def native_task_cards_enabled(self) -> bool:
        """TurnRunner opt-in probe (gateway/run.py calls THIS name, same contract as
        native Slack); without the alias the card lane silently stays text-mode."""
        return self.supports_native_task_cards()

    @staticmethod
    def _draft_key(chat_id: str, metadata: Optional[Dict[str, Any]]) -> str:
        """Coordination key for one turn's stream. Prefers a PER-TURN identity (the
        triggering inbound ``message_id`` / ``reply_to_message_id``) over the thread
        anchor: two parallel turns inside ONE thread share thread_ts (turn A's final
        sealed turn B's stream), and a flat DM with no anchor degraded to the bare
        chat id. Anchor is the fallback for placement-only callers; bare chat last."""
        md = metadata or {}
        turn_id = md.get("message_id") or md.get("reply_to_message_id")
        if turn_id:
            return f"{chat_id}:turn:{turn_id}"
        anchor = md.get("thread_ts") or md.get("thread_id") or ""
        return f"{chat_id}:{anchor}"

    # Cap for the draft/seal coordination dicts (per-turn keys); matches the
    # connector's tombstone store size.
    _DRAFT_STATE_CAP = 512

    @classmethod
    def _evict_oldest(cls, d: Dict[str, Any], cap: Optional[int] = None) -> None:
        """FIFO-bound an insertion-ordered dict in place (default cap: draft state)."""
        while len(d) > (cls._DRAFT_STATE_CAP if cap is None else cap):
            d.pop(next(iter(d)), None)

    @staticmethod
    def _card_key(reply_to: Optional[str], metadata: Optional[Dict[str, Any]]) -> str:
        """Per-turn task-card identity — same precedence as ``_draft_key``; one
        derivation for send AND stop so the stop always hits the stream the send opened."""
        md = metadata or {}
        anchor = (
            reply_to
            or md.get("message_id")
            or md.get("reply_to_message_id")
            or md.get("thread_ts")
            or md.get("thread_id")
            or "root"
        )
        return f"turn:{anchor}"

    def _match_open_draft(self, chat_id: str, metadata: Optional[Dict[str, Any]]) -> Optional[str]:
        """Resolve which open stream (if any) a turn-final send belongs to. Exact key
        match first. Callers carrying a per-turn MESSAGE id never fall back — their
        identity is authoritative. Callers without one may absorb into the chat's
        single open stream; with several open the send stays plain: a duplicate
        message is recoverable, sealing someone else's stream is not."""
        key = self._draft_key(str(chat_id), metadata)
        if key in self._open_draft_by_chat:
            return key
        md = metadata or {}
        if md.get("message_id") or md.get("reply_to_message_id"):
            return None
        prefix = f"{chat_id}:"
        candidates = [k for k in self._open_draft_by_chat if k.startswith(prefix)]
        if len(candidates) == 1:
            # Absorbing a send into a stream is a significant decision (the
            # prompt-ack-seals-own-stream bug); log it so the next mismatch is a grep.
            logger.info(
                "relay: absorbing identity-less send into the single open "
                "stream %s (single-open-stream fallback)",
                candidates[0],
            )
            return candidates[0]
        return None

    async def _outbound(self, chat_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        """Send one outbound frame tagged with the chat's underlying platform.

        P5(b): the second frame path (the first is ``_gated_op``). Lanes that
        return bool/None by contract — typing, delete, thread create/rename —
        come through here, and a silent degrade made an AUTHORIZATION refusal
        indistinguishable from "op unsupported" in the logs. The return
        contract is unchanged; the refusal is recorded.
        """
        op = str(action.get("op", "?"))
        result = await self._transport.send_outbound(  # type: ignore[union-attr]
            action, platform=self._platform_by_chat.get(str(chat_id))
        )
        if isinstance(result, dict):
            if not result.get("success") and is_egress_decline(result):
                log_decline(op, chat_id, result)
        return result

    async def _gated_op(
        self,
        chat_id: str,
        action: Dict[str, Any],
        *,
        decline_level: Optional[int] = logging.WARNING,
        subject: Any = None,
        platform: Optional[str] = None,
        surface_declines: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Emit one best-effort, op-gated frame; None when the caller must fall back.

        None covers every unavailability: op not advertised (probe the descriptor
        instead of parsing a connector error), no transport, transport raised, or a
        structured connector decline (logged at ``decline_level``; None = silent).
        """
        op = action["op"]
        if self._transport is None or not self.descriptor.supports_op(op):
            return None
        try:
            result = await self._transport.send_outbound(
                action, platform=platform or self._platform_by_chat.get(str(chat_id))
            )
        except Exception:  # noqa: BLE001 - transport failure degrades to the caller's fallback
            logger.debug("relay %s transport failure", op, exc_info=True)
            return None
        if not result.get("success"):
            # P5(b): an AUTHORIZATION decline is not lane unavailability. This
            # helper's `None` means "lane absent — do your fallback", and the
            # fallbacks re-address the SAME chat by another route (media -> a
            # text notice, prompt -> numbered text). The connector authorized
            # that destination and REFUSED it, so degrading launders a security
            # decision into "op unsupported" and delivers the content anyway.
            # Callers whose fallback would leak pass surface_declines=True and
            # turn this into a failed lane; cosmetic ops (typing, card stop)
            # keep the old None contract.
            if is_egress_decline(result):
                # Every lane records an authorization decline — the cosmetic
                # ones (typing, delete, thread create/rename, card stop) still
                # degrade to None, but a silent degrade made a security refusal
                # indistinguishable from "op unsupported" in the logs.
                log_decline(op, chat_id if subject is None else subject, result)
                if surface_declines:
                    return result
                return None
            if decline_level is not None:
                logger.log(
                    decline_level, "relay %s declined for %s: %s",
                    op, chat_id if subject is None else subject, result.get("error"),
                )
            return None
        return result

    def _text_metadata(self, chat_id: str, metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Metadata for a text egress frame: format hints + tenant discriminators.
        Draft, seal, send and edit are all text lanes — a streamed final can only
        render blocks if every frame carries the hint (a hintless seal is the
        plain-code-block downgrade)."""
        return self._with_scope(chat_id, self._with_format_hints_for_chat(chat_id, metadata))

    def _draft_frame(
        self, chat_id: str, draft_id: int, content: str, final: bool, metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """One ``draft`` op frame (``final=True`` seals the stream)."""
        return {
            "op": "draft",
            "chat_id": chat_id,
            "draft_id": draft_id,
            "content": content,
            "final": final,
            "metadata": self._text_metadata(chat_id, dict(metadata or {})),
        }

    async def send_draft(
        self, chat_id: str, draft_id: int, content: str, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self.supports_draft_streaming(chat_id=str(chat_id)):
            raise NotImplementedError("connector does not advertise the 'draft' relay op")
        if self._transport is None:
            return SendResult(success=False, error="no transport")
        # Arm optimistically BEFORE the transport call (a lossy ack often means
        # delivered), but NEVER for a draft_id already sealed on this key: a straggler
        # after the seal re-armed interception with no live stream, and the next
        # unrelated send was wrongly converted into a seal.
        chat_key = self._draft_key(str(chat_id), metadata)
        if self._sealed_draft_by_chat.get(chat_key) == draft_id:
            # Post-seal straggler: content is already in the sealed message; report
            # success, send nothing. Log the FIRST swallow per key — one straggler is
            # the normal race, but a burst means something sealed a live stream
            # mid-flight (silence here cost a forensic hunt).
            if chat_key not in self._tombstone_swallow_logged:
                self._tombstone_swallow_logged[chat_key] = draft_id
                self._evict_oldest(self._tombstone_swallow_logged)
                logger.warning(
                    "relay: draft frame for %s swallowed by post-seal "
                    "tombstone (draft_id=%s) — expected for a straggler; "
                    "a live stream freezing NOW means something sealed it "
                    "mid-flight",
                    chat_key,
                    draft_id,
                )
            return SendResult(success=True)
        # Arm seal-interception ONLY for stream-is-the-message chats: on a
        # Telegram-shaped connector the final MUST go out as a real send.
        if self.stream_is_message_for_chat(str(chat_id)):
            self._open_draft_by_chat[chat_key] = draft_id
            self._evict_oldest(self._open_draft_by_chat)
        try:
            result = await self._outbound(chat_id, self._draft_frame(chat_id, draft_id, content, False, metadata))
        except Exception as e:
            # Ambiguous (stale socket, mid-write drop): may have been delivered;
            # keep interception armed.
            return SendResult(success=False, error=f"draft transport error: {e}")
        if result.get("success"):
            return SendResult(success=True)
        if result.get("ambiguous"):
            # Ack lost (transport timeout, returned rather than raised): same
            # contract as the except branch — keep interception armed.
            #
            # RAW BODY PRESERVED. Dropping it made `declined_send` fall through
            # to the error-text branch, and an ambiguous result whose text
            # happens to carry the decline marker ("... egress declined: ack
            # lost") then read as a DEFINITE refusal and terminated the run.
            # Ambiguous means the frame may well have been delivered, so it is
            # a transport outcome, never an authorization one.
            return SendResult(
                success=False,
                error=str(result.get("error") or "draft ack lost"),
                raw_response=result,
            )
        # DEFINITE connector rejection: disarm. The stream consumer falls back to
        # edit-based streaming and its turn-final must go out as a REAL send, not a
        # seal on a stream the connector just declared unusable.
        if self._open_draft_by_chat.get(chat_key) == draft_id:
            self._open_draft_by_chat.pop(chat_key, None)
        # P5(b): carry the structured body. The stream consumer reads a bare
        # draft failure as "draft transport unusable", disables drafts, and
        # falls through to a plain send — a second op against the chat the
        # connector just refused. Verified end to end with the real
        # GatewayStreamConsumer: ops were ['draft', 'send'].
        if is_egress_decline(result):
            log_decline("draft", chat_id, result)
        return SendResult(
            success=False,
            error=str(result.get("error") or decline_error(result) or "draft failed"),
            raw_response=result,
        )

    async def _seal_open_draft(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]],
        *,
        draft_key: Optional[str] = None,
    ) -> SendResult:
        """Convert the turn-final send into the sealing draft frame."""
        if draft_key is None:
            draft_key = self._draft_key(str(chat_id), metadata)
        draft_id = self._open_draft_by_chat.pop(draft_key)
        # Tombstone BEFORE the transport call: whatever the ack says, this draft_id
        # must never be re-armed by a straggler frame.
        self._sealed_draft_by_chat[draft_key] = draft_id
        self._evict_oldest(self._sealed_draft_by_chat)
        if self._transport is None:
            return SendResult(success=False, error="no transport")
        seal_frame = self._draft_frame(chat_id, draft_id, content, True, metadata)

        _seal_platform = self._platform_by_chat.get(str(chat_id))
        _transport = self._transport  # narrowed by the None-guard above

        async def _attempt() -> Optional[Dict[str, Any]]:
            """One seal attempt; None means ambiguous (exception or lost ack)."""
            try:
                r = await _transport.send_outbound(seal_frame, platform=_seal_platform)
            except Exception as e:
                logger.warning("relay seal transport error (ambiguous): %s", e)
                return None
            if r.get("ambiguous"):
                logger.warning("relay seal ack lost (ambiguous): %s", r.get("error"))
                return None
            return r

        # Ambiguous outcomes retry the SAME idempotent frame once: the connector's
        # sealed-key tombstone returns the original stream ts for a repeated final
        # and never opens a second stream. Two consecutive ack losses on one socket
        # almost always mean the transport is down. Cancellation safety: the open
        # entry was popped and the tombstone written BEFORE the await; restore both
        # before re-raising so the later abandon pass can still seal the stream.
        try:
            result = await _attempt()
            if result is None:
                result = await _attempt()
        except asyncio.CancelledError:
            self._open_draft_by_chat[draft_key] = draft_id
            if self._sealed_draft_by_chat.get(draft_key) == draft_id:
                self._sealed_draft_by_chat.pop(draft_key, None)
            raise
        if result is None:
            # Same ambiguity contract as send_draft: the retry's ack was lost,
            # so the seal may have been applied. Marked explicitly rather than
            # left to text inference.
            return SendResult(
                success=False,
                error="draft seal ambiguous after retry (transport ack lost)",
                raw_response={"success": False, "ambiguous": True},
            )
        if result.get("success"):
            # The connector returns the stream's ts as the message identity.
            return SendResult(success=True, message_id=str(result.get("message_id") or "") or None)
        # P5(b): carry the structured body. Without it the caller cannot tell a
        # lane failure (fall through to a plain send, correct) from an
        # AUTHORIZATION decline (a plain send re-delivers the very content the
        # connector refused, to the same chat).
        if is_egress_decline(result):
            log_decline("draft_seal", chat_id, result)
        return SendResult(
            success=False,
            error=str(result.get("error") or decline_error(result) or "draft seal failed"),
            raw_response=result,
        )

    async def _absorb_into_open_draft(
        self, chat_id: str, content: str, metadata: Dict[str, Any], interim: bool
    ) -> Optional[SendResult]:
        """Seal an open native stream with this turn-final; None = do a plain send.

        An open stream absorbs the turn-final whichever egress door it arrives
        through (send / send_for_platform) — otherwise the stream is left frozen
        mid-word AND the final posts as a duplicate. A failed seal must NOT swallow
        the final: the consumer already disabled the draft transport, so fall through
        to a plain send (the orphaned stream is sealed connector-side). Interim sends
        (commentary, tail flush, lifecycle acks) never seal.
        """
        if interim:
            return None
        key = self._match_open_draft(str(chat_id), metadata)
        if key is None:
            return None
        seal = await self._seal_open_draft(chat_id, content, metadata, draft_key=key)
        if seal.success:
            return seal
        # An AUTHORIZATION decline is not a lane failure. Falling through here
        # re-sends the sealed content as a plain `send` into the destination the
        # connector just refused — review demonstrated the leak end to end
        # (ops: draft(partial) -> send(SECRET)). Surface the refusal instead.
        if is_egress_decline(getattr(seal, "raw_response", None)):
            logger.warning(
                "relay draft seal DECLINED for %s — not falling back to a plain "
                "send (the destination is not approved for this connection)",
                chat_id,
            )
            return seal
        logger.warning("relay seal failed (%s); delivering turn-final as plain send", seal.error)
        return None

    async def _card_frame(
        self, chat_id: str, op: str, reply_to: Optional[str], metadata: Dict[str, Any], **fields: Any
    ) -> Union[SendResult, Dict[str, Any]]:
        """Emit one task-card op: the connector result dict, or a failed SendResult
        when the lane is unavailable / the transport raised.

        Card frames are advisory and run inside the progress loop / turn-cleanup
        path: an escaping exception there skipped final delivery, so transport
        errors degrade to the TurnRunner's text fallback instead of raising.
        """
        if not self.supports_native_task_cards():
            return SendResult(success=False, error="connector does not advertise task_card")
        if self._transport is None:
            return SendResult(success=False, error="no transport")
        frame = {
            "op": op,
            "chat_id": chat_id,
            "card_id": self._card_key(reply_to, metadata),
            **fields,
            "metadata": self._with_scope(chat_id, metadata),
        }
        try:
            return await self._outbound(chat_id, frame)
        except Exception as e:
            return SendResult(success=False, error=f"{op} transport error: {e}")

    @staticmethod
    def _task_card_metadata(
        reply_to: Optional[str], metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        merged_meta = dict(metadata or {})
        if reply_to and "thread_ts" not in merged_meta:
            # Slack card streams are thread replies anchored on the trigger.
            merged_meta["thread_ts"] = str(reply_to)
        return merged_meta

    def native_task_card_destination_supported(
        self, chat_id: str, *, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Check the actual card-frame placement, not its per-turn card identity."""
        if self._chat_platform(chat_id) != _SLACK:
            return True
        md = self._task_card_metadata(reply_to, metadata)
        # Connector threadTs(): thread_id ?? thread_ts, and only strings thread.
        thread = md.get("thread_id")
        if thread is None:
            thread = md.get("thread_ts")
        return isinstance(thread, str)

    async def send_native_task_card_progress(
        self,
        chat_id: str,
        tasks: list,
        *,
        title: str = "Hermes is working",
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        fallback_text: Optional[str] = None,
    ) -> SendResult:
        """Relay leg of the task-card lane: emit one card frame.

        SIGNATURE CONTRACT: the TurnRunner calls this with the NATIVE Slack
        adapter's keyword contract, not a card_id. ``fallback_text``/``title``
        are accepted for parity but not forwarded (the connector's plan-mode
        stream renders task chunks; field limits are enforced connector-side).

        See #85476.
        """
        merged_meta = self._task_card_metadata(reply_to, metadata)
        result = await self._card_frame(
            chat_id, "task_card", reply_to, merged_meta, chunks=[dict(task) for task in tasks]
        )
        if isinstance(result, SendResult):
            return result
        if result.get("success"):
            return SendResult(success=True)
        # P5(b): carry the structured body. The TurnRunner reads a bare failure
        # as "card lane unavailable" and sends fallback TEXT to the same chat —
        # the same decline-laundering fixed for media and prompt, in a sibling
        # content lane.
        if is_egress_decline(result):
            log_decline("task_card", chat_id, result)
        return SendResult(
            success=False,
            error=str(result.get("error") or decline_error(result) or "task_card failed"),
            raw_response=result,
        )

    async def stop_native_task_card_progress(
        self,
        chat_id: str,
        *,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Seal the card stream at turn end (idempotent connector-side); same key derivation as send."""
        result = await self._card_frame(chat_id, "task_card_stop", reply_to, dict(metadata or {}))
        if isinstance(result, SendResult):
            return result
        # P5(b): carry the connector's reason. Dropping `error` reported a
        # refusal as a bare failure, which reads as "the card lane is broken"
        # rather than "this destination was refused".
        return SendResult(
            success=bool(result.get("success")),
            error=result.get("error"),
            raw_response=result,
        )

    async def abandon_open_draft(
        self, chat_id: str, content: str, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Seal an orphaned stream when its turn dies (/stop, /new, supersede), in
        place with ``content`` (the text already on screen) so the seal adds and
        claims nothing; otherwise the live indicator stays forever and the NEXT turn
        could inherit the armed interception state. Failure is reported, never raised."""
        draft_key = self._match_open_draft(str(chat_id), metadata)
        if draft_key is None:
            return SendResult(success=True)  # nothing armed — no-op
        try:
            return await self._seal_open_draft(chat_id, content, metadata, draft_key=draft_key)
        except Exception as e:
            return SendResult(success=False, error=f"abandon seal transport error: {e}")

    # ── abstract methods (delegated to the transport) ────────────────────
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # ``is_reconnect`` is part of the BasePlatformAdapter.connect contract (the
        # reconnect watcher passes it; refusing the kwarg would break recovery).
        # Relay IGNORES it: messages buffered during a gap live in the CONNECTOR's
        # durable buffer and replay on re-handshake; routine WS drops are handled by
        # the transport's own reconnect supervisor.
        if self._transport is None:
            # ``is_reconnect`` is part of the BasePlatformAdapter.connect contract: the gateway's reconnect
            # watcher (gateway/run.py) re-establishes a platform after a fatal adapter error by building a
            # fresh adapter and calling ``connect(is_reconnect=True)``. Relay MUST accept the kwarg or that
            # recovery path raises TypeError and the relay platform can never come back through the watcher.
            # The flag exists so adapters with a server-side update queue (e.g. Telegram's Bot API) preserve
            # that queue across an outage instead of dropping it (#46621). Routine WS drops are handled
            # entirely by the transport's own reconnect supervisor (WebSocketRelayTransport,
            # reconnect=True); a watcher-driven reconnect builds a fresh transport from scratch (the
            # fatal-error handler disconnect()s the old adapter first, cancelling its supervisor), so there
            # is nothing at the adapter layer to preserve.
            raise RuntimeError("RelayAdapter has no transport configured")
        self._transport.set_inbound_handler(self._on_inbound)
        # Interrupts and passthrough-plane forwards (Discord interactions, Twilio, …)
        # ride the SAME outbound WS — no inbound HTTP receiver, no public port.
        for setter_name, handler in (
            ("set_interrupt_inbound_handler", self.on_interrupt),
            ("set_passthrough_handler", self._on_passthrough),
        ):
            setter = getattr(self._transport, setter_name, None)
            if callable(setter):
                setter(handler)
        if not await self._transport.connect():
            return False
        # Adopt the connector-advertised descriptor in place of the placeholder.
        try:
            descriptor = await self._transport.handshake()
        except Exception as exc:  # noqa: BLE001 - a failed handshake = a failed connect
            logger.warning("relay handshake failed: %s", exc)
            return False
        self._apply_descriptor(descriptor)
        # Only the production WebSocket transport exposes `auth_revoked`.
        if hasattr(self._transport, "auth_revoked"):
            self._start_revocation_monitor()
        return True

    def _start_revocation_monitor(self) -> None:
        """Spawn (once) the task turning a transport auth-revocation into a clean
        non-retryable 'relay disabled' fatal. Idempotent."""
        if self._revocation_monitor is not None and not self._revocation_monitor.done():
            return
        try:
            self._revocation_monitor = asyncio.create_task(
                self._watch_for_revocation(), name="relay-revocation-monitor"
            )
        except RuntimeError:
            # No running loop (a unit test calling connect() via a stub).
            self._revocation_monitor = None

    async def _watch_for_revocation(self, poll_interval_s: float = 1.0) -> None:
        """Poll for a terminal 4401 revocation (opt-out), then surface a non-retryable
        `relay_disabled` fatal so the adapter is cleanly removed rather than queued
        for reconnection (the credential is dead until the instance is recreated)."""
        transport = self._transport
        if transport is None:
            return
        while not getattr(transport, "auth_revoked", False):
            await asyncio.sleep(poll_interval_s)
        logger.warning("relay credential revoked (opt-out) — marking the relay adapter disabled")
        self._set_fatal_error(
            "relay_disabled", "Relay disabled (opted out — recreate the instance to re-enable)",
            retryable=False,
        )
        try:
            await self._notify_fatal_error()
        except Exception:  # noqa: BLE001 - notification is best-effort
            logger.debug("relay revocation fatal-error notify failed", exc_info=True)

    def _apply_descriptor(self, descriptor: CapabilityDescriptor) -> None:
        """Adopt a (re)negotiated descriptor into the live capability surface."""
        self.descriptor = descriptor
        self.MAX_MESSAGE_LENGTH = descriptor.max_message_length
        self.supports_code_blocks = descriptor.markdown_dialect not in ("", "plain")
        # Cron in_channel continuable surface (D6 gate in cron/scheduler.py);
        # class default is False, so only an explicit descriptor bit turns it on.
        self.supports_inchannel_continuable = bool(getattr(descriptor, "supports_inchannel_continuable", False))

    async def _on_inbound(self, event) -> None:
        """Bridge a connector-delivered MessageEvent into the normal adapter path."""
        # Inbound replay dedupe: the relay leg is at-least-once — on WS re-handshake
        # the connector replays its durable buffer, and a long turn straddling a
        # quiet socket drop got re-run (final answer 2-5x). Platform message identity
        # is stable across replays.
        dedupe_key = self._inbound_dedupe_key(event)
        if dedupe_key is not None:
            if dedupe_key in self._seen_inbound:
                logger.info("relay inbound dropped as replay (dedupe key=%s)", dedupe_key)
                return
            self._seen_inbound[dedupe_key] = None
            self._evict_oldest(self._seen_inbound, self._SEEN_INBOUND_MAX)
        self._capture_scope(event)
        self._stamp_slack_session_thread(event)
        # A structured prompt answer resolves its waiting primitive and is CONSUMED —
        # never also dispatched as chat.
        if await self._consume_prompt_response(event):
            return
        await self._localize_inbound_media(event)
        await self.handle_message(event)

    _SEEN_INBOUND_MAX = 512

    def _inbound_dedupe_key(self, event) -> Optional[str]:
        """Stable replay identity: (platform, chat, platform message id). The platform
        joins the key because one relay socket can front several platforms whose
        numeric ids may collide. None when the event carries no platform message id —
        those never dedupe (fail-open: dropping a real message beats rerunning one)."""
        source = getattr(event, "source", None)
        message_id = getattr(event, "message_id", None)
        chat_id = getattr(source, "chat_id", None)
        if not message_id or not chat_id:
            return None
        # Enum value when present, plain string otherwise: both spellings of one
        # platform must produce ONE key.
        raw_platform = getattr(source, "platform", None)
        platform = getattr(raw_platform, "value", raw_platform) or ""
        return f"{platform}:{chat_id}:{message_id}"

    def _relay_platform_extra(self, platform: str) -> Dict[str, Any]:
        """``platforms.relay.extra.<platform>.*`` — relay-namespaced mirror of a native
        platform's knobs (``platforms.<platform>`` keeps meaning native settings).
        Legacy fallback: flat keys on the relay extra when no ``<platform>`` object exists."""
        extra = getattr(self.config, "extra", None) or {}
        sub = extra.get(platform)
        return sub if isinstance(sub, dict) else extra

    def _relay_slack_extra(self) -> Dict[str, Any]:
        return self._relay_platform_extra("slack")

    @staticmethod
    def _coerce_flag(raw: Any, default: bool) -> bool:
        """Coerce an operator-supplied boolean exactly as native Slack does: a
        YAML-quoted ``"false"`` must turn the flag OFF (bare ``bool()`` would read
        the non-empty string as True and silently ignore the switch)."""
        if raw is None:
            return default
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in _TRUTHY

    def _slack_flag(self, knob: str, default: bool) -> bool:
        """A coerced boolean knob from the relay Slack extra; ``default`` on any config-shape error."""
        try:
            return self._coerce_flag(self._relay_slack_extra().get(knob), default)
        except Exception:  # noqa: BLE001 - config shape is operator-owned
            return default

    def _effective_reply_in_thread(self) -> bool:
        """Resolve the thread-per-message vs flat-DM mode for fronted Slack."""
        return self._slack_flag("reply_in_thread", True)

    def _dm_top_level_threads_as_sessions(self) -> bool:
        """Native-parity escape hatch: per-message DM sessions on/off. Default True:
        in thread-per-message mode each top-level DM message keys its own session.
        False keeps threaded PLACEMENT but ONE rolling DM session (le