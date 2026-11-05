"""
Safety fallback when a call ends from the service side
(docs/CLINICAL_SAFETY.md, "Voice: when TTS fails").

The inbound TwiML (src/api/server.py, /twiml/inbound-call) is fixed when the
call starts: <Connect><Stream/></Connect> then <Say>VOICE_FALLBACK_MESSAGE
</Say>, which Twilio speaks if the service closes the media stream. That
generic goodbye ("Please call back later") is wrong for a call that involved
an urgent-risk answer -- e.g. the caller said "I can't breathe", ElevenLabs
was down, and the emergency instruction was never heard.

So before closing the stream of such a call, the server asks Twilio to
replace the running TwiML with a safety message spoken by Twilio itself
(independent of ElevenLabs), then hang up -- Twilio's documented call-update
request: POST /2010-04-01/Accounts/{AccountSid}/Calls/{CallSid}.json with a
`Twiml` parameter, authenticated with the account SID and TWILIO_AUTH_TOKEN.
If that request cannot be made or fails, the call falls back to the
ordinary TwiML <Say>: no worse than before, and recorded in metrics.

No phone number, transfer or callback is ever claimed: none is configured
or verified (ADR-006).
"""

import logging
import re
from typing import Optional
from xml.sax.saxutils import escape

import httpx

logger = logging.getLogger(__name__)

# Spoken by Twilio when a call that produced an URGENT-risk response ends
# from the service side. Overridable with VOICE_URGENT_FALLBACK_MESSAGE
# (e.g. to name a VERIFIED local emergency number); empty -> this default.
DEFAULT_URGENT_FALLBACK_MESSAGE = (
    "This may need urgent medical help. Please hang up and call your local emergency number right now, "
    "or poison control if it's a possible overdose. We can't continue this call, and we can't call "
    "anyone for you from this line. Goodbye."
)

# Spoken when a MEDICATION-safety response could not be heard and the call
# ends: same content as ConversationManager.CLINICAL_HANDOFF_RESPONSE.
MEDICATION_FALLBACK_MESSAGE = (
    "We can't continue this call right now. We can't advise on medicines or doses -- for your safety, "
    "please ask your pharmacist or doctor directly. If you feel unwell, call your local emergency number. Goodbye."
)

# Twilio SIDs: two letters and 32 hex digits. Anything else is never put in
# a request URL.
_ACCOUNT_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")

TWILIO_API_BASE_URL = "https://api.twilio.com"
REDIRECT_TIMEOUT_SECONDS = 5.0


def fallback_message(tier: Optional[str], urgent_message: str) -> Optional[str]:
    """The message Twilio must speak for a call's safety tier, or None for an ordinary call."""
    if tier == "urgent":
        return urgent_message.strip() or DEFAULT_URGENT_FALLBACK_MESSAGE
    if tier == "medication":
        return MEDICATION_FALLBACK_MESSAGE
    return None


def build_fallback_twiml(message: str) -> str:
    """TwiML that has Twilio speak `message` (XML-escaped) and end the call."""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(message)}</Say><Hangup/></Response>'


async def redirect_call(
    account_sid: str,
    call_sid: str,
    twiml: str,
    *,
    auth_token: str,
    base_url: str = TWILIO_API_BASE_URL,
    timeout: float = REDIRECT_TIMEOUT_SECONDS,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> bool:
    """
    Replaces the call's running TwiML with `twiml`. Returns True when Twilio
    accepted it. Never raises, never logs the token, the TwiML or the
    response body -- only the call SID and a status / error type.
    """
    if not auth_token or not _ACCOUNT_SID.match(account_sid or "") or not _CALL_SID.match(call_sid or ""):
        logger.warning("Safety fallback for call %s not requested: Twilio credentials or SIDs unavailable.", call_sid)
        return False
    url = f"{base_url.rstrip('/')}/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(url, data={"Twiml": twiml}, auth=(account_sid, auth_token))
    except Exception as exc:
        logger.warning("Safety fallback for call %s failed: %s", call_sid, type(exc).__name__)
        return False
    if response.status_code != 200:
        logger.warning("Safety fallback for call %s refused by Twilio: HTTP %s", call_sid, response.status_code)
        return False
    return True
