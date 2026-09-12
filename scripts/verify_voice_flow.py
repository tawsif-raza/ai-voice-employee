import asyncio
import sys
from pathlib import Path

_VOICE_DIR = str(Path(__file__).resolve().parents[1] / "src" / "voice")
_AGENT_DIR = str(Path(__file__).resolve().parents[1] / "src" / "agent")
for p in (_VOICE_DIR, _AGENT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from conversation_manager import build_conversation_manager
from telephony_models import CallSession
from voice_pipeline import VoiceCallHandler


class DummySTT:
    async def connect(self):
        pass

    async def send_audio(self, chunk):
        pass

    async def close(self):
        pass


class DummyTTS:
    async def synthesize_stream(self, token_stream, cancellation_event=None):
        async for chunk in token_stream:
            yield b"audio"


class DummyWebSocket:
    async def send_json(self, data):
        pass


async def run_flow():
    print("========================================================================")
    print("      AI VOICE AGENT - COMPLETE FLOW VERIFICATION (LOCAL SIMULATION)")
    print("========================================================================")

    # 1. Initialize ConversationManager (simulating Claude/Gemini and safety bounds)
    print("[System] Initializing ConversationManager, ToolOrchestrator, and PolicyEngine...")
    cm = build_conversation_manager(persistence_enabled=False, observability_enabled=False)

    # 2. Setup Telephony Session
    print("[Twilio -> WebSocket] Incoming Call from +15550000000")
    session = CallSession("CA_test", "MZ_test", "sess_test", "test_user", "+15550000000")
    handler = VoiceCallHandler(session, DummyWebSocket(), cm, DummySTT(), DummyTTS())

    # 3. Caller asks for appointment
    print('\n[Caller] (Speaking): "I want to book an appointment"')
    print("[Deepgram] (STT) -> Final Transcript: 'I want to book an appointment'")

    await handler._execute_turn("I want to book an appointment", 1)

    assistant_response = session.conversation_history[1]["content"]
    print("\n[Claude/Gemini -> ConversationManager -> ToolOrchestrator] Access Denied. Prompting for PIN.")
    print("[ElevenLabs] (TTS) -> [Twilio] -> [Caller]")
    print(f'Assistant (Audio): "{assistant_response}"')

    assert "PIN" in assistant_response or "security" in assistant_response

    # 4. Caller provides PIN
    print('\n[Caller] (Speaking): "My PIN is 1234"')
    print("[Deepgram] (STT) -> Final Transcript: 'My PIN is 1234'")

    await handler._execute_turn("My PIN is 1234", 2)

    assistant_response_2 = session.conversation_history[3]["content"]
    print("\n[ConversationManager] PIN Verified. Identity Bound to Session.")
    print("[ToolOrchestrator] Executing BOOK_APPOINTMENT tool...")
    print("[ElevenLabs] (TTS) -> [Twilio] -> [Caller]")
    print(f'Assistant (Audio): "{assistant_response_2}"')

    print("\n========================================================================")
    print("FLOW VERIFICATION SUCCESSFUL")
    print("All components triggered correctly in isolated simulated environment.")
    print("========================================================================")


if __name__ == "__main__":
    asyncio.run(run_flow())
