# Gemini voice

**Off since September 28, 2026, 1:30 PM ET; Deepgram Aura-2 Arcas leads again.** On the 1:06 PM
phone call (`RM_RhLPj79o2kFx`) Gemini 3.8 Flash took 1.0 to 1.8 s to first audio on every turn,
against 0.5 to 0.85 s on the turns another voice took, and the median reply was 2.96 s. The call
used Gemini's 10-requests-a-minute limit in about 50 seconds: the three-sentence greeting was three
requests, and replies begun for four fragments of the caller's opening sentence, then dropped,
were four more. The eleventh request got HTTP 429 and a different model spoke until 3.8 Flash
recovered 20 s later. Every call longer than a minute would do the same. The record of the
activation follows.

Activated on the demo host September 28, 2026, after a direct synthesis test returned first audio
in 0.807 seconds (3.72 seconds of generated speech). This is one API measurement, not end-to-end
phone latency. A phone check of the new voice is still required.

The speech provider is selected with `TTS_PROVIDER`. The default stays `deepgram` so installing
this change does not silently switch a running deployment. Set these in `.env.local`:

```dotenv
TTS_PROVIDER=gemini
GOOGLE_API_KEY=your-key
GEMINI_TTS_VOICE=Achird
# optional; this is the default order
GEMINI_TTS_MODELS=gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts,gemini-3.1-flash-tts-preview
```

**Three Gemini models, best first (2026-09-28).** On the paid Tier 1 key each Gemini TTS model is
limited to 10 requests a minute, every spoken sentence is one request, and one phone call peaked at
7 on 3.1 alone. So a sentence a model refuses goes to the next Gemini model, in the same voice (Achird, male; Kore until 2026-09-28),
before it goes to Deepgram's different voice. Probes that morning, with each result transcribed:

| Model | First audio | Read the text exactly |
|---|---|---|
| `gemini-3.8-flash-tts` | 1.0 to 1.1 s | 3 of 3 bare; 1 of 2 with the style prompt |
| `gemini-3.8-flash-lite-tts` | 0.4 to 0.6 s | 3 of 3 bare; 0 of 2 with the style prompt |
| `gemini-3.1-flash-tts-preview` | 0.7 to 1.0 s | 2 of 2 with the style prompt |
| `gemini-2.5-flash-preview-tts` | 2.8 to 3.3 s | yes; left out, too slow |
| `gemini-2.5-pro-preview-tts` | 4.7 s | yes; left out, too slow |

3.8 Flash leads because it ranks highest on independent voice arenas. The plugin puts the style
prompt in front of the text, and the 3.8 models read that prompt aloud ("Speak as a calm, friendly
HVAC receptionist..."), so only 3.1 gets it. `GEMINI_TTS_MODEL` now only picks the model
`scripts/check_voice.py` probes. Neither 3.8 model had answered a phone call when this was
written.

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
