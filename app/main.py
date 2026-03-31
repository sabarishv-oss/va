"""
main.py
-------
FastAPI server for the Samantha OUTBOUND-ONLY voice verification agent.
Replaces Azure Voice Live with a Pipecat pipeline:
  Deepgram STT → GPT-4o LLM → ElevenLabs TTS

Endpoints:
  GET  /                          Health check
  POST /api/outboundCall          Trigger a single outbound call via HTTP
  POST /api/callbacks/{sessionId} ACS call lifecycle events
  POST /api/hangup/{connId}       Explicit ACS hangup
  WS   /ws                        ACS bidirectional PCM audio stream

Outbound campaign:
  Run app/dialer.py to place calls from campaign_input.csv
"""

import sys
import os

# Fix Windows UTF-8 encoding for log file (must be before any imports that log)
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import base64
import json
import os
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import JSONResponse, Response
from fastapi.websockets import WebSocketState
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.pipeline.runner import PipelineRunner

# Write all logs to file as well as terminal
logger.add(
    "server_logs.txt",
    rotation="10 MB",
    retention="7 days",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}",
    encoding="utf-8",
    errors="replace",
)

# Intercept standard Python logging (uvicorn, fastapi, etc.)
# so their output also goes into server_logs.txt
import logging

class InterceptHandler(logging.Handler):
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )

logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

# Suppress noisy DEBUG loggers — websockets binary frames, httpx request details
for noisy in ("websockets", "httpx", "httpcore", "hpack"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Show pipecat LLM metrics so cache token usage is visible in logs
logging.getLogger("pipecat.services.openai").setLevel(logging.DEBUG)

from azure.communication.callautomation import (
    CallAutomationClient,
    PhoneNumberIdentifier,
    MediaStreamingOptions,
    AudioFormat,
    MediaStreamingTransportType,
    MediaStreamingContentType,
    MediaStreamingAudioChannelType,
)

from app.agent_settings import AGENT_SETTINGS
from app.acs_transport import ACSTransport, ACSTransportParams
from app.call_session import CallSession
from app.call_timeline import log_call_timeline
from app.pipecat_pipeline import create_pipeline

load_dotenv()

# ---------------------------------------------------------------------------
# Env validation — fail fast
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}\n"
            f"Set it in your .env file before starting the server."
        )
    return val


def _optional_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


ACS_CONNECTION_STRING   = _require_env("ACS_CONNECTION_STRING")
ACS_SOURCE_PHONE_NUMBER = _require_env("ACS_SOURCE_PHONE_NUMBER")
CALLBACK_URI_HOST       = _require_env("CALLBACK_URI_HOST")
CALLBACK_EVENTS_URI     = CALLBACK_URI_HOST + "/api/callbacks"
ACS_INCOMING_EVENTS_URI = CALLBACK_URI_HOST + "/api/acs/incoming"
TWILIO_ACCOUNT_SID      = _optional_env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN       = _optional_env("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER      = _optional_env("TWILIO_FROM_NUMBER")
TWILIO_ACS_BRIDGE_NUMBER = _optional_env(
    "TWILIO_ACS_BRIDGE_NUMBER", ACS_SOURCE_PHONE_NUMBER
)
TWILIO_STATUS_CALLBACK_URL = _optional_env(
    "TWILIO_STATUS_CALLBACK_URL", f"{CALLBACK_URI_HOST}/twilio-status"
)
TWILIO_VOICE_WEBHOOK_URL = _optional_env(
    "TWILIO_VOICE_WEBHOOK_URL", f"{CALLBACK_URI_HOST}/voice"
)
TWILIO_RING_TIMEOUT_SECONDS = int(_optional_env("TWILIO_RING_TIMEOUT_SECONDS", "30"))
TWILIO_STUDIO_FLOW_SID = _optional_env("TWILIO_STUDIO_FLOW_SID")

# Call results: default is one append-only JSONL file under this dir (see CALL_RESULTS_MODE in call_session).
RESULTS_DIR = Path(os.getenv("CALL_RESULTS_DIR", "./call_results"))

# Validate STT/LLM/TTS keys at startup — fail fast rather than silently
# dropping every WebSocket connection with a cryptic close.
# _require_env already checks for empty; here we additionally reject
# un-edited placeholder values from .env.template.
def _validate_api_key(name: str) -> str:
    val = _require_env(name)
    if val.startswith("your_"):
        raise RuntimeError(
            f"Environment variable {name} still contains the placeholder value "
            f"'{val}'. Edit .env and set a real API key before starting."
        )
    return val

_validate_api_key("OPENAI_API_KEY")
_validate_api_key("DEEPGRAM_API_KEY")
_validate_api_key("INWORLD_API_KEY")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Samantha — ACS Outbound Pipecat Voice Agent")


@app.on_event("startup")
async def _warmup_silero_vad() -> None:
    """
    Instantiate SileroVADAnalyzer once at server startup so the Silero neural-net
    model file is downloaded and deserialized into memory before the first call
    arrives.  Every subsequent call reuses the already-loaded model — no cold-load
    penalty on the live call path (~1–2 s saved on the first call).
    """
    try:
        vad_cfg   = AGENT_SETTINGS["vad"]
        audio_cfg = AGENT_SETTINGS["audio"]
        _warmup_vad = SileroVADAnalyzer(
            sample_rate=audio_cfg["sample_rate"],
            params=VADParams(
                confidence=vad_cfg["confidence"],
                start_secs=vad_cfg["start_secs"],
                stop_secs=vad_cfg["stop_secs"],
                min_volume=vad_cfg["min_volume"],
            ),
        )
        logger.info("[STARTUP] Silero VAD model warmed up — cold-load cost paid once.")
    except Exception as exc:
        logger.warning(f"[STARTUP] Silero VAD warm-up failed (non-fatal): {exc}")


acs_client = CallAutomationClient.from_connection_string(ACS_CONNECTION_STRING)

# session_id → call_connection_id
# Populated by create_call() response AND by CallConnected callback —
# whichever arrives first. The WS hangup closure re-reads this at call time
# so it always gets the latest value even if CallConnected is slightly late.
_session_registry: dict[str, str] = {}
_answered_incoming_contexts: set[str] = set()
# Stores per-call context keyed by target phone number for Twilio-originated calls
# so the inbound auto-attach can recover org_name/services/unique_id
_twilio_call_context: dict[str, dict] = {}
# When ACS shows From=Twilio (not callee), we pop the oldest pending row (same order as dialer)
_twilio_pending_contexts: deque = deque(maxlen=100)
# Inbound Twilio→ACS: full org/phone/services live here keyed by session_id so the
# WebSocket URL stays short (ACS/proxies may truncate long query strings).
_ws_context_by_session_id: dict[str, dict] = {}
# Last seen Twilio CallSid per callee number (digits only) from /twilio-status.
_twilio_recent_call_sid_by_callee: dict[str, str] = {}
# Preferred mapping for safe concurrent hangups: session_id → Twilio CallSid
_twilio_call_sid_by_session_id: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_websocket_url(params: str = "") -> str:
    parsed = urlparse(CALLBACK_EVENTS_URI)
    return urlunparse(("wss", parsed.netloc, "/ws", "", params, ""))


def _build_media_streaming_options(websocket_url: str) -> MediaStreamingOptions:
    return MediaStreamingOptions(
        transport_url=websocket_url,
        transport_type=MediaStreamingTransportType.WEBSOCKET,
        content_type=MediaStreamingContentType.AUDIO,
        audio_channel_type=MediaStreamingAudioChannelType.UNMIXED,
        start_media_streaming=True,
        enable_bidirectional=True,
        audio_format=AudioFormat.PCM16_K_MONO,
    )

def _twilio_is_configured() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER)


