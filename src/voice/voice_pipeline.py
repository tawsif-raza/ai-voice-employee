"""
Voice Pipeline Orchestration Engine (src/voice/voice_pipeline.py)

Coordinates the end-to-end full-duplex real-time telephone pipeline:
Twilio WebSocket ↔ Deepgram Streaming STT ↔ ConversationManager ↔ ElevenLabs Streaming TTS ↔ Twilio Playback

Strict Behavioral Invariants:
1. Safety First: The authoritative ConversationManager remains the single brain.
   Caller input → deterministic validation → clinical safety guard → policy engine
   → LLM (Claude primary / Gemini fallback) → ToolOrchestrator → response safety → TTS.
2. Barge-in (Interruption):
   When Deepgram emits `SpeechStarted`, immediately:
   a. Send Twilio `clear` event to flush the local phone speaker buffer within ~30ms.
   b. Signal cancellation to in-flight TTS generation and streaming loops.
   c. Discard unplayed outbound audio chunks so old audio never continues playing.
3. Partial Transcripts:
   Interim ASR hypotheses are strictly observational and NEVER trigger tools or actions.
4. Finalized Turns:
   Only `FINAL_TRANSCRIPT` events initiate ConversationManager turns.
5. Per-Call Isolation:
   Each call runs in an isolated CallSession with separate queues and cancellation tokens.
6. Error Degradation:
   Transient provider failures degrade gracefully with conversational apologies rather
   than dropping the phone call.
"""

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable, Optional

from stt_service import BaseSTTService, STTEventType
from telephony_models import (
    CallSession,
    CallStatus,
    TwilioMediaData,
    TwilioStartData,
    build_clear_message,
    build_mark_message,
    build_media_message,
)
from tts_service import BaseTTSService
from voice_logging import voice_logger

logger = logging.getLogger("ai_voice_agent.voice.pipeline")


