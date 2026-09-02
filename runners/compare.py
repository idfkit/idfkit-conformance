"""The value comparator, a direct transcription of ``runners/compare.md``.

``compare.md`` is normative and this module implements it, section by section, under the same
names. ``compare.mjs`` is the mirror of this file in JavaScript, and
``runners/tests/compare_fixtures.json`` is the shared table both are tested against. Nothing here
decides anything ``compare.md`` leaves unstated: a case that raises a question the rules do not
answer is an amendment to that file first, and a change to both comparators in the same change.

Where each rule lives:

* Rule 1, values not text: :func:`compare_values` walks parsed values. This module never calls
  ``json.dumps`` except in :func:`Difference.render`, which formats a report line and never decides
  a verdict.
* Rule 2, numbers: :func:`_numbers_equal`.
* Rule 3, object key order: :func:`_compare_object`.
* Rule 4, IDF field order: :func:`compare_documents`, which differs from :func:`compare_epjson`
  only in what the runner hands it.
* Rule 5, strings: :func:`_compare_scalar`, one ``!=``, no normalisation of any kind.
* Rule 6, encoding: the runner's, not this module's. Values arrive already decoded.
* Rule 7, unordered collections: :func:`compare_unordered`.

Orientation, fixed and never swapped: ``left`` is the library under test, ``right`` is the
expectation. A ``missing`` therefore always means the library omitted something.

Difference ordering, fixed so the two comparators can be diffed against each other: document order
of the left side, and keys absent from the left visited sorted by code point, after the left walk
of the object that holds them.

Two readings this module had to settle, recorded so a reviewer can overturn them in ``compare.md``
rather than in code:

* Rule 7 says unmatched elements are reported "under rule 3's missing and extra kinds", while the
  reporting table defines ``unmatched`` for exactly this situation. This module reads rule 3's
  kinds as governing object key sets, the first unordered collection, and ``unmatched`` as
  governing every other unordered collection, the second. The alternative leaves ``unmatched``
  unreachable in both comparators, which cannot be what a normative table means.
* Rule 2 makes NaN and the infinities always a difference. Rule 7 pairs unordered elements by
  exact equality, and exact equality would pair infinity with infinity. This module keeps rule 2:
  an element carrying NaN or an infinity anywhere inside it pairs with nothing, so it is reported
  on both sides.

Two portability hazards, neither reachable from the corpus as it stands, both left unhandled on
purpose so that handling them is a visible decision:

* JavaScript reorders integer-like object keys ahead of the rest, so an epJSON object named ``3``
  would be visited in a different order by the two comparators. The corpus has no such key.
* ``sorted`` orders by code point and JavaScript's default sort orders by UTF-16 code unit. The two
  agree below U+10000, and rule 6 confines the corpus to latin-1.

This module expects ``runners/`` on ``sys.path``, the same as ``run.py``, so that ``import model``
resolves.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from model import Assertion, Library, ParseOutcome

__all__ = [
    "ABSENT",
    "ROOT_POINTER",
    "TOLERANCE",
    "Absent",
    "AssertionReport",
    "Comparison",
    "ComparisonError",
    "Difference",
    "DifferenceKind",
    "JsonType",
    "compare_documents",
    "compare_epjson",
    "compare_outcome",
    "compare_unordered",
    "compare_values",
    "escape_token",
    "json_pointer",
]

# The relative tolerance of rule 2. There is no absolute floor, deliberately: exact zero is equal
# only to exact zero, because an unset field read as 0 instead of omitted is a real bug.
TOLERANCE: Final = 1e-12

# RFC 6901 names the whole document with the empty string.
ROOT_POINTER: Final = ""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DifferenceKind(str, Enum):
    """The six kinds in ``compare.md``'s reporting table, in the order that table lists them."""

    VALUE = "value"
    TYPE = "type"
    MISSING = "missing"
    EXTRA = "extra"
    LENGTH = "length"
    UNMATCHED = "unmatched"


