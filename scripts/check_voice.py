"""One paid Gemini speech smoke test; saves synthetic audio under ignored logs/.

Run: uv run python scripts/check_voice.py
This checks the configured Gemini voice directly, so fallback cannot hide a failed test.
"""

import asyncio
import importlib
import json
import sys
import time
import wave
from pathlib import Path

from livekit.agents import APIConnectOptions

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
agent = importlib.import_module("agent")


async def main():
    voice = agent.gemini_voice()
    text = "Thanks for calling Summit Air. What can we help you with today?"
    started = time.monotonic()
    first_audio = None
    frames = []
    try:
        async with voice.synthesize(
            text, conn_options=APIConnectOptions(max_retry=0, timeout=15)
        ) as stream:
            async for event in stream:
                if first_audio is None:
                    first_audio = time.monotonic() - started
                frames.append(event.frame)
        if not frames:
            raise RuntimeError("Gemini returned no audio")
        path = ROOT / "logs" / "gemini-voice-check.wav"
        path.parent.mkdir(exist_ok=True)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(voice.num_channels)
            out.setsampwidth(2)
            out.setframerate(voice.sample_rate)
            for frame in frames:
                out.writeframes(bytes(frame.data))
        print(
            json.dumps(
                {
                    "model": voice.model,
                    "first_audio_seconds": round(first_audio, 3),
                    "total_seconds": round(time.monotonic() - started, 3),
                    "audio_seconds": round(
                        sum(f.samples_per_channel for f in frames) / voice.sample_rate, 3
                    ),
                    "saved": str(path.relative_to(ROOT)),
                }
            )
        )
    finally:
        await voice.aclose()


if __name__ == "__main__":
    asyncio.run(main())
