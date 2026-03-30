#saving file 26th march 2026 9.15am
"""
transcript_processor.py
------------------------
A Pipecat FrameProcessor that sits between Deepgram STT and the
LLM context aggregator.

It intercepts TranscriptionFrame (finalised STT text) and calls the
CallSession methods that implement V1's behavioural logic:

  1. on_first_caller_speech()  — legacy opening when agent_speaks_first is False
  2. on_transcript(text)       — voicemail/IVR detection + human guard

All other frames are passed through transparently.
"""

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.call_session import CallSession


class TranscriptProcessor(FrameProcessor):
    """
    Intercepts finalised STT transcripts to drive CallSession logic.
    Passes all frames downstream unchanged.
    """

    def __init__(self, session: CallSession, **kwargs):
        super().__init__(**kwargs)
        self._session          = session
        self._first_speech_seen = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                # First finalised transcript — trigger caller-speaks-first gate
                if not self._first_speech_seen:
                    self._first_speech_seen = True
                    # Prepare opening context before the first caller transcript
                    # reaches the user aggregator, so the first turn runs once
                    # with the correct instructions already in context.
                    await self._session.on_first_caller_speech()

                # Always run transcript logic (voicemail/IVR detection, human guard)
                await self._session.on_transcript(text)

        # Pass every frame downstream regardless
        await self.push_frame(frame, direction)