"""
call_session.py
---------------
CallSession manages all per-call state and behavioural logic
that lived in V1's CommunicationHandler, now adapted for Pipecat.

Responsibilities:
  1. Opening turn (configurable)
       If agent_speaks_first is True (default), Samantha injects context and queues
       LLMRunFrame as soon as the pipeline is running — no wait for callee speech.
       If False, the legacy behaviour applies: first STT transcript triggers opening.

  2. Voicemail keyword detection
       Every caller transcript (before human_confirmed=True) is scanned
       for voicemail/IVR keywords. On match, result is saved and call ends.

  3. 15-second silence timeout
       If no opening turn yet and no caller speech within 15s, assume voicemail.
       (Cancelled once Samantha opens when agent_speaks_first is True.)

  4. Human confirmed guard
       After the first real caller transcript, keyword detection is disabled
       permanently for the rest of the call to prevent false positives.

  5. extract_call_details handler
       Saves structured JSON result, tells GPT-4o to say goodbye,
       schedules ACS hangup.

  6. Partial result capture on mid-call disconnect
       If the caller hangs up before extract_call_details fires,
       saves whatever partial state is available with
       call_outcome = call_disconnected.

  7. Fallback hangup
       300-second hard ceiling — if the call never ends cleanly,
       hang up anyway.
"""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import LLMRunFrame
from pipecat.metrics.metrics import LLMTokenUsage
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext

from app.agent_settings import (
    AGENT_SETTINGS,
    AGENT_SPEAKS_FIRST_ADDENDUM,
    VOICEMAIL_KEYWORDS,
    IVR_KEYWORDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _contains_keyword(text: str, keywords: list[str]) -> str | None:
    """Return the first matched keyword found in text (case-insensitive), else None."""
    text_lower = text.lower()
    for kw in keywords:
        if kw in text_lower:
            return kw
    return None


def _format_phone_for_speech(phone: str) -> str:
    """
    Strip +1 country code and format as 'XXX XXX XXXX' for natural speech.
    Matches V1 _format_phone_for_speech exactly.
    """
    digits = (
        phone.strip()
        .replace("-", "").replace(" ", "")
        .replace("(", "").replace(")", "")
    )
    if digits.startswith("+1"):
        digits = digits[2:]
    elif digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]} {digits[3:6]} {digits[6:]}"
    return digits


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "n/a"
    total_seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes}m {secs}s"


# ---------------------------------------------------------------------------
# CallSession
# ---------------------------------------------------------------------------

