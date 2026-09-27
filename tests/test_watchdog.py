"""The watchdog's paging rules: pages on a change only, plus one morning all-good. No network."""

import importlib.util
from datetime import datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "watchdog", Path(__file__).resolve().parent.parent / "scripts" / "watchdog.py"
)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

MORNING = datetime(2026, 9, 28, 9, 5, tzinfo=watchdog.TZ)
NIGHT = datetime(2026, 9, 28, 2, 0, tzinfo=watchdog.TZ)


def state(worker_up=True, on_ac=True, **extra):
    return {"worker_up": worker_up, "worker_detail": "detail", "on_ac": on_ac, **extra}


def titles(previous, current, at=NIGHT):
    return [title for title, _, _ in watchdog.pages_for(previous, current, at)]


def test_a_steady_healthy_line_pages_nothing_at_night():
    assert titles(state(), state()) == []


def test_the_line_going_down_and_coming_back_pages_once_each():
    assert titles(state(), state(worker_up=False)) == ["Summit Air line is DOWN"]
    assert titles(state(worker_up=False), state(worker_up=False)) == []
    assert titles(state(worker_up=False), state()) == ["Summit Air line is back"]


def test_a_first_run_pages_only_if_the_line_is_down():
    assert titles({}, state()) == []
    assert titles({}, state(worker_up=False)) == ["Summit Air line is DOWN"]


def test_unplugging_the_host_pages_once():
    assert titles(state(), state(on_ac=False)) == ["Summit Air host on battery"]
    assert titles(state(on_ac=False), state(on_ac=False)) == []
    assert titles(state(on_ac=False), state()) == ["Summit Air host back on AC"]


def test_all_good_goes_out_once_after_9_am():
    current = state()
    assert titles(state(), current, MORNING) == ["Summit Air all good"]
    assert titles(current, state(), MORNING) == []


def test_no_all_good_while_the_line_is_down():
    assert "Summit Air all good" not in titles(state(), state(worker_up=False), MORNING)
