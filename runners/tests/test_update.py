"""The ``update`` operation and the ``post-operation-epjson`` assertion, as ``model.py`` reads them.

``test-update.mjs`` holds the same tests against ``model.mjs``, test for test and in this order, so
that a rule enforced on one side and not the other fails one of the two files.

Run it from the root of the repository::

    python -m pytest runners/tests/test_update.py
"""

from __future__ import annotations

import json
import pickle
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

RUNNERS: Final = Path(__file__).resolve().parents[1]
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

import pytest  # noqa: E402
from model import (  # noqa: E402
    AFTER_IDF,
    EXPECTED_AFTER_EPJSON,
    Assertion,
    CaseError,
    GroupValue,
    ManifestError,
    Operation,
    OperationKind,
    OperationOutcome,
    load_case,
    load_corpus,
    load_manifest,
)
from run import _coerced_fields, _OutcomeMismatch, _require_named  # noqa: E402

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
    raw = {**UPDATE, "outcome": "refused", "refused": ["x_origin"]}
    manifest = load_manifest(manifest_with(tmp_path, raw))
    (operation,) = manifest.convention[0].operations
    assert operation.outcome is OperationOutcome.REFUSED
    assert operation.refused == ("x_origin",)
    assert operation.to_json_obj() == raw


def test_a_refused_update_must_name_what_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="requires 'refused'"):
        load_manifest(manifest_with(tmp_path, {**UPDATE, "outcome": "refused"}))


def test_refused_belongs_to_a_refused_update_alone(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="'refused' belongs only"):
        load_manifest(manifest_with(tmp_path, {**UPDATE, "refused": ["x_origin"]}))


def test_refused_names_only_entries_of_the_group(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="does not contain"):
        load_manifest(manifest_with(tmp_path, {**UPDATE, "outcome": "refused", "refused": ["y_origin"]}))


def test_an_update_may_carry_an_empty_group() -> None:
    operation = Operation(op=OperationKind.UPDATE, type="Zone", name="Office", fields={})
    assert operation.fields == {}


def test_a_field_value_may_be_a_list_of_groups(tmp_path: Path) -> None:
    group = {"vertex_x_coordinate": "0", "vertex_y_coordinate": "1", "vertex_z_coordinate": "2"}
    raw = {**UPDATE, "type": "BuildingSurface:Detailed", "name": "Wall", "fields": {"vertices": [group, group]}}
    (operation,) = load_manifest(manifest_with(tmp_path, raw)).convention[0].operations
    assert operation.fields == {"vertices": (GroupValue(group), GroupValue(group))}
    assert operation.to_json_obj() == raw


def test_an_operation_is_frozen_all_the_way_down(tmp_path: Path) -> None:
    group = {"vertex_x_coordinate": "0"}
    raw = {**UPDATE, "fields": {"x_origin": "1", "vertices": [group]}}
    (operation,) = load_manifest(manifest_with(tmp_path, raw)).convention[0].operations
    assert operation.fields is not None
    vertices = operation.fields["vertices"]
    assert isinstance(vertices, tuple)
    with pytest.raises(TypeError):
        operation.fields["x_origin"] = "2"  # type: ignore[index]
    with pytest.raises(TypeError):
        vertices[0].members["vertex_x_coordinate"] = "9"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        vertices[0].members = {}  # type: ignore[misc]
    # The runner hands each case to a worker process, so the frozen form must pickle.
    assert pickle.loads(pickle.dumps(operation)) == operation


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


