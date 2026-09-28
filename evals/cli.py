"""uv run python -m evals [scenario ...] [-n RUNS] [-m MODEL] [--clock demo|night] [--compare FILE]

Defaults: every scenario on each of its clocks, openai/gpt-4.1-mini, 1 run. More runs and models
only when asked for. Before anything runs it prints the estimate and the ledger, and it refuses if
the estimate is over what the ledger allows or the OpenAI balance is under $1.50.
"""

import argparse
import asyncio
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from evals import ledger, report
from evals.runner import CLOCKS, clock, play
from evals.scenarios import BY_NAME, SCENARIOS, Conversation, style

DEFAULT_MODEL = "openai/gpt-4.1-mini"


def plan(args) -> list[tuple]:
    """(scenario, clock, model, run) for every conversation, grouped by clock."""
    chosen = [BY_NAME[n] for n in args.scenarios] if args.scenarios else SCENARIOS
    out = []
    for clock_name in CLOCKS:
        for s in chosen:
            clocks = args.clock or s.clocks
            if clock_name not in clocks:
                continue
            runs = args.runs_for.get(s.category, args.runs)
            for m in args.model or [DEFAULT_MODEL]:
                out += [(s, clock_name, m, i) for i in range(1, runs + 1)]
    return out


def dirty() -> bool:
    out = subprocess.run(
        ["git", "status", "--porcelain", "src", "config", "evals/scenarios.py", "evals/runner.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(out.stdout.strip())


def rescore(path: Path, compare: Path | None) -> int:
    """Run the current checks over the conversations a results file already holds. The transcript,
    bookings and tasks are what the checks read, so a fixed check can re-grade a run for free."""
    saved = json.loads(path.read_text())
    meta = saved["meta"] | {"suffix": "-rescored", "rescored_with": ledger.commit()}
    results = []
    for r in saved["results"]:
        if not r.get("skipped"):
            convo = Conversation(r["transcript"], r["bookings"], r["tasks"])
            failures = BY_NAME[r["scenario"]].check(convo)
            failures += [f"crashed: {r['error']}"] if r["error"] else []
            r = r | {"failures": failures, "passed": not failures, "style": style(convo)}
            r.pop("transcript_file", None)
        results.append(r)
    out = report.write_results(meta, results)
    changes = (
        report.compare_lines(json.loads(compare.read_text())["results"], results) if compare else []
    )
    report.write_report(report.render(meta, results, ledger.total(ledger.load()), changes))
    if report.write_readme(
        meta, results, json.loads(compare.read_text())["results"] if compare else None
    ):
        print("README eval table updated.")
    print(f"Re-graded {path.name} -> {out.name}")
    for k, (p, n) in sorted(report.tally(results).items()):
        print(f"  {k[0]:16} {k[1]:5} {p}/{n}")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="Simulated calls, priced and ledgered.")
    ap.add_argument("scenarios", nargs="*", help=f"any of {', '.join(BY_NAME)}")
    ap.add_argument("-n", "--runs", type=int, default=1)
    ap.add_argument(
        "--runs-for",
        action="append",
        default=[],
        metavar="CATEGORY=N",
        help="runs for one category, overriding -n (e.g. --runs-for safety=2)",
    )
    ap.add_argument("-m", "--model", action="append", help=f"default: {DEFAULT_MODEL}")
    ap.add_argument("--clock", action="append", choices=list(CLOCKS), help="default: each's own")
    ap.add_argument("-j", "--jobs", type=int, default=3, help="conversations at once")
    ap.add_argument("--budget", type=float, help="USD this run may spend (default: what is left)")
    ap.add_argument("--compare", type=Path, help="an earlier results JSON to compare against")
    ap.add_argument("--label", default="", help="a note for the ledger")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and estimate only")
    ap.add_argument(
        "--rescore",
        type=Path,
        metavar="FILE",
        help="re-grade a results JSON with the current checks; no model calls, no spend",
    )
    args = ap.parse_args()
    if args.rescore:
        return rescore(args.rescore, args.compare)
    try:
        args.runs_for = {k: int(v) for k, v in (x.split("=") for x in args.runs_for)}
    except ValueError:
        ap.error("--runs-for takes CATEGORY=N")
    if bad := set(args.runs_for) - set(report.CATEGORIES):
        ap.error(f"unknown category: {', '.join(sorted(bad))}")
    unknown = [n for n in args.scenarios if n not in BY_NAME]
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)}")

    todo = plan(args)
    per = ledger.per_conversation(ledger.load())
    estimate = len(todo) * per
    print(f"{len(todo)} conversations at ${per:.4f} each (measured average, or the default).")
    if args.dry_run:
        for s, c, m, i in todo:
            print(f"  {s.name} / {c} / {m} / run {i}")
        ledger.check(estimate, args.budget)
        return 0
    try:
        allowed = ledger.check(estimate, args.budget)
    except ledger.Refused as refused:
        print(f"Refused: {refused}")
        return 2

    commit = ledger.commit() + ("-dirty" if dirty() else "")
    started = datetime.now(ledger.TZ)
    meta = {
        "started": started.isoformat(timespec="seconds"),
        "stamp": started.strftime("%Y-%m-%d-%H%M"),
        "commit": commit,
        "models": sorted({m for _, _, m, _ in todo}),
        "runs": report.runs_text(args.runs, args.runs_for),
        "scenarios": sorted({s.name for s, *_ in todo}),
        "compare": str(args.compare or ""),
        "estimate": round(estimate, 4),
    }
    spent = 0.0
    in_flight = 0
    results: list[dict] = []
    gate = asyncio.Semaphore(args.jobs)

    async def one(s, c, m, i) -> dict:
        nonlocal spent, in_flight
        async with gate:
            # Stop launching once measured spend plus what is running could reach the allowance.
            if spent + (in_flight + 1) * per > allowed:
                return {"scenario": s.name, "category": s.category, "clock": c, "model": m,
                        "run": i, "skipped": True, "cost": 0.0}  # fmt: skip
            in_flight += 1
            try:
                for attempt in range(3):
                    r = await play(s, c, m, i)
                    spent += r["cost"]
                    if "429" not in r["error"] or attempt == 2:
                        return r
                    await asyncio.sleep(60)  # the rate limit; the replay costs, and is counted
                return r
            finally:
                in_flight -= 1

    t0 = time.monotonic()
    try:
        for clock_name in CLOCKS:
            batch = [t for t in todo if t[1] == clock_name]
            if not batch:
                continue
            with clock(clock_name):
                results += await asyncio.gather(*(one(*t) for t in batch))
    finally:
        # Whatever ran is recorded, even if the run was interrupted.
        path = report.write_results(meta, results) if results else None
        total = ledger.record(
            "eval",
            args.label or " ".join(meta["scenarios"]),
            spent,
            conversations=sum(not r.get("skipped") for r in results),
            estimate=estimate,
            results=str(path.relative_to(report.ROOT)) if path else "",
        )

    changes = []
    before = None
    if args.compare:
        before = json.loads(args.compare.read_text())["results"]
        changes = report.compare_lines(before, results)
    report.write_report(report.render(meta, results, total, changes))
    if report.write_readme(meta, results, before):
        print("README eval table updated.")

    ran = [r for r in results if not r.get("skipped")]
    print(f"\n{len(ran)} conversations in {time.monotonic() - t0:.0f}s, ${spent:.4f}. -> {path}")
    for k, (p, n) in sorted(report.tally(ran).items()):
        print(f"  {k[0]:16} {k[1]:5} {k[2].split('/')[-1]:13} {p}/{n}")
        for r in ran:
            if report.key(r) == k:
                for why in r["failures"]:
                    print(f"      run {r['run']}: {why}")
    if changes:
        print("\n" + "\n".join(changes))
    print(f"\nLedger total ${total:.4f} of ${ledger.CAP:.2f}. Report: evals/REPORT.md")
    return 0
