"""The fan-out dispatches in waves, stops only what is behind a failure, and never waits across languages."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pytest
from consumers import Register
from dispatch_waves import DispatchError, RunState, adopt, exit_code, main, rehearsal_targets, rehearse, summary

LEVEL = "1.0.0"


def _register() -> Register:
    def direct(library: str = "python") -> dict:
        locator = "project.dependencies[idfkit]" if library == "python" else 'dependencies["@idfkit/core"]'
        path = "pyproject.toml" if library == "python" else "package.json"
        entry = "pypi" if library == "python" else "scoped"
        return {"library": library, "entry_point": entry, "means": "direct", "declared_at": [{"path": path, "locator": locator, "form": "exact"}]}

    def delivered(package: str, via: str) -> dict:
        return {
            "library": "python",
            "entry_point": "pypi",
            "means": "runtime-fetch",
            "declared_at": [{"path": ".mcp.json", "locator": "x.args[0]", "form": "exact", "package": package, "via": via}],
        }

    return Register.from_toml(
        {
            "register": {"schema_version": "1", "libraries": {"python": "o/idfkit", "javascript": "o/idfkit-js"}},
            "consumer": [
                {"id": "a", "repository": "o/a", "role": "builds", "libraries": [direct()],
                 "rehearsal": [{"library": "python", "install": "uv-wheel", "typecheck": "t", "test": "t"}]},
                {"id": "b", "repository": "o/b", "role": "builds", "libraries": [direct(), direct("javascript")]},
                {"id": "c", "repository": "o/c", "role": "delivers", "depends_on": ["a"], "libraries": [delivered("idfkit-mcp", "a")],
                 "rehearsal": [{"library": "python", "install": "through-provider"}]},
                {"id": "d", "repository": "o/d", "role": "delivers", "depends_on": ["b"], "libraries": [delivered("idfkit-lsp", "b")]},
                {"id": "e", "repository": "o/e", "role": "builds", "libraries": [direct("javascript")]},
                {"id": "site", "repository": "o/site", "role": "teaches"},
            ],
        }
    )


class FakeGitHub:
    def __init__(self, conclusions: Mapping[str, str | None] | None = None, refuse: set[str] | None = None) -> None:
        self.conclusions = dict(conclusions or {})
        self.refuse = refuse or set()
        self.events: list[tuple[str, str]] = []
        self.inputs: dict[str, Mapping[str, str]] = {}

    def dispatch(self, repository: str, workflow: str, inputs: Mapping[str, str]) -> None:
        if repository in self.refuse:
            raise DispatchError("HTTP 404: workflow bump-idfkit.yml not found")
        self.events.append(("dispatch", repository))
        self.inputs[repository] = inputs

    def find_run(self, repository: str, workflow: str, since: datetime) -> RunState | None:
        self.events.append(("find", repository))
        return RunState(f"{repository}-1", f"https://github.com/{repository}/actions/runs/1", self.conclusions.get(repository, "success"))

    def state(self, repository: str, run_id: str) -> RunState:
        return RunState(run_id, f"https://github.com/{repository}/actions/runs/1", self.conclusions.get(repository, "success"))


def _clock():
    now = [datetime(2026, 9, 11, tzinfo=timezone.utc)]

    def tick() -> datetime:
        now[0] += timedelta(seconds=1)
        return now[0]

    return tick


def _released(*packages: str):
    return lambda package, level: "9.9.9" if package in packages else None


BOTH_RELEASED = _released("idfkit-mcp", "idfkit-lsp")


def _run(github: FakeGitHub, library: str = "python", released=BOTH_RELEASED, timeout: float = 100):
    return {o.consumer: o for o in adopt(_register(), library, LEVEL, github, released, timeout=timeout, poll=0, clock=_clock(), sleep=lambda s: None)}


def test_wave_two_waits_for_wave_one_to_conclude() -> None:
    github = FakeGitHub()
    outcomes = _run(github)
    assert {k: o.status for k, o in outcomes.items()} == {"a": "succeeded", "b": "succeeded", "c": "succeeded", "d": "succeeded"}
    assert github.events.index(("dispatch", "o/c")) > github.events.index(("find", "o/a"))
    assert github.events.index(("dispatch", "o/d")) > github.events.index(("find", "o/b"))
    assert outcomes["c"].wave == 2 and outcomes["a"].wave == 1


def test_a_failed_provider_stops_its_dependents_and_not_its_siblings() -> None:
    outcomes = _run(FakeGitHub({"o/a": "failure"}))
    assert outcomes["a"].status == "failed"
    assert outcomes["b"].status == "succeeded"
    assert outcomes["c"].status == "skipped-behind" and "through a" in (outcomes["c"].reason or "")
    assert outcomes["d"].status == "succeeded"


def test_an_out_of_order_adoption_is_rejected_naming_the_provider() -> None:
    github = FakeGitHub()
    outcomes = _run(github, released=_released("idfkit-mcp"))
    assert outcomes["d"].status == "rejected"
    assert "provider b" in (outcomes["d"].reason or "")
    assert ("dispatch", "o/d") not in github.events
    assert exit_code(list(outcomes.values())) == 3


def test_a_dispatch_that_does_not_land_fails_the_run() -> None:
    outcomes = _run(FakeGitHub(refuse={"o/b"}))
    assert outcomes["b"].status == "dispatch-failed"
    assert outcomes["d"].status == "skipped-behind"
    assert outcomes["a"].status == "succeeded"
    assert exit_code(list(outcomes.values())) == 1


def test_a_run_that_never_concludes_times_out_rather_than_passing() -> None:
    outcomes = _run(FakeGitHub({"o/a": None}), timeout=5)
    assert outcomes["a"].status == "timed-out"
    assert outcomes["c"].status == "skipped-behind"


def test_a_javascript_release_consults_no_python_state() -> None:
    def refuse(package: str, level: str) -> str | None:
        raise AssertionError("a second-language adoption asked about a first-language provider")

    github = FakeGitHub()
    outcomes = _run(github, library="javascript", released=refuse)
    assert set(outcomes) == {"b", "e"}
    assert github.inputs["o/e"] == {"idfkit_js_version": LEVEL}


def test_the_python_input_names_the_level() -> None:
    github = FakeGitHub()
    _run(github)
    assert github.inputs["o/a"] == {"idfkit_version": LEVEL}


def test_the_report_says_where_it_stopped() -> None:
    outcomes = list(_run(FakeGitHub({"o/a": "failure"})).values())
    text = summary(outcomes, "python", LEVEL)
    assert "stopped in wave 1" in text and "`a` (failed)" in text


def test_rehearsal_fans_out_only_to_consumers_with_their_own_commands() -> None:
    assert rehearsal_targets(_register(), "python") == [("a", "o/a")]
    outcomes = rehearse(_register(), "python", "abc123", FakeGitHub(refuse={"o/a"}))
    assert [o.status for o in outcomes] == ["dispatch-failed"]
    assert exit_code(outcomes) == 1


def test_main_writes_the_report(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    register = tmp_path / "consumers.toml"
    register.write_text(
        '[register]\nschema_version = "1"\nlibraries = { python = "o/idfkit", javascript = "o/idfkit-js" }\n'
        '[[consumer]]\nid = "e"\nrepository = "o/e"\nrole = "builds"\n'
        '  [[consumer.libraries]]\n  library = "javascript"\n  entry_point = "scoped"\n  means = "direct"\n'
        '    [[consumer.libraries.declared_at]]\n    path = "package.json"\n    locator = \'dependencies["@idfkit/core"]\'\n    form = "exact"\n'
    )
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    report = tmp_path / "adoption-report.json"
    code = main(["--register", str(register), "--library", "javascript", "--rehearse-ref", "abc", "--report", str(report)], dispatcher=FakeGitHub())
    assert code == 0 and '"rehearse_ref": "abc"' in report.read_text()