def _twilio_complete_active_calls_for_callee(phone_number: str) -> None:
    """
    Deprecated for concurrent calling (unsafe). Intentionally a no-op.
    Keep the symbol to avoid breaking older code paths, but do not sweep.
    """
    return


def _require_twilio_config(*, studio_only: bool = False) -> None:
    missing = []
    if not TWILIO_ACCOUNT_SID:
        missing.append("TWILIO_ACCOUNT_SID")
    if not TWILIO_AUTH_TOKEN:
        missing.append("TWILIO_AUTH_TOKEN")
    if not TWILIO_FROM_NUMBER:
        missing.append("TWILIO_FROM_NUMBER")
    if not studio_only and not TWILIO_ACS_BRIDGE_NUMBER:
        missing.append("TWILIO_ACS_BRIDGE_NUMBER")
    if missing:
        raise RuntimeError(f"Missing required Twilio env vars: {', '.join(missing)}")


def _identifier_to_number(value) -> str:
    if isinstance(value, dict):
        phone_number = value.get("phoneNumber", {})
        if isinstance(phone_number, dict):
            return phone_number.get("value", "")
        return value.get("rawId", "")
    return ""


def _digits_only(phone: str) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())


def _phones_equivalent(a: str, b: str) -> bool:
    """E.164 or national; US numbers compared by last 10 digits."""
    da = _digits_only(a)
    db = _digits_only(b)
    if not da or not db:
        return False
    if da == db:
        return True
    if len(da) >= 10 and len(db) >= 10:
        return da[-10:] == db[-10:]
    return False


def _enqueue_twilio_context(phone: str, ctx: dict) -> None:
    """Store by recipient number + FIFO for Twilio-number-as-From fallback."""
    row = {
        "org_name":   ctx.get("org_name", ""),
        "services":   ctx.get("services", ""),
        "unique_id":  ctx.get("unique_id", ""),
        "target_phone": phone.strip(),
    }
    _twilio_call_context[phone.strip()] = row
    _twilio_pending_contexts.append(dict(row))


def _discard_pending_matching_target(target_phone: str) -> None:
    """Remove first queued row matching this callee (after dict pop)."""
    if not target_phone or not _twilio_pending_contexts:
        return
    tmp = deque(maxlen=_twilio_pending_contexts.maxlen)
    removed = False
    while _twilio_pending_contexts:
        item = _twilio_pending_contexts.popleft()
        if (
            not removed
            and _phones_equivalent(item.get("target_phone", ""), target_phone)
        ):
            removed = True
            continue
        tmp.append(item)
    _twilio_pending_contexts.clear()
    for item in tmp:
        _twilio_pending_contexts.append(item)


