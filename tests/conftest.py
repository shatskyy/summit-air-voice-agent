import pytest


@pytest.fixture(autouse=True)
def no_real_pages(monkeypatch):
    """test_agent.py loads .env.local, which names the real ntfy topic. Unset it for every test so
    a test that files an urgent task can never page the on-call phone."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