class JsonType(str, Enum):
    """The six JSON types. A ``type`` difference is a disagreement between two of these.

    ``BOOLEAN`` is separate from ``NUMBER`` and ``STRING`` because rule 2 says so: ``true`` against
    ``1`` is a difference, and ``"3"`` against ``3`` is a difference.
    """

    NULL = "null"
    BOOLEAN = "boolean"
    NUMBER = "number"
    STRING = "string"
    ARRAY = "array"
    OBJECT = "object"


class Absent(Enum):
    """The marker for "there is no value at this path on this side".

    Rule 3 is explicit that absent is not ``null``, so the two cannot share a representation. A
    difference carrying ``ABSENT`` on one side is a ``missing``, an ``extra``, or an ``unmatched``.
    """

    TOKEN = "absent"

    def __repr__(self) -> str:
        return "<absent>"

    def __str__(self) -> str:
        return "<absent>"


ABSENT: Final = Absent.TOKEN


class ComparisonError(Exception):
    """A value reached the comparator that is not a parsed JSON value.

    Raised rather than reported, because it is a runner bug and not a finding about a library.
    """


# ---------------------------------------------------------------------------
# RFC 6901 JSON Pointers
# ---------------------------------------------------------------------------


def escape_token(token: str) -> str:
    """Escape one pointer token: ``~`` becomes ``~0`` and ``/`` becomes ``~1``, in that order."""
    return token.replace("~", "~0").replace("/", "~1")


def json_pointer(*tokens: str) -> str:
    """Build a pointer from tokens. No tokens is the document root, the empty string."""
    return "".join(f"/{escape_token(token)}" for token in tokens)


def _child(path: str, token: str) -> str:
    """The pointer to ``token`` inside the value at ``path``."""
    return f"{path}/{escape_token(token)}"


# ---------------------------------------------------------------------------
# The reported shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Difference:
    """One reported disagreement: a kind, a path, and both sides' values.

    ``left`` and ``right`` hold the values verbatim as parsed, never reformatted, so the exit
    contract's "the differing value" is the value the library actually produced. Either may be
    :data:`ABSENT`. Formatting happens in :meth:`render` and nowhere else.
    """

    kind: DifferenceKind
    path: str
    left: Any
    right: Any

    @property
    def left_is_absent(self) -> bool:
        """Whether the left side has no value at this path."""
        return isinstance(self.left, Absent)

    @property
    def right_is_absent(self) -> bool:
        """Whether the right side has no value at this path."""
        return isinstance(self.right, Absent)

    def render(self, *, max_value_length: int = 160) -> str:
        """One report line: the kind, the path to both sides, and both values.

        Truncating a long value here is a display choice and affects no verdict. The verbatim
        values stay on the dataclass.
        """
        location = self.path if self.path else "(document root)"
        left = _render_value(self.left, max_value_length)
        right = _render_value(self.right, max_value_length)
        return f"{self.kind.value} at {location}: left {left}, right {right}"


@dataclass(frozen=True, slots=True)
class Comparison:
    """Every difference between one pair of values, in the deterministic order of ``compare.md``.

    The comparator returns them all, never just the first. Truncation is the runner's choice, and
    :meth:`render` prints the total count whenever it truncates.
    """

    differences: tuple[Difference, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "differences", tuple(self.differences))

    @property
    def equal(self) -> bool:
        """Whether the two sides agree under every rule."""
        return not self.differences

    @property
    def count(self) -> int:
        """How many differences were found."""
        return len(self.differences)

    @property
    def first(self) -> Difference | None:
        """The first difference in document order, or ``None`` when the sides agree."""
        return self.differences[0] if self.differences else None

    def render(self, *, limit: int | None = None, max_value_length: int = 160) -> tuple[str, ...]:
        """Report lines, at most ``limit`` of them, with a total count appended when truncated."""
        shown = self.differences if limit is None else self.differences[:limit]
        lines = [difference.render(max_value_length=max_value_length) for difference in shown]
        if len(shown) < self.count:
            lines.append(f"... {self.count} differences in total, {len(shown)} shown")
        return tuple(lines)


