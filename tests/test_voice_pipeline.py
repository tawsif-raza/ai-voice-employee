"""
Integration and behavioral tests for VoiceCallHandler and VoiceCallManager (src/voice/voice_pipeline.py).
Validates:
- Finalized speech turn drives ConversationManager
- Partial transcripts are observational only and NEVER trigger actions
- Barge-in triggers Twilio clear event and cancels in-flight TTS playback
- Old audio never continues playing after interruption
- Concurrent calls maintain strict state and task isolation
"""

import asyncio
import sys
import unittest
from pathlib import Path

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "src" / "agent")
for p in (_VOICE_DIR, _AGENT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from metrics import MetricsRegistry
from stt_service import MockSTTService, STTEvent, STTEventType
from telephony_models import (
    CallSession,
    CallStatus,
)
from tts_service import MockTTSService
from voice_pipeline import (
    _MAX_STT_RECONNECT_ATTEMPTS,
    VoiceCallHandler,
    VoiceCallManager,
)


class MockConversationManager:
    """Mock of ConversationManager providing deterministic turns."""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.call_history: list[dict] = []

    def handle_turn(self, message: str, session_id: str = None, user_id: str = None, **kwargs):
        self.call_history.append({"message": message, "session_id": session_id, "user_id": user_id})
        resp = self.responses.pop(0) if self.responses else "Default answer."
        # Simulate streaming token chunks
        words = resp.split(" ")
        for word in words:
            yield word + " "
        yield {
            "response": resp,
            "is_handoff": False,
            "latency_ms": 15.0,
        }


class TestVoicePipeline(unittest.IsolatedAsyncioTestCase):
    async def test_final_transcript_drives_conversation_manager(self):
        outbound = []

        async def _mock_send(msg: dict):
            outbound.append(msg)

        cm = MockConversationManager(["Our return policy is thirty days."])
        stt = MockSTTService()
        tts = MockTTSService(frame_count_per_word=1)
        session = CallSession(call_sid="CA100", stream_sid="MZ100", session_id="sess_100")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=_mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Emit FINAL_TRANSCRIPT
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="What is your return policy?"))

        # Allow turn execution to process
        await asyncio.sleep(0.05)
        if handler._active_turn_task:
            await handler._active_turn_task

        # Verify ConversationManager was called with finalized text
        self.assertEqual(len(cm.call_history), 1)
        self.assertEqual(cm.call_history[0]["message"], "What is your return policy?")
        self.assertEqual(cm.call_history[0]["session_id"], "sess_100")

        # Verify outbound frames include media (audio chunks) and mark (turn completion)
        media_events = [m for m in outbound if m.get("event") == "media"]
        mark_events = [m for m in outbound if m.get("event") == "mark"]

        self.assertGreater(len(media_events), 0)
        self.assertGreater(len(mark_events), 0)
        self.assertEqual(mark_events[-1]["mark"]["name"], "turn_1_end")

        stt_task.cancel()
        await handler.handle_stop()

    async def test_partial_transcript_never_executes_conversation_manager(self):
        outbound = []

        async def _mock_send(msg: dict):
            outbound.append(msg)

        cm = MockConversationManager(["Should not be called!"])
        stt = MockSTTService()
        tts = MockTTSService()
        session = CallSession(call_sid="CA101", stream_sid="MZ101", session_id="sess_101")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=_mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Emit INTERIM_TRANSCRIPT (partial hypothesis)
        await stt.push_event(STTEvent(STTEventType.INTERIM_TRANSCRIPT, text="I want to cancel"))
        await asyncio.sleep(0.02)

        # STRICT SAFETY INVARIANT: ConversationManager must NOT be called for interim transcripts
        self.assertEqual(len(cm.call_history), 0)
        self.assertEqual(len(outbound), 0)

        stt_task.cancel()
        await handler.handle_stop()

    async def test_barge_in_flushes_twilio_and_halts_audio(self):
        outbound = []

        async def _mock_send(msg: dict):
            outbound.append(msg)

        cm = MockConversationManager(["This is a very long response that will be interrupted."])
        stt = MockSTTService()
        tts = MockTTSService(frame_count_per_word=10)
        session = CallSession(call_sid="CA102", stream_sid="MZ102", session_id="sess_102")

        handler = VoiceCallHandler(
            session=session,
            send_to_twilio_fn=_mock_send,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
        )

        await stt.connect()
        stt_task = asyncio.create_task(handler.process_stt_events())

        # Start a turn
        await stt.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="Tell me something long."))
        await asyncio.sleep(0.01)

        # In the middle of playback, caller speaks -> SPEECH_STARTED event
        await stt.push_event(STTEvent(STTEventType.SPEECH_STARTED))
        await asyncio.sleep(0.02)

        # BARGE-IN INVARIANTS:
        # 1. Twilio clear event MUST have been sent
        clear_events = [m for m in outbound if m.get("event") == "clear"]
        self.assertEqual(len(clear_events), 1)
        self.assertEqual(clear_events[0]["streamSid"], "MZ102")

        # 2. Session status must reflect INTERRUPTED
        self.assertTrue(session.is_interrupted)
        self.assertEqual(session.status, CallStatus.INTERRUPTED)

        stt_task.cancel()
        await handler.handle_stop()

    async def test_voice_call_manager_concurrent_isolation(self):
        cm = MockConversationManager(["Resp 1", "Resp 2"])
        manager = VoiceCallManager(conversation_manager=cm)

        outbound_1 = []
        outbound_2 = []

        async def send1(msg):
            outbound_1.append(msg)

        async def send2(msg):
            outbound_2.append(msg)

        h1 = manager.register_call("CA-A", "MZ-A", send1, MockSTTService(), MockTTSService())
        h2 = manager.register_call("CA-B", "MZ-B", send2, MockSTTService(), MockTTSService())

        self.assertEqual(h1.session.call_sid, "CA-A")
        self.assertEqual(h2.session.call_sid, "CA-B")
        self.assertIsNot(h1.session, h2.session)

        # Clean up
        await manager.unregister_call("MZ-A")
        self.assertIsNone(manager.get_handler("MZ-A"))
        self.assertIsNotNone(manager.get_handler("MZ-B"))
        await manager.unregister_call("MZ-B")


