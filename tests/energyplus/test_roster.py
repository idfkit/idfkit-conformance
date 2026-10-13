"""The roster's model and its thirteen validation rules. Each rule has a roster that breaks it."""

from __future__ import annotations

import pytest
from energyplus.check_roster import check, tally
from energyplus.roster import Form, Release, Roster, StepClass, Target
from support import APP, HEADER, LIB, NONE, ROSTER, VERIFICATIONS, VERSIONS

R = Release(26, 2, 0)


def test_releases_are_ordered() -> None:
    assert Release(9, 6, 0) < Release(25, 2, 0) < Release(26, 1, 0) < R


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        (Form.FULL, "26.2.0"),
        (Form.LINE, "26.2"),
        (Form.DASHED, "26-2-0"),
        (Form.DASHED_LINE, "26-2"),
        (Form.UNDERSCORED_LINE, "26_2"),
        (Form.TAG, "v26.2.0"),
        (Form.DIRECTORY, "V26-2-0"),
        (Form.TUPLE, "(26, 2, 0)"),
    ],
)
def test_the_eight_renderings(form: Form, expected: str) -> None:
    assert form.render(Target(R, 18)) == expected
    assert form.read(expected) == R
    assert R.substitute("x {" + form.placeholder() + "} y") == f"x {expected} y"


def test_the_methods_agree_with_the_forms() -> None:
    assert (R.full(), R.line(), R.dashed(), R.dashed_line()) == ("26.2.0", "26.2", "26-2-0", "26-2")
    assert (R.underscored_line(), R.tag(), R.directory(), R.tuple_literal()) == ("26_2", "v26.2.0", "V26-2-0", "(26, 2, 0)")


def test_counts_render_as_a_numeral_and_as_a_word() -> None:
    target = Target(R, 18)
    assert Form.COUNT.render(target) == "18"
    assert Form.COUNT_WORD.render(target) == "eighteen"
    assert Form.COUNT_WORD.rewrite("Seventeen", target) == "Eighteen"
    assert Form.COUNT.read("17") is None


def test_a_rewrite_keeps_the_delimiters_the_file_uses() -> None:
    assert Form.TUPLE.rewrite("26, 1, 0", Target(R, 0)) == "26, 2, 0"
    assert Form.TUPLE.rewrite("(26, 1, 0)", Target(R, 0)) == "(26, 2, 0)"


def test_parse_tag_accepts_only_a_final_release() -> None:
    assert Release.parse_tag("v26.2.0") == R
    for text in ("v26.2.0-RC3", "v26.1.0-LWRadTest2", "fix-ems-vars", "26.2.0", "v26.2", " v26.2.0"):
        assert Release.parse_tag(text) is None


def test_parse_accepts_an_optional_v() -> None:
    assert Release.parse("26.2.0") == Release.parse("v26.2.0") == R
    with pytest.raises(ValueError, match="26.2.0-RC3"):
        Release.parse("26.2.0-RC3")


def test_a_minimal_roster_parses_into_the_typed_model() -> None:
    roster = Roster.from_text(ROSTER)
    assert [project.id for project in roster.projects] == ["lib", "app", "auth"]
    assert [project.id for project in roster.rostered()] == ["lib", "app"]
    lib = roster.project("lib")
    assert lib.step("sweep-row").step_class is StepClass.REVIEWED
    assert lib.statements[0].form is Form.LINE
    assert roster.supported(VERSIONS) == (Release(25, 2, 0), Release(26, 1, 0))
    assert check(ROSTER) == []
    counted = tally(roster)
    assert (counted.with_content, counted.without_content, counted.steps) == (2, 1, 13)
    assert (counted.automated, counted.reviewed, counted.manual) == (11, 1, 1)


def _broken(old: str, new: str) -> str:
    assert old in ROSTER, old
    return ROSTER.replace(old, new, 1)


BREAKS = {
    "V1": _broken('id = "statements"', 'id = "registry"'),
    "V2": _broken('content = ["library-level"]', ""),
    "V3": _broken('class = "manual"', 'class = "by-hand"'),
    "V4": _broken('  inspect = "The findings count in the new row."\n', ""),
    "V5": _broken('  prepares = "The model written to disk."\n', ""),
    "V6": _broken('run = "echo {full}"', 'run = "npm publish --tag {full}"'),
    "V7": _broken('reason = "Fixtures name the release whose behaviour they assert."', 'reason = " "'),
    "V8": HEADER + LIB + '\n  [[project.generated]]\n  glob = "stubs.pyi"\n  by_step = "regenerate"\n' + APP + NONE + VERIFICATIONS,
    "V9": HEADER + LIB.replace('kind = "upstream-assets"\n  roles = ["linux-x86_64"]', 'kind = "project-complete"\n  project = "app"')
    + APP.replace('gate = "npm test"', 'gate = "npm test"\n\n  [[project.opens_on]]\n  kind = "merged"\n  project = "lib"\n  step = "registry"')
    + NONE
    + VERIFICATIONS,
    "V10": HEADER + LIB + APP + '\n  [project.lag]\n  kind = "deliberate"\n  reason = "It stays on 25.1.0 until the platform moves."\n' + NONE + VERIFICATIONS,
    "V11": _broken('inspect = "The model on disk."', 'inspect = "The model for 26.1.0 on disk."'),
    "V12": _broken("'through v(?P<release>\\d+\\.\\d+)'", "'through v(\\d+\\.\\d+)'"),
    "V13": _broken('id = "migrates"', 'id = "migrate"'),
}


@pytest.mark.parametrize("rule", [f"V{number}" for number in range(1, 14)])
def test_each_rule_rejects_the_roster_that_breaks_it(rule: str) -> None:
    failures = check(BREAKS[rule])
    assert failures, f"{rule} accepted a roster that breaks it"
    assert rule in {failure.rule for failure in failures}, [failure.stated() for failure in failures]
    assert all(failure.stated().startswith(failure.rule) for failure in failures)


def test_a_release_in_a_pin_reason_is_allowed() -> None:
    text = _broken('reason = "Fixtures name the release whose behaviour they assert."', 'reason = "Written for 26.1.0."')
    assert check(text) == []


def test_a_not_yet_lag_needs_an_issue() -> None:
    text = HEADER + LIB + APP + '\n  [project.lag]\n  kind = "not-yet"\n' + NONE + VERIFICATIONS
    assert {failure.rule for failure in check(text)} == {"V10"}