@dataclass(frozen=True, slots=True)
class AssertionReport:
    """One assertion's result for one case and one library: everything the exit contract prints.

    The contract asks for the case id, the library, the assertion, the differing value, and the
    path to both sides. The first three are fields here, and the last two come from every
    :class:`Difference` in the comparison.
    """

    case_id: str
    library: Library
    assertion: Assertion
    comparison: Comparison

    @property
    def passed(self) -> bool:
        """Whether the assertion held."""
        return self.comparison.equal

    def render(self, *, limit: int | None = None, max_value_length: int = 160) -> tuple[str, ...]:
        """The failure block for this assertion, or a single pass line."""
        head = f"{self.case_id}: {self.library.value}: {self.assertion.value}"
        if self.passed:
            return (f"{head}: pass",)
        lines = self.comparison.render(limit=limit, max_value_length=max_value_length)
        return (
            f"{head}: {self.comparison.count} difference(s), left is {self.library.value}",
            *(f"  {line}" for line in lines),
        )


# ---------------------------------------------------------------------------
# Rule 1: compare parsed JSON values, never JSON text
# ---------------------------------------------------------------------------


def compare_values(left: Any, right: Any, *, path: str = ROOT_POINTER) -> Comparison:
    """Compare two parsed values under every rule in ``compare.md``.

    ``left`` is the library under test and ``right`` is the expectation. ``path`` is the pointer
    the two values already sit at, for a caller comparing a subtree.
    """
    differences: list[Difference] = []
    _compare(left, right, path, differences)
    return Comparison(tuple(differences))


def compare_epjson(left: Any, right: Any, *, path: str = ROOT_POINTER) -> Comparison:
    """Assertion 2: the library's canonical epJSON against ``expected.epJSON``.

    Rule 4 does not apply here. An epJSON field is a named key and its order is not observable, so
    rule 3 governs in full and comparing key order would test the oracle's key order, which is not
    a claim either library makes.
    """
    return compare_values(left, right, path=path)


def compare_documents(left: Any, right: Any, *, path: str = ROOT_POINTER) -> Comparison:
    """Assertion 3: a document re-parsed from the library's own IDF output against the original.

    Rule 4 applies here and only here: a parsed document retains, per object, the sequence of its
    fields as written, and that sequence is compared position by position, including trailing unset
    fields and the boundaries of extensible groups. This function enforces nothing extra to achieve
    that. It is the runner's job to hand over a value in which the field sequence is an array,
    because rule 7 makes every array ordered and compared index by index. A runner that flattened
    the field sequence into an object would throw the order away before the comparator ever saw it.
    """
    return compare_values(left, right, path=path)


def compare_outcome(left: ParseOutcome, right: ParseOutcome) -> Comparison:
    """Assertion 1: the declared parse outcome against the observed one.

    Assertion 1 compares an outcome, not a value, and uses no rule but rule 6, which the runner
    applies when it reads the input. It is here so both runners report through one shape.
    """
    if left is right:
        return Comparison()
    return Comparison((Difference(DifferenceKind.VALUE, ROOT_POINTER, left.value, right.value),))


def _compare(left: Any, right: Any, path: str, out: list[Difference]) -> None:
    """Dispatch on JSON type, then apply the rule for that type."""
    left_type = _json_type(left, path, "left")
    right_type = _json_type(right, path, "right")
    if left_type is not right_type:
        out.append(Difference(DifferenceKind.TYPE, path, left, right))
        return
    if left_type is JsonType.OBJECT:
        _compare_object(left, right, path, out)
    elif left_type is JsonType.ARRAY:
        _compare_array(left, right, path, out)
    else:
        _compare_scalar(left, right, left_type, path, out)


