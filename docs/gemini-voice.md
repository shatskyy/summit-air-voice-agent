# Gemini voice

The speech provider is selected with `TTS_PROVIDER`. The default stays `deepgram` so installing
this change does not silently switch a running deployment. Set these in `.env.local`:

```dotenv
TTS_PROVIDER=gemini
GOOGLE_API_KEY=your-key
GEMINI_TTS_MODEL=gemini-3.1-flash-tts-preview
GEMINI_TTS_VOICE=Kore
```

Keys come from [Google AI Studio](https://aistudio.google.com/apikey). The key must have access and
quota for the selected TTS model. Never commit `.env.local`.

Gemini speaks first, with Deepgram Aura-2 and then OpenAI as backups. Deepgram Nova-3 still handles
transcription; GPT-4.1 mini still handles reasoning and tools. Gemini uses its own Google account,
not OpenAI credit or LiveKit Inference credit. The usage script reports an unpriced model for
Gemini; check Google billing for its actual cost rather than treating that omission as free usage.

The installed LiveKit plugin streams output audio but accepts complete text segments. LiveKit
adapts streamed text into segments. That can change first-audio latency and interruption behavior,
so a successful API request is not a phone quality test. On the Gemini path each provider gets one
attempt before fallback, without extra retries. Fallback does not replay audio already heard.

Before activating:

```sh
uv run pytest
uv run python scripts/check_voice.py
```

The second command makes one paid synthesis request directly to Gemini, with no fallback. It prints
time to first audio and saves a synthetic greeting to `logs/gemini-voice-check.wav`. Listen to it,
then follow [the deployment procedure](../ops/launchd/README.md), checking there is no active call
before restarting. Call the number to verify greeting, response timing, interruption and booking
confirmation. To roll back, set `TTS_PROVIDER=deepgram` and restart between calls.

Integration reference: [LiveKit Gemini TTS](https://docs.livekit.io/agents/models/tts/gemini/).
