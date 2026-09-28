"""Provider selection and startup checks, without paid synthesis requests."""

from types import SimpleNamespace

import pytest

import agent


def test_gemini_requires_its_own_key_without_affecting_deepgram(monkeypatch):
    for key in agent.REQUIRED_KEYS:
        monkeypatch.setenv(key, "test")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("TTS_PROVIDER", "gemini")
    assert agent.missing_keys() == ["GOOGLE_API_KEY"]
    monkeypatch.setenv("TTS_PROVIDER", "deepgram")
    assert agent.missing_keys() == []


def test_unknown_voice_provider_fails_at_startup(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "gmeini")
    with pytest.raises(ValueError, match="TTS_PROVIDER"):
        agent.missing_keys()


@pytest.mark.parametrize("provider, retries", [("gemini", 0), ("deepgram", 2)])
def test_voice_order_and_transcription_are_independent(monkeypatch, provider, retries):
    monkeypatch.setenv("TTS_PROVIDER", provider)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    monkeypatch.setenv("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
    monkeypatch.setenv("GEMINI_TTS_VOICE", "Kore")
    captured = {}

    def gemini(**kwargs):
        captured.update(kwargs)
        return "gemini"

    monkeypatch.setattr(agent.google.beta, "GeminiTTS", gemini)
    monkeypatch.setattr(agent.deepgram, "TTS", lambda **kwargs: "deepgram")
    monkeypatch.setattr(agent.deepgram, "STT", lambda **kwargs: kwargs)
    monkeypatch.setattr(agent, "backup_voice", lambda: "openai")
    monkeypatch.setattr(
        agent.tts,
        "FallbackAdapter",
        lambda voices, **kwargs: SimpleNamespace(voices=voices, **kwargs),
    )
    listening, speaking = agent.speech()
    assert listening["model"] == "nova-3"
    assert speaking.voices == (
        ["gemini", "deepgram", "openai"] if provider == "gemini" else ["deepgram", "openai"]
    )
    assert speaking.max_retry_per_tts == retries
    if provider == "gemini":
        assert captured["vertexai"] is False
        assert "Read exactly" in captured["instructions"]
        assert captured["voice_name"] == "Kore"