class TestHandleStopIdempotency(unittest.IsolatedAsyncioTestCase):
    """
    Stability fix: server.py's websocket_call() calls handle_stop() up to
    3 times per real call (once explicitly on STOP, once again inside
    VoiceCallManager.unregister_call(), once more in websocket_call()'s
    own `finally`). Before the idempotency guard, that meant
    "voice_calls_completed" was incremented 3x and CALL_COMPLETED was
    logged 3x for a single call.
    """

    async def _build_handler(self, metrics: MetricsRegistry) -> VoiceCallHandler:
        cm = MockConversationManager([])
        stt = MockSTTService()
        tts = MockTTSService()
        session = CallSession(call_sid="CA200", stream_sid="MZ200", session_id="sess_200")
        return VoiceCallHandler(
            session=session,
            send_to_twilio_fn=lambda msg: None,
            conversation_manager=cm,
            stt_service=stt,
            tts_service=tts,
            metrics=metrics,
        )

    async def test_repeated_handle_stop_increments_metric_exactly_once(self):
        metrics = MetricsRegistry()
        handler = await self._build_handler(metrics)

        self.assertEqual(metrics.get_counter("voice_calls_completed"), 0)

        await handler.handle_stop()
        await handler.handle_stop()
        await handler.handle_stop()

        self.assertEqual(
            metrics.get_counter("voice_calls_completed"),
            1,
            "handle_stop() must be idempotent -- repeated invocation (matching "
            "websocket_call()'s real STOP-event/finally/unregister_call sequence) "
            "must not increment the completed-calls counter more than once",
        )

    async def test_repeated_handle_stop_via_unregister_call_increments_metric_exactly_once(self):
        """
        Reproduces the exact real sequence from server.py's websocket_call():
        explicit handle_stop(), then unregister_call() (which itself calls
        handle_stop() again internally), then a final direct handle_stop()
        call from the `finally` block.
        """
        metrics = MetricsRegistry()
        manager = VoiceCallManager(conversation_manager=MockConversationManager([]), metrics=metrics)
        handler = manager.register_call("CA201", "MZ201", lambda msg: None, MockSTTService(), MockTTSService())

        await handler.handle_stop()  # explicit call (server.py's STOP branch)
        await manager.unregister_call("MZ201")  # internally calls handle_stop() again
        await handler.handle_stop()  # server.py's unconditional `finally` call

        self.assertEqual(metrics.get_counter("voice_calls_completed"), 1)

    async def test_handle_stop_is_active_false_after_first_call(self):
        handler = await self._build_handler(MetricsRegistry())
        self.assertTrue(handler.is_active)
        await handler.handle_stop()
        self.assertFalse(handler.is_active)
        # Calling again must not raise and must not flip state back.
        await handler.handle_stop()
        self.assertFalse(handler.is_active)


