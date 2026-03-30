"""
dialer.py
---------
Reads campaign_input.csv and places outbound ACS calls for each row.

CSV columns expected:
    org_name, phone_number, services, unique_id

A session_id (UUID) is generated per call and embedded in BOTH the
WebSocket URL and the callback URI.  When ACS fires CallConnected, the
server stores session_id → call_connection_id so the WebSocket handler
can resolve it for clean hangup.

Batching / throttling is controlled via env vars:
    CALLS_PER_BATCH      (default 10)
    BATCH_DELAY_SECONDS  (default 10)
"""

import sys
from pathlib import Path

# `python app/dialer.py` puts only `app/` on sys.path; package root must be the
# project directory so `from app...` resolves.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import csv
import json
import os
import time
import uuid
from typing import Iterable
from urllib.parse import urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from loguru import logger
import requests

from app.call_timeline import log_call_timeline
from azure.communication.callautomation import (
    CallAutomationClient,
    PhoneNumberIdentifier,
    MediaStreamingOptions,
    AudioFormat,
    MediaStreamingTransportType,
    MediaStreamingContentType,
    MediaStreamingAudioChannelType,
)

load_dotenv()

ACS_CONNECTION_STRING   = os.getenv("ACS_CONNECTION_STRING")
ACS_SOURCE_PHONE_NUMBER = os.getenv("ACS_SOURCE_PHONE_NUMBER")
CALLBACK_URI_HOST       = os.getenv("CALLBACK_URI_HOST")
CALLBACK_EVENTS_URI     = (CALLBACK_URI_HOST or "") + "/api/callbacks"

CAMPAIGN_INPUT_CSV  = os.getenv("CAMPAIGN_INPUT_CSV", "./campaign_input.csv")
CALLS_PER_BATCH     = int(os.getenv("CALLS_PER_BATCH", "10"))
BATCH_DELAY_SECONDS = int(os.getenv("BATCH_DELAY_SECONDS", "10"))
DIALER_PROVIDER     = os.getenv("DIALER_PROVIDER", "twilio").strip().lower()

TWILIO_ACCOUNT_SID         = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN          = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER         = os.getenv("TWILIO_FROM_NUMBER", "").strip()
TWILIO_VOICE_WEBHOOK_URL   = os.getenv("TWILIO_VOICE_WEBHOOK_URL", "").strip()
TWILIO_STATUS_CALLBACK_URL = os.getenv("TWILIO_STATUS_CALLBACK_URL", "").strip()
TWILIO_STUDIO_FLOW_SID     = os.getenv("TWILIO_STUDIO_FLOW_SID", "").strip()


def validate_env() -> None:
    if DIALER_PROVIDER not in {"acs", "twilio"}:
        raise RuntimeError("DIALER_PROVIDER must be either 'acs' or 'twilio'")

    if DIALER_PROVIDER == "acs":
        required = [
            ("ACS_CONNECTION_STRING", ACS_CONNECTION_STRING),
            ("ACS_SOURCE_PHONE_NUMBER", ACS_SOURCE_PHONE_NUMBER),
            ("CALLBACK_URI_HOST", CALLBACK_URI_HOST),
        ]
    else:
        required = [
            ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
            ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
            ("TWILIO_FROM_NUMBER", TWILIO_FROM_NUMBER),
        ]
        if not TWILIO_STUDIO_FLOW_SID:
            required.append(("TWILIO_VOICE_WEBHOOK_URL", TWILIO_VOICE_WEBHOOK_URL))
    missing = [name for name, val in required if not val]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def load_targets(path: str) -> Iterable[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def _e164(number: str) -> str:
    """Normalise to E.164 — prepend +1 if no country code present."""
    number = (
        number.strip()
        .replace("-", "").replace(" ", "")
        .replace("(", "").replace(")", "")
    )
    if not number.startswith("+"):
        number = "+1" + number
    return number


def _build_websocket_url(params: str) -> str:
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


def _register_context_with_server(
    phone_number: str,
    org_name: str,
    services: str,
    unique_id: str,
) -> None:
    """So ACS /api/acs/incoming can attach org/services when Studio triggers the call."""
    if not CALLBACK_URI_HOST:
        return
    url = (CALLBACK_URI_HOST.rstrip("/")) + "/api/twilio/register-context"
    try:
        r = requests.post(
            url,
            json={
                "phone_number": phone_number,
                "org_name": org_name,
                "services": services,
                "unique_id": unique_id,
            },
            timeout=10,
        )
        if r.status_code >= 400:
            logger.warning(
                f"register-context failed | status={r.status_code} | body={r.text[:200]}"
            )
    except Exception as e:
        logger.warning(f"register-context request failed: {e}")


def _place_call_via_twilio_studio(
    phone_number: str,
    org_name: str,
    services: str,
    unique_id: str,
) -> str:
    """Start outbound call via Twilio Studio Flow Execution API."""
    _register_context_with_server(phone_number, org_name, services, unique_id)
    url = f"https://studio.twilio.com/v1/Flows/{TWILIO_STUDIO_FLOW_SID}/Executions"
    params_obj = {
        "org_name": org_name,
        "services": services,
        "unique_id": unique_id,
    }
    payload = {
        "To": phone_number,
        "From": TWILIO_FROM_NUMBER,
        "Parameters": json.dumps(params_obj),
    }
    log_call_timeline(
        "twilio_studio_execution_request",
        flow_sid=TWILIO_STUDIO_FLOW_SID,
        to_=phone_number,
        from_=TWILIO_FROM_NUMBER,
        unique_id=unique_id,
    )
    response = requests.post(
        url,
        data=payload,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Twilio Studio execution failed | status={response.status_code} | body={response.text}"
        )
    exec_sid = response.json().get("sid", "")
    log_call_timeline(
        "twilio_studio_execution_created",
        execution_sid=exec_sid,
        flow_sid=TWILIO_STUDIO_FLOW_SID,
        to_=phone_number,
        unique_id=unique_id,
    )
    return exec_sid


def _place_call_via_twilio(phone_number: str) -> str:
    create_call_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json"
    )
    payload = {
        "To": phone_number,
        "From": TWILIO_FROM_NUMBER,
        "Url": TWILIO_VOICE_WEBHOOK_URL,
        "Method": "POST",
    }
    if TWILIO_STATUS_CALLBACK_URL:
        payload["StatusCallback"] = TWILIO_STATUS_CALLBACK_URL

    log_call_timeline(
        "twilio_rest_call_request",
        to_=phone_number,
        from_=TWILIO_FROM_NUMBER,
        voice_webhook=TWILIO_VOICE_WEBHOOK_URL,
    )
    response = requests.post(
        create_call_url,
        data=payload,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Twilio call create failed | status={response.status_code} | body={response.text}"
        )
    call_sid = response.json().get("sid", "")
    log_call_timeline("twilio_rest_call_created", call_sid=call_sid, to_=phone_number)
    return call_sid


