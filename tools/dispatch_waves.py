"""Dispatch a published level to its consumers, in waves, and say exactly where the sequence stopped.

Both libraries' `notify-downstream.yml` call this after a release has reached its index. It replaces
the flat loop that used to live in idfkit's workflow, which read a heredoc of four repositories and
ended every dispatch with `|| echo "Skipping $repo"`, so a repository that never received the bump
printed the same line as one that did (contracts/adoption.md).

WHAT IT DOES, per wave from `adoption.waves`:

  1. A consumer behind one that did not adopt is not dispatched. It is recorded as skipped-behind,
     naming the consumer it waited on, and so is everything behind it (FR-022, T065). Its siblings in
     the same wave are independent by construction and proceed.
  2. A consumer that reaches the library through another is asked `adoption.check_order` first, and
     one proposed before its provider has released on the level is REJECTED, naming the provider,
     rather than dispatched (FR-020, T070).
  3. Everything else is dispatched. A dispatch that does not land is a failure (T063).
  4. The wave's runs are waited for. Only a run that concludes `success` lets the next wave go.

Waves are computed per library. A second-language release never consults first-language state, and
the reverse, because lockstep between the two is prohibited (FR-021, T071).

THE REPORT IS THE RUN'S, NOT THE REGISTER'S. Where an adoption stopped is written to the JSON report
and the step summary. The register holds the roster and never the state of a run in progress (FR-022).

Exit codes: 0 every target adopted; 1 a dispatch did not land, or a run failed or timed out; 3 no
dispatch failed and no run failed, and at least one target was rejected or held behind a rejection,
which is the normal state of wave 2 until a provider publishes a release on the new level.

`--rehearse-ref` is the same fan-out without waves or waiting, for `rehearse-all.yml`: it dispatches
`rehearse-candidate.yml` to every consumer with a rehearsal of that library that is not
`through-provider`, and fails if any dispatch does not land.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adoption import _fetch_json, check_order, pypi_released_on, waves  # noqa: E402
from consumers import Register  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: The bump workflow's input carrying the level, per library.
LEVEL_INPUT = {"python": "idfkit_version", "javascript": "idfkit_js_version"}
REHEARSE_WORKFLOW = "rehearse-candidate.yml"

EXIT_FAILED = 1
EXIT_WAITING = 3


class DispatchError(RuntimeError):
    """A dispatch that did not land. Never downgraded to a skip."""


@dataclass(frozen=True, slots=True)
class RunState:
    run_id: str
    url: str
    #: None while the run has not concluded.
    conclusion: str | None


class Dispatcher(Protocol):
    """The three things the fan-out asks of GitHub, injectable so that the ordering can be tested."""

    def dispatch(self, repository: str, workflow: str, inputs: Mapping[str, str]) -> None: ...

    def find_run(self, repository: str, workflow: str, since: datetime) -> RunState | None: ...

    def state(self, repository: str, run_id: str) -> RunState: ...


class GhDispatcher:
    def _gh(self, *args: str) -> str:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            raise DispatchError(result.stderr.strip() or f"gh {' '.join(args)} exited {result.returncode}")
        return result.stdout

    def dispatch(self, repository: str, workflow: str, inputs: Mapping[str, str]) -> None:
        fields = [arg for key, value in inputs.items() for arg in ("-f", f"{key}={value}")]
        self._gh("workflow", "run", workflow, "--repo", repository, *fields)

    def find_run(self, repository: str, workflow: str, since: datetime) -> RunState | None:
        listing = json.loads(
            self._gh(
                "run", "list", "--repo", repository, "--workflow", workflow, "--event", "workflow_dispatch",
                "--limit", "20", "--json", "databaseId,createdAt,url,status,conclusion",
            )
        )
        # A little slack for clock skew between this runner and GitHub.
        after = since - timedelta(seconds=30)
        mine = [r for r in listing if datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) >= after]
        if not mine:
            return None
        run = min(mine, key=lambda r: r["createdAt"])
        return RunState(str(run["databaseId"]), run["url"], run["conclusion"] or None if run["status"] == "completed" else None)

    def state(self, repository: str, run_id: str) -> RunState:
        run = json.loads(self._gh("run", "view", run_id, "--repo", repository, "--json", "databaseId,url,status,conclusion"))
        return RunState(str(run["databaseId"]), run["url"], run["conclusion"] or None if run["status"] == "completed" else None)


@dataclass
class Outcome:
    consumer: str
    repository: str
    workflow: str
    wave: int
    #: dispatched, succeeded, failed, dispatch-failed, rejected, skipped-behind, timed-out
    status: str = "pending"
    run_url: str | None = None
    reason: str | None = None


def adopt(
    register: Register,
    library: str,
    level: str,
    dispatcher: Dispatcher,
    released_on: Callable[[str, str], str | None],
    *,
    timeout: float = 3600.0,
    poll: float = 20.0,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    blocked: dict[str, str] = {}
    for wave in waves(register, library):
        in_flight: list[tuple[Outcome, datetime]] = []
        for target in wave:
            outcome = Outcome(target.consumer, target.repository, target.workflow, target.wave)
            outcomes.append(outcome)
            consumer = register.consumer(target.consumer)
            behind = [d for d in consumer.depends_on if d in blocked]
            if behind:
                outcome.status = "skipped-behind"
                outcome.reason = f"not dispatched: it resolves {library} through {', '.join(behind)}, which did not adopt {level}"
                blocked[target.consumer] = outcome.status
                continue
            rejections = check_order(register, target.consumer, library, level, released_on)
            if rejections:
                outcome.status = "rejected"
                outcome.reason = " ".join(rejections)
                blocked[target.consumer] = outcome.status
                continue
            started = clock()
            try:
                dispatcher.dispatch(target.repository, target.workflow, {LEVEL_INPUT[library]: level})
            except DispatchError as error:
                outcome.status = "dispatch-failed"
                outcome.reason = f"the dispatch did not land: {error}"
                blocked[target.consumer] = outcome.status
                continue
            outcome.status = "dispatched"
            in_flight.append((outcome, started))

        for outcome, started in in_flight:
            deadline = clock() + timedelta(seconds=timeout)
            run: RunState | None = None
            while clock() < deadline:
                run = dispatcher.find_run(outcome.repository, outcome.workflow, started) if run is None else dispatcher.state(outcome.repository, run.run_id)
                if run is not None:
                    outcome.run_url = run.url
                    if run.conclusion is not None:
                        break
                sleep(poll)
            if run is None or run.conclusion is None:
                outcome.status = "timed-out"
                outcome.reason = f"no concluded run within {int(timeout)}s of the dispatch"
            elif run.conclusion == "success":
                outcome.status = "succeeded"
            else:
                outcome.status = "failed"
                outcome.reason = f"the bump workflow concluded {run.conclusion}"
            if outcome.status != "succeeded":
                blocked[outcome.consumer] = outcome.status
    return outcomes


def rehearsal_targets(register: Register, library: str) -> list[tuple[str, str]]:
    """Every consumer with a rehearsal of *library* that runs its own commands."""
    return sorted(
        (c.id, c.repository)
        for c in register.consumers
        if (r := c.rehearsal(library)) is not None and r.install != "through-provider"
    )


def rehearse(register: Register, library: str, candidate_ref: str, dispatcher: Dispatcher) -> list[Outcome]:
    outcomes = []
    for consumer_id, repository in rehearsal_targets(register, library):
        outcome = Outcome(consumer_id, repository, REHEARSE_WORKFLOW, 1)
        try:
            dispatcher.dispatch(repository, REHEARSE_WORKFLOW, {"library": library, "candidate_ref": candidate_ref})
            outcome.status = "dispatched"
        except DispatchError as error:
            outcome.status = "dispatch-failed"
            outcome.reason = f"the dispatch did not land: {error}"
        outcomes.append(outcome)
    return outcomes


def exit_code(outcomes: Sequence[Outcome]) -> int:
    statuses = {o.status for o in outcomes}
    if statuses & {"dispatch-failed", "failed", "timed-out"}:
        return EXIT_FAILED
    if statuses & {"rejected", "skipped-behind"}:
        return EXIT_WAITING
    return 0


def summary(outcomes: Sequence[Outcome], library: str, level: str) -> str:
    lines = [f"## {library} {level}: adoption", "", "| Wave | Consumer | Status | Run | Why |", "| --- | --- | --- | --- | --- |"]
    for o in outcomes:
        run = f"[run]({o.run_url})" if o.run_url else ""
        lines.append(f"| {o.wave} | `{o.consumer}` | {o.status} | {run} | {o.reason or ''} |")
    stopped = [o for o in outcomes if o.status not in {"succeeded", "dispatched"}]
    lines.append("")
    if not stopped:
        lines.append("Every consumer was offered the level and its bump workflow succeeded.")
    else:
        first = min(o.wave for o in stopped)
        lines.append(
            f"**The sequence stopped in wave {first}**, at "
            + ", ".join(f"`{o.consumer}` ({o.status})" for o in stopped if o.wave == first)
            + ". Everything listed behind it was held back rather than dispatched."
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, dispatcher: Dispatcher | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch a published level, or a rehearsal, to every consumer.")
    parser.add_argument("--register", type=Path, default=ROOT / "governance" / "consumers.toml")
    parser.add_argument("--library", choices=sorted(LEVEL_INPUT), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--level", help="the published level to adopt, in waves")
    mode.add_argument("--rehearse-ref", help="a candidate ref to rehearse against every consumer")
    parser.add_argument("--report", type=Path, default=Path("adoption-report.json"))
    parser.add_argument("--timeout-minutes", type=float, default=60.0)
    args = parser.parse_args(argv)

    register = Register.load(args.register)
    gh = dispatcher or GhDispatcher()
    if args.rehearse_ref:
        outcomes = rehearse(register, args.library, args.rehearse_ref, gh)
        title = f"rehearsal of {args.rehearse_ref}"
    else:
        outcomes = adopt(register, args.library, args.level, gh, pypi_released_on(_fetch_json), timeout=args.timeout_minutes * 60)
        title = args.level

    text = summary(outcomes, args.library, title)
    print(text)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "library": args.library,
        "level": args.level,
        "rehearse_ref": args.rehearse_ref,
        "outcomes": [asdict(o) for o in outcomes],
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return exit_code(outcomes)


if __name__ == "__main__":
    sys.exit(main())