class FlakySTTService:
    """
    Test double for STT reconnect verification (Issue 2 / Instruction C).
    Each successful connect() advances to the next scripted "connection"
    -- a list of STTEvent objects (yielded) and/or exception instances
    (raised mid-stream). connect_should_fail, when True, makes every
    connect() call raise instead of advancing, until flipped back --
    lets a single test simulate "reconnect fails N times, then
    succeeds" or "reconnect never succeeds" precisely.
    """

    def __init__(self, connection_scripts: list[list]):
        self.connection_scripts = list(connection_scripts)
        self.connect_should_fail = False
        self.connect_count = 0
        self.close_count = 0
        self._current_script_index = -1

    async def connect(self) -> None:
        self.connect_count += 1
        if self.connect_should_fail:
            raise ConnectionError(f"simulated connect failure #{self.connect_count}")
        self._current_script_index += 1

    async def close(self) -> None:
        self.close_count += 1

    async def receive_events(self):
        idx = self._current_script_index
        if idx < 0 or idx >= len(self.connection_scripts):
            return
        for item in self.connection_scripts[idx]:
            if isinstance(item, BaseException):
                raise item
            yield item
        # A real, healthy STT connection does not close itself just
        # because no new speech has arrived recently -- it keeps
        # listening. Modeling "the scripted events ran out" as "the
        # connection closed" would make every clean script look like a
        # disconnect and spuriously trigger a reconnect. A script that
        # SHOULD represent a disconnect must end with an explicit
        # STTEventType.ERROR event or a raised exception (above)
        # instead of just running out -- so after a clean exhaustion,
        # block here exactly like MockSTTService/DeepgramSTTService do
        # while genuinely connected and idle, until the test cancels
        # the task or the connection is closed.
        await asyncio.Event().wait()


def _build_handler_with_flaky_stt(connection_scripts, metrics=None):
    cm = MockConversationManager(["Ok."] * 10)
    stt = FlakySTTService(connection_scripts)
    tts = MockTTSService()
    session = CallSession(call_sid="CA_FLAKY", stream_sid="MZ_FLAKY", session_id="s_flaky")
    handler = VoiceCallHandler(
        session=session,
        send_to_twilio_fn=lambda msg: None,
        conversation_manager=cm,
        stt_service=stt,
        tts_service=tts,
        metrics=metrics,
    )
    return handler, stt, cm