def place_calls() -> None:
    validate_env()
    client = None
    if DIALER_PROVIDER == "acs":
        client = CallAutomationClient.from_connection_string(ACS_CONNECTION_STRING)

    source_number = ACS_SOURCE_PHONE_NUMBER if DIALER_PROVIDER == "acs" else TWILIO_FROM_NUMBER
    logger.info(
        f"Starting outbound campaign | provider={DIALER_PROVIDER} | source: {source_number}"
    )
    logger.info(f"Reading targets from: {CAMPAIGN_INPUT_CSV}")

    batch_count = 0
    batch_calls = 0

    for row in load_targets(CAMPAIGN_INPUT_CSV):
        raw_number = (
            row.get("phone_number") or row.get("phone") or row.get("to") or ""
        ).strip()

        if not raw_number:
            logger.warning(f"Skipping row — no phone number: {row}")
            continue

        phone_number = _e164(raw_number)
        org_name     = (row.get("org_name") or "").strip()
        services     = (
            row.get("services") or
            row.get("services_listed") or
            row.get("services_list") or ""
        ).strip()
        unique_id    = (row.get("unique_id") or row.get("id") or str(uuid.uuid4())).strip()

        session_id = str(uuid.uuid4())

        ws_params = urlencode({
            "org_name":     org_name,
            "phone_number": phone_number,
            "services":     services,
            "unique_id":    unique_id,
            "session_id":   session_id,
        })

        logger.info(
            f"Placing call → {phone_number} | org={org_name} | "
            f"unique_id={unique_id} | session_id={session_id}"
        )

        try:
            if DIALER_PROVIDER == "acs":
                callback_uri = f"{CALLBACK_EVENTS_URI}/{session_id}?{ws_params}"
                websocket_url = _build_websocket_url(ws_params)
                logger.debug(f"  callback_uri : {callback_uri}")
                logger.debug(f"  websocket_url: {websocket_url}")
                log_call_timeline(
                    "acs_outbound_create_call_start",
                    session_id=session_id,
                    to_=phone_number,
                    unique_id=unique_id,
                    org_name=org_name,
                )
                result = client.create_call(
                    target_participant=PhoneNumberIdentifier(phone_number),
                    source_caller_id_number=PhoneNumberIdentifier(ACS_SOURCE_PHONE_NUMBER),
                    callback_url=callback_uri,
                    media_streaming=_build_media_streaming_options(websocket_url),
                    operation_context=ws_params,
                )
                log_call_timeline(
                    "acs_outbound_create_call_ok",
                    session_id=session_id,
                    call_connection_id=result.call_connection_id,
                    unique_id=unique_id,
                )
                logger.info(
                    f"Call placed | provider=acs | connection_id={result.call_connection_id} | "
                    f"session_id={session_id} | unique_id={unique_id}"
                )
            else:
                if TWILIO_STUDIO_FLOW_SID:
                    exec_sid = _place_call_via_twilio_studio(
                        phone_number, org_name, services, unique_id,
                    )
                    logger.info(
                        f"Call placed | provider=twilio_studio | "
                        f"execution_sid={exec_sid} | session_id={session_id} | "
                        f"unique_id={unique_id}"
                    )
                else:
                    call_sid = _place_call_via_twilio(phone_number)
                    logger.info(
                        f"Call placed | provider=twilio | call_sid={call_sid} | "
                        f"session_id={session_id} | unique_id={unique_id}"
                    )
        except Exception as e:
            logger.error(f"Failed to place call to {phone_number}: {e}")
            continue

        batch_calls += 1
        if batch_calls >= CALLS_PER_BATCH:
            batch_count += 1
            logger.info(
                f"Batch {batch_count} done ({batch_calls} calls). "
                f"Sleeping {BATCH_DELAY_SECONDS}s before next batch…"
            )
            time.sleep(BATCH_DELAY_SECONDS)
            batch_calls = 0

    logger.info("Campaign complete.")


if __name__ == "__main__":
    place_calls()

#saving file 26th march 2026 9.15am