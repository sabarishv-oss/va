# Samantha — ACS Outbound Voice Verification Agent (Pipecat v2.1)

Replaces **Azure Voice Live** with a **Pipecat STT/LLM/TTS pipeline** while preserving every V1 behavioural feature exactly.

| Component | V1 (Azure Voice Live) | V2 (This version) |
|---|---|---|
| Orchestration | Manual WebSocket event loop | **Pipecat** |
| STT | Azure Speech (built into Voice Live) | **Deepgram** `nova-2-phonecall` |
| LLM | Azure OpenAI GPT-4o Realtime | **OpenAI GPT-4o** |
| TTS | Azure HD Neural TTS | **ElevenLabs** `eleven_turbo_v2_5` |
| VAD | Azure Semantic VAD | **Silero VAD** |
| Caller-speaks-first gate | ✅ | ✅ |
| Voicemail keyword detection | ✅ | ✅ |
| IVR keyword detection | ✅ | ✅ |
| 15s silence → assume voicemail | ✅ | ✅ |
| Human confirmed guard | ✅ | ✅ |
| Partial result on disconnect | ✅ | ✅ |
| Fallback hard hangup (300s) | ✅ | ✅ |
| Barge-in / interruption | ✅ | ✅ |
| extract_call_details tool | ✅ | ✅ |
| JSON result files | ✅ | ✅ |
| Batch CSV dialer | ✅ | ✅ |

---

## Architecture

```
campaign_input.csv
       │
       ▼
  app/dialer.py  ──── ACS create_call ────► Target phone
                            │
                  ACS Callback Events
                            │
                            ▼
              FastAPI /api/callbacks (main.py)
                            │
                  ACS PCM24K audio (WebSocket)
                            │
                            ▼
              FastAPI WebSocket /ws (main.py)
                            │
                    ACSTransport (acs_transport.py)
                            │
          ┌─────────────────┴───────────────────────────────────┐
          │              Pipecat Pipeline                        │
          │                                                      │
          │  ACSAudioInput                                       │
          │      → Deepgram STT                                  │
          │      → TranscriptProcessor  ─── CallSession ──►      │
          │            (voicemail/IVR detect, caller-first gate) │
          │      → SileroVAD (inside user aggregator)            │
          │      → user context aggregator                       │
          │      → OpenAI GPT-4o (extract_call_details tool)     │
          │      → ElevenLabs TTS                                │
          │      → ACSAudioOutput                                │
          │      → assistant context aggregator                  │
          └──────────────────────────────────────────────────────┘
                            │
                  CallSession._save_result()
                            │
                  call_results/<unique_id>.json
```

---

## File Structure

```
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app — HTTP callbacks + WebSocket
│   ├── acs_transport.py         # Pipecat transport bridging ACS PCM ↔ frames
│   ├── call_session.py          # ALL V1 behavioural logic (ported from CommunicationHandler)
│   ├── transcript_processor.py  # Pipecat processor — feeds transcripts to CallSession
│   ├── pipecat_pipeline.py      # STT/LLM/TTS pipeline factory
│   ├── agent_settings.py        # Service config, system prompt, tool schema, keyword lists
│   └── dialer.py                # CSV campaign dialer (unchanged from V1)
├── campaign_input.csv
├── .env.template
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## V1 Feature → V2 Implementation Mapping

| V1 (CommunicationHandler) | V2 (Where it lives) |
|---|---|
| `_response_triggered` flag | `CallSession._response_triggered` |
| `_human_confirmed` flag | `CallSession._human_confirmed` |
| `_call_ended` guard | `CallSession._call_ended` |
| `_goodbye_done` flag | `CallSession._goodbye_done` |
| `speech_started` → trigger opening | `TranscriptProcessor` → `CallSession.on_first_caller_speech()` |
| `0.5s` opening delay | `AGENT_SETTINGS["call"]["opening_delay_seconds"]` |
| Voicemail keyword scan | `CallSession.on_transcript()` + `VOICEMAIL_KEYWORDS` |
| IVR keyword scan | `CallSession.on_transcript()` + `IVR_KEYWORDS` |
| `_voicemail_silence_timeout` (15s) | `CallSession._voicemail_silence_timeout()` |
| `_handle_voicemail_detected` | `CallSession.handle_voicemail_detected()` |
| `_handle_ivr_detected` | `CallSession.handle_ivr_detected()` |
| `_handle_extract_call_details` | `CallSession.handle_extract_call_details()` |
| `_handle_call_disconnected` | `CallSession.handle_call_disconnected()` |
| `_fn_args_buf` partial recovery | `CallSession._pending_tool_args` |
| `_hangup_after_delay` | `hangup_after()` in `main.py` (passed as `hangup_fn`) |
| Fallback hangup (300s) | `CallSession._fallback_hangup()` |
| `_format_phone_for_speech` | `call_session._format_phone_for_speech()` |
| `VOICEMAIL_KEYWORDS` list | `agent_settings.VOICEMAIL_KEYWORDS` |
| `IVR_KEYWORDS` list | `agent_settings.IVR_KEYWORDS` |
| Voicemail message text | `CallSession.handle_voicemail_detected()` |
| `session.update` / `response.create` | Pipecat `LLMRunFrame` queued on task |
| Opening cue as system message | `CallSession.on_first_caller_speech()` appends to context |
| `_session_registry` dict | `main._session_registry` (unchanged) |

---

## Setup

### 1. Install
```bash
uv sync
# or
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.template .env
# Fill in all values
```

### 3. Start DevTunnel
```bash
devtunnel create --allow-anonymous
devtunnel port create -p 8000
devtunnel host
# Copy HTTPS URL → CALLBACK_URI_HOST in .env
```

### 4. Start server
```bash
uv run uvicorn app.main:app --reload --port 8000
```

### 5. Run campaign
```bash
uv run python -m app.dialer
```

### 6. Single call trigger (optional)
```bash
curl -X POST https://<your-tunnel>/api/outboundCall \
  -H "Content-Type: application/json" \
  -d '{"phone_number":"+16179925508","org_name":"Bright Kids","services":"adoption services","unique_id":"00001"}'
```

---

## Customising the Voice

Change `AGENT_SETTINGS["tts"]["voice_id"]` in `app/agent_settings.py`.
Browse voices: https://elevenlabs.io/voice-library

## Adjusting VAD Sensitivity

Edit `AGENT_SETTINGS["vad"]` in `app/agent_settings.py`:
- `stop_secs`: how long silence before end-of-turn (higher = less likely to cut caller off)
- `confidence`: VAD sensitivity (lower = detects quieter speech)

## Adjusting Timing

Edit `AGENT_SETTINGS["call"]` in `app/agent_settings.py`:
- `voicemail_silence_timeout_seconds`: how long to wait before assuming voicemail (default 15)
- `hangup_delay_seconds`: gap after goodbye before ACS hangup (default 10)
- `opening_delay_seconds`: pause after first caller speech before Samantha responds (default 0.5)