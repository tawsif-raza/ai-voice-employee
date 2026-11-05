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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from reliability_config import VoiceDeadlines, load_reliability_config
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

# Stability fix (Phase 16.2): bounds process_stt_events()'s reconnect
# loop below -- a fixed, small ceiling with a fixed backoff, checked
# against self._is_active on every iteration, so a permanently-broken
# STT dependency gives up deterministically instead of looping forever,
# and a call that ends normally (handle_stop() -> _is_active=False)
# is never mistaken for a connection to reconnect. Configurable via
# configs/reliability.yaml's `stt` section (Phase 1.2 Test 9) -- a
# missing/malformed file falls back to these exact values (see
# load_reliability_config()'s fail-safe-not-fail-closed contract), so
# this is not a behavior change, just an exposed knob.
_stt_reliability = load_reliability_config().stt
_MAX_STT_RECONNECT_ATTEMPTS = _stt_reliability.max_attempts
_STT_RECONNECT_BACKOFF_SECONDS = _stt_reliability.backoff_seconds

# H3: spoken (via the call's TTS) while a turn is still waiting for the
# first LLM text, and after a failed/abandoned turn. Never added to the
# conversation history. Service-initiated call endings are announced by
# Twilio itself (the TwiML after <Connect>, see server.py), so they work
# even when TTS is the failed component.
FILLER_TEXT = "One moment, please. "
TURN_FAILURE_TEXT = "I'm sorry, I'm having trouble right now. Could you please say that again?"