class CallSession:
    """
    One instance per active ACS call.
    Holds all mutable state and orchestrates the V1 behavioural logic.
    """

    def __init__(
        self,
        org_name: str,
        phone_number: str,
        services_list: str,
        unique_id: str,
        session_id: str,
        results_dir: Path,
        hangup_fn,          # async callable(delay_seconds: int)
    ):
        self.org_name       = org_name
        self.phone_number   = phone_number
        self.phone_for_speech = _format_phone_for_speech(phone_number)
        self.services_list  = services_list
        self.unique_id      = unique_id
        self.session_id     = session_id
        self.results_dir    = results_dir
        self._hangup_fn     = hangup_fn
        self._started_at_monotonic = time.monotonic()
        self._voicemail_started_at_monotonic: float | None = None
        self._metrics_logged = False

        # ── V1 flags (exact equivalents) ────────────────────────────────────
        # _response_triggered  → True after first caller speech detected
        #                        (Samantha's opening is queued at that point)
        self._response_triggered: bool = False

        # _human_confirmed     → True after first real caller transcript
        #                        Once True, keyword detection is disabled
        self._human_confirmed: bool    = False

        # _call_ended          → guards all result-saving paths so only one runs
        self._call_ended: bool         = False

        # _goodbye_done        → True after extract_call_details fires;
        #                        used to know hangup should follow greeting
        self._goodbye_done: bool       = False

        # ── Pipeline references (set after pipeline is created) ──────────────
        self._pipeline_task: PipelineTask | None  = None
        self._llm_context: LLMContext | None      = None

        # ── Background tasks ─────────────────────────────────────────────────
        self._silence_timeout_task: asyncio.Task | None  = None
        self._fallback_hangup_task: asyncio.Task | None  = None

        # ── Partial result buffer ─────────────────────────────────────────────
        # Populated by the pipeline the moment GPT-4o invokes extract_call_details
        # (even before the handler finishes), so handle_call_disconnected() can
        # recover whatever args were captured if the caller hangs up mid-tool-call.
        self._pending_tool_args: dict = {}

        # ── Lightweight progress state for update_call_progress tool ─────────
        # This holds high-level flags (stage, org_known, phone_known, etc.)
        # and is written out as the call proceeds so 00001.json always shows
        # the latest known state even before extract_call_details runs.
        self._progress_state: dict = {}

        # ── Per-call usage metrics ───────────────────────────────────────────
        self._llm_prompt_tokens = 0
        self._llm_completion_tokens = 0
        self._llm_cache_read_input_tokens = 0
        self._tts_characters = 0
        self._tts_total_seconds = 0.0
        self._tts_context_starts: dict[str, float] = {}

        # Hangup-after-goodbye coordination: we wait for TTS to stop, then
        # delay a bit before hanging up so the last sentence isn't cut off.
        self._awaiting_final_tts: bool = False
        self._final_tts_context_id: str | None = None
        self._final_tts_stopped_event: asyncio.Event = asyncio.Event()
        self._final_tts_stopped_event.set()

    # ------------------------------------------------------------------
    # Called by pipeline factory after task is created
    # ------------------------------------------------------------------

    def attach_pipeline(self, task: PipelineTask, context: LLMContext):
        self._pipeline_task = task
        self._llm_context   = context

    def update_pending_tool_args(self, args: dict):
        """
        Called by the pipeline the moment GPT-4o fires extract_call_details,
        BEFORE the async handler finishes. This snapshot means that if the
        caller disconnects mid-tool-call, handle_call_disconnected() will have
        the best available args rather than an empty dict.

        Equivalent to V1's _fn_args_buf which accumulated streamed JSON deltas.
        In Pipecat, the LLM service delivers complete arguments at once, so we
        snapshot the whole dict here rather than accumulating deltas.
        """
        self._pending_tool_args = dict(args)

    async def handle_update_call_progress(self, progress: dict) -> dict:
        """
        Lightweight progress updates called by the LLM as the call moves
        through major milestones (org confirmed, phone confirmed, services asked).

        This does NOT end the call. It simply merges flags into an internal
        progress state and writes a snapshot via _save_result so callers can
        inspect evolving state mid-call.
        """
        if self._call_ended:
            # Once a terminal path has run, ignore further progress calls.
            return {"status": "ended"}

        # Normalise booleans / strings from the tool args
        stage = (progress.get("stage") or "").strip()
        org_known = bool(progress.get("org_known", False))
        phone_known = bool(progress.get("phone_known", False))
        services_known = bool(progress.get("services_known", False))

        # Initialise base shape once
        if not self._progress_state:
            self._progress_state = {
                "unique_id":          self.unique_id,
                "session_id":         self.session_id,
                "dialed_number":      self.phone_number,
                "dialed_services":    self.services_list,
                "phone_status":       "unknown",
                "is_correct_number":  "unknown",
                "org_valid":          "unknown",
                "services_confirmed": "unknown",
                "available_services": [],
                "unavailable_services": [],
                "other_services":     [],
                "other_numbers":      None,
                "mentioned_funding":  "no",
                "mentioned_callback": "no",
            }

        # Merge new flags
        if stage:
            self._progress_state["stage"] = stage
        self._progress_state["org_known"] = org_known
        self._progress_state["phone_known"] = phone_known
        self._progress_state["services_known"] = services_known

        snapshot = dict(self._progress_state)
        snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()
        snapshot["call_outcome"] = "in_progress"
        snapshot["partial_capture"] = True

        self._save_result(snapshot)
        return {"status": "ok"}

    def record_llm_usage(self, usage: LLMTokenUsage):
        self._llm_prompt_tokens += usage.prompt_tokens
        self._llm_completion_tokens += usage.completion_tokens
        self._llm_cache_read_input_tokens += usage.cache_read_input_tokens or 0

    def record_tts_usage(self, characters: int):
        self._tts_characters += characters

    def record_tts_started(self, context_id: str | None):
        if context_id:
            self._tts_context_starts[context_id] = time.monotonic()
            if self._awaiting_final_tts:
                self._final_tts_context_id = context_id
                self._final_tts_stopped_event.clear()

    def record_tts_stopped(self, context_id: str | None):
        if not context_id:
            return
        started_at = self._tts_context_starts.pop(context_id, None)
        if started_at is not None:
            self._tts_total_seconds += max(0.0, time.monotonic() - started_at)
        if self._awaiting_final_tts and (
            self._final_tts_context_id is None or context_id == self._final_tts_context_id
        ):
            self._final_tts_stopped_event.set()

    async def _hangup_after_final_tts(self, *, extra_seconds: float = 2.0, timeout_seconds: float = 20.0):
        """Wait for final TTS to stop, then wait extra_seconds, then hang up."""
        try:
            await asyncio.wait_for(self._final_tts_stopped_event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(max(0.0, float(extra_seconds)))
        await self._hangup_fn(0)

    # ------------------------------------------------------------------
    # Session start — kick off timers
    # ------------------------------------------------------------------

    def start_timers(self):
        """
        Start the voicemail silence timeout and fallback hangup.
        Call this as soon as the WebSocket is open and pipeline is running.
        """
        cfg = AGENT_SETTINGS["call"]
        self._silence_timeout_task = asyncio.create_task(
            self._voicemail_silence_timeout(cfg["voicemail_silence_timeout_seconds"])
        )
        self._fallback_hangup_task = asyncio.create_task(
            self._fallback_hangup(cfg["fallback_hangup_seconds"])
        )
        logger.info(
            f"[SESSION {self.session_id[:8]}] Timers started | "
            f"silence_timeout={cfg['voicemail_silence_timeout_seconds']}s | "
            f"fallback_hangup={cfg['fallback_hangup_seconds']}s"
        )

    def cancel_timers(self):
        """Cancel background timer tasks on clean call end."""
        for task in (self._silence_timeout_task, self._fallback_hangup_task):
            if task and not task.done():
                task.cancel()

    # ------------------------------------------------------------------
    # Opening / callee-speaks-first (legacy) vs agent speaks first
    # ------------------------------------------------------------------

    def _inject_opening_context_messages(self) -> None:
        """Append per-call CRM facts as a system message."""
        if self._llm_context is None:
            return
        opening_context = (
            f"[CALL CONTEXT — do not read aloud]\n"
            f"Organization to verify: {self.org_name}\n"
            f"Phone number dialed (say as): {self.phone_for_speech}\n"
            f"Services to verify: {self.services_list}\n"
            f"Unique ID for this call (never say aloud): {self.unique_id}\n"
            f"CRITICAL: Follow the opening logic from the main system prompt exactly. "
            f"Speak only as Samantha. Never roleplay the callee, never invent another "
            f"organization, and never output scene descriptions, bracketed stage "
            f"directions, or quoted dialogue for the receiver."
        )
        self._llm_context.messages.append({
            "role": "system",
            "content": opening_context,
        })
        logger.info(f"[SESSION {self.session_id[:8]}] Opening context prepared")

    async def kickoff_agent_speaks_first(self) -> None:
        """
        Queue Samantha's first turn as soon as the pipeline is live (ACS media up).
        See AGENT_SETTINGS["call"]["agent_speaks_first"].
        """
        cfg = AGENT_SETTINGS["call"]
        if not cfg.get("agent_speaks_first", False):
            return
        if self._call_ended or self._response_triggered:
            return
        self._response_triggered = True

        if self._silence_timeout_task and not self._silence_timeout_task.done():
            self._silence_timeout_task.cancel()

        org = self.org_name or "the organization"
        if self._llm_context is not None:
            addendum = AGENT_SPEAKS_FIRST_ADDENDUM.format(org_name=org)
            self._llm_context.messages.append({
                "role": "system",
                "content": addendum,
            })
        self._inject_opening_context_messages()

        if self._pipeline_task is not None:
            await self._pipeline_task.queue_frames([LLMRunFrame()])
        logger.info(
            f"[SESSION {self.session_id[:8]}] Agent speaks first — opening queued"
        )

    async def on_first_caller_speech(self):
        """
        When agent_speaks_first is False: first STT final triggers opening.
        When True: kickoff_agent_speaks_first already ran; this is a no-op.
        """
        if self._response_triggered or self._call_ended:
            return
        self._response_triggered = True

        if self._silence_timeout_task and not self._silence_timeout_task.done():
            self._silence_timeout_task.cancel()

        logger.info(
            f"[SESSION {self.session_id[:8]}] First caller speech detected — "
            f"waiting {AGENT_SETTINGS['call']['opening_delay_seconds']}s before opening"
        )

        await asyncio.sleep(AGENT_SETTINGS["call"]["opening_delay_seconds"])

        if self._call_ended:
            return

        self._inject_opening_context_messages()

    # ------------------------------------------------------------------
    # Transcript processing — voicemail / IVR detection + human guard
    # Called by TranscriptProcessor for every finalised STT transcript
    # ------------------------------------------------------------------

    async def on_transcript(self, text: str):
        """
        Equivalent to V1 conversation.item.input_audio_transcription.completed handler.

        Before human_confirmed:
          - Check for voicemail keywords → handle_voicemail
          - Check for IVR keywords      → handle_ivr
        After first real transcript: set human_confirmed = True (disables checks)
        """
        if self._call_ended:
            return

        logger.info(f"[CALLER]: {text}")

        # Fast-path: if the first real human response is a clear "no / wrong number",
        # end the call immediately instead of relying on the LLM to call the tool.
        # This prevents calls that linger until the callee hangs up.
        if not self._human_confirmed:
            t = (text or "").strip().lower()
            hard_no = {
                "no", "nope", "nah", "wrong number", "not you", "not this number",
                "you have the wrong number", "this is the wrong number",
                "not united states adoption centre", "not that organization",
            }
            if t in hard_no or ("wrong number" in t) or ("have the wrong number" in t):
                logger.info(
                    f"[SESSION {self.session_id[:8]}] Early negative response — ending call"
                )
                # Save a minimal structured result and hang up quickly.
                result = {
                    "unique_id":            self.unique_id,
                    "session_id":           self.session_id,
                    "timestamp":            datetime.now(timezone.utc).isoformat(),
                    "phone_status":         "invalid",
                    "is_correct_number":    "no",
                    "org_valid":            "incorrect_org",
                    "call_outcome":         "not_org_wrong_number",
                    "call_summary": (
                        f"Receiver indicated this is not {self.org_name or 'the organization'} "
                        f"or it is the wrong number. Ended call."
                    ),
                    "other_numbers":        None,
                    "services_confirmed":   "unknown",
                    "available_services":   [],
                    "unavailable_services": [],
                    "other_services":       [],
                    "mentioned_funding":    "no",
                    "mentioned_callback":   "no",
                    "dialed_number":        self.phone_number,
                    "dialed_services":      self.services_list,
                }
                self._save_result(result)
                self._call_ended = True
                self.cancel_timers()
                asyncio.create_task(self._hangup_fn(0))
                return

        if not self._human_confirmed:
            vm_match = _contains_keyword(text, VOICEMAIL_KEYWORDS)
            if vm_match:
                logger.info(
                    f"[SESSION {self.session_id[:8]}] Voicemail keyword: '{vm_match}'"
                )
                await self.handle_voicemail_detected(reason=f"keyword: {vm_match}")
                return

            if not self._call_ended:
                ivr_match = _contains_keyword(text, IVR_KEYWORDS)
                if ivr_match:
                    logger.info(
                        f"[SESSION {self.session_id[:8]}] IVR keyword: '{ivr_match}'"
                    )
                    await self.handle_ivr_detected(matched_keyword=ivr_match)
                    return

        # Set AFTER keyword check — first real transcript still gets checked,
        # all subsequent ones skip keyword detection entirely
        if not self._human_confirmed and not self._call_ended:
            self._human_confirmed = True
            logger.info(
                f"[SESSION {self.session_id[:8]}] Human confirmed — "
                f"keyword detection disabled for rest of call"
            )

    # ------------------------------------------------------------------
    # Voicemail silence timeout
    # ------------------------------------------------------------------

    async def _voicemail_silence_timeout(self, seconds: int):
        """
        If no caller speech within `seconds`, assume voicemail picked up silently.
        Equivalent to V1 _voicemail_silence_timeout.
        """
        await asyncio.sleep(seconds)
        if not self._response_triggered and not self._call_ended:
            logger.info(
                f"[SESSION {self.session_id[:8]}] No speech after {seconds}s "
                f"— assuming voicemail"
            )
            await self.handle_voicemail_detected(reason="silence_timeout")

    # ------------------------------------------------------------------
    # Fallback hangup
    # ------------------------------------------------------------------

    async def _fallback_hangup(self, seconds: int):
        """
        Hard ceiling — hang up after `seconds` regardless of call state.
        Prevents zombie calls if something goes wrong.
        """
        await asyncio.sleep(seconds)
        if not self._call_ended:
            logger.warning(
                f"[SESSION {self.session_id[:8]}] Fallback hangup after {seconds}s"
            )
            self._call_ended = True
            await self._hangup_fn(0)

    # ------------------------------------------------------------------
    # Voicemail handler
    # Equivalent to V1 _handle_voicemail_detected
    # ------------------------------------------------------------------

    async def handle_voicemail_detected(self, reason: str = "keyword"):
        """Save voicemail result, speak the voicemail message, then hang up."""
        if self._call_ended:
            return
        self._call_ended = True
        self._voicemail_started_at_monotonic = time.monotonic()

        logger.info(
            f"[SESSION {self.session_id[:8]}] Voicemail detected ({reason}) | "
            f"unique_id={self.unique_id}"
        )

        result = {
            "unique_id":            self.unique_id,
            "session_id":           self.session_id,
            "timestamp":            datetime.now(timezone.utc).isoformat(),
            "phone_status":         "sent_to_voicemail",
            "is_correct_number":    "unknown",
            "org_valid":            "unknown",
            "call_outcome":         "no_answer_voicemail",
            "call_summary": (
                f"Reached voicemail for {self.org_name}. "
                f"Detection reason: {reason}. Left voicemail message."
            ),
            "other_numbers":        None,
            "services_confirmed":   "unknown",
            "available_services":   [],
            "unavailable_services": [],
            "other_services":       [],
            "mentioned_funding":    "no",
            "mentioned_callback":   "no",
            "dialed_number":        self.phone_number,
            "dialed_services":      self.services_list,
        }
        self._save_result(result)

        # Build the exact voicemail message from V1
        voicemail_message = (
            f"Hi, this is Samantha calling from GroundGame dot Health. "
            f"I'm reaching out to verify contact information for {self.org_name}. "
            f"If this is the correct number for {self.org_name}, no action is needed. "
            f"If this is not the right number, please call us back so we can update "
            f"our records. Thank you, and have a great day."
        )

        # Inject voicemail instruction into LLM context so GPT-4o speaks it
        if self._llm_context is not None and self._pipeline_task is not None:
            self._llm_context.messages.append({
                "role": "user",
                "content": (
                    f"You have reached voicemail. "
                    f"Leave this exact message word for word: {voicemail_message}"
                ),
            })
            await self._pipeline_task.queue_frames([LLMRunFrame()])

        # Hang up after voicemail message has time to play (V1: 20s)
        cfg = AGENT_SETTINGS["call"]
        asyncio.create_task(self._hangup_fn(cfg["voicemail_hangup_seconds"]))

    # ------------------------------------------------------------------
    # IVR handler
    # Equivalent to V1 _handle_ivr_detected
    # ------------------------------------------------------------------

    async def handle_ivr_detected(self, matched_keyword: str):
        """Save IVR result and hang up immediately — no Voice interaction needed."""
        if self._call_ended:
            return
        self._call_ended = True

        logger.info(
            f"[SESSION {self.session_id[:8]}] IVR/call centre detected | "
            f"keyword='{matched_keyword}' | unique_id={self.unique_id}"
        )

        result = {
            "unique_id":            self.unique_id,
            "session_id":           self.session_id,
            "timestamp":            datetime.now(timezone.utc).isoformat(),
            "phone_status":         "invalid",
            "is_correct_number":    "unknown",
            "org_valid":            "unknown",
            "call_outcome":         "other",
            "call_summary": (
                f"Reached IVR or call centre for {self.org_name}. "
                f"Detected keyword: '{matched_keyword}'. Hung up without verifying."
            ),
            "other_numbers":        None,
            "services_confirmed":   "unknown",
            "available_services":   [],
            "unavailable_services": [],
            "other_services":       [],
            "mentioned_funding":    "no",
            "mentioned_callback":   "no",
            "dialed_number":        self.phone_number,
            "dialed_services":      self.services_list,
        }
        self._save_result(result)

        # IVR: hang up immediately (V1: 1s delay)
        asyncio.create_task(self._hangup_fn(1))

    # ------------------------------------------------------------------
    # extract_call_details handler
    # Equivalent to V1 _handle_extract_call_details
    # ------------------------------------------------------------------

    async def handle_extract_call_details(self, args: dict) -> dict:
        """
        Called when GPT-4o invokes the extract_call_details tool.
        Fills defaults, saves JSON, triggers goodbye, schedules hangup.
        Returns the result_callback payload for Pipecat.
        """
        if self._call_ended:
            logger.info(
                f"[SESSION {self.session_id[:8]}] extract_call_details already captured — skipping"
            )
            return {"status": "already_captured"}

        self._call_ended   = True
        self._goodbye_done = True

        # Fill defaults for any missing fields (exact same logic as V1)
        args.setdefault("unique_id",            self.unique_id)
        args.setdefault("phone_status",         "unknown")
        args.setdefault("is_correct_number",    "unknown")
        args.setdefault("org_valid",            "unknown")
        args.setdefault("call_outcome",         "other")
        args.setdefault("call_summary",         "")
        args.setdefault("other_numbers",        None)
        args.setdefault("services_confirmed",   "unknown")
        args.setdefault("available_services",   [])
        args.setdefault("unavailable_services", [])
        args.setdefault("other_services",       [])
        args.setdefault("mentioned_funding",    "no")
        args.setdefault("mentioned_callback",   "no")

        args["session_id"]      = self.session_id
        args["timestamp"]       = datetime.now(timezone.utc).isoformat()
        args["dialed_number"]   = self.phone_number
        args["dialed_services"] = self.services_list

        self._save_result(args)

        # Cancel fallback timer now that we have a clean result
        self.cancel_timers()

        # Hang up AFTER the final goodbye audio finishes, plus a small cushion.
        # This prevents the call from cutting off the last sentence.
        self._awaiting_final_tts = True
        self._final_tts_context_id = None
        self._final_tts_stopped_event.clear()
        asyncio.create_task(self._hangup_after_final_tts(extra_seconds=2.0))

        # Return "captured" so GPT-4o knows to say goodbye
        return {"status": "captured"}

    # ------------------------------------------------------------------
    # Disconnect handler — partial result capture
    # Equivalent to V1 _handle_call_disconnected
    # ------------------------------------------------------------------

    async def handle_call_disconnected(self):
        """
        Called when the ACS WebSocket closes before extract_call_details fired.
        Saves whatever partial state was captured.
        Equivalent to V1 _handle_call_disconnected.
        """
        if self._call_ended:
            return
        self._call_ended = True

        self.cancel_timers()

        logger.info(
            f"[SESSION {self.session_id[:8]}] Caller disconnected mid-call | "
            f"unique_id={self.unique_id}"
        )

        # Try to recover partial tool args (equivalent to V1 _fn_args_buf recovery)
        partial = self._pending_tool_args

        # Use `or` so empty strings from partial tool args don't wipe session fields.
        uid_partial = (partial.get("unique_id") or "").strip()
        result = {
            "unique_id":            uid_partial or self.unique_id,
            "session_id":           self.session_id,
            "timestamp":            datetime.now(timezone.utc).isoformat(),
            "phone_status":         partial.get("phone_status",        "unknown"),
            "is_correct_number":    partial.get("is_correct_number",   "unknown"),
            "org_valid":            partial.get("org_valid",           "unknown"),
            "call_outcome":         "call_disconnected",   # always override
            "call_summary":         partial.get(
                                        "call_summary",
                                        f"Caller disconnected mid-call for {self.org_name or 'the organization'}. "
                                        f"Partial data captured where available."
                                    ),
            "other_numbers":        partial.get("other_numbers",       None),
            "services_confirmed":   partial.get("services_confirmed",  "unknown"),
            "available_services":   partial.get("available_services",  []),
            "unavailable_services": partial.get("unavailable_services",[]),
            "other_services":       partial.get("other_services",      []),
            "mentioned_funding":    partial.get("mentioned_funding",   "no"),
            "mentioned_callback":   partial.get("mentioned_callback",  "no"),
            "partial_capture":      True,
            "dialed_number":        self.phone_number,
            "dialed_services":      self.services_list,
        }
        self._save_result(result)

    # ------------------------------------------------------------------
    # Internal: save result JSON to disk
    # ------------------------------------------------------------------

    def _save_result(self, result: dict):
        logger.info(
            f"[CALL RESULT] unique_id={result.get('unique_id')} | "
            f"org_valid={result.get('org_valid')} | "
            f"phone_status={result.get('phone_status')} | "
            f"is_correct_number={result.get('is_correct_number')} | "
            f"services_confirmed={result.get('services_confirmed')} | "
            f"outcome={result.get('call_outcome')} | "
            f"other_numbers={result.get('other_numbers')} | "
            f"available={result.get('available_services')} | "
            f"unavailable={result.get('unavailable_services')} | "
            f"other_services={result.get('other_services')} | "
            f"summary={result.get('call_summary')}"
        )

        self.results_dir.mkdir(parents=True, exist_ok=True)

        mode = (os.getenv("CALL_RESULTS_MODE") or "jsonl").strip().lower()
        if mode in ("jsonl", "single", "one"):
            # One append-only file (JSON Lines): one JSON object per line.
            name = (os.getenv("CALL_RESULTS_JSONL_NAME") or "call_results.jsonl").strip()
            if not name:
                name = "call_results.jsonl"
            result_file = self.results_dir / name
            try:
                line = json.dumps(result, ensure_ascii=False) + "\n"
                with open(result_file, "a", encoding="utf-8") as f:
                    f.write(line)
                logger.info(f"Result appended → {result_file}")
            except Exception as e:
                logger.error(f"Failed to append result: {e}")
            return

        # Legacy: one file per call ({unique_id}.json)
        raw_uid = result.get("unique_id") or self.unique_id or ""
        uid = raw_uid.strip() if raw_uid.strip() else str(uuid.uuid4())
        result_file = self.results_dir / f"{uid}.json"
        try:
            result_file.write_text(json.dumps(result, indent=2))
            logger.info(f"Result saved → {result_file}")
        except Exception as e:
            logger.error(f"Failed to save result: {e}")

    def log_metrics_summary(self):
        if self._metrics_logged:
            return
        self._metrics_logged = True

        now = time.monotonic()
        call_duration_seconds = now - self._started_at_monotonic
        voicemail_duration_seconds = None
        if self._voicemail_started_at_monotonic is not None:
            voicemail_duration_seconds = now - self._voicemail_started_at_monotonic

        # Deepgram is active for the duration of the streaming session, so media
        # lifetime is the closest per-call approximation available in-app.
        stt_duration_seconds = call_duration_seconds

        logger.info(
            "[CALL METRICS] "
            f"session={self.session_id[:8]} | unique_id={self.unique_id or 'n/a'} | "
            f"GPT-4o input={self._llm_prompt_tokens} | "
            f"cache_input={self._llm_cache_read_input_tokens} | "
            f"output={self._llm_completion_tokens} | "
            f"Deepgram STT approx={_format_duration(stt_duration_seconds)} | "
            f"ElevenLabs TTS approx={_format_duration(self._tts_total_seconds)} | "
            f"ElevenLabs chars={self._tts_characters} | "
            f"ACS total={_format_duration(call_duration_seconds)} | "
            f"Voicemail={_format_duration(voicemail_duration_seconds)}"
        )

#saving file 26th march 2026 9.15am