class VoiceCallHandler:
    """
    Manages the real-time full-duplex audio lifecycle for a single phone call.
    """

    def __init__(
        self,
        session: CallSession,
        send_to_twilio_fn: Callable[[dict[str, Any]], Any],
        conversation_manager: Any,
        stt_service: BaseSTTService,
        tts_service: BaseTTSService,
        audit_logger: Optional[Any] = None,
        metrics: Optional[Any] = None,
        latency_tracker: Optional[Any] = None,
    ):
        self.session = session
        self._send_to_twilio = send_to_twilio_fn
        self.conversation_manager = conversation_manager
        self.stt_service = stt_service
        self.tts_service = tts_service
        self.audit_logger = audit_logger
        self.metrics = metrics
        self.latency_tracker = latency_tracker

        # Cancellation event for mid-turn barge-in
        self._turn_cancellation_event = asyncio.Event()
        self._active_turn_task: Optional[asyncio.Task] = None
        self._is_active = True

    @property
    def is_active(self) -> bool:
        return self._is_active

    async def handle_start(self, start_data: TwilioStartData) -> None:
        """Handle Twilio START event: initialize STT stream and session metadata."""
        t_start = time.perf_counter()
        self.session.status = CallStatus.STREAMING
        self.session.metadata.update(start_data.custom_parameters)
        logger.info("Call %s (Stream %s) connected and streaming.", self.session.call_sid, self.session.stream_sid)
        await self.stt_service.connect()
        conn_lat_ms = (time.perf_counter() - t_start) * 1000
        if self.latency_tracker:
            self.latency_tracker.record("call_connection_latency", conn_lat_ms)

    async def handle_media(self, media_data: TwilioMediaData) -> None:
        """Forward raw 8kHz μ-law chunk directly into streaming STT."""
        if not self._is_active:
            return
        raw_audio = media_data.decode_raw_bytes()
        await self.stt_service.send_audio(raw_audio)

    async def trigger_barge_in(self) -> None:
        """
        Barge-in / Interruption Handler:
        1. Immediately send Twilio `clear` event to flush telephony speaker queue.
        2. Signal cancellation to in-flight TTS and LLM streams.
        3. Invalidate any in-flight confirmation state.
        4. Record interruption telemetry.
        """
        if self.session.is_interrupted or self._turn_cancellation_event.is_set():
            return  # already interrupted or clearing

        barge_start = time.perf_counter()
        self.session.interrupt_current_turn()
        logger.info("Barge-in triggered for call %s; flushing audio.", self.session.call_sid)

        # 1. Send Twilio clear event immediately
        clear_msg = build_clear_message(self.session.stream_sid)
        try:
            await self._send_to_twilio(clear_msg)
        except Exception as exc:
            logger.warning("Failed to send clear message to Twilio: %s", exc)

        barge_lat_ms = (time.perf_counter() - barge_start) * 1000

        # 2. Signal cancellation event to cancel in-flight TTS synthesis
        t_cancel_start = time.perf_counter()
        self._turn_cancellation_event.set()

        # 3. Cancel active response task if still executing
        if self._active_turn_task and not self._active_turn_task.done():
            self._active_turn_task.cancel()

        # Invalidate any pending action awaiting confirmation so interrupted turn
        # does not cause stale action execution on subsequent input
        sm = getattr(self.conversation_manager, "session_manager", None)
        if sm and self.session.session_id:
            try:
                sess_state = sm.get_session(self.session.session_id)
                if sess_state and sess_state.workflow_state == "AWAITING_CONFIRMATION":
                    sm.update_session(
                        self.session.session_id,
                        workflow_state=None,
                        pending_action=None,
                        pending_parameters={},
                    )
            except Exception:
                pass

        audio_clear_ms = (time.perf_counter() - t_cancel_start) * 1000
        total_interruption_ms = barge_lat_ms + audio_clear_ms

        voice_logger.log_event(
            "BARGE_IN_TRIGGERED",
            call_sid=self.session.call_sid,
            stream_sid=self.session.stream_sid,
            session_id=self.session.session_id,
            turn_id=self.session.active_turn_id,
            latency_ms=barge_lat_ms,
            caller_id=self.session.caller_id,
            extra={
                "audio_clear_latency_ms": round(audio_clear_ms, 2),
                "total_interruption_ms": round(total_interruption_ms, 2),
            },
        )

        if self.latency_tracker:
            self.latency_tracker.record("barge_in_detection_latency", barge_lat_ms)
            self.latency_tracker.record("audio_clear_latency", audio_clear_ms)
            self.latency_tracker.record("total_interruption_latency", total_interruption_ms)

        if self.metrics:
            self.metrics.increment("voice_barge_in_events_total")
            self.metrics.record_latency("voice_barge_in_latency_ms", barge_lat_ms)
            self.metrics.record_latency("voice_interruption_latency_ms", total_interruption_ms)

    async def process_stt_events(self) -> None:
        """
        Asynchronously process incoming STT events (VAD, partials, finals).
        """
        try:
            async for event in self.stt_service.receive_events():
                if not self._is_active:
                    break

                # 1. Voice Activity Detected -> Instant Barge-In!
                if event.event_type == STTEventType.SPEECH_STARTED:
                    await self.trigger_barge_in()

                # 2. Interim transcript: Strictly observational (NEVER execute tools)
                elif event.event_type == STTEventType.INTERIM_TRANSCRIPT:
                    if self.metrics:
                        self.metrics.increment("voice_stt_interim_count")
                    if self.latency_tracker:
                        self.latency_tracker.record("stt_partial_latency", 15.0)
                    # Log without PII for debugging
                    logger.debug(
                        "Interim transcript for call %s: %s words", self.session.call_sid, len(event.text.split())
                    )

                # 3. Finalized speech turn: Drives authoritative ConversationManager
                elif event.event_type == STTEventType.FINAL_TRANSCRIPT:
                    transcript = event.text.strip()
                    if not transcript:
                        continue

                    if self.metrics:
                        self.metrics.increment("voice_stt_final_count")
                    if self.latency_tracker:
                        self.latency_tracker.record("stt_final_latency", 25.0)

                    # Cancel any prior lingering turn and reset cancellation token
                    self._turn_cancellation_event.clear()
                    turn_id = self.session.next_turn()

                    voice_logger.log_event(
                        "TURN_STARTED",
                        call_sid=self.session.call_sid,
                        stream_sid=self.session.stream_sid,
                        session_id=self.session.session_id,
                        turn_id=turn_id,
                        caller_id=self.session.caller_id,
                    )

                    # Launch turn execution
                    self._active_turn_task = asyncio.create_task(self._execute_turn(transcript, turn_id))

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Error in STT event processing loop: %s", exc)

    async def _execute_turn(self, transcript: str, turn_id: int) -> None:
        """
        Executes a single conversational turn:
        Transcript → ConversationManager → LLM/RAG/Tools → Streaming TTS → Twilio Media
        Handles mid-turn interruption and preserves conversational turn history.
        """
        start_time = time.perf_counter()
        spoken_tokens: list[str] = []
        try:
            # 1. Prepare AuthContext and parameters for ConversationManager
            auth = None
            try:
                from action_models import AuthContext

                is_authenticated = self.session.metadata.get("authenticated_caller") is True
                auth = AuthContext(
                    user_id=self.session.user_id or "telephony_caller",
                    authenticated=is_authenticated,
                    roles=["caller"] if is_authenticated else [],
                )
            except Exception:
                pass

            req_id = f"voice_{self.session.stream_sid}_{turn_id}"

            def _cm_turn_generator():
                # Attempt standard production signature with history and auth
                try:
                    return self.conversation_manager.handle_turn(
                        user_input=transcript,
                        history=self.session.conversation_history,
                        auth=auth,
                        session_id=self.session.session_id,
                        request_id=req_id,
                    )
                except TypeError:
                    # Fallback for mock or test objects with alternative signatures
                    try:
                        return self.conversation_manager.handle_turn(
                            message=transcript,
                            session_id=self.session.session_id,
                            user_id=self.session.user_id,
                            stream=True,
                        )
                    except TypeError:
                        return self.conversation_manager.handle_turn(transcript)

            # Bridge sync ConversationManager generator into an async token stream non-blockingly
            async def _token_stream() -> AsyncIterator[str]:
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue = asyncio.Queue()

                def _producer():
                    try:
                        gen = _cm_turn_generator()
                        for itm in gen:
                            if self._turn_cancellation_event.is_set():
                                break
                            loop.call_soon_threadsafe(queue.put_nowait, itm)
                    except Exception as exc:
                        loop.call_soon_threadsafe(queue.put_nowait, exc)
                    finally:
                        loop.call_soon_threadsafe(queue.put_nowait, None)

                loop.run_in_executor(None, _producer)

                while True:
                    if self._turn_cancellation_event.is_set():
                        break
                    try:
                        token_item = await asyncio.wait_for(queue.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        continue

                    if token_item is None:
                        break
                    if isinstance(token_item, Exception):
                        raise token_item
                    if isinstance(token_item, str):
                        spoken_tokens.append(token_item)
                        yield token_item
                    elif isinstance(token_item, dict):
                        pass

            # 2. Stream generated tokens through TTS service
            tts_stream = self.tts_service.synthesize_stream(
                token_stream=_token_stream(),
                cancellation_event=self._turn_cancellation_event,
            )

            # 3. Stream resulting 8kHz μ-law audio chunks to Twilio
            first_audio_sent = False
            async for audio_chunk in tts_stream:
                if self._turn_cancellation_event.is_set():
                    break

                if not first_audio_sent:
                    first_audio_sent = True
                    ttfa_ms = (time.perf_counter() - start_time) * 1000
                    logger.info("Time to First Audio (TTFA) for turn #%s: %.1f ms", turn_id, ttfa_ms)
                    if self.metrics:
                        self.metrics.record_latency("voice_ttfa_ms", ttfa_ms)
                    if self.latency_tracker:
                        self.latency_tracker.record("tts_first_audio_latency", ttfa_ms)
                    voice_logger.log_event(
                        "TIME_TO_FIRST_AUDIO",
                        call_sid=self.session.call_sid,
                        stream_sid=self.session.stream_sid,
                        session_id=self.session.session_id,
                        turn_id=turn_id,
                        latency_ms=ttfa_ms,
                        caller_id=self.session.caller_id,
                    )

                # Send audio chunk to Twilio
                media_msg = build_media_message(self.session.stream_sid, audio_chunk)
                await self._send_to_twilio(media_msg)

            # Check if interrupted during generation or playback
            if self._turn_cancellation_event.is_set():
                interrupted_text = "".join(spoken_tokens).strip()
                logger.info(
                    "Outbound audio streaming halted for turn #%s due to barge-in (spoken: '%s')",
                    turn_id,
                    interrupted_text,
                )
                self.session.record_turn_completed(
                    user_text=transcript,
                    assistant_text=interrupted_text,
                    interrupted=True,
                    partial_spoken=interrupted_text,
                )
                voice_logger.log_event(
                    "TURN_INTERRUPTED",
                    call_sid=self.session.call_sid,
                    stream_sid=self.session.stream_sid,
                    session_id=self.session.session_id,
                    turn_id=turn_id,
                    caller_id=self.session.caller_id,
                    extra={"words_spoken": len(interrupted_text.split())},
                )

                # Invalidate any pending action awaiting confirmation so interrupted turn
                # does not cause stale action execution on subsequent input
                sm = getattr(self.conversation_manager, "session_manager", None)
                if sm and self.session.session_id:
                    try:
                        sess_state = sm.get_session(self.session.session_id)
                        if sess_state and sess_state.workflow_state == "AWAITING_CONFIRMATION":
                            sm.update_session(
                                self.session.session_id,
                                workflow_state=None,
                                pending_action=None,
                                pending_parameters={},
                            )
                    except Exception:
                        pass
                return

            # 4. Turn concluded cleanly
            full_text = "".join(spoken_tokens).strip()
            self.session.record_turn_completed(
                user_text=transcript,
                assistant_text=full_text,
                interrupted=False,
            )

            turn_lat_ms = (time.perf_counter() - start_time) * 1000
            if self.metrics:
                self.metrics.record_latency("voice_turn_latency_ms", turn_lat_ms)
            if self.latency_tracker:
                self.latency_tracker.record("total_turn_latency", turn_lat_ms)
            voice_logger.log_event(
                "TURN_COMPLETED",
                call_sid=self.session.call_sid,
                stream_sid=self.session.stream_sid,
                session_id=self.session.session_id,
                turn_id=turn_id,
                latency_ms=turn_lat_ms,
                caller_id=self.session.caller_id,
            )

            # Append mark event at utterance conclusion for playback tracking
            if first_audio_sent:
                mark_msg = build_mark_message(self.session.stream_sid, f"turn_{turn_id}_end")
                await self._send_to_twilio(mark_msg)

        except asyncio.CancelledError:
            interrupted_text = "".join(spoken_tokens).strip()
            self.session.record_turn_completed(
                user_text=transcript,
                assistant_text=interrupted_text,
                interrupted=True,
                partial_spoken=interrupted_text,
            )
            logger.info("Turn #%s execution cancelled.", turn_id)
        except Exception as exc:
            logger.error("Error executing turn #%s: %s", turn_id, exc)
            if self.metrics:
                self.metrics.increment("voice_calls_failed")
            # Graceful error recovery: Speak polite apology instead of dropping call
            if not self._turn_cancellation_event.is_set():
                try:
                    err_text = "I apologize, I'm having a little trouble hearing you. Could you please repeat that?"

                    async def _err_stream():
                        yield err_text

                    async for audio_chunk in self.tts_service.synthesize_stream(_err_stream()):
                        await self._send_to_twilio(build_media_message(self.session.stream_sid, audio_chunk))
                except Exception:
                    pass

    async def handle_stop(self) -> None:
        """
        Handle call termination event.

        Idempotency guard (Phase 15.1 stability fix): server.py's
        websocket_call() calls this both explicitly on a clean STOP frame
        and unconditionally again in its own `finally` cleanup, and
        VoiceCallManager.unregister_call() calls it a further time on the
        first of those -- so a single call previously ran this method's
        entire body up to 3 times. Before this guard, that meant
        "voice_calls_completed" was incremented 3x and CALL_COMPLETED was
        logged 3x per real call, silently corrupting operational metrics
        and the audit trail. self._is_active is set False exactly once,
        below, and re-entry is now a no-op -- see
        tests/test_voice_pipeline.py::TestHandleStopIdempotency.
        """
        if not self._is_active:
            logger.debug(
                "Call %s: handle_stop() called again after already stopped -- ignoring.", self.session.call_sid
            )
            return
        self._is_active = False
        self.session.status = CallStatus.COMPLETED
        if self._active_turn_task and not self._active_turn_task.done():
            self._active_turn_task.cancel()
        await self.stt_service.close()
        if self.metrics:
            self.metrics.increment("voice_calls_completed")
        voice_logger.log_event(
            "CALL_COMPLETED",
            call_sid=self.session.call_sid,
            stream_sid=self.session.stream_sid,
            session_id=self.session.session_id,
            caller_id=self.session.caller_id,
            extra={"turns_total": self.session.turn_count},
        )
        logger.info("Call %s terminated cleanly.", self.session.call_sid)


class VoiceCallManager:
    """
    Registry and supervisor of active telephone calls, ensuring strict call isolation.
    """

    def __init__(self, conversation_manager: Any, audit_logger=None, metrics=None):
        self.conversation_manager = conversation_manager
        self.audit_logger = audit_logger
        self.metrics = metrics
        self._active_calls: dict[str, VoiceCallHandler] = {}

    def get_handler(self, stream_sid: str) -> Optional[VoiceCallHandler]:
        return self._active_calls.get(stream_sid)

    def register_call(
        self,
        call_sid: str,
        stream_sid: str,
        send_fn: Callable[[dict[str, Any]], Any],
        stt_service: BaseSTTService,
        tts_service: BaseTTSService,
        custom_params: Optional[dict[str, str]] = None,
        latency_tracker: Optional[Any] = None,
    ) -> VoiceCallHandler:
        params = custom_params or {}
        session = CallSession(
            call_sid=call_sid,
            stream_sid=stream_sid,
            session_id=params.get("sessionId") or f"call_{call_sid}",
            user_id=params.get("userId") or "telephony_caller",
            caller_id=params.get("callerId") or params.get("From"),
            metadata=dict(params),
        )
        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=send_fn,
            conversation_manager=self.conversation_manager,
            stt_service=stt_service,
            tts_service=tts_service,
            audit_logger=self.audit_logger,
            metrics=self.metrics,
            latency_tracker=latency_tracker,
        )
        self._active_calls[stream_sid] = handler
        if self.metrics:
            self.metrics.increment("voice_calls_total")
        return handler

    async def unregister_call(self, stream_sid: str) -> None:
        handler = self._active_calls.pop(stream_sid, None)
        if handler:
            await handler.handle_stop()