class TurnDeadlineExceeded(Exception):
    """A voice turn produced no text within first_token_timeout_seconds."""


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
        deadlines: Optional[VoiceDeadlines] = None,
        end_call_fn: Optional[Callable[[str], Awaitable[None]]] = None,
        executor: Optional[ThreadPoolExecutor] = None,
    ):
        self.session = session
        self._send_to_twilio = send_to_twilio_fn
        self.conversation_manager = conversation_manager
        self.stt_service = stt_service
        self.tts_service = tts_service
        self.audit_logger = audit_logger
        self.metrics = metrics
        self.latency_tracker = latency_tracker

        # H3: deadlines for this call's turns, the server-supplied way to end
        # the call, and the bounded worker pool turns run in (None = the
        # event loop's default executor, used by standalone handlers/tests).
        self._deadlines = deadlines or load_reliability_config().voice
        self._end_call_fn = end_call_fn
        self._executor = executor
        self._consecutive_turn_failures = 0
        self._ending = False

        # Cancellation token of the CURRENT turn (barge-in, superseding turn,
        # call end). Replaced for every new turn so cancelling one turn can
        # never be undone by the next one clearing a shared event.
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
        try:
            await asyncio.wait_for(self.stt_service.connect(), timeout=self._deadlines.stt_connect_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Without STT the call cannot work at all: end it so Twilio plays
            # the TwiML fallback message, instead of a silent open line.
            logger.error("STT connect failed for call %s: %s", self.session.call_sid, type(exc).__name__)
            if self.metrics:
                self.metrics.increment("voice_stt_connect_failures_total")
            await self.end_call("stt_connect_failed")
            return
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

        # Invalidate any pending action awaiting confirmation OR
        # authentication so an interrupted turn does not cause stale
        # action execution, or a barged-in unrelated utterance being
        # mis-parsed as a 4-digit PIN attempt, on subsequent input.
        # Phase 20 fix: this previously only cleared AWAITING_CONFIRMATION
        # -- AWAITING_AUTHENTICATION was left stuck across a barge-in.
        sm = getattr(self.conversation_manager, "session_manager", None)
        if sm and self.session.session_id:
            try:
                sess_state = sm.get_session(self.session.session_id)
                if sess_state and sess_state.workflow_state in ("AWAITING_CONFIRMATION", "AWAITING_AUTHENTICATION"):
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

    async def _supersede_active_turn(self) -> None:
        task = self._active_turn_task
        if task is None or task.done():
            return
        self._turn_cancellation_event.set()
        task.cancel()
        try:
            await self._send_to_twilio(build_clear_message(self.session.stream_sid))
        except Exception as exc:
            logger.warning("Failed to send clear message to Twilio: %s", exc)
        if self.metrics:
            self.metrics.increment("voice_turns_superseded_total")

    async def process_stt_events(self) -> None:
        """
        Asynchronously process incoming STT events (VAD, partials, finals).

        Bounded reconnect (Phase 16.2 stability fix): before this fix,
        once the STT stream ended for any reason other than this call's
        own handle_stop() (e.g. the Deepgram connection dropping), this
        loop simply returned -- silently "deafening" the call for its
        remaining duration with no attempt to recover and no signal to
        the caller. Now, an unexpected end (an ERROR event, an
        exception, or the stream just ending) triggers up to
        _MAX_STT_RECONNECT_ATTEMPTS reconnect attempts with a fixed
        backoff, all within this SAME task/coroutine -- never spawning
        an additional task per attempt, so there is no risk of task
        explosion regardless of how many times the stream drops.
        self._is_active is checked before every attempt, so a call that
        ends normally (handle_stop() already ran) is never mistaken for
        one that needs reconnecting, and cancellation (asyncio
        CancelledError) always exits immediately without ever
        attempting to reconnect.
        """
        reconnect_attempts = 0
        while self._is_active and not self._ending:
            try:
                async for event in self.stt_service.receive_events():
                    if not self._is_active:
                        return

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
                            "Interim transcript for call %s: %s words",
                            self.session.call_sid,
                            len(event.text.split()),
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

                        # A newer utterance supersedes any turn still running:
                        # cancel it (its own token + task) and give the new turn
                        # a fresh token, so two turns never speak at once.
                        await self._supersede_active_turn()
                        self._turn_cancellation_event = asyncio.Event()
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
                        self._active_turn_task = asyncio.create_task(
                            self._execute_turn(transcript, turn_id, self._turn_cancellation_event)
                        )

                    # 4. STT-reported error: log and fall through to the
                    # reconnect logic below rather than continuing to
                    # iterate a stream that has already reported itself
                    # broken.
                    elif event.event_type == STTEventType.ERROR:
                        logger.warning(
                            "STT stream reported an error for call %s: %s", self.session.call_sid, event.text
                        )
                        break

            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Error in STT event processing loop for call %s: %s", self.session.call_sid, exc)

            if not self._is_active:
                return  # call ended normally (handle_stop already ran) -- never reconnect

            reconnect_attempts += 1
            if reconnect_attempts > _MAX_STT_RECONNECT_ATTEMPTS:
                logger.error(
                    "STT stream for call %s failed to reconnect after %s attempts -- giving up.",
                    self.session.call_sid,
                    _MAX_STT_RECONNECT_ATTEMPTS,
                )
                if self.metrics:
                    self.metrics.increment("voice_stt_reconnect_exhausted_total")
                # The call can no longer hear the caller: end it (Twilio then
                # plays the TwiML fallback) rather than leave a deaf open line
                # until MAX_CALL_DURATION_SECONDS.
                await self.end_call("stt_unavailable")
                return

            logger.warning(
                "STT stream dropped for call %s -- reconnect attempt %s/%s in %ss.",
                self.session.call_sid,
                reconnect_attempts,
                _MAX_STT_RECONNECT_ATTEMPTS,
                _STT_RECONNECT_BACKOFF_SECONDS,
            )
            if self.metrics:
                self.metrics.increment("voice_stt_reconnect_attempts_total")
            await asyncio.sleep(_STT_RECONNECT_BACKOFF_SECONDS)

            try:
                await self.stt_service.close()
            except Exception:
                pass
            try:
                await self.stt_service.connect()
            except Exception as exc:
                logger.error(
                    "STT reconnect attempt %s failed for call %s: %s", reconnect_attempts, self.session.call_sid, exc
                )
                # Loop back to the top: self._is_active and the attempt
                # count are checked again before trying anything further.

    async def _execute_turn(self, transcript: str, turn_id: int, cancel_event: Optional[asyncio.Event] = None) -> None:
        """
        Executes a single conversational turn:
        Transcript → ConversationManager → LLM/RAG/Tools → Streaming TTS → Twilio Media
        Handles mid-turn interruption and preserves conversational turn history.

        H3 deadlines (configs/reliability.yaml `voice`): a holding phrase if
        no text has arrived by `filler_after_seconds`; the turn is abandoned
        if no text by `first_token_timeout_seconds` or if the whole turn
        exceeds `turn_timeout_seconds`. An abandoned, failed, or empty turn
        is answered with a short apology (itself bounded by
        `fallback_speech_timeout_seconds`); after
        `max_consecutive_turn_failures` such turns in a row the call is
        ended. `cancel_event` is this turn's own cancellation token (barge-in,
        a newer turn, call end); it defaults to the handler's current one.
        """
        cancel_event = cancel_event or self._turn_cancellation_event
        deadlines = self._deadlines
        start_time = time.perf_counter()
        spoken_tokens: list[str] = []
        failure: Optional[str] = None
        try:
            # 1. Prepare AuthContext and parameters for ConversationManager
            auth = None
            try:
                from action_models import AuthContext
                from identity import Role, permissions_for_roles

                # Phase 20 fixes (both found reviewing this same telephony
                # auth path; same root cause class as PHASE_18's F-04):
                #
                # 1. This used to read self.session.metadata (the local
                #    CallSession object, set once from Twilio's start
                #    frame custom_parameters at connection time -- see
                #    on_start() above -- and never updated again). The
                #    real "authenticated_caller" flag is written by
                #    ConversationManager._execute_pending_authentication()
                #    onto the SessionManager-persisted Session, a
                #    different object. Reading the wrong one meant this
                #    check was always False for a real caller, making the
                #    entire PIN-authentication feature silently
                #    non-functional end-to-end regardless of what PIN was
                #    spoken or whether it matched.
                # 2. roles=["caller"] has no entry in identity.py's
                #    ROLE_PERMISSIONS table, and `permissions` was never
                #    set either -- AuthContext.has_permission() never
                #    derives permissions from roles, so even a correctly-
                #    detected authenticated turn would still fail every
                #    real permission check. Grant the same least-privilege
                #    Role.USER set an ordinary authenticated user gets,
                #    matching ConversationManager's own
                #    _execute_pending_authentication() fix.
                is_authenticated = False
                _sm = getattr(self.conversation_manager, "session_manager", None)
                if _sm is not None and self.session.session_id:
                    _persisted = _sm.get_session(self.session.session_id)
                    if _persisted is not None:
                        is_authenticated = _persisted.metadata.get("authenticated_caller") is True
                auth = AuthContext(
                    user_id=self.session.user_id or "telephony_caller",
                    authenticated=is_authenticated,
                    roles=(Role.USER.value,) if is_authenticated else (),
                    permissions=permissions_for_roles((Role.USER,)) if is_authenticated else (),
                    authentication_method="telephony_pin" if is_authenticated else "none",
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

            # Bridge the sync ConversationManager generator (run in a worker
            # thread) into an async token stream, enforcing the filler and
            # first-token deadlines on the event-loop side. The worker can't
            # be killed, so it is told to stop (cancel_event / producer_stop)
            # and closes the generator itself -- which releases
            # ConversationManager's generation semaphore -- as soon as its
            # current provider call returns (provider timeouts bound that).
            producer_stop = threading.Event()

            async def _token_stream() -> AsyncIterator[str]:
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue = asyncio.Queue()

                def _put(item) -> None:
                    try:
                        loop.call_soon_threadsafe(queue.put_nowait, item)
                    except RuntimeError:
                        pass  # event loop already closed (process shutting down)

                def _producer():
                    gen = None
                    try:
                        gen = _cm_turn_generator()
                        for itm in gen:
                            if cancel_event.is_set() or producer_stop.is_set():
                                break
                            _put(itm)
                    except Exception as exc:
                        _put(exc)
                    finally:
                        close = getattr(gen, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:
                                pass
                        _put(None)

                loop.run_in_executor(self._executor, _producer)
                waited_from = time.monotonic()
                got_text = False
                filler_sent = False
                try:
                    while True:
                        if cancel_event.is_set():
                            break
                        if not got_text:
                            waited = time.monotonic() - waited_from
                            if waited >= deadlines.first_token_timeout_seconds:
                                raise TurnDeadlineExceeded("first_token")
                            if (
                                not filler_sent
                                and deadlines.filler_after_seconds > 0
                                and waited >= deadlines.filler_after_seconds
                            ):
                                filler_sent = True
                                if self.metrics:
                                    self.metrics.increment("voice_turn_fillers_total")
                                # Spoken, but never recorded as the assistant's answer.
                                yield FILLER_TEXT
                        try:
                            token_item = await asyncio.wait_for(queue.get(), timeout=0.05)
                        except asyncio.TimeoutError:
                            continue

                        if token_item is None:
                            break
                        if isinstance(token_item, Exception):
                            raise token_item
                        if isinstance(token_item, str) and token_item:
                            got_text = True
                            spoken_tokens.append(token_item)
                            yield token_item
                finally:
                    producer_stop.set()

            # 2 + 3. Stream tokens through TTS and the audio to Twilio, the
            # whole turn bounded by turn_timeout_seconds.
            first_audio_sent = False
            try:
                async with asyncio.timeout(deadlines.turn_timeout_seconds):
                    tts_stream = self.tts_service.synthesize_stream(
                        token_stream=_token_stream(),
                        cancellation_event=cancel_event,
                    )
                    async for audio_chunk in tts_stream:
                        if cancel_event.is_set():
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
            except TimeoutError:
                # asyncio.timeout() expiry (whole-turn deadline).
                failure = "turn_timeout"
                producer_stop.set()
            except TurnDeadlineExceeded as exc:
                failure = str(exc)
                producer_stop.set()

            # Check if interrupted during generation or playback
            if failure is None and cancel_event.is_set():
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

            full_text = "".join(spoken_tokens).strip()
            if failure is None and not full_text:
                # The conversation layer produced nothing to say (e.g. an
                # empty provider reply): never leave the caller in silence.
                failure = "empty_response"

            if failure is not None:
                await self._handle_turn_failure(transcript, turn_id, failure, full_text)
                return

            # 4. Turn concluded cleanly
            self._consecutive_turn_failures = 0
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
            logger.error("Error executing turn #%s: %s", turn_id, type(exc).__name__)
            if self.metrics:
                self.metrics.increment("voice_calls_failed")
            if not cancel_event.is_set():
                await self._handle_turn_failure(transcript, turn_id, "error", "".join(spoken_tokens).strip())

    async def _handle_turn_failure(self, transcript: str, turn_id: int, reason: str, partial_text: str) -> None:
        """
        A turn that timed out, failed, or produced nothing: record it, then
        either apologise (bounded) and keep listening, or -- after
        max_consecutive_turn_failures in a row -- end the call so Twilio
        plays the TwiML fallback message instead of looping on a broken
        provider.
        """
        self._consecutive_turn_failures += 1
        logger.warning(
            "Turn #%s for call %s failed (%s); consecutive failures: %s.",
            turn_id,
            self.session.call_sid,
            reason,
            self._consecutive_turn_failures,
        )
        if self.metrics:
            self.metrics.increment("voice_turn_failures_total")
            if reason in ("first_token", "turn_timeout"):
                self.metrics.increment("voice_turn_deadline_exceeded_total")
        voice_logger.log_event(
            "TURN_FAILED",
            call_sid=self.session.call_sid,
            stream_sid=self.session.stream_sid,
            session_id=self.session.session_id,
            turn_id=turn_id,
            caller_id=self.session.caller_id,
            extra={"reason": reason, "consecutive_failures": self._consecutive_turn_failures},
        )
        if self._consecutive_turn_failures >= self._deadlines.max_consecutive_turn_failures:
            self.session.record_turn_completed(user_text=transcript, assistant_text=partial_text)
            await self.end_call("repeated_turn_failures")
            return
        spoken = await self.speak_bounded(TURN_FAILURE_TEXT)
        # History records what the caller actually heard (any partial answer,
        # then the apology) -- not a caller interruption, which this wasn't.
        heard = " ".join(part for part in (partial_text, TURN_FAILURE_TEXT if spoken else "") if part)
        self.session.record_turn_completed(user_text=transcript, assistant_text=heard)
        if not spoken:
            # The caller cannot hear us at all (TTS down); don't keep them
            # talking to silence for more turns.
            await self.end_call("fallback_speech_failed")

    async def speak_bounded(self, text: str) -> bool:
        """
        Speaks `text` with the call's TTS, bounded by
        fallback_speech_timeout_seconds -- the TTS provider may be the very
        component that failed. Returns False if it could not be spoken.
        """

        async def _one() -> AsyncIterator[str]:
            yield text

        async def _run() -> None:
            async for audio_chunk in self.tts_service.synthesize_stream(_one()):
                await self._send_to_twilio(build_media_message(self.session.stream_sid, audio_chunk))

        try:
            await asyncio.wait_for(_run(), timeout=self._deadlines.fallback_speech_timeout_seconds)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Could not speak fallback message on call %s: %s", self.session.call_sid, type(exc).__name__)
            if self.metrics:
                self.metrics.increment("voice_fallback_speech_failures_total")
            return False

    async def end_call(self, reason: str) -> None:
        """
        Ends the call from the service side (provider unavailable, repeated
        failures). Idempotent. With an `end_call_fn` (the server's: closes
        the media-stream WebSocket), Twilio then continues with the TwiML
        after <Connect> -- a provider-independent goodbye message -- and the
        server's normal cleanup runs; without one, the handler stops itself.
        """
        if self._ending:
            return
        self._ending = True
        logger.warning("Ending call %s from the service side: %s.", self.session.call_sid, reason)
        if self.metrics:
            self.metrics.increment("voice_calls_ended_by_service_total")
        voice_logger.log_event(
            "CALL_ENDED_BY_SERVICE",
            call_sid=self.session.call_sid,
            stream_sid=self.session.stream_sid,
            session_id=self.session.session_id,
            caller_id=self.session.caller_id,
            extra={"reason": reason},
        )
        self._turn_cancellation_event.set()
        if self._end_call_fn is not None:
            try:
                await self._end_call_fn(reason)
            except Exception as exc:
                logger.warning("end_call callback failed for call %s: %s", self.session.call_sid, type(exc).__name__)
        else:
            await self.handle_stop()

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
        self._turn_cancellation_event.set()
        # Never cancel the task we are running in (a turn ending its own call).
        if (
            self._active_turn_task
            and not self._active_turn_task.done()
            and self._active_turn_task is not asyncio.current_task()
        ):
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

    def __init__(
        self,
        conversation_manager: Any,
        audit_logger=None,
        metrics=None,
        turn_workers: Optional[int] = None,
        deadlines: Optional[VoiceDeadlines] = None,
    ):
        self.conversation_manager = conversation_manager
        self.audit_logger = audit_logger
        self.metrics = metrics
        self._active_calls: dict[str, VoiceCallHandler] = {}
        self._deadlines = deadlines
        # H3: voice turns run in their own bounded pool, never the default
        # executor shared with /jobs/generate, so batch work cannot starve
        # live calls. A turn that is waiting for a free worker is still
        # bounded by its first-token deadline. None = default executor.
        self._executor: Optional[ThreadPoolExecutor] = (
            ThreadPoolExecutor(max_workers=turn_workers, thread_name_prefix="voice-turn") if turn_workers else None
        )

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

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
        end_call_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> VoiceCallHandler:
        params = custom_params or {}
        session = CallSession(
            call_sid=call_sid,
            stream_sid=stream_sid,
            session_id=params.get("sessionId") or f"call_{call_sid}",
            # Per-call identity (H2, F-09): one shared "telephony_caller"
            # id made every caller the owner of every other caller's
            # bookings and memory.
            user_id=params.get("userId") or f"telephony:{call_sid}",
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
            deadlines=self._deadlines,
            end_call_fn=end_call_fn,
            executor=self._executor,
        )

        previous = self._active_calls.get(stream_sid)
        if previous is not None:
            # Stability fix (Phase 16.3): a duplicate/replayed START
            # frame for a stream_sid that already had a handler used to
            # silently overwrite it here, leaking the previous
            # handler's STT connection and any in-flight turn task --
            # neither was ever closed/cancelled. register_call() is
            # sync (server.py's websocket_call() calls it without
            # awaiting) while handle_stop() is async, so the cleanup
            # runs as its own short-lived task -- exactly one per
            # duplicate START, never a retry loop, so repeated
            # duplicates cannot accumulate or explode: each spawns
            # exactly one self-terminating cleanup task and this method
            # returns immediately either way.
            logger.warning(
                "Duplicate START for stream_sid=%s (new call_sid=%s) -- cleaning up the previous "
                "handler (call_sid=%s) before registering the new one.",
                stream_sid,
                call_sid,
                previous.session.call_sid,
            )
            asyncio.create_task(previous.handle_stop())

        self._active_calls[stream_sid] = handler
        if self.metrics:
            self.metrics.increment("voice_calls_total")
        return handler

    async def unregister_call(self, stream_sid: str) -> None:
        handler = self._active_calls.pop(stream_sid, None)
        if handler:
            await handler.handle_stop()
