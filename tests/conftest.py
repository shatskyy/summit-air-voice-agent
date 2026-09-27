import pytest

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
