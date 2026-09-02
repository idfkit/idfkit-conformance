"""Drive ``runners/compare.py`` over the shared fixture table.

``compare_fixtures.json`` is the contract between the two comparators: this file asserts that the
Python one returns exactly the verdicts the table records, and the JavaScript harness asserts the
same of ``compare.mjs``. Both harnesses decode the table the same way, so a fixture added here
constrains both implementations.

Run it from the root of the repository::

    python -m pytest runners/tests/test_compare.py
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

RUNNERS = Path(__file__).resolve().parents[1]
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

import compare  # noqa: E402
import pytest  # noqa: E402
from compare import Comparison, Difference, DifferenceKind  # noqa: E402
from model import Assertion, Library, ParseOutcome  # noqa: E402

FIXTURE_FILE: Final = Path(__file__).resolve().parent / "compare_fixtures.json"

# Every rule in compare.md that a fixture can exercise. Rule 6, the encoding, belongs to the
# runner: values reach the comparator already decoded, so the fixtures pin only that a decoded high
# byte compares as itself.
RULES: Final = frozenset({1, 2, 3, 4, 5, 6, 7})


class Mode(str, Enum):
    """Which comparator a fixture drives."""

    VALUES = "values"
    UNORDERED = "unordered"


@dataclass(frozen=True, slots=True)
class Fixture:
    """One row of the shared table, decoded."""

    id: str
    rules: tuple[int, ...]
    mode: Mode
    path: str
    why: str
    left: Any
    right: Any
    expect: tuple[Difference, ...]


def _load_fixtures() -> tuple[tuple[Fixture, ...], str]:
    """Read the table, decode the special values, and build a :class:`Fixture` per row."""
    document = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
    special_key = document["special_key"]
    specials: dict[str, Any] = {
        "nan": math.nan,
        "infinity": math.inf,
        "-infinity": -math.inf,
        "absent": compare.ABSENT,
    }
    assert sorted(document["specials"]) == sorted(specials), "the table declares a special this harness cannot decode"

    def decode(value: Any) -> Any:
        if isinstance(value, dict):
            if len(value) == 1 and special_key in value:
                name = value[special_key]
                if name not in specials:
                    raise AssertionError(f"unknown special {name!r}")
                return specials[name]
            return {key: decode(member) for key, member in value.items()}
        if isinstance(value, list):
            return [decode(member) for member in value]
        return value

    fixtures = tuple(
        Fixture(
            id=row["id"],
            rules=tuple(row["rules"]),
            mode=Mode(row["mode"]),
            path=row.get("path", ""),
            why=row["why"],
            left=decode(row["left"]),
            right=decode(row["right"]),
            expect=tuple(
                Difference(
                    kind=DifferenceKind(entry["kind"]),
                    path=entry["path"],
                    left=decode(entry["left"]),
                    right=decode(entry["right"]),
                )
                for entry in row["expect"]
            ),
        )
        for row in document["fixtures"]
    )
    return fixtures, special_key


FIXTURES, SPECIAL_KEY = _load_fixtures()


def _same(left: Any, right: Any) -> bool:
    """Exact structural equality, for checking a verdict against the table.

    This is not the comparator's equality and must not become it: no tolerance, and NaN matches
    NaN, because the table records the NaN a comparator reported verbatim.
    """
    if isinstance(left, compare.Absent) or isinstance(right, compare.Absent):
        return left is right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    return False


def _run(fixture: Fixture) -> Comparison:
    """Drive the comparator the fixture names."""
    if fixture.mode is Mode.VALUES:
        return compare.compare_values(fixture.left, fixture.right, path=fixture.path)
    return compare.compare_unordered(fixture.left, fixture.right, path=fixture.path)


def _shape(differences: tuple[Difference, ...]) -> list[tuple[str, str]]:
    """Kinds and paths only, so a failure reads before the values are inspected."""
    return [(difference.kind.value, difference.path) for difference in differences]


# ---------------------------------------------------------------------------
# The shared table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURES, ids=[fixture.id for fixture in FIXTURES])
def test_fixture_verdict(fixture: Fixture) -> None:
    """The comparator returns exactly the differences the table records, in the table's order."""
    result = _run(fixture)
    assert _shape(result.differences) == _shape(fixture.expect), fixture.why
    for observed, expected in zip(result.differences, fixture.expect, strict=True):
        assert _same(observed.left, expected.left), f"{fixture.id}: left value at {expected.path}"
        assert _same(observed.right, expected.right), f"{fixture.id}: right value at {expected.path}"
    assert result.equal is (not fixture.expect)
    assert result.count == len(fixture.expect)


