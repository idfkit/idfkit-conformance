#!/usr/bin/env python3
"""seed_tier1.py - seed the Tier 1 expectation files for a case from the Python library.

READ THIS BEFORE USING IT.

Assertions 1 to 3 take their expectation from ConvertInputFormat, an authority outside both
libraries, and `regenerate.sh` says plainly that an expectation is never copied from a library.
Assertions 5 to 7 have no such authority. ConvertInputFormat does not validate, does not describe a
type, and does not build a documentation address, so nothing outside the two implementations can
rule on what they should produce.

What rules instead is written down: `runners/validate.md` for assertion 5, and the schema itself
for assertions 6 and 7. This script exists so that a maintainer does not hand-type a hundred lines
of JSON, and for no other reason. **The output is a draft.** Every entry must be read against the
rule that governs it before the file is committed, and an entry that disagrees with the rule means
the library is wrong, not the rule. Seeding from Python and then running the JavaScript runner is
not a check on the expectation: it is a check that the port is faithful, which is a different
question and the one assertion 5 to 7 exist to answer second.

This is a MAINTAINER task. CI never runs it.

Usage:
    tools/seed_tier1.py --library ../idfkit <case-id> [<case-id>...]

Writes cases/<id>/expected.validation.json, expected.introspection.json and
expected.docs-url.json, one per assertion the case declares.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
IDF_ENCODING = "latin-1"

# The corpus vocabulary is snake_case and belongs to neither library: Python spells these
# attributes this way and TypeScript spells them in camelCase, so writing the file in either
# library's spelling would make the corpus look like a transcript of that library.
FIELD_KEYS = (
    "name",
    "field_type",
    "required",
    "default",
    "units",
    "enum_values",
    "minimum",
    "maximum",
    "exclusive_minimum",
    "exclusive_maximum",
    "note",
    "is_reference",
    "object_list",
)


def _jsonable(value: Any) -> Any:
    """None becomes null. Every other value is already a JSON value or the seed is wrong."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise SystemExit(f"seed_tier1.py: {type(value).__name__} is not a JSON value: {value!r}")


def seed_validation(doc: Any) -> dict[str, Any]:
    """Findings as the corpus records them: no message, because message wording is not compared."""
    from idfkit.validation import validate_document

    result = validate_document(doc)
    findings = [
        {
            "object_type": issue.obj_type,
            "object_name": issue.obj_name,
            "field": issue.field,
            "code": issue.code,
            "severity": issue.severity.value,
        }
        for issue in (*result.errors, *result.warnings, *result.info)
    ]
    # Sorted for a readable diff. The comparison is an unordered multiset (compare.md rule 7), so
    # the order here carries no meaning and a reader must not infer one.
    findings.sort(key=lambda f: (f["object_type"], f["object_name"], f["field"] or "", f["code"]))
    return {"findings": findings}


def seed_introspection(doc: Any) -> dict[str, Any]:
    """One description per object type present in the document."""
    from idfkit.introspection import describe_object_type

    out: dict[str, Any] = {}
    for obj_type in sorted(doc.collections):
        described = describe_object_type(doc.schema, obj_type)
        out[obj_type] = {
            "obj_type": described.obj_type,
            "memo": _jsonable(described.memo),
            "has_name": described.has_name,
            "is_extensible": described.is_extensible,
            "extensible_size": _jsonable(described.extensible_size),
            "required_fields": list(described.required_fields),
            "fields": [{key: _jsonable(getattr(f, key)) for key in FIELD_KEYS} for f in described.fields],
        }
    return {"object_types": out}


def seed_docs_url(doc: Any) -> dict[str, Any]:
    """One address per object type present in the document, or null where none is built."""
    from idfkit.docs import docs_url_for_object

    out: dict[str, Any] = {}
    for obj_type in sorted(doc.collections):
        url = docs_url_for_object(obj_type, doc.version, doc.schema)
        out[obj_type] = (
            None
            if url is None
            else {"url": url.url, "doc_set": url.doc_set, "version": url.version, "label": url.label}
        )
    return {"object_types": out}


SEEDERS = {
    "validation": ("expected.validation.json", seed_validation),
    "introspection": ("expected.introspection.json", seed_introspection),
    "docs-url": ("expected.docs-url.json", seed_docs_url),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seed_tier1.py", description=__doc__)
    parser.add_argument("--library", required=True, type=Path, help="path to the idfkit checkout")
    parser.add_argument("cases", nargs="+", metavar="ID")
    args = parser.parse_args(argv)

    sys.path.insert(0, str((args.library / "src").resolve()))
    import idfkit

    for case_id in args.cases:
        case_dir = REPO_ROOT / "cases" / case_id
        declared = tomllib.loads((case_dir / "case.toml").read_text())["assertions"]
        doc = idfkit.parse_idf(case_dir / "input.idf", encoding=IDF_ENCODING)
        for assertion in declared:
            if assertion not in SEEDERS:
                continue
            filename, seeder = SEEDERS[assertion]
            path = case_dir / filename
            path.write_text(json.dumps(seeder(doc), indent=2, ensure_ascii=False) + "\n")
            print(f"seeded {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
