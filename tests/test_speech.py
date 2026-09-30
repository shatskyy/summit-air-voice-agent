"""Voice selection and startup checks, without paid synthesis requests."""

from types import SimpleNamespace

import agent


def fake_speech(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test")
    monkeypatch.setattr(agent.deepgram, "TTS", lambda **kwargs: "deepgram")
    monkeypatch.setattr(agent.deepgram, "STT", lambda **kwargs: kwargs)
    monkeypatch.setattr(agent, "backup_voice", lambda: "openai")
    monkeypatch.setattr(
        agent.tts,
        "FallbackAdapter",
        lambda voices, **kwargs: SimpleNamespace(voices=voices, **kwargs),
    )
    return agent.speech()


def test_deepgram_speaks_with_openai_as_the_backup(monkeypatch):
    listening, speaking = fake_speech(monkeypatch)
    assert listening["model"] == "nova-3"
    assert speaking.voices == ["deepgram", "openai"]
    assert speaking.max_retry_per_tts == 2


def test_every_voice_in_the_chain_is_male(monkeypatch):
    """A man's voice, kept through a fallback."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    made = {}
    monkeypatch.setattr(agent.openai, "TTS", lambda **kwargs: made.update(kwargs))
    agent.backup_voice()
    assert made["voice"] == "onyx"
    assert agent.TTS_VOICE == "aura-2-arcas-en"