def test_fixture_ids_are_unique() -> None:
    """A duplicated id would let one fixture silently replace another in a report."""
    ids = [fixture.id for fixture in FIXTURES]
    assert len(ids) == len(set(ids))


def test_every_difference_kind_is_pinned() -> None:
    """A kind no fixture produces is a kind the two comparators can disagree about."""
    produced = {difference.kind for fixture in FIXTURES for difference in fixture.expect}
    assert produced == set(DifferenceKind)


def test_every_rule_is_pinned() -> None:
    """Every section of compare.md is exercised by at least one fixture."""
    covered = {rule for fixture in FIXTURES for rule in fixture.rules}
    assert covered == RULES


def test_both_modes_are_pinned() -> None:
    """Both entry points are covered, so neither comparator can ship half the contract."""
    modes = {fixture.mode for fixture in FIXTURES}
    assert modes == set(Mode)


def test_the_float_repr_case_is_pinned() -> None:
    """The case compare.md cites by name stays in the table, whatever else is edited around it."""
    fixture = next(item for item in FIXTURES if item.id == "float-repr-noise-is-not-a-difference")
    assert fixture.expect == ()
    assert sorted(fixture.left.values()) == [0.0, 1e-05, 3.0]
    assert sorted(fixture.right.values()) == [0, 1e-05, 3]


def test_no_fixture_uses_the_special_key_literally() -> None:
    """The special encoding is only readable while no fixture writes that key for real."""
    text = FIXTURE_FILE.read_text(encoding="utf-8")
    document = json.loads(text)
    names = set(document["specials"])

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if SPECIAL_KEY in value:
                assert len(value) == 1 and value[SPECIAL_KEY] in names, f"literal {SPECIAL_KEY} key in the table"
            for member in value.values():
                walk(member)
        elif isinstance(value, list):
            for member in value:
                walk(member)

    for row in document["fixtures"]:
        walk(row["left"])
        walk(row["right"])
        walk(row["expect"])


# ---------------------------------------------------------------------------
# The pieces the table cannot reach
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "escaped"),
    [("a", "a"), ("a/b", "a~1b"), ("m~n", "m~0n"), ("~/", "~0~1"), ("", ""), ("Zone 1", "Zone 1")],
)
def test_escape_token(token: str, escaped: str) -> None:
    """RFC 6901 escaping, tilde before slash, so ``~1`` never turns into ``~01``."""
    assert compare.escape_token(token) == escaped


def test_json_pointer() -> None:
    """A pointer is the tokens, each escaped and each preceded by a slash. No tokens is the root."""
    assert compare.json_pointer() == compare.ROOT_POINTER == ""
    assert compare.json_pointer("Zone", "Zone 1", "ceiling_height") == "/Zone/Zone 1/ceiling_height"
    assert compare.json_pointer("a/b") == "/a~1b"


def test_tolerance_is_the_documented_one() -> None:
    """The number in compare.md, not a number near it."""
    assert compare.TOLERANCE == 1e-12


def test_difference_render_carries_the_path_and_both_values() -> None:
    """The exit contract prints the differing value and the path to both sides."""
    difference = Difference(DifferenceKind.VALUE, "/Zone/Zone 1/ceiling_height", 3.0, 2.4)
    line = difference.render()
    assert "/Zone/Zone 1/ceiling_height" in line
    assert "3.0" in line
    assert "2.4" in line
    assert line.startswith("value at ")


def test_difference_render_names_the_root_and_the_absent_side() -> None:
    """An empty pointer and an absent value both have to read as something."""
    difference = Difference(DifferenceKind.MISSING, "", compare.ABSENT, 1)
    line = difference.render()
    assert "(document root)" in line
    assert "<absent>" in line
    assert difference.left_is_absent and not difference.right_is_absent


