"""The change summary, from two small schemas. A rename is never inferred."""

from __future__ import annotations

import json

from energyplus.changes import FieldChange, Rename, compare, renamed_variables, summarise
from energyplus.roster import Release
from support import UPSTREAM

OLD, NEW = Release(26, 1, 0), Release(26, 2, 0)


def _schema(types: dict[str, list[str]]) -> str:
    return json.dumps({"properties": {name: {"patternProperties": {".*": {"properties": dict.fromkeys(fields, {})}}} for name, fields in types.items()}})


BEFORE = _schema({"Zone": ["x_origin", "volume"], "Lights": ["watts"], "Material": ["roughness", "thickness"]})
AFTER = _schema({"Zone": ["x_origin", "volume", "floor_area"], "Lights:Definition": ["watts"], "Material": ["roughness", "depth"]})


def test_object_types_added_and_removed() -> None:
    summary = compare(OLD, NEW, BEFORE, AFTER)
    assert (summary.types_added, summary.types_removed) == (("Lights:Definition",), ("Lights",))
    assert (summary.previous, summary.release) == (OLD, NEW)


def test_fields_added_and_removed_within_a_shared_type() -> None:
    summary = compare(OLD, NEW, BEFORE, AFTER)
    assert summary.fields_added == (FieldChange("Material", "depth"), FieldChange("Zone", "floor_area"))
    assert summary.fields_removed == (FieldChange("Material", "thickness"),)


def test_a_rename_is_one_removal_and_one_addition_and_is_never_inferred() -> None:
    summary = compare(OLD, NEW, BEFORE, AFTER)
    assert FieldChange("Material", "thickness") in summary.fields_removed and FieldChange("Material", "depth") in summary.fields_added
    assert summary.variables_renamed == ()
    assert not hasattr(summary, "fields_renamed") and not hasattr(summary, "types_renamed")


def test_identical_schemas_have_no_changes() -> None:
    summary = compare(OLD, NEW, BEFORE, BEFORE)
    assert (summary.types_added, summary.types_removed, summary.fields_added, summary.fields_removed) == ((), (), (), ())


def test_renamed_output_variables_are_read_from_the_transition_table() -> None:
    table = "26.1.0,26.2.0,Transition notes\n7\nold,new\nZone Air Chiller Rate,Zone Air Chiller Coil Rate,\nKept Name,Kept Name,\n! a comment,x\n"
    assert renamed_variables(table) == (Rename("Zone Air Chiller Rate", "Zone Air Chiller Coil Rate"),)


class _Fetch:
    def __init__(self, table: str | None, schema: str | None) -> None:
        self.table, self.schema, self.asked = table, schema, []

    def text(self, url: str) -> str | None:
        self.asked.append(url)
        return self.table

    def schema_from_archive(self, url: str) -> str | None:
        self.asked.append(url)
        return self.schema


def test_the_new_schema_comes_from_the_release_archive_when_the_library_bundles_none() -> None:
    assert UPSTREAM is not None
    fetch = _Fetch("A,B\n", AFTER)
    summary = summarise(OLD, UPSTREAM, BEFORE, None, fetch, "up/EnergyPlus")
    assert summary.types_added == ("Lights:Definition",) and summary.variables_renamed == (Rename("A", "B"),) and summary.notes == ()
    assert fetch.asked[0].endswith("EnergyPlus-26.2.0-4bd7a1f26f-Linux-Ubuntu24.04-x86_64.tar.gz")
    assert fetch.asked[1] == "https://raw.githubusercontent.com/up/EnergyPlus/v26.2.0/src/Transition/SupportFiles/Report%20Variables%2026-1-0%20to%2026-2-0.csv"


def test_a_part_that_could_not_be_computed_is_stated() -> None:
    assert UPSTREAM is not None
    summary = summarise(OLD, UPSTREAM, None, None, _Fetch(None, None), "up/EnergyPlus")
    assert len(summary.notes) == 2 and "could not be read" in summary.notes[0]