def write_corpus(
    root: Path, operation: dict[str, Any], *, after: str | None, assertions: tuple[str, ...] = ("parse-outcome", "post-operation-epjson")
) -> Path:
    """A one-case corpus whose input is ``Version,26.1;`` and whose after.idf is ``after``."""
    entry: dict[str, Any] = {
        "id": "update-example",
        "title": "An update",
        "tags": ["tier1"],
        "energyplus_version": "26.1.0",
        "assertions": list(assertions),
        "input": "input.idf",
        "parse_outcome": "success",
        "operations": [operation],
    }
    if "post-operation-epjson" in assertions:
        entry["expected_after_epjson"] = EXPECTED_AFTER_EPJSON
    (root / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "oracle": [], "convention": [entry]}), encoding="utf-8"
    )
    case_dir = root / "cases" / "update-example"
    case_dir.mkdir(parents=True)
    (case_dir / "case.toml").write_text(
        'title = "An update"\n'
        'why = "Because."\n'
        'tags = ["tier1"]\n'
        'energyplus_version = "26.1.0"\n'
        'truth = "convention"\n'
        f"assertions = {json.dumps(list(assertions))}\n",
        encoding="utf-8",
    )
    (case_dir / "input.idf").write_text("Version,26.1;\n", encoding="utf-8")
    if "post-operation-epjson" in assertions:
        (case_dir / EXPECTED_AFTER_EPJSON).write_text("{}", encoding="utf-8")
    if after is not None:
        (case_dir / AFTER_IDF).write_text(after, encoding="utf-8")
    return root


REFUSED: Final[dict[str, Any]] = {**UPDATE, "outcome": "refused", "refused": ["x_origin"]}


def test_a_refused_case_after_idf_must_equal_its_input(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="must equal the input"):
        load_corpus(write_corpus(tmp_path, REFUSED, after="Version,26.2;\n"), require_divergences=False)


def test_an_applied_case_after_idf_must_differ_from_its_input(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="must differ from the input"):
        load_corpus(write_corpus(tmp_path, UPDATE, after="Version,26.1;\n"), require_divergences=False)


def test_a_post_operation_case_needs_its_after_idf(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match=AFTER_IDF):
        load_corpus(write_corpus(tmp_path, UPDATE, after=None), require_divergences=False)


def test_an_update_needs_the_post_operation_assertion(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="without 'post-operation-epjson'"):
        load_corpus(
            write_corpus(tmp_path, UPDATE, after=None, assertions=("parse-outcome",)), require_divergences=False
        )


def test_consistent_after_idf_loads(tmp_path: Path) -> None:
    load_corpus(write_corpus(tmp_path, REFUSED, after="Version,26.1;\n"), require_divergences=False)


def refused_operation(*entries: str) -> Operation:
    return Operation(
        op=OperationKind.UPDATE,
        type="Zone",
        name="Office",
        fields={entry: "1" for entry in entries},
        outcome=OperationOutcome.REFUSED,
        refused=entries,
    )


def test_a_refusal_naming_every_entry_is_accepted() -> None:
    error = ValueError(
        "Update of object type 'Zone' refused; no field was changed:\n"
        "  - x_orign: unknown (not a field of Zone)\n"
        "  - name: name (use rename)"
    )
    _require_named(refused_operation("x_orign", "name"), error)


@pytest.mark.parametrize(
    "message",
    [
        '"x_orign" is not a field of Zone',
        "Update of object type 'Zone' refused; no field was changed:\n  - name: name (use rename)",
        "Update refused: x_orign: unknown",
    ],
)
def test_a_refusal_that_does_not_name_an_entry_is_a_mismatch(message: str) -> None:
    with pytest.raises(_OutcomeMismatch, match="names no refused entry"):
        _require_named(refused_operation("x_orign", "name"), ValueError(message))


def test_a_field_described_without_a_type_is_left_as_text() -> None:
    # Python only: the second language reads a schema type code, which is never absent.
    described = [
        SimpleNamespace(name="x_origin", field_type="number"),
        SimpleNamespace(name="sizing", field_type="number|string"),
        SimpleNamespace(name="note", field_type=None),
    ]
    document = SimpleNamespace(describe=lambda _type: SimpleNamespace(fields=described))
    fields = {"x_origin": "1.5", "sizing": "Autosize", "note": "3"}
    assert _coerced_fields(document, "Zone", fields) == {"x_origin": 1.5, "sizing": "Autosize", "note": "3"}