def _pop_twilio_context_for_inbound(from_number: str, to_number: str) -> dict:
    """
    ACS IncomingCall often has From=Twilio trunk, To=ACS — not the callee.
    Try exact keys, digit match on any stored recipient, then FIFO if From is Twilio.
    """
    fn = (from_number or "").strip()
    tn = (to_number or "").strip()

    for key in (fn, tn):
        if key and key in _twilio_call_context:
            ctx = _twilio_call_context.pop(key)
            _discard_pending_matching_target(ctx.get("target_phone", ""))
            return ctx

    for stored_key in list(_twilio_call_context.keys()):
        if _phones_equivalent(stored_key, fn) or _phones_equivalent(stored_key, tn):
            ctx = _twilio_call_context.pop(stored_key)
            _discard_pending_matching_target(ctx.get("target_phone", ""))
            return ctx

    twilio_from = (TWILIO_FROM_NUMBER or "").strip()
    if twilio_from and _phones_equivalent(fn, twilio_from) and _twilio_pending_contexts:
        return dict(_twilio_pending_contexts.popleft())

    # Single in-flight campaign row: no phone match but only one pending row
    if len(_twilio_pending_contexts) == 1:
        logger.warning(
            "[Twilio context] No phone key match; using sole pending FIFO row "
            "(safe only for one outbound at a time)"
        )
        return dict(_twilio_pending_contexts.popleft())

    return {}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return JSONResponse({"message": "Samantha Outbound Voice Agent — ready."})

# ---------------------------------------------------------------------------
# Twilio bridge endpoints
# ---------------------------------------------------------------------------

