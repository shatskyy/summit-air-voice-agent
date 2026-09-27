import pytest

# Every ntfy topic the code can page. .env.local names the real ones; a new topic variable joins this
# list in the commit that adds it.
PAGE_TOPICS = ("NTFY_TOPIC", "WATCHDOG_NTFY_TOPIC")


@pytest.fixture(autouse=True)
def no_real_pages(monkeypatch):
    """test_agent.py loads .env.local, which names the real ntfy topic. Unset it for every test so
    a test that files an urgent task can never page the on-call phone."""
    for topic in PAGE_TOPICS:
        monkeypatch.delenv(topic, raising=False)
