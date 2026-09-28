"""The eval harness's own rules: prices, the spend ledger, the fixed clocks. No model, no network."""

from datetime import timedelta

import pytest

import receptionist
from evals import ledger, pricing, report
from evals.runner import CLOCKS, clock
from evals.scenarios import SCENARIOS


def test_a_conversation_is_priced_from_uncached_cached_and_output_tokens():
    # 1M input of which 400k cached, 100k output, on GPT-4.1 mini: 0.6*0.40 + 0.4*0.10 + 0.1*1.60
    assert pricing.cost("openai/gpt-4.1-mini", 1_000_000, 400_000, 100_000) == pytest.approx(0.44)
    assert pricing.cost("gpt-4.1", 1_000_000, 0, 0) == pytest.approx(2.00)


def test_an_unpriced_model_cannot_run():
    with pytest.raises(KeyError):
        pricing.rate("google/gemma-4-31b-it")


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "openai_balance", lambda: (4.00, "test"))
    return tmp_path / "spend.json"


def test_a_run_over_what_is_left_is_refused(book):
    ledger.record("eval", "earlier", 2.90, conversations=10, path=book)
    with pytest.raises(ledger.Refused):
        ledger.check(0.20, path=book)
    assert ledger.check(0.05, path=book) == pytest.approx(0.10)


def test_a_budget_never_lifts_the_cap(book):
    ledger.record("eval", "earlier", 2.50, conversations=10, path=book)
    assert ledger.check(0.10, budget=5.00, path=book) == pytest.approx(0.50)
    with pytest.raises(ledger.Refused):
        ledger.check(0.30, budget=0.20, path=book)


def test_a_low_balance_refuses_even_a_cheap_run(book, monkeypatch):
    monkeypatch.setattr(ledger, "openai_balance", lambda: (1.49, "test"))
    with pytest.raises(ledger.Refused):
        ledger.check(0.01, path=book)


def test_an_unreadable_balance_is_skipped_and_said(book, monkeypatch, capsys):
    monkeypatch.setattr(ledger, "openai_balance", lambda: (None, "no admin key"))
    ledger.check(0.01, path=book)
    assert "Balance check skipped" in capsys.readouterr().out


def test_the_estimate_uses_the_measured_average_once_there_is_one(book):
    assert ledger.per_conversation(ledger.load(book)) == ledger.DEFAULT_PER_CONVERSATION
    ledger.record("eval", "calibration", 0.04, conversations=2, path=book)
    ledger.record("pytest-llm", "tests", 1.00, path=book)  # not a conversation
    assert ledger.per_conversation(ledger.load(book)) == pytest.approx(0.02)
    assert ledger.load(book)["total"] == pytest.approx(1.04)


def test_the_clock_moves_the_prompt_and_office_hours_together():
    real = receptionist.now
    with clock("demo"):
        at = receptionist.now()
        assert at - CLOCKS["demo"] < timedelta(seconds=5)
        assert receptionist.office_open(at)
        assert "Tuesday, September 29, 2026" in receptionist.render_instructions(at, None)
    with clock("night"):
        assert not receptionist.office_open(receptionist.now())
    assert receptionist.now is real


def test_every_scenario_has_a_known_category_and_clock():
    for s in SCENARIOS:
        assert s.category in report.CATEGORIES
        assert set(s.clocks) <= set(CLOCKS)
        assert bool(s.brief) != bool(s.lines) or not (s.brief or s.lines)


def test_compare_flags_a_scenario_that_got_worse():
    def r(passed):
        return {
            "scenario": "gas",
            "clock": "demo",
            "model": "openai/gpt-4.1-mini",
            "passed": passed,
        }

    lines = report.compare_lines([r(True), r(True)], [r(True), r(False)])
    assert lines[-1] == "| gas | demo | gpt-4.1-mini | 2/2 | 1/2 (worse) |"


@pytest.mark.parametrize(
    ("said", "counts"),
    [
        ("Our target is a callback for you by 9:15 PM tonight.", True),
        ("The on-call technician will call you back by 9:15 PM.", True),
        ("Someone will call within 15 minutes.", True),
        ("Our on-call tech should call you soon.", False),
    ],
)
def test_the_urgent_target_counts_however_it_is_worded(said, counts):
    from evals.scenarios import TARGET_SAID

    assert bool(TARGET_SAID.search(said)) is counts


async def test_dry_run_needs_no_balance_or_spending_permission(monkeypatch, capsys):
    import sys

    from evals import cli

    def forbidden(*args, **kwargs):
        raise AssertionError("a preview must not check billing or start a paid run")

    monkeypatch.setattr(sys, "argv", ["evals", "--dry-run"])
    monkeypatch.setattr(ledger, "check", forbidden)
    assert await cli.main() == 0
    assert "Estimated total:" in capsys.readouterr().out
