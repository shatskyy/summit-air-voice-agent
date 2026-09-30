from datetime import datetime

import pytest

import receptionist
from evals import PAGE_TOPICS  # every ntfy topic the code can page; new ones join that list


def pytest_addoption(parser):
    parser.addoption(
        "--fallback",
        action="store_true",
        help="also run the llm tests on GPT-4.1, the fallback model (or set LLM_TESTS_FALLBACK=1)",
    )


@pytest.fixture(autouse=True)
def no_real_pages(monkeypatch):
    """test_agent.py loads .env.local, which names the real ntfy topic. Unset it for every test so
    a test that files an urgent task can never page the on-call phone."""
    for topic in PAGE_TOPICS:
        monkeypatch.delenv(topic, raising=False)


@pytest.fixture(autouse=True)
def monday_morning(monkeypatch):
    """The tests name days and slot ids from the week they were written in, so the clock stays at
    Monday, September 28, 2026, 9 AM. A test that needs another time sets its own."""
    monkeypatch.setattr(
        receptionist, "now", lambda: datetime(2026, 9, 28, 9, 0, tzinfo=receptionist.TZ)
    )