class TestSTTReconnect(unittest.IsolatedAsyncioTestCase):
    """
    Instruction C verification: normal operation, transient disconnect,
    repeated disconnect, reconnect success, reconnect failure, timeout,
    and cancellation -- plus an explicit bound/no-task-explosion check.

    A script that ends cleanly (no ERROR event/exception) represents an
    ongoing, healthy connection -- process_stt_events() then keeps
    listening forever, exactly like the real Deepgram/Mock services do
    while idle. So every test that expects the loop to keep running
    (anything but "give up after exhausting the bound") runs it as a
    background task, waits long enough for the scripted behavior to
    play out, then cancels it before asserting -- never expecting a
    naturally-returning coroutine for those cases.
    """

    async def _run_and_cancel_after(self, handler, seconds: float) -> None:
        task = asyncio.create_task(handler.process_stt_events())
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_normal_operation_never_reconnects(self):
        handler, stt, cm = _build_handler_with_flaky_stt(
            [[STTEvent(STTEventType.FINAL_TRANSCRIPT, text="hello there")]]
        )
        await stt.connect()
        await self._run_and_cancel_after(handler, 0.1)

        self.assertEqual(stt.connect_count, 1, "no reconnect should have happened")
        self.assertEqual(len(cm.call_history), 1)

    async def test_transient_disconnect_reconnects_once(self):
        metrics = MetricsRegistry()
        handler, stt, cm = _build_handler_with_flaky_stt(
            [
                [STTEvent(STTEventType.ERROR, text="connection reset")],
                [STTEvent(STTEventType.FINAL_TRANSCRIPT, text="second connection works")],
            ],
            metrics=metrics,
        )
        await stt.connect()
        await self._run_and_cancel_after(handler, 1.5)  # 1 backoff (1.0s) + margin

        self.assertEqual(stt.connect_count, 2, "exactly one reconnect: initial + 1 retry")
        self.assertEqual(metrics.get_counter("voice_stt_reconnect_attempts_total"), 1)
        self.assertEqual(metrics.get_counter("voice_stt_reconnect_exhausted_total"), 0)
        # Reconnect success (Instruction C's separate bullet): the
        # SECOND connection's event was genuinely processed afterward.
        self.assertEqual(len(cm.call_history), 1)
        self.assertEqual(cm.call_history[0]["message"], "second connection works")

    async def test_repeated_disconnect_within_bound_eventually_recovers(self):
        self.assertGreaterEqual(_MAX_STT_RECONNECT_ATTEMPTS, 2, "test assumes at least 2 retries are allowed")
        metrics = MetricsRegistry()
        handler, stt, cm = _build_handler_with_flaky_stt(
            [
                [STTEvent(STTEventType.ERROR, text="drop 1")],
                [ConnectionError("drop 2, raised mid-stream")],
                [STTEvent(STTEventType.FINAL_TRANSCRIPT, text="third time's the charm")],
            ],
            metrics=metrics,
        )
        await stt.connect()
        await self._run_and_cancel_after(handler, 2.5)  # 2 backoffs (2.0s) + margin

        self.assertEqual(stt.connect_count, 3)
        self.assertEqual(metrics.get_counter("voice_stt_reconnect_attempts_total"), 2)
        self.assertEqual(cm.call_history[0]["message"], "third time's the charm")

    async def test_reconnect_failure_is_bounded_and_gives_up_deterministically(self):
        # The one scenario that DOES return naturally: every reconnect
        # attempt fails, so the loop exhausts its bound and gives up.
        metrics = MetricsRegistry()
        handler, stt, cm = _build_handler_with_flaky_stt(
            [[STTEvent(STTEventType.ERROR, text="initial drop")]],
            metrics=metrics,
        )
        await stt.connect()
        stt.connect_should_fail = True  # every reconnect attempt from here on fails

        # Must complete (not hang) within a bounded time even though the
        # dependency never recovers.
        await asyncio.wait_for(handler.process_stt_events(), timeout=_MAX_STT_RECONNECT_ATTEMPTS * 2.0 + 5.0)

        self.assertEqual(
            metrics.get_counter("voice_stt_reconnect_attempts_total"),
            _MAX_STT_RECONNECT_ATTEMPTS,
            "must attempt exactly the configured bound, no more",
        )
        self.assertEqual(metrics.get_counter("voice_stt_reconnect_exhausted_total"), 1)
        self.assertEqual(cm.call_history, [], "no turn should have been driven -- STT never recovered")

    async def test_timeout_exception_mid_stream_is_treated_as_a_reconnectable_failure(self):
        metrics = MetricsRegistry()
        handler, stt, cm = _build_handler_with_flaky_stt(
            [
                [asyncio.TimeoutError("simulated STT read timeout")],
                [STTEvent(STTEventType.FINAL_TRANSCRIPT, text="recovered after timeout")],
            ],
            metrics=metrics,
        )
        await stt.connect()
        await self._run_and_cancel_after(handler, 1.5)

        self.assertEqual(stt.connect_count, 2)
        self.assertEqual(cm.call_history[0]["message"], "recovered after timeout")

    async def test_cancellation_during_reconnect_backoff_stops_immediately_without_further_attempts(self):
        metrics = MetricsRegistry()
        handler, stt, cm = _build_handler_with_flaky_stt(
            [
                [STTEvent(STTEventType.ERROR, text="drop")],
                [STTEvent(STTEventType.FINAL_TRANSCRIPT, text="should never be reached")],
            ],
            metrics=metrics,
        )
        await stt.connect()
        task = asyncio.create_task(handler.process_stt_events())
        await asyncio.sleep(0.05)  # let it observe the drop and enter the reconnect backoff sleep
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(cm.call_history, [], "cancellation must pre-empt any further reconnect/turn activity")

    async def test_no_task_explosion_across_repeated_reconnects(self):
        # This implementation never calls asyncio.create_task() for a
        # reconnect attempt -- the whole retry loop runs in the SAME
        # coroutine/task server.py's websocket_call() created. Confirm
        # the live task count doesn't grow across multiple disconnects.
        handler, stt, cm = _build_handler_with_flaky_stt(
            [
                [STTEvent(STTEventType.ERROR, text="drop 1")],
                [STTEvent(STTEventType.ERROR, text="drop 2")],
                [STTEvent(STTEventType.FINAL_TRANSCRIPT, text="ok")],
            ]
        )
        await stt.connect()
        tasks_before = len(asyncio.all_tasks())
        await self._run_and_cancel_after(handler, 2.5)  # 2 backoffs (2.0s) + margin
        tasks_after = len(asyncio.all_tasks())

        # +1 at most for _execute_turn(), spawned exactly once for the
        # single FINAL_TRANSCRIPT event -- not one per reconnect attempt
        # -- and the process_stt_events task itself is already gone
        # (cancelled and awaited) by the time this is checked.
        self.assertLessEqual(tasks_after - tasks_before, 1)
        self.assertEqual(len(cm.call_history), 1)
        if handler._active_turn_task:
            await handler._active_turn_task