def test_difference_render_truncates_long_values_only_in_the_line() -> None:
    """Truncation is a display choice. The dataclass keeps the value verbatim."""
    value = ["x" * 40] * 20
    difference = Difference(DifferenceKind.LENGTH, "/v", value, [])
    line = difference.render(max_value_length=40)
    assert "characters)" in line
    assert difference.left is value


def test_comparison_render_reports_the_total_when_it_truncates() -> None:
    """The runner may truncate what it prints, and must print the total count when it does."""
    result = compare.compare_values({"a": 1, "b": 2, "c": 3}, {"a": 9, "b": 9, "c": 9})
    assert result.count == 3
    lines = result.render(limit=2)
    assert len(lines) == 3
    assert lines[-1] == "... 3 differences in total, 2 shown"
    assert result.render() == tuple(difference.render() for difference in result.differences)


def test_comparison_first_is_the_first_in_document_order() -> None:
    """``first`` is a convenience over the same ordering, never a second ordering."""
    assert compare.Comparison().first is None
    result = compare.compare_values({"b": 1, "a": 1}, {"b": 2, "a": 2})
    assert result.first is not None
    assert result.first.path == "/b"


def test_assertion_report_prints_the_whole_exit_contract() -> None:
    """Case id, library, assertion, the differing value, and the path to both sides."""
    result = compare.compare_values(
        {"Zone": {"Zone 1": {"ceiling_height": 3.0}}},
        {"Zone": {"Zone 1": {"ceiling_height": 2.4}}},
    )
    report = compare.AssertionReport(
        case_id="numeric-zero-against-absent",
        library=Library.PYTHON,
        assertion=Assertion.EPJSON,
        comparison=result,
    )
    assert not report.passed
    text = "\n".join(report.render())
    assert "numeric-zero-against-absent" in text
    assert "python" in text
    assert "epjson" in text
    assert "/Zone/Zone 1/ceiling_height" in text
    assert "3.0" in text and "2.4" in text


def test_assertion_report_pass_is_one_line() -> None:
    """A passing assertion says so and says nothing else."""
    report = compare.AssertionReport(
        case_id="naming-blank-vs-absent",
        library=Library.TYPESCRIPT,
        assertion=Assertion.ROUND_TRIP,
        comparison=compare.Comparison(),
    )
    assert report.passed
    assert report.render() == ("naming-blank-vs-absent: typescript: round-trip: pass",)


def test_compare_outcome() -> None:
    """Assertion 1 compares an outcome, not a value, and reports through the same shape."""
    assert compare.compare_outcome(ParseOutcome.SUCCESS, ParseOutcome.SUCCESS).equal
    result = compare.compare_outcome(ParseOutcome.SUCCESS, ParseOutcome.FAILURE)
    assert _shape(result.differences) == [("value", "")]
    assert result.differences[0].left == "success"
    assert result.differences[0].right == "failure"


def test_epjson_and_documents_are_the_same_comparison() -> None:
    """Rule 4 lives in what the runner hands over, not in a second set of rules."""
    left = {"Zone": [{"name": "Zone 1"}, {"name": None}]}
    right = {"Zone": [{"name": "Zone 1"}]}
    assert compare.compare_epjson(left, right).differences == compare.compare_documents(left, right).differences


def test_a_value_that_is_not_parsed_json_raises() -> None:
    """A runner bug is raised, never reported as a finding about a library."""
    with pytest.raises(compare.ComparisonError) as raised:
        compare.compare_values({"a": b"bytes"}, {"a": "bytes"})
    assert "/a" in str(raised.value)

    with pytest.raises(compare.ComparisonError):
        compare.compare_values({1: "a"}, {1: "a"})


def test_deeply_nested_values_are_compared_all_the_way_down() -> None:
    """Recursion is not depth limited, and a difference at the bottom is still reported."""
    depth = 40
    left: Any = 1
    right: Any = 2
    for _ in range(depth):
        left = {"child": left}
        right = {"child": right}
    result = compare.compare_values(left, right)
    assert _shape(result.differences) == [("value", "/child" * depth)]