@app.post("/voice")
async def twilio_voice_webhook(request: Request):
    """
    Twilio voice webhook:
    once the recipient answers, Twilio requests TwiML from this endpoint.
    We then bridge that live call to the configured ACS PSTN number.
    """
    form = await request.form()
    payload = dict(form)
    log_call_timeline(
        "twilio_twiml_bridge_to_acs",
        call_sid=payload.get("CallSid"),
        from_=payload.get("From"),
        to=payload.get("To"),
        call_status=payload.get("CallStatus"),
        direction=payload.get("Direction"),
        parent_call_sid=payload.get("ParentCallSid"),
        bridge_target=TWILIO_ACS_BRIDGE_NUMBER,
    )

    if not TWILIO_ACS_BRIDGE_NUMBER:
        logger.error("[Twilio] Missing TWILIO_ACS_BRIDGE_NUMBER; cannot bridge call.")
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Hangup/></Response>"
            ),
            media_type="application/xml",
        )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Dial "
        f"timeout=\"{TWILIO_RING_TIMEOUT_SECONDS}\" "
        "answerOnBridge=\"true\">"
        f"<Number>{TWILIO_ACS_BRIDGE_NUMBER}</Number>"
        "</Dial>"
        "</Response>"
    )
    logger.info(
        f"[Twilio] Bridging answered call to ACS number {TWILIO_ACS_BRIDGE_NUMBER}"
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/api/twilio/register-context")
async def twilio_register_context(request: Request):
    """
    Store per-recipient context before a Twilio Studio execution (dialer uses this).
    When ACS inbound fires, /api/acs/incoming looks up context by caller (recipient) number.
    """
    body = await request.json()
    phone = body.get("phone_number", "").strip()
    if not phone:
        return JSONResponse({"error": "phone_number is required"}, status_code=400)
    _enqueue_twilio_context(
        phone,
        {
            "org_name":   body.get("org_name", "").strip(),
            "services":   body.get("services", "").strip(),
            "unique_id":  body.get("unique_id", "").strip(),
        },
    )
    log_call_timeline(
        "twilio_register_context",
        phone_number=phone,
        org_name=body.get("org_name", "").strip(),
        unique_id=body.get("unique_id", "").strip(),
    )
    logger.info(f"[Twilio] Registered call context for {phone}")
    return JSONResponse({"status": "ok"})


@app.post("/twilio-status")
async def twilio_status_callback(request: Request):
    """
    Twilio call status callback endpoint. Twilio posts form data here.
    """
    form = await request.form()
    payload = dict(form)
    status = (payload.get("CallStatus") or "").strip().lower()

    # Track latest CallSid per callee so we can proactively hang up the
    # Twilio leg when ACS ends the call from our side.
    to_number = (payload.get("To") or "").strip()
    call_sid = (payload.get("CallSid") or "").strip()
    if to_number and call_sid:
        _twilio_recent_call_sid_by_callee[_digits_only(to_number)] = call_sid

    # Safe concurrent mapping: if caller included session_id in the status callback URL,
    # store session_id → CallSid so we only ever complete the correct Twilio call.
    sid_from_qs = (request.query_params.get("session_id") or "").strip()
    if sid_from_qs and call_sid:
        _twilio_call_sid_by_session_id[sid_from_qs] = call_sid
    phase_map = {
        "queued": "twilio_status_queued",
        "initiated": "twilio_status_initiated",
        "ringing": "twilio_status_ringing",
        "in-progress": "twilio_status_in_progress",
        "completed": "twilio_status_completed",
        "busy": "twilio_status_busy",
        "no-answer": "twilio_status_no_answer",
        "failed": "twilio_status_failed",
        "canceled": "twilio_status_canceled",
    }
    phase = phase_map.get(status, "twilio_status_other")
    log_call_timeline(
        phase,
        call_sid=payload.get("CallSid"),
        call_status=payload.get("CallStatus"),
        from_=payload.get("From"),
        to=payload.get("To"),
        direction=payload.get("Direction"),
        timestamp=payload.get("Timestamp"),
        duration=payload.get("Duration"),
        answered_by=payload.get("AnsweredBy"),
        parent_call_sid=payload.get("ParentCallSid"),
        forwarded_from=payload.get("ForwardedFrom"),
        sip_response_code=payload.get("SipResponseCode"),
        error_code=payload.get("ErrorCode"),
        error_message=payload.get("ErrorMessage"),
    )
    # Human-readable hint for the most common statuses
    if status == "in-progress":
        log_call_timeline(
            "twilio_hint_recipient_line_active",
            call_sid=payload.get("CallSid"),
            note="CallStatus=in-progress usually means callee answered; "
            "next step is often TwiML bridge or Studio connect to ACS.",
        )
    logger.info(
        "[Twilio status] "
        f"CallSid={payload.get('CallSid', '')} | "
        f"CallStatus={payload.get('CallStatus', '')} | "
        f"From={payload.get('From', '')} | To={payload.get('To', '')} | "
        f"Direction={payload.get('Direction', '')}"
    )
    return JSONResponse({"status": "ok"})


@app.post("/api/twilio/outboundCall")
async def twilio_outbound_call(request: Request):
    """
    Trigger a Twilio outbound call to recipient.
    Twilio displays TWILIO_FROM_NUMBER and, after answer, pulls TwiML from /voice
    to bridge into ACS PSTN number.
    """
    if not _twilio_is_configured():
        return JSONResponse(
            {
                "error": (
                    "Twilio not configured. Set TWILIO_ACCOUNT_SID, "
                    "TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, and "
                    "TWILIO_ACS_BRIDGE_NUMBER in .env"
                )
            },
            status_code=500,
        )

    try:
        _require_twilio_config(studio_only=bool(TWILIO_STUDIO_FLOW_SID))
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    body = await request.json()
    to_number = body.get("phone_number", "").strip()
    if not to_number:
        return JSONResponse({"error": "phone_number is required"}, status_code=400)

    org_name = body.get("org_name", "").strip()
    services = body.get("services", "").strip()
    unique_id = body.get("unique_id", "").strip()

    _enqueue_twilio_context(
        to_number,
        {"org_name": org_name, "services": services, "unique_id": unique_id},
    )

    if TWILIO_STUDIO_FLOW_SID:
        studio_url = (
            f"https://studio.twilio.com/v1/Flows/{TWILIO_STUDIO_FLOW_SID}/Executions"
        )
        post_data = {
            "To": to_number,
            "From": TWILIO_FROM_NUMBER,
            "Parameters": json.dumps({
                "org_name": org_name,
                "services": services,
                "unique_id": unique_id,
            }),
        }

        def _create_studio_execution():
            return requests.post(
                studio_url,
                data=post_data,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                timeout=20,
            )

        log_call_timeline(
            "twilio_studio_execution_request",
            flow_sid=TWILIO_STUDIO_FLOW_SID,
            to_=to_number,
            from_=TWILIO_FROM_NUMBER,
            unique_id=unique_id,
        )
        try:
            response = await asyncio.to_thread(_create_studio_execution)
        except Exception as e:
            logger.error(f"[Twilio Studio] Failed to start execution: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        if response.status_code >= 400:
            logger.error(
                f"[Twilio Studio] Execution failed | status={response.status_code} | body={response.text}"
            )
            return JSONResponse(
                {
                    "error": "Twilio Studio execution failed",
                    "status_code": response.status_code,
                    "details": response.text,
                },
                status_code=502,
            )

        data = response.json()
        exec_sid = data.get("sid", "")
        log_call_timeline(
            "twilio_studio_execution_created",
            execution_sid=exec_sid,
            flow_sid=TWILIO_STUDIO_FLOW_SID,
            to_=to_number,
            from_=TWILIO_FROM_NUMBER,
            unique_id=unique_id,
        )
        return JSONResponse(
            {
                "status": "queued",
                "provider": "twilio_studio",
                "flow_sid": TWILIO_STUDIO_FLOW_SID,
                "execution_sid": exec_sid,
                "to": to_number,
                "from": TWILIO_FROM_NUMBER,
                "parameters": post_data["Parameters"],
            }
        )

    create_call_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json"
    )
    post_data = {
        "To": to_number,
        "From": TWILIO_FROM_NUMBER,
        "Url": TWILIO_VOICE_WEBHOOK_URL,
        "Method": "POST",
        "StatusCallback": TWILIO_STATUS_CALLBACK_URL,
    }
    # Twilio accepts repeated StatusCallbackEvent fields.
    event_fields = [
        ("StatusCallbackEvent", "initiated"),
        ("StatusCallbackEvent", "ringing"),
        ("StatusCallbackEvent", "answered"),
        ("StatusCallbackEvent", "completed"),
    ]

    def _create_twilio_call():
        return requests.post(
            create_call_url,
            data=[*post_data.items(), *event_fields],
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=20,
        )

    try:
        response = await asyncio.to_thread(_create_twilio_call)
    except Exception as e:
        logger.error(f"[Twilio] Failed to create outbound call: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

    if response.status_code >= 400:
        logger.error(
            f"[Twilio] Call create failed | status={response.status_code} | body={response.text}"
        )
        return JSONResponse(
            {
                "error": "Twilio call create failed",
                "status_code": response.status_code,
                "details": response.text,
            },
            status_code=502,
        )

    data = response.json()
    call_sid = data.get("sid", "")
    log_call_timeline(
        "twilio_rest_call_created",
        call_sid=call_sid,
        to_=to_number,
        from_=TWILIO_FROM_NUMBER,
        voice_webhook=TWILIO_VOICE_WEBHOOK_URL,
        status_callback=TWILIO_STATUS_CALLBACK_URL,
        bridge_number=TWILIO_ACS_BRIDGE_NUMBER,
        unique_id=unique_id,
    )
    logger.info(
        f"[Twilio] Outbound call created | sid={call_sid} | to={to_number}"
    )
    return JSONResponse(
        {
            "status": "queued",
            "provider": "twilio",
            "call_sid": call_sid,
            "to": to_number,
            "from": TWILIO_FROM_NUMBER,
            "voice_webhook": TWILIO_VOICE_WEBHOOK_URL,
            "status_callback": TWILIO_STATUS_CALLBACK_URL,
            "bridge_number": TWILIO_ACS_BRIDGE_NUMBER,
        }
    )


# ---------------------------------------------------------------------------
# ACS inbound auto-attach (for Twilio -> ACS PSTN transfer)
# ---------------------------------------------------------------------------

@app.post("/api/acs/incoming")
async def acs_incoming_events(request: Request):
    """
    Handles ACS Event Grid events. For IncomingCall, auto-answer and immediately
    attach media streaming to /ws so Samantha joins transferred calls.
    """
    raw = await request.json()
    events = raw if isinstance(raw, list) else [raw]

    for event in events:
        event_type = event.get("eventType") or event.get("type", "")
        data = event.get("data", {}) or {}

        if event_type == "Microsoft.EventGrid.SubscriptionValidationEvent":
            code = data.get("validationCode", "")
            logger.info("[ACS inbound] Subscription validation received.")
            return JSONResponse({"validationResponse": code})

        if event_type != "Microsoft.Communication.IncomingCall":
            continue

        incoming_call_context = data.get("incomingCallContext", "")
        if not incoming_call_context:
            logger.warning("[ACS inbound] Missing incomingCallContext, skipping event.")
            continue

        if incoming_call_context in _answered_incoming_contexts:
            logger.info("[ACS inbound] Duplicate IncomingCall event ignored.")
            continue

        from_number = _identifier_to_number(data.get("from", {}))
        to_number = _identifier_to_number(data.get("to", {}))
        correlation_id = data.get("correlationId") or data.get("correlation_id") or ""

        log_call_timeline(
            "acs_incoming_call_event",
            correlation_id=correlation_id,
            from_=from_number,
            to_=to_number,
        )

        # Recover org/services — ACS often shows From=Twilio, To=ACS (not callee)
        ctx = _pop_twilio_context_for_inbound(from_number, to_number)
        org_name = ctx.get("org_name", "")
        services = ctx.get("services", "")
        unique_id = ctx.get("unique_id", "") or f"twilio-transfer-{str(uuid.uuid4())[:8]}"
        twilio_from = (TWILIO_FROM_NUMBER or "").strip()
        callee_phone = (ctx.get("target_phone") or "").strip()
        # Never use Twilio caller-ID as the callee org phone (pollutes prompts/results)
        if not callee_phone and from_number and not _phones_equivalent(
            from_number, twilio_from
        ):
            callee_phone = from_number.strip()

        session_id = str(uuid.uuid4())
        _ws_context_by_session_id[session_id] = {
            "org_name":     org_name,
            "services":     services,
            "unique_id":    unique_id,
            "target_phone": callee_phone,
        }
        # Short URL: full context is in _ws_context_by_session_id
        context_params = urlencode({"session_id": session_id})
        callback_uri = f"{CALLBACK_EVENTS_URI}/{session_id}?{context_params}"
        ws_url = _build_websocket_url(context_params)

        logger.info(
            "[ACS inbound] Answering transferred call | "
            f"from={from_number or 'unknown'} | to={to_number or 'unknown'} | "
            f"callee_phone={callee_phone or 'unknown'} | org={org_name or '—'} | "
            f"session={session_id}"
        )
        log_call_timeline(
            "acs_answer_call_start",
            session_id=session_id,
            unique_id=unique_id,
            callee_phone=callee_phone,
            org_name=org_name,
            from_=from_number,
            to_=to_number,
        )

        try:
            result = acs_client.answer_call(
                incoming_call_context=incoming_call_context,
                callback_url=callback_uri,
                media_streaming=_build_media_streaming_options(ws_url),
                operation_context=context_params,
            )
            _answered_incoming_contexts.add(incoming_call_context)
            _session_registry[session_id] = result.call_connection_id
            log_call_timeline(
                "acs_answer_call_ok",
                session_id=session_id,
                call_connection_id=result.call_connection_id,
                unique_id=unique_id,
            )
            logger.info(
                "[ACS inbound] Auto-attached media streaming | "
                f"session={session_id} | conn_id={result.call_connection_id}"
            )
        except Exception as e:
            logger.error(f"[ACS inbound] Failed to answer incoming call: {e}")

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Single outbound call trigger
# (The dialer.py batch runner calls ACS directly; this endpoint is for
#  one-off calls triggered via HTTP, e.g. from a webhook or test script.)
# ---------------------------------------------------------------------------

@app.post("/api/outboundCall")
async def outbound_call(request: Request):
    """
    Trigger a single outbound call.
    Body (JSON):
        {
            "phone_number": "+15551234567",
            "org_name":     "Bright Kids",
            "services":     "adoption services",
            "unique_id":    "00001"
        }
    """
    body         = await request.json()
    phone_number = body.get("phone_number", "").strip()
    org_name     = body.get("org_name", "").strip()
    services     = body.get("services", "").strip()
    unique_id    = body.get("unique_id", "").strip()

    if not phone_number:
        return JSONResponse({"error": "phone_number is required"}, status_code=400)

    session_id = str(uuid.uuid4())
    context_params = urlencode({
        "org_name":     org_name,
        "phone_number": phone_number,
        "services":     services,
        "unique_id":    unique_id,
        "session_id":   session_id,
    })

    callback_uri = f"{CALLBACK_EVENTS_URI}/{session_id}?{context_params}"
    ws_url       = _build_websocket_url(context_params)

    logger.info(
        f"Outbound call → {phone_number} | org={org_name} | "
        f"unique_id={unique_id} | session={session_id}"
    )
    log_call_timeline(
        "acs_outbound_create_call_start",
        session_id=session_id,
        to_=phone_number,
        unique_id=unique_id,
        org_name=org_name,
    )

    result = acs_client.create_call(
        target_participant=PhoneNumberIdentifier(phone_number),
        source_caller_id_number=PhoneNumberIdentifier(ACS_SOURCE_PHONE_NUMBER),
        callback_url=callback_uri,
        media_streaming=_build_media_streaming_options(ws_url),
        operation_context=context_params,
    )

    _session_registry[session_id] = result.call_connection_id
    log_call_timeline(
        "acs_outbound_create_call_ok",
        session_id=session_id,
        call_connection_id=result.call_connection_id,
        unique_id=unique_id,
    )
    logger.info(f"Registered session {session_id} → {result.call_connection_id}")

    return JSONResponse({
        "call_connection_id": result.call_connection_id,
        "session_id":         session_id,
        "unique_id":          unique_id,
    })


# ---------------------------------------------------------------------------
# ACS callback events
# ---------------------------------------------------------------------------

@app.post("/api/callbacks/{contextId}")
async def handle_callback(contextId: str, request: Request):
    for event in await request.json():
        event_data         = event["data"]
        call_connection_id = event_data.get("callConnectionId", "")
        event_type         = event["type"]

        logger.info(f"ACS Event: {event_type} | connectionId: {call_connection_id}")
        log_call_timeline(
            "acs_callback_event",
            session_id=contextId,
            event_type=event_type,
            call_connection_id=call_connection_id,
        )

        if event_type == "Microsoft.Communication.CallConnected":
            # contextId in the path IS the session_id
            _session_registry[contextId] = call_connection_id
            log_call_timeline(
                "acs_call_connected",
                session_id=contextId,
                call_connection_id=call_connection_id,
            )
            logger.info(f"Registered session {contextId} → {call_connection_id}")

        elif event_type == "Microsoft.Communication.MediaStreamingStarted":
            msu = event_data.get("mediaStreamingUpdate") or {}
            log_call_timeline(
                "acs_media_streaming_started",
                session_id=contextId,
                call_connection_id=call_connection_id,
                media_streaming_status=msu.get("mediaStreamingStatus"),
            )
            _ms = (event_data.get("mediaStreamingUpdate") or {}).get(
                "mediaStreamingStatus", ""
            )
            logger.info(f"Media streaming started | status={_ms}")

        elif event_type == "Microsoft.Communication.MediaStreamingStopped":
            log_call_timeline(
                "acs_media_streaming_stopped",
                session_id=contextId,
                call_connection_id=call_connection_id,
            )
            logger.info("Media streaming stopped.")

        elif event_type == "Microsoft.Communication.MediaStreamingFailed":
            ri = event_data.get("resultInformation") or {}
            log_call_timeline(
                "acs_media_streaming_failed",
                session_id=contextId,
                call_connection_id=call_connection_id,
                code=ri.get("code"),
                message=ri.get("message"),
            )
            logger.error(
                f"Media streaming failed | "
                f"code={event_data['resultInformation']['code']} | "
                f"msg={event_data['resultInformation']['message']}"
            )

        elif event_type == "Microsoft.Communication.CallDisconnected":
            log_call_timeline(
                "acs_call_disconnected",
                session_id=contextId,
                call_connection_id=call_connection_id,
            )
            logger.info(f"Call disconnected | connectionId: {call_connection_id}")
            # Do NOT pop from registry — hangup closure may still need it

    return JSONResponse({}, status_code=200)


# ---------------------------------------------------------------------------
# Explicit ACS hangup
# ---------------------------------------------------------------------------

@app.post("/api/hangup/{call_connection_id}")
async def hangup_call(call_connection_id: str):
    try:
        acs_client.get_call_connection(call_connection_id).hang_up(is_for_everyone=True)
        logger.info(f"Hung up | connectionId: {call_connection_id}")
        return JSONResponse({"status": "hung_up"})
    except Exception as e:
        logger.error(f"Hangup failed | connectionId: {call_connection_id} | {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# WebSocket — one connection per active outbound call
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """
    ACS streams bidirectional PCM24K audio here after the outbound call
    is answered. Query params carry per-call context set by the dialer
    or /api/outboundCall.
    """
    await websocket.accept()
    log_call_timeline("acs_websocket_accepted")

    params        = dict(websocket.query_params)
    session_id    = params.get("session_id", str(uuid.uuid4()))

    if session_id in _ws_context_by_session_id:
        # Do NOT pop here. ACS can reconnect the media WebSocket for the same
        # session_id; popping would make subsequent connects lose org/phone/services.
        stored = _ws_context_by_session_id.get(session_id) or {}
        org_name      = stored.get("org_name", "")
        phone_number  = stored.get("target_phone", "")
        services_list = stored.get("services", "")
        unique_id     = stored.get("unique_id", "")
        logger.info(
            f"WebSocket context from server store | session={session_id[:8]} | "
            f"org={org_name} | phone={phone_number}"
        )
    else:
        org_name      = params.get("org_name", "")
        phone_number  = params.get("phone_number", "")
        services_list = params.get("services", "")
        unique_id     = params.get("unique_id", "")

    call_connection_id = _session_registry.get(session_id, "")

    logger.info(
        f"WebSocket connected | org={org_name} | phone={phone_number} | "
        f"unique_id={unique_id} | session={session_id} | "
        f"conn_id={call_connection_id or 'pending'}"
    )
    log_call_timeline(
        "acs_media_websocket_connected",
        session_id=session_id,
        call_connection_id=call_connection_id or None,
        unique_id=unique_id,
        org_name=org_name,
        phone_number=phone_number,
    )

    # ── Hangup callback ──────────────────────────────────────────────────────
    # Re-reads registry at call time so CallConnected latency doesn't matter.
    async def hangup_after(delay_seconds: int = 10):
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        conn_id = _session_registry.get(session_id, "") or call_connection_id
        if not conn_id:
            logger.warning(f"Cannot hang up session {session_id[:8]} — no conn_id")
            return
        try:
            acs_client.get_call_connection(conn_id).hang_up(is_for_everyone=True)
            logger.info(f"Hung up | session={session_id[:8]} | conn_id={conn_id}")
            log_call_timeline(
                "acs_hangup_requested",
                session_id=session_id,
                call_connection_id=conn_id,
                unique_id=unique_id,
            )
            # Best-effort Twilio hangup: if we recently saw a Twilio CallSid
            # for this callee, tell Twilio to end that leg as well.
            if _twilio_is_configured() and phone_number:
                # Prefer exact session mapping (safe for concurrency), then fall back
                # to per-callee last-seen CallSid (unsafe if multiple calls share To).
                twilio_sid = _twilio_call_sid_by_session_id.get(session_id)
                if not twilio_sid:
                    callee_digits = _digits_only(phone_number)
                    twilio_sid = _twilio_recent_call_sid_by_callee.get(callee_digits)
                if twilio_sid:
                    log_call_timeline(
                        "twilio_hangup_attempt",
                        session_id=session_id,
                        unique_id=unique_id,
                        call_sid=twilio_sid,
                        callee=phone_number,
                    )

                    def _twilio_complete_call():
                        twilio_url = (
                            f"https://api.twilio.com/2010-04-01/Accounts/"
                            f"{TWILIO_ACCOUNT_SID}/Calls/{twilio_sid}.json"
                        )
                        return requests.post(
                            twilio_url,
                            data={"Status": "completed"},
                            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                            timeout=10,
                        )

                    try:
                        resp = await asyncio.to_thread(_twilio_complete_call)
                        if resp.status_code >= 400:
                            log_call_timeline(
                                "twilio_hangup_failed",
                                session_id=session_id,
                                unique_id=unique_id,
                                call_sid=twilio_sid,
                                status_code=resp.status_code,
                            )
                            logger.warning(
                                f"[Twilio hangup] Failed for CallSid={twilio_sid} | "
                                f"status={resp.status_code} | body={resp.text[:200]}"
                            )
                        else:
                            log_call_timeline(
                                "twilio_hangup_ok",
                                session_id=session_id,
                                unique_id=unique_id,
                                call_sid=twilio_sid,
                            )
                            logger.info(
                                f"[Twilio hangup] Completed CallSid={twilio_sid} "
                                f"for callee={phone_number}"
                            )
                    except Exception as e:
                        log_call_timeline(
                            "twilio_hangup_error",
                            session_id=session_id,
                            unique_id=unique_id,
                            call_sid=twilio_sid,
                        )
                        logger.warning(f"[Twilio hangup] Error for {twilio_sid}: {e}")
                else:
                    log_call_timeline(
                        "twilio_hangup_skipped_no_callsid",
                        session_id=session_id,
                        unique_id=unique_id,
                        callee=phone_number,
                    )
                    # No sweeping fallback here: unsafe for concurrent calling.
        except Exception as e:
            logger.error(f"Hangup failed | session={session_id[:8]} | {e}")

    # ── Create CallSession ───────────────────────────────────────────────────
    session = CallSession(
        org_name=org_name,
        phone_number=phone_number,
        services_list=services_list,
        unique_id=unique_id,
        session_id=session_id,
        results_dir=RESULTS_DIR,
        hangup_fn=hangup_after,
    )

    # ── Create ACS transport ─────────────────────────────────────────────────
    transport = ACSTransport(
        websocket=websocket,
        params=ACSTransportParams(sample_rate=16000),
    )

    # ── Create Pipecat pipeline ──────────────────────────────────────────────
    try:
        pipeline, task = create_pipeline(
            transport=transport,
            session=session,
        )
    except Exception as e:
        logger.error(f"Pipeline creation failed | session={session_id[:8]} | {e}")
        import traceback
        traceback.print_exc()
        await websocket.close()
        return

    # ── Start timers (voicemail silence timeout + fallback hangup) ───────────
    session.start_timers()

    # ── Run pipeline in background ───────────────────────────────────────────
    async def run_pipeline():
        try:
            runner = PipelineRunner(handle_sigint=False)
            await runner.run(task)
        except Exception as e:
            logger.error(f"Pipeline error | session={session_id[:8]} | {e}")
            import traceback
            traceback.print_exc()

    pipeline_task = asyncio.create_task(run_pipeline())

    if AGENT_SETTINGS["call"].get("agent_speaks_first", False):
        try:
            # Yield one event-loop tick so StartFrame begins propagating through
            # the pipeline before we queue the LLMRunFrame. This ensures the
            # pipeline services (Deepgram, OpenAI, Inworld) are initializing in
            # parallel with the kickoff rather than receiving the frame cold.
            await asyncio.sleep(0)
            await session.kickoff_agent_speaks_first()
            log_call_timeline(
                "agent_first_turn_queued",
                session_id=session_id,
                unique_id=unique_id,
            )
        except Exception as e:
            logger.error(
                f"Agent-first kickoff failed | session={session_id[:8]} | {e}"
            )

    # ── Read ACS audio directly and feed into pipeline ───────────────────────
    # This runs in the main ws_endpoint coroutine so it starts immediately
    # when the WebSocket opens — before the pipeline task even starts.
    try:
        while True:
            if websocket.client_state != WebSocketState.CONNECTED:
                break
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), timeout=30.0
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.info(f"WebSocket receive ended: {e}")
                break

            # Handle disconnect
            if message.get("type") == "websocket.disconnect":
                logger.info(f"WebSocket disconnected | session={session_id[:8]}")
                break

            raw = message.get("text") or message.get("bytes")
            if not raw:
                continue

            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except Exception:
                    continue

            try:
                data = json.loads(raw)
            except Exception:
                continue

            kind = data.get("kind", "")

            if kind == "AudioData":
                audio_data = data.get("audioData", {})
                b64 = audio_data.get("data", "")
                if b64:
                    try:
                        pcm_bytes = base64.b64decode(b64)
                        frame = InputAudioRawFrame(
                            audio=pcm_bytes,
                            sample_rate=16000,
                            num_channels=1,
                        )
                        await task.queue_frames([frame])
                    except Exception as e:
                        logger.warning(f"Audio frame error: {e}")

            elif kind == "StopAudio":
                logger.debug("[ACS] StopAudio received")

    finally:
        logger.info(f"WebSocket loop ended | session={session_id[:8]}")
        await session.handle_call_disconnected()
        session.log_metrics_summary()
        session.cancel_timers()
        if not pipeline_task.done():
            pipeline_task.cancel()
        # Cleanup server-side context after the media session ends.
        _ws_context_by_session_id.pop(session_id, None)

#saving file 26th march 2026 9.15am