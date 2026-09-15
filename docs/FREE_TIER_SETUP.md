# Running Without Paying for an LLM API

This deployment can run entirely on free-tier or fully-local LLM inference.
Nothing about the app requires Claude/Anthropic -- that path exists as an
option, not a dependency.

## The short version

Don't set `ANTHROPIC_API_KEY`. Set `GEMINI_API_KEY` and/or `GROQ_API_KEY`
instead (or neither, for fully offline/local). `LLM_PROVIDER` is then
auto-selected from whichever of those are actually present -- see
`src/inference/llm_provider.py`'s `_default_provider_mode()`:

| Keys present | Auto-selected mode | Cost |
|---|---|---|
| `ANTHROPIC_API_KEY` + `GEMINI_API_KEY` | `fallback` (Claude primary, Gemini fallback) | **Paid** (Claude) |
| `GEMINI_API_KEY` + `GROQ_API_KEY` (no Claude key) | `free_fallback` (Gemini primary, Groq fallback) | Free |
| `GEMINI_API_KEY` only | `gemini` | Free |
| `GROQ_API_KEY` only | `groq` | Free |
| none of the above | `local` (Qwen2.5-0.5B, in-repo, CPU) | Free, fully offline |

`configs/config.yaml`'s `llm.provider` key is deliberately left unset so
this auto-selection actually runs; set `LLM_PROVIDER` explicitly (env var)
or uncomment `llm.provider` in that file only if you want one fixed mode
regardless of which keys happen to be configured.

## Getting free-tier keys

- **Gemini**: https://aistudio.google.com/apikey -- Google AI Studio's free
  tier has real (non-trial) rate/daily quotas; requests beyond quota are
  rejected, not billed, unless you separately attach a Google Cloud billing
  account to the project.
- **Groq**: https://console.groq.com/keys -- Groq's free tier is generous
  and the inference is extremely fast (hosted on their own LPU hardware).

Neither requires a credit card to obtain a working key.

## A real defect this uncovered (read before changing the Groq model)

Both `GeminiLLMProvider` (2.5-family models) and `GroqLLMProvider`'s
reasoning models (e.g. `openai/gpt-oss-20b`, `openai/gpt-oss-120b`) spend
part of the requested token budget on an internal, hidden "thinking"/
"reasoning" step before producing visible text -- non-deterministically,
per call. With a modest `max_tokens`, the model can spend the *entire*
budget reasoning and return a real HTTP 200 with **zero visible text and
no error**. This was demonstrated live against both real APIs:

- Gemini: fixed by setting `generationConfig.thinkingConfig.thinkingBudget
  = 0` for 2.5-family models (`GeminiLLMProvider.generate_stream()`).
- Groq: worked around by defaulting to `qwen/qwen3.8-27b`, which has no
  such reasoning overhead (verified: 3/3 real calls returned visible text
  immediately, `finish_reason: "stop"`). If you switch `GROQ_MODEL` to one
  of Groq's `openai/gpt-oss-*` models, either raise `max_tokens`
  substantially (350+, this app's own default) or pass
  `reasoning_effort: "low"` -- GroqLLMProvider does not currently set this
  automatically, since the default model doesn't need it.

See `docs/phase1.4-external-integration-report.md` Section 13 for the full
Problem/Evidence/Root cause/Fix/Regression-test/Real-verification writeup
of the Gemini half of this.

## What's still unverified

The free path has been verified live end-to-end, including real failover
(Gemini forced to fail via an invalid model name, Groq picked up the
turn for real -- both genuine API calls, not mocked). What has NOT been
exercised: sustained/soak load against Gemini or Groq's free-tier rate
limits, and the free path has not been run through a real phone call
(Twilio/STT/TTS are unrelated to which LLM provider is selected, but a
full voice call was never placed against `free_fallback`/`groq` mode in
this pass). Neither is fabricated as verified here.