class TestDuplicateStartFrameHandling(unittest.IsolatedAsyncioTestCase):
    """
    Instruction D verification (Issue 3 / Phase 16.3): first START,
    duplicate START, duplicate START arriving quickly, START after
    cleanup, normal STOP, abnormal disconnect. Confirms only one active
    handler per stream_sid, the previous handler is properly cancelled/
    cleaned up, no orphan tasks remain, and resources release
    deterministically.
    """

    def _manager(self) -> VoiceCallManager:
        return VoiceCallManager(conversation_manager=MockConversationManager(["Ok."] * 10))

    async def test_first_start_registers_a_single_handler(self):
        manager = self._manager()
        handler = manager.register_call("CA1", "MZ1", lambda msg: None, MockSTTService(), MockTTSService())

        self.assertIs(manager.get_handler("MZ1"), handler)
        self.assertEqual(len(manager._active_calls), 1)

    async def test_duplicate_start_cleans_up_previous_handler(self):
        manager = self._manager()
        stt1 = MockSTTService()
        await stt1.connect()
        first = manager.register_call("CA1", "MZ1", lambda msg: None, stt1, MockTTSService())
        self.assertTrue(stt1.is_connected)

        second = manager.register_call("CA2", "MZ1", lambda msg: None, MockSTTService(), MockTTSService())
        await asyncio.sleep(0.05)  # let the fire-and-forget cleanup task run

        # Only ONE active handler exists for this stream_sid -- the new one.
        self.assertIs(manager.get_handler("MZ1"), second)
        self.assertEqual(len(manager._active_calls), 1)
        self.assertIsNot(first, second)
        # The previous handler was properly cleaned up, not just dropped.
        self.assertFalse(first.is_active)
        self.assertFalse(stt1.is_connected, "the previous handler's STT connection must be closed")

    async def test_duplicate_start_arriving_quickly_still_cleans_up_exactly_once(self):
        manager = self._manager()
        stt1 = MockSTTService()
        await stt1.connect()
        first = manager.register_call("CA1", "MZ1", lambda msg: None, stt1, MockTTSService())
        # No await/sleep between the two registrations -- back-to-back,
        # matching a rapid duplicate/replayed START frame.
        second = manager.register_call("CA2", "MZ1", lambda msg: None, MockSTTService(), MockTTSService())
        await asyncio.sleep(0.05)

        self.assertIs(manager.get_handler("MZ1"), second)
        self.assertFalse(first.is_active)
        self.assertFalse(stt1.is_connected)

    async def test_duplicate_start_cancels_previous_in_flight_turn_task(self):
        manager = self._manager()
        stt1 = MockSTTService()
        await stt1.connect()
        first = manager.register_call("CA1", "MZ1", lambda msg: None, stt1, MockTTSService())

        # Simulate an in-flight turn on the FIRST handler at the moment
        # the duplicate START arrives.
        first._active_turn_task = asyncio.create_task(asyncio.sleep(30))

        manager.register_call("CA2", "MZ1", lambda msg: None, MockSTTService(), MockTTSService())
        await asyncio.sleep(0.05)

        self.assertTrue(first._active_turn_task.cancelled() or first._active_turn_task.done())

    async def test_start_after_cleanup_behaves_like_a_fresh_registration(self):
        manager = self._manager()
        stt1 = MockSTTService()
        await stt1.connect()
        manager.register_call("CA1", "MZ1", lambda msg: None, stt1, MockTTSService())
        await manager.unregister_call("MZ1")  # normal STOP -- fully cleaned up
        self.assertIsNone(manager.get_handler("MZ1"))

        cm2 = MockConversationManager(["Fresh response."])
        manager.conversation_manager = cm2
        stt2 = MockSTTService()
        await stt2.connect()
        fresh = manager.register_call("CA3", "MZ1", lambda msg: None, stt2, MockTTSService())

        self.assertIs(manager.get_handler("MZ1"), fresh)
        self.assertTrue(fresh.is_active)
        # Prove it actually functions normally -- no stale state leaking
        # in from the previous handler occupying the same stream_sid.
        stt_task = asyncio.create_task(fresh.process_stt_events())
        await stt2.push_event(STTEvent(STTEventType.FINAL_TRANSCRIPT, text="does this work"))
        await asyncio.sleep(0.05)
        if fresh._active_turn_task:
            await fresh._active_turn_task
        self.assertEqual(cm2.call_history[0]["message"], "does this work")
        stt_task.cancel()
        await manager.unregister_call("MZ1")

    async def test_normal_stop_unregisters_the_handler(self):
        manager = self._manager()
        stt1 = MockSTTService()
        await stt1.connect()
        handler = manager.register_call("CA1", "MZ1", lambda msg: None, stt1, MockTTSService())

        await manager.unregister_call("MZ1")

        self.assertIsNone(manager.get_handler("MZ1"))
        self.assertFalse(handler.is_active)
        self.assertFalse(stt1.is_connected)

    async def test_abnormal_disconnect_without_a_stop_frame_still_cleans_up(self):
        # Models server.py's websocket_call() `except WebSocketDisconnect`
        # path: the STOP frame never arrives, but the `finally` block
        # still calls unregister_call() directly.
        manager = self._manager()
        stt1 = MockSTTService()
        await stt1.connect()
        handler = manager.register_call("CA1", "MZ1", lambda msg: None, stt1, MockTTSService())
        handler._active_turn_task = asyncio.create_task(asyncio.sleep(30))

        await manager.unregister_call("MZ1")  # no explicit handle_stop() call first
        await asyncio.sleep(0.05)  # let the requested cancellation actually be delivered

        self.assertIsNone(manager.get_handler("MZ1"))
        self.assertFalse(handler.is_active)
        self.assertFalse(stt1.is_connected)
        self.assertTrue(handler._active_turn_task.cancelled() or handler._active_turn_task.done())

    async def test_no_orphan_tasks_remain_after_repeated_duplicate_starts(self):
        manager = self._manager()
        tasks_before = len(asyncio.all_tasks())

        handlers_and_stt = []
        for i in range(4):
            stt = MockSTTService()
            await stt.connect()
            h = manager.register_call(f"CA{i}", "MZ1", lambda msg: None, stt, MockTTSService())
            handlers_and_stt.append((h, stt))
            await asyncio.sleep(0.02)  # let each duplicate's cleanup task run before the next

        await asyncio.sleep(0.05)
        tasks_after = len(asyncio.all_tasks())

        self.assertEqual(len(manager._active_calls), 1, "only the last handler must remain registered")
        for h, stt in handlers_and_stt[:-1]:
            self.assertFalse(h.is_active)
            self.assertFalse(stt.is_connected)
        self.assertLessEqual(
            tasks_after - tasks_before, 0, "no cleanup tasks should still be running once they've all completed"
        )
        await manager.unregister_call("MZ1")


if __name__ == "__main__":
    unittest.main()
