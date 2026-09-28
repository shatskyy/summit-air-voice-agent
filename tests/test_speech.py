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


def fake_speech(monkeypatch, provider, models=None):
    monkeypatch.setenv("TTS_PROVIDER", provider)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    monkeypatch.delenv("GEMINI_TTS_MODEL", raising=False)
    if models is None:
        monkeypatch.delenv("GEMINI_TTS_MODELS", raising=False)
    else:
        monkeypatch.setenv("GEMINI_TTS_MODELS", models)
    monkeypatch.delenv("GEMINI_TTS_VOICE", raising=False)
    made = []

    def gemini(**kwargs):
        made.append(kwargs)
        return kwargs["model"]

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
    return listening, speaking, made


@pytest.mark.parametrize("provider, retries", [("gemini", 0), ("deepgram", 2)])
def test_voice_order_and_transcription_are_independent(monkeypatch, provider, retries):
    """Best Gemini first, each Gemini model before Deepgram's different voice: every model has its
    own 10-a-minute limit, and one call peaked at 7 on 3.1 alone."""
    listening, speaking, made = fake_speech(monkeypatch, provider)
    assert listening["model"] == "nova-3"
    assert speaking.voices == (
        [
            "gemini-3.8-flash-tts",
            "gemini-3.8-flash-lite-tts",
            "gemini-3.1-flash-tts-preview",
            "deepgram",
            "openai",
        ]
        if provider == "gemini"
        else ["deepgram", "openai"]
    )
    assert speaking.max_retry_per_tts == retries
    assert all(v["vertexai"] is False and v["voice_name"] == "Achird" for v in made)


def test_only_31_gets_the_style_prompt(monkeypatch):
    """Both 3.8 models read the prompt aloud as the text ("Speak as a calm, friendly HVAC
    receptionist..."), so they get the bare text; 3.1 read it as direction."""
    _, _, made = fake_speech(monkeypatch, "gemini")
    prompts = {v["model"]: v["instructions"] for v in made}
    assert prompts["gemini-3.8-flash-tts"] is None
    assert prompts["gemini-3.8-flash-lite-tts"] is None
    assert "Read exactly" in prompts["gemini-3.1-flash-tts-preview"]


def test_the_gemini_order_is_configuration(monkeypatch):
    _, speaking, _ = fake_speech(monkeypatch, "gemini", " gemini-3.1-flash-tts-preview , ")
    assert speaking.voices == ["gemini-3.1-flash-tts-preview", "deepgram", "openai"]


def test_every_voice_in_the_chain_is_male(monkeypatch):
    """A man's voice, kept through a fallback."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    made = {}
    monkeypatch.setattr(agent.openai, "TTS", lambda **kwargs: made.update(kwargs))
    agent.backup_voice()
    assert made["voice"] == "onyx"
    assert agent.TTS_VOICE == "aura-2-arcas-en"
    _, _, gemini = fake_speech(monkeypatch, "gemini")
    assert {v["voice_name"] for v in gemini} == {"Achird"}
