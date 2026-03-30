"""
Structured call timeline logging (grep for [CALL_TIMELINE] in server_logs.txt).

Phases are stable strings so you can correlate Twilio ↔ ACS ↔ agent without
always having a single shared ID across providers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_call_timeline(phase: str, **fields: Any) -> None:
    """Log one timeline row: phase + UTC timestamp + optional key=value pairs."""
    parts = [f"phase={phase}", f"ts_utc={utc_now_iso()}"]
    for key in sorted(fields.keys()):
        val = fields[key]
        if val is None or val == "":
            continue
        parts.append(f"{key}={val}")
    logger.info("[CALL_TIMELINE] " + " | ".join(parts))
