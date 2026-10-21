"""The ``update`` operation and the ``post-operation-epjson`` assertion, as ``model.py`` reads them.

``test-update.mjs`` holds the same tests against ``model.mjs``, test for test and in this order, so
that a rule enforced on one side and not the other fails one of the two files.

Run it from the root of the repository::

    python -m pytest runners/tests/test_update.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

RUNNERS: Final = Path(__file__).resolve().parents[1]
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

import pytest  # noqa: E402
from model import (  # noqa: E402
    EXPECTED_AFTER_EPJSON,
    Assertion,
    CaseError,
    ManifestError,
    Operation,
    OperationKind,
    OperationOutcome,
    load_case,
    load_manifest,
)

UPDATE: Final[dict[str, Any]] = {
    "op": "update",
    "type": "Zone",
    "name": "Office",
    "fields": {"x_origin": "10.0"},
}


def manifest_with(tmp_path: Path, operation: dict[str, Any], **entry: Any) -> Path:
    """A one-entry manifest in the convention section, carrying ``operation``."""
    raw = {
        "id": "update-example",
        "title": "An update",
        "tags": ["tier1"],
        "energyplus_version": "26.1.0",
        "assertions": ["parse-outcome", "post-operation-epjson"],
        "input": "input.idf",
        "parse_outcome": "success",
        "expected_after_epjson": EXPECTED_AFTER_EPJSON,
        "operations": [operation],
        **entry,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "oracle": [], "convention": [raw]}), encoding="utf-8")
    return path


def test_update_is_the_fifth_kind() -> None:
    assert OperationKind("update") is OperationKind.UPDATE
    assert len(OperationKind) == 5


def test_an_update_defaults_to_applied() -> None:
    operation = Operation(op=OperationKind.UPDATE, type="Zone", name="Office", fields={"x_origin": "1"})
    assert operation.outcome is OperationOutcome.APPLIED
    assert "outcome" not in operation.to_json_obj()


def test_a_refused_update_round_trips_its_outcome(tmp_path: Path) -> None:
    manifest = load_manifest(manifest_with(tmp_path, {**UPDATE, "outcome": "refused"}))
    (operation,) = manifest.convention[0].operations
    assert operation.outcome is OperationOutcome.REFUSED
    assert operation.to_json_obj() == {**UPDATE, "outcome": "refused"}


def test_an_update_may_carry_an_empty_group() -> None:
    operation = Operation(op=OperationKind.UPDATE, type="Zone", name="Office", fields={})
    assert operation.fields == {}


def test_a_field_value_may_be_a_list_of_groups(tmp_path: Path) -> None:
    group = {"vertex_x_coordinate": "0", "vertex_y_coordinate": "1", "vertex_z_coordinate": "2"}
    raw = {**UPDATE, "type": "BuildingSurface:Detailed", "name": "Wall", "fields": {"vertices": [group, group]}}
    (operation,) = load_manifest(manifest_with(tmp_path, raw)).convention[0].operations
    assert operation.fields == {"vertices": [group, group]}
    assert operation.to_json_obj() == raw


def test_a_group_member_must_be_text(tmp_path: Path) -> None:
    raw = {**UPDATE, "fields": {"vertices": [{"vertex_x_coordinate": 0}]}}
    with pytest.raises(ManifestError, match="must be a string"):
        load_manifest(manifest_with(tmp_path, raw))


def test_an_update_requires_fields() -> None:
    with pytest.raises(ManifestError, match="'update' requires 'fields'"):
        Operation(op=OperationKind.UPDATE, type="Zone", name="Office")


@pytest.mark.parametrize("key", ["field", "value", "to"])
def test_an_update_takes_no_single_field_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(ManifestError, match=repr(key)):
        load_manifest(manifest_with(tmp_path, {**UPDATE, key: "x"}))


def test_an_unknown_outcome_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="outcome"):
        load_manifest(manifest_with(tmp_path, {**UPDATE, "outcome": "partial"}))


def test_outcome_belongs_to_update_alone(tmp_path: Path) -> None:
    operation = {"op": "set-field", "type": "Zone", "name": "Office", "field": "x_origin", "value": "1", "outcome": "applied"}
    with pytest.raises(ManifestError, match="takes no 'outcome'"):
        load_manifest(manifest_with(tmp_path, operation))


def test_post_operation_epjson_requires_its_manifest_key(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="expected_after_epjson"):
        load_manifest(manifest_with(tmp_path, UPDATE, expected_after_epjson=None))


def test_a_convention_entry_may_name_the_post_operation_expectation(tmp_path: Path) -> None:
    entry = load_manifest(manifest_with(tmp_path, UPDATE)).convention[0]
    assert Assertion.POST_OPERATION_EPJSON in entry.assertions
    assert entry.expected_after_epjson == EXPECTED_AFTER_EPJSON
    assert entry.expected is None


def write_case(case_dir: Path) -> None:
    case_dir.mkdir()
    (case_dir / "case.toml").write_text(
        'title = "An update"\n'
        'why = "Because."\n'
        'tags = ["tier1"]\n'
        'energyplus_version = "26.1.0"\n'
        'truth = "convention"\n'
        'assertions = ["parse-outcome", "post-operation-epjson"]\n',
        encoding="utf-8",
    )


def test_a_case_declaring_post_operation_epjson_needs_the_file(tmp_path: Path) -> None:
    case_dir = tmp_path / "update-example"
    write_case(case_dir)
    with pytest.raises(CaseError, match=EXPECTED_AFTER_EPJSON):
        load_case(case_dir)
    (case_dir / EXPECTED_AFTER_EPJSON).write_text("{}", encoding="utf-8")
    assert load_case(case_dir).truth.value == "convention"