def _json_type(value: Any, path: str, side: str) -> JsonType:
    """The JSON type of a parsed value.

    ``bool`` is tested before ``int`` because Python makes ``True`` an integer and rule 2 does not.
    ``str`` is tested before ``Sequence`` because Python makes a string a sequence and JSON does
    not. ``bytes`` is not a JSON value: rule 6 says the runner decodes, so undecoded bytes reaching
    here are a runner bug.
    """
    if value is None:
        return JsonType.NULL
    if isinstance(value, bool):
        return JsonType.BOOLEAN
    if isinstance(value, (int, float)):
        return JsonType.NUMBER
    if isinstance(value, str):
        return JsonType.STRING
    if isinstance(value, Mapping):
        return JsonType.OBJECT
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return JsonType.ARRAY
    location = path if path else "(document root)"
    raise ComparisonError(
        f"{side} value at {location} is a {type(value).__name__}, which is not a parsed JSON value. "
        f"Rule 1 compares parsed values, so the runner parses before it compares"
    )


# ---------------------------------------------------------------------------
# Rules 2 and 5: scalars
# ---------------------------------------------------------------------------


def _compare_scalar(left: Any, right: Any, json_type: JsonType, path: str, out: list[Difference]) -> None:
    """Compare two scalars of the same JSON type."""
    if json_type is JsonType.NULL:
        return  # null equals null, and rule 3 keeps absent out of this branch entirely.
    if json_type is JsonType.NUMBER:
        if not _numbers_equal(left, right):
            out.append(Difference(DifferenceKind.VALUE, path, left, right))
        return
    # Rule 5 for strings, exact sequences of scalar values, no case folding, no trimming, no
    # collapsing of internal whitespace, no Unicode normalisation, no normalisation of line
    # endings. Booleans take the same single comparison.
    if left != right:
        out.append(Difference(DifferenceKind.VALUE, path, left, right))


def _numbers_equal(left: int | float, right: int | float) -> bool:
    """Rule 2. Integer against float is not a difference. The tolerance is relative only.

    NaN and the infinities are always differences, including NaN against NaN and infinity against
    infinity: JSON cannot carry them, so one reaching the comparator is itself the finding.
    """
    if not _is_finite(left) or not _is_finite(right):
        return False
    if left == right:
        # Covers integer against float and zero against zero, both exactly.
        return True
    try:
        return abs(left - right) <= TOLERANCE * max(abs(left), abs(right))
    except OverflowError:
        # An integer too large for a double, against a double it is not already equal to. No
        # relative tolerance can close that gap, so the two differ.
        return False


def _is_finite(value: int | float) -> bool:
    """Whether a number is neither NaN nor an infinity. Every Python ``int`` is finite."""
    if isinstance(value, int):
        return True
    return math.isfinite(value)


# ---------------------------------------------------------------------------
# Rule 3: object key order is not compared
# ---------------------------------------------------------------------------


def _compare_object(left: Mapping[Any, Any], right: Mapping[Any, Any], path: str, out: list[Difference]) -> None:
    """Compare two objects as key sets plus per-key values.

    Left keys are visited in document order, so a difference inside the left side is reported where
    it sits. Keys absent from the left are visited afterwards, sorted by code point, because
    document order cannot order what the document does not contain.
    """
    _check_keys(left, path, "left")
    _check_keys(right, path, "right")
    for key, value in left.items():
        child = _child(path, key)
        if key in right:
            _compare(value, right[key], child, out)
        else:
            out.append(Difference(DifferenceKind.EXTRA, child, value, ABSENT))
    for key in sorted(key for key in right if key not in left):
        out.append(Difference(DifferenceKind.MISSING, _child(path, key), ABSENT, right[key]))


def _check_keys(value: Mapping[Any, Any], path: str, side: str) -> None:
    """JSON object keys are strings. Anything else is a runner bug, not a finding."""
    for key in value:
        if not isinstance(key, str):
            location = path if path else "(document root)"
            raise ComparisonError(
                f"{side} object at {location} has a {type(key).__name__} key {key!r}. JSON object keys are strings"
            )


# ---------------------------------------------------------------------------
# Rule 7: arrays are ordered, unordered collections are compared unordered
# ---------------------------------------------------------------------------


