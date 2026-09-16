import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "src" / "agent")
for p in (_VOICE_DIR, _AGENT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from identity import Permission
from telephony_models import CallSession
from voice_pipeline import VoiceCallHandler


class MockConversationManager:
    def __init__(self, session_manager, tool_orchestrator):
        self.session_manager = session_manager
        self.tool_orchestrator = tool_orchestrator
        self.turn_count = 0
        self.last_auth = None

    def handle_turn(self, user_input, history=None, auth=None, session_id=None, request_id=None):
        self.turn_count += 1
        self.last_auth = auth

        session = self.session_manager.get_session(session_id)
        if not session:
            session = self.session_manager.create_session(session_id)

        if session.workflow_state == "AWAITING_AUTHENTICATION":
            if "1234" in user_input:
                new_metadata = dict(session.metadata)
                new_metadata["authenticated_caller"] = True
                self.session_manager.update_session(session_id, metadata=new_metadata, workflow_state=None)
                yield "You're all set \u2014 your appointment is booked for 2026-08-20 at 10:00 AM."
                yield {"response": "You're all set", "is_handoff": False}
                return
            else:
                self.session_manager.update_session(session_id, workflow_state=None, pending_action=None)
                yield "That PIN doesn't seem to match. Let me connect you with a human agent."
                yield {"response": "Mismatch", "is_handoff": True}
                return

        if not auth or not auth.authenticated:
            self.session_manager.update_session(
                session_id, workflow_state="AWAITING_AUTHENTICATION", pending_action="BOOK_APPOINTMENT"
            )
            yield "For your security, could you please tell me your 4-digit PIN?"
            yield {"response": "Auth required", "is_handoff": False}
            return

        yield "You're all set \u2014 your appointment is booked for 2026-08-20 at 10:00 AM."
        yield {"response": "Booked", "is_handoff": False}


@pytest.fixture
def session_manager():
    from src.agent.session_manager import SessionManager

    return SessionManager()


@pytest.fixture
def mock_stt():
    stt = Mock()
    stt.connect = AsyncMock()
    stt.send_audio = AsyncMock()
    stt.close = AsyncMock()
    return stt


@pytest.fixture
def mock_tts():
    tts = Mock()

    async def _synth(token_stream, cancellation_event=None):
        async for chunk in token_stream:
            yield b"audio"

    tts.synthesize_stream = _synth
    return tts


@pytest.mark.asyncio
async def test_auth_workflow_success(session_manager, mock_stt, mock_tts):
    cm = MockConversationManager(session_manager, None)
    session = CallSession("call_1", "stream_1", "sess_1", "user_1", "+15551234")
    handler = VoiceCallHandler(session, AsyncMock(), cm, mock_stt, mock_tts)

    await handler._execute_turn("I want to book an appointment", 1)

    sess = session_manager.get_session("sess_1")
    assert sess.workflow_state == "AWAITING_AUTHENTICATION"
    assert "For your security, could you please tell me your 4-digit PIN?" in session.conversation_history[1]["content"]

    await handler._execute_turn("My PIN is 1234", 2)

    sess = session_manager.get_session("sess_1")
    assert sess.workflow_state is None
    assert sess.metadata.get("authenticated_caller") is True
    assert "booked" in session.conversation_history[3]["content"]


@pytest.mark.asyncio
async def test_auth_workflow_wrong_pin(session_manager, mock_stt, mock_tts):
    cm = MockConversationManager(session_manager, None)
    session = CallSession("call_2", "stream_2", "sess_2", "user_2", "+15551234")
    handler = VoiceCallHandler(session, AsyncMock(), cm, mock_stt, mock_tts)

    await handler._execute_turn("Book an appointment", 1)
    await handler._execute_turn("My PIN is 9999", 2)

    sess = session_manager.get_session("sess_2")
    assert sess.metadata.get("authenticated_caller") is not True
    assert sess.workflow_state is None
    assert "doesn't seem to match" in session.conversation_history[3]["content"]


@pytest.mark.asyncio
async def test_repeated_failed_verification(session_manager, mock_stt, mock_tts):
    cm = MockConversationManager(session_manager, None)
    session = CallSession("call_3", "stream_3", "sess_3", "user_3", "+15551234")
    handler = VoiceCallHandler(session, AsyncMock(), cm, mock_stt, mock_tts)

    await handler._execute_turn("Book an appointment", 1)
    await handler._execute_turn("My PIN is 9999", 2)
    assert session_manager.get_session("sess_3").metadata.get("authenticated_caller") is not True

    await handler._execute_turn("Let me try booking again", 3)
    await handler._execute_turn("My PIN is 8888", 4)
    assert session_manager.get_session("sess_3").metadata.get("authenticated_caller") is not True


@pytest.mark.asyncio
async def test_session_takeover_isolation(session_manager, mock_stt, mock_tts):
    cm = MockConversationManager(session_manager, None)

    sess_a = CallSession("call_a", "stream_a", "sess_a", "user_a", "+15551111")
    handler_a = VoiceCallHandler(sess_a, AsyncMock(), cm, mock_stt, mock_tts)
    await handler_a._execute_turn("Book", 1)
    await handler_a._execute_turn("1234", 2)
    assert session_manager.get_session("sess_a").metadata.get("authenticated_caller") is True

    sess_b = CallSession("call_b", "stream_b", "sess_b", "user_b", "+15552222")
    handler_b = VoiceCallHandler(sess_b, AsyncMock(), cm, mock_stt, mock_tts)
    await handler_b._execute_turn("Book", 1)

    assert session_manager.get_session("sess_b").metadata.get("authenticated_caller") is not True
    assert "For your security, could you please tell me your 4-digit PIN?" in sess_b.conversation_history[1]["content"]


@pytest.mark.asyncio
async def test_authenticated_caller_auth_context_has_usable_permissions(session_manager, mock_stt, mock_tts):
    """
    Phase 20 finding (same root cause as PHASE_18's F-04): VoiceCallHandler
    ._execute_turn() builds its own AuthContext every turn from
    session.metadata["authenticated_caller"]. It used to set
    roles=["caller"] with no `permissions` -- a role with no entry in
    identity.py's ROLE_PERMISSIONS table and a context that can never
    pass any real AuthContext.has_permission() check, silently making
    every turn AFTER a "successful" PIN authentication just as
    unauthorized as before it. This asserts the real AuthContext object
    (not just the session metadata flag the other tests here check)
    actually carries a usable Role.USER permission set once authenticated,
    and none before.
    """
    from identity import Role, permissions_for_roles

    cm = MockConversationManager(session_manager, None)
    session = CallSession("call_5", "stream_5", "sess_5", "user_5", "+15551234")
    handler = VoiceCallHandler(session, AsyncMock(), cm, mock_stt, mock_tts)

    await handler._execute_turn("I want to book an appointment", 1)
    assert cm.last_auth is not None
    assert cm.last_auth.authenticated is False
    assert cm.last_auth.permissions == ()

    await handler._execute_turn("My PIN is 1234", 2)
    # The turn that carries the PIN itself is auth'd against the
    # pre-PIN-entry session state, so re-run one more turn to observe
    # the AuthContext built AFTER authentication succeeded.
    await handler._execute_turn("Can you check my order status for order_1001?", 3)

    assert cm.last_auth.authenticated is True
    assert cm.last_auth.has_permission(Permission.BOOK_APPOINTMENT.value)
    assert cm.last_auth.permissions == permissions_for_roles((Role.USER,))


@pytest.mark.asyncio
async def test_expired_auth_state(session_manager, mock_stt, mock_tts):
    cm = MockConversationManager(session_manager, None)
    session = CallSession("call_4", "stream_4", "sess_4", "user_4", "+15551234")
    handler = VoiceCallHandler(session, AsyncMock(), cm, mock_stt, mock_tts)

    await handler._execute_turn("Book an appointment", 1)

    session_manager.expire_session("sess_4")

    await handler._execute_turn("1234", 2)

    assert session_manager.get_session("sess_4").metadata.get("authenticated_caller") is not True