def _compare_array(left: Sequence[Any], right: Sequence[Any], path: str, out: list[Difference]) -> None:
    """Compare two arrays index by index. Every JSON array is ordered.

    A length disagreement is one ``length`` difference at the array's own path, carrying both
    arrays verbatim, and the shared prefix is still compared so a value difference inside it is not
    hidden behind the length.
    """
    if len(left) != len(right):
        out.append(Difference(DifferenceKind.LENGTH, path, left, right))
    for index in range(min(len(left), len(right))):
        _compare(left[index], right[index], _child(path, str(index)), out)


def compare_unordered(left: Sequence[Any], right: Sequence[Any], *, path: str = ROOT_POINTER) -> Comparison:
    """Compare a collection whose structure has no defined order, as a multiset.

    ``compare.md`` lists exactly two unordered collections, and extending the list is an amendment
    to that file made before either comparator changes. Object key sets are the first, handled by
    rule 3 in :func:`_compare_object`. The assertion 4 diagnostic collection is the second, and
    this is where it will be compared once assertion 4 ships.

    A multiset, not a set: a duplicated element is a difference. Elements pair by exact equality of
    the whole element, so no tolerance is involved and the pairing is unambiguous. An element
    carrying NaN or an infinity pairs with nothing, because rule 2 makes those always a difference.
    Unmatched elements are reported individually, left side first in document order, then right.
    The path is the collection's own, since an element with no partner has no index on the other
    side to point at.
    """
    unpaired: dict[Any, list[int]] = {}
    for index, element in enumerate(right):
        key = _pairing_key(element, path)
        if key is None:
            continue
        unpaired.setdefault(key, []).append(index)

    matched: set[int] = set()
    differences: list[Difference] = []
    for element in left:
        key = _pairing_key(element, path)
        indices = unpaired.get(key) if key is not None else None
        if indices:
            matched.add(indices.pop(0))
        else:
            differences.append(Difference(DifferenceKind.UNMATCHED, path, element, ABSENT))
    for index, element in enumerate(right):
        if index not in matched:
            differences.append(Difference(DifferenceKind.UNMATCHED, path, ABSENT, element))
    return Comparison(tuple(differences))


def _pairing_key(value: Any, path: str) -> Any:
    """A hashable canonical form for unordered pairing, or ``None`` for an element that cannot pair.

    Numbers normalise to a double so that ``3`` pairs with ``3.0``, the same value JavaScript sees.
    Object keys sort, because rule 3 never compares key order. Arrays keep their order, because
    rule 7 keeps every array ordered. NaN and the infinities make the whole element unpairable,
    under rule 2.
    """
    json_type = _json_type(value, path, "element")
    if json_type is JsonType.NULL:
        return (JsonType.NULL,)
    if json_type is JsonType.BOOLEAN:
        return (JsonType.BOOLEAN, value)
    if json_type is JsonType.NUMBER:
        if not _is_finite(value):
            return None
        try:
            return (JsonType.NUMBER, float(value))
        except OverflowError:
            # An integer no double can represent has no counterpart in the other language.
            return None
    if json_type is JsonType.STRING:
        return (JsonType.STRING, value)
    if json_type is JsonType.ARRAY:
        elements = [_pairing_key(element, path) for element in value]
        if any(element is None for element in elements):
            return None
        return (JsonType.ARRAY, tuple(elements))
    _check_keys(value, path, "element")
    entries = []
    for key in sorted(value):
        member = _pairing_key(value[key], path)
        if member is None:
            return None
        entries.append((key, member))
    return (JsonType.OBJECT, tuple(entries))


# ---------------------------------------------------------------------------
# Rendering, which decides nothing
# ---------------------------------------------------------------------------


def _render_value(value: Any, limit: int) -> str:
    """A value as one report line's worth of text. Display only, never a verdict."""
    if isinstance(value, Absent):
        return "<absent>"
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - defensive, values are parsed JSON
        text = repr(value)
    if 0 < limit < len(text):
        return f"{text[:limit]}... ({len(text)} characters)"
    return text
