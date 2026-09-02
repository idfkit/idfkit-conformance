"""Typed shapes for the idfkit conformance corpus.

Three files carry the corpus structure, and this module is the single Python definition of all
three:

* ``cases/<id>/case.toml``  becomes a :class:`Case`
* ``manifest.json``         becomes a :class:`Manifest` of :class:`ManifestEntry`
* ``known-divergence.toml`` becomes a :class:`DivergenceRegister` of :class:`Divergence`

Every shape is a frozen dataclass and every enumerated field is a real enum, so a runner never
indexes into a bare dict and never compares a bare string against a spelling it hopes is correct.
The loaders below are the only place where untyped JSON and TOML crosses into the model, and they
raise :class:`CorpusError` naming the offending file and field on any rule the file violates.

``manifest.schema.json`` stays the normative schema for ``manifest.json``. This module mirrors it
field for field and adds the two constraints JSON Schema cannot express: case-id uniqueness across
the two sections, and agreement between a manifest entry and the case's own ``case.toml``.

There is deliberately no ``truth`` field in a manifest entry on disk: the section an entry sits in
is the truth value (FR-020). :class:`ManifestEntry` carries ``truth`` in memory, derived from the
section it was read from, and never serializes it back.

Python version: the corpus README requires Python 3.10 or newer. ``tomllib`` entered the standard
library in 3.11, so on 3.10 this module falls back to ``tomli``, the same parser under its
pre-standardisation name.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final, NoReturn, TypeVar

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10 has no tomllib. tomli is the same parser, pre-standardisation.
    import tomli as tomllib

__all__ = [
    "Assertion",
    "Case",
    "CaseError",
    "Corpus",
    "CorpusError",
    "Divergence",
    "DivergenceError",
    "DivergenceRegister",
    "InputFile",
    "Library",
    "Manifest",
    "ManifestEntry",
    "ManifestError",
    "ParseOutcome",
    "Tag",
    "Truth",
    "dump_manifest",
    "load_case",
    "load_corpus",
    "load_divergences",
    "load_manifest",
]

# ---------------------------------------------------------------------------
# Constants and patterns, mirroring manifest.schema.json
# ---------------------------------------------------------------------------

MANIFEST_SCHEMA_VERSION: Final = 1
MANIFEST_SCHEMA_REF: Final = "./manifest.schema.json"

CASE_FILE: Final = "case.toml"
EXPECTED_EPJSON: Final = "expected.epJSON"
EXPECTED_DIAGNOSTICS: Final = "expected.diag.json"
EXPECTED_VALIDATION: Final = "expected.validation.json"
EXPECTED_INTROSPECTION: Final = "expected.introspection.json"
EXPECTED_DOCS_URL: Final = "expected.docs-url.json"

CASES_DIR: Final = "cases"
MANIFEST_FILE: Final = "manifest.json"
DIVERGENCE_FILE: Final = "known-divergence.toml"

CASE_ID_PATTERN: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CASE_ID_MAX_LENGTH: Final = 64
ENERGYPLUS_VERSION_PATTERN: Final = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
CORPUS_LEVEL_PATTERN: Final = re.compile(r"^conformance-[0-9]{4}\.[0-9]+$")
ISSUE_URL_PATTERN: Final = re.compile(r"^https?://\S+$")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Truth(str, Enum):
    """Where a case's expectation comes from (FR-020).

    ``ORACLE`` means EnergyPlus ConvertInputFormat produced it and it is committed. ``CONVENTION``
    means the two libraries agreed on it and no external authority ruled, so no expectation file
    may exist. The manifest keeps the two in separate sections precisely so the second is never
    read as the first.
    """

    ORACLE = "oracle"
    CONVENTION = "convention"


class Assertion(str, Enum):
    """The assertions a case may declare. ``DIAGNOSTICS`` is accepted and skipped until phase two.

    ``VALIDATION``, ``INTROSPECTION`` and ``DOCS_URL`` are the Tier 1 assertions: they cover the
    three capabilities ported into the JavaScript library under this feature, and ``compare.md``
    states plainly that none of them has an oracle behind it.
    """

    PARSE_OUTCOME = "parse-outcome"
    EPJSON = "epjson"
    ROUND_TRIP = "round-trip"
    DIAGNOSTICS = "diagnostics"
    VALIDATION = "validation"
    INTROSPECTION = "introspection"
    DOCS_URL = "docs-url"


class Tag(str, Enum):
    """The hazard taxonomy. Cases are grouped by the hazard they pin, never by feature area (FR-021)."""

    POSITIONAL = "positional"
    NAMING = "naming"
    EXTENSIBLE = "extensible"
    NUMERIC = "numeric"
    TYPES = "types"
    REFERENCES = "references"
    VERSIONS = "versions"
    ENCODING = "encoding"
    MALFORMED = "malformed"
    TIER1 = "tier1"


# The assertion that needs an expectation file, the manifest key that names it, and the file it
# names. One table rather than four hand-written rules, so that adding an assertion cannot leave one
# of the four checks behind. ``EPJSON``'s expectation is ``expected``, which is governed by ``truth``
# rather than by an assertion, and is deliberately not in here.
EXPECTATION_FILES: Final[dict[str, tuple[str, str]]] = {
    "diagnostics": ("expected_diagnostics", EXPECTED_DIAGNOSTICS),
    "validation": ("expected_validation", EXPECTED_VALIDATION),
    "introspection": ("expected_introspection", EXPECTED_INTROSPECTION),
    "docs-url": ("expected_docs_url", EXPECTED_DOCS_URL),
}


class InputFile(str, Enum):
    """The case input. The extension is what the runner dispatches its reader on."""

    IDF = "input.idf"
    EPJSON = "input.epJSON"

    @property
    def suffix(self) -> str:
        """The extension the runner dispatches on, including the dot."""
        return self.value[self.value.rindex(".") :]


class ParseOutcome(str, Enum):
    """What reading the input is declared to do. The ``parse-outcome`` assertion checks this."""

    SUCCESS = "success"
    FAILURE = "failure"


class Library(str, Enum):
    """The two implementations under test."""

    PYTHON = "python"
    TYPESCRIPT = "typescript"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CorpusError(Exception):
    """A corpus file violates a rule this module enforces."""


class CaseError(CorpusError):
    """A ``case.toml``, or the case directory around it, is malformed."""


class ManifestError(CorpusError):
    """``manifest.json`` is malformed, or disagrees with a case it indexes."""


class DivergenceError(CorpusError):
    """``known-divergence.toml`` is malformed, or names a case that does not exist."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One ``cases/<id>/case.toml``, plus the id taken from the directory name.

    ``why`` is not optional: a case whose reason is unrecorded cannot be maintained.
    """

    id: str
    title: str
    why: str
    tags: tuple[Tag, ...]
    energyplus_version: str
    truth: Truth
    assertions: tuple[Assertion, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "assertions", tuple(self.assertions))
        _check_case_id(self.id, CaseError)
        _check_non_empty(self.title, "title", CaseError)
        _check_non_empty(self.why, "why", CaseError)
        _check_unique_non_empty(self.tags, "tags", CaseError)
        _check_unique_non_empty(self.assertions, "assertions", CaseError)
        _check_energyplus_version(self.energyplus_version, CaseError)

    @property
    def expects_diagnostics(self) -> bool:
        """Whether the ``diagnostics`` assertion applies, which is what requires the diagnostics file."""
        return Assertion.DIAGNOSTICS in self.assertions


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One entry in ``manifest.json``, in either section.

    ``truth`` is derived from the section the entry was read from and is never written back:
    ``additionalProperties: false`` in the schema rejects it, and duplicating it on disk would let
    the two disagree.
    """

    id: str
    title: str
    tags: tuple[Tag, ...]
    energyplus_version: str
    assertions: tuple[Assertion, ...]
    input: InputFile
    parse_outcome: ParseOutcome
    truth: Truth
    expected: str | None = None
    expected_diagnostics: str | None = None
    expected_validation: str | None = None
    expected_introspection: str | None = None
    expected_docs_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "assertions", tuple(self.assertions))
        _check_case_id(self.id, ManifestError)
        _check_non_empty(self.title, "title", ManifestError)
        _check_unique_non_empty(self.tags, "tags", ManifestError)
        _check_unique_non_empty(self.assertions, "assertions", ManifestError)
        _check_energyplus_version(self.energyplus_version, ManifestError)

        # FR-020, enforced here as well as by the schema: the section is the truth value, so an
        # oracle entry must carry an expectation and a convention entry must not.
        if self.truth is Truth.ORACLE and self.expected != EXPECTED_EPJSON:
            raise ManifestError(
                f"case {self.id!r}: truth = oracle requires expected = {EXPECTED_EPJSON!r}, got {self.expected!r}"
            )
        if self.truth is Truth.CONVENTION and self.expected is not None:
            raise ManifestError(
                f"case {self.id!r}: truth = convention forbids an expectation, got expected = {self.expected!r}"
            )

        # An assertion that reads an expectation file must name it, and an entry must not name a
        # file for an assertion it does not declare: a named expectation nobody reads looks like
        # coverage and is not.
        declared = {assertion.value for assertion in self.assertions}
        for assertion_value, (key, filename) in EXPECTATION_FILES.items():
            named: str | None = getattr(self, key)
            if assertion_value in declared and named != filename:
                raise ManifestError(
                    f"case {self.id!r}: the {assertion_value!r} assertion requires {key} = {filename!r}, got {named!r}"
                )
            if assertion_value not in declared and named is not None:
                raise ManifestError(
                    f"case {self.id!r}: {key} is set to {named!r} but {assertion_value!r} is not among the assertions"
                )

    def to_json_obj(self) -> dict[str, Any]:
        """The entry as it is written to ``manifest.json``. The JSON boundary, not a model type."""
        entry: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "tags": [tag.value for tag in self.tags],
            "energyplus_version": self.energyplus_version,
            "assertions": [assertion.value for assertion in self.assertions],
            "input": self.input.value,
            "parse_outcome": self.parse_outcome.value,
        }
        if self.expected is not None:
            entry["expected"] = self.expected
        for key, _ in EXPECTATION_FILES.values():
            named: str | None = getattr(self, key)
            if named is not None:
                entry[key] = named
        return entry


@dataclass(frozen=True, slots=True)
class Manifest:
    """``manifest.json``: the index over every case, in two sections that never mix."""

    oracle: tuple[ManifestEntry, ...] = ()
    convention: tuple[ManifestEntry, ...] = ()
    corpus_level: str | None = None
    schema_version: int = MANIFEST_SCHEMA_VERSION
    schema_ref: str | None = MANIFEST_SCHEMA_REF

    def __post_init__(self) -> None:
        object.__setattr__(self, "oracle", tuple(self.oracle))
        object.__setattr__(self, "convention", tuple(self.convention))
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(f"schema_version must be {MANIFEST_SCHEMA_VERSION}, got {self.schema_version!r}")
        if self.corpus_level is not None and not CORPUS_LEVEL_PATTERN.match(self.corpus_level):
            raise ManifestError(f"corpus_level must match conformance-YYYY.N, got {self.corpus_level!r}")
        for section, expected_truth in ((self.oracle, Truth.ORACLE), (self.convention, Truth.CONVENTION)):
            for entry in section:
                if entry.truth is not expected_truth:
                    raise ManifestError(
                        f"case {entry.id!r} carries truth = {entry.truth.value} but sits in the "
                        f"{expected_truth.value} section"
                    )

        # JSON Schema's uniqueItems compares whole objects within one array, so it cannot catch an
        # id reused across the two sections. That check lives here.
        seen: set[str] = set()
        for entry in self.entries():
            if entry.id in seen:
                raise ManifestError(f"case id {entry.id!r} appears more than once across the two sections")
            seen.add(entry.id)

    def entries(self) -> Iterator[ManifestEntry]:
        """Every entry, oracle first, then convention."""
        yield from self.oracle
        yield from self.convention

    def entry(self, case_id: str) -> ManifestEntry:
        """The entry for ``case_id``, from whichever section holds it."""
        for entry in self.entries():
            if entry.id == case_id:
                return entry
        raise ManifestError(f"no case {case_id!r} in the manifest")

    def to_json_obj(self) -> dict[str, Any]:
        """The manifest as it is written to ``manifest.json``. The JSON boundary, not a model type."""
        document: dict[str, Any] = {}
        if self.schema_ref is not None:
            document["$schema"] = self.schema_ref
        document["schema_version"] = self.schema_version
        document["corpus_level"] = self.corpus_level
        document["oracle"] = [entry.to_json_obj() for entry in self.oracle]
        document["convention"] = [entry.to_json_obj() for entry in self.convention]
        return document


@dataclass(frozen=True, slots=True)
class Divergence:
    """One ``[[divergence]]`` table in ``known-divergence.toml``: an accepted disagreement.

    ``issue`` is mandatory (FR-018). An exception without a tracked resolution is
    indistinguishable from an accepted bug, so the constructor rejects a missing one rather than
    leaving the rule to a comment.
    """

    case: str
    library: Library
    assertion: Assertion
    issue: str
    observed: str
    expected: str

    def __post_init__(self) -> None:
        _check_case_id(self.case, DivergenceError)
        if not self.issue.strip():
            raise DivergenceError(
                f"divergence for case {self.case!r} on {self.library.value}/{self.assertion.value}: "
                f"'issue' is mandatory (FR-018). An exception without a tracked resolution is "
                f"indistinguishable from an accepted bug"
            )
        if not ISSUE_URL_PATTERN.match(self.issue):
            raise DivergenceError(
                f"divergence for case {self.case!r}: 'issue' must be a tracker URL, got {self.issue!r}"
            )
        _check_non_empty(self.observed, "observed", DivergenceError)
        _check_non_empty(self.expected, "expected", DivergenceError)


@dataclass(frozen=True, slots=True)
class DivergenceRegister:
    """``known-divergence.toml`` as a whole: the accepted disagreements, at most one per case, library and assertion."""

    entries: tuple[Divergence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        seen: set[tuple[str, Library, Assertion]] = set()
        for entry in self.entries:
            key = (entry.case, entry.library, entry.assertion)
            if key in seen:
                raise DivergenceError(
                    f"duplicate divergence for case {entry.case!r} on {entry.library.value}/{entry.assertion.value}"
                )
            seen.add(key)

    def find(self, case_id: str, library: Library, assertion: Assertion) -> Divergence | None:
        """The accepted exception for this case, library and assertion, or ``None`` if the failure blocks."""
        for entry in self.entries:
            if entry.case == case_id and entry.library is library and entry.assertion is assertion:
                return entry
        return None

    def for_library(self, library: Library) -> tuple[Divergence, ...]:
        """Every exception recorded against one library, in file order."""
        return tuple(entry for entry in self.entries if entry.library is library)


@dataclass(frozen=True, slots=True)
class Corpus:
    """A loaded corpus: the manifest, every case it indexes, and the exception register."""

    root: Path
    manifest: Manifest
    cases: tuple[Case, ...]
    divergences: DivergenceRegister

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", tuple(self.cases))

    def case(self, case_id: str) -> Case:
        """The loaded ``case.toml`` for ``case_id``."""
        for case in self.cases:
            if case.id == case_id:
                return case
        raise CaseError(f"no case {case_id!r} in {self.root / CASES_DIR}")

    def case_dir(self, case_id: str) -> Path:
        """The directory holding ``case_id``, whether or not it exists."""
        return self.root / CASES_DIR / case_id


# ---------------------------------------------------------------------------
# Shared field checks
# ---------------------------------------------------------------------------


def _check_case_id(value: str, error: type[CorpusError]) -> None:
    if not value or len(value) > CASE_ID_MAX_LENGTH or not CASE_ID_PATTERN.match(value):
        raise error(f"case id {value!r} must be a lowercase hyphenated slug of 1 to {CASE_ID_MAX_LENGTH} characters")


def _check_non_empty(value: str, field_name: str, error: type[CorpusError]) -> None:
    if not value.strip():
        raise error(f"{field_name!r} must be a non-empty string")


def _check_energyplus_version(value: str, error: type[CorpusError]) -> None:
    if not ENERGYPLUS_VERSION_PATTERN.match(value):
        raise error(f"energyplus_version must be three dotted numbers such as '26.1.0', got {value!r}")


def _check_unique_non_empty(values: Sequence[Enum], field_name: str, error: type[CorpusError]) -> None:
    if not values:
        raise error(f"{field_name!r} needs at least one entry")
    if len(set(values)) != len(values):
        raise error(f"{field_name!r} must not repeat an entry, got {[v.value for v in values]}")


# ---------------------------------------------------------------------------
# Reading untyped JSON and TOML
#
# These helpers are the only code that touches a parsed mapping. Everything above this line, and
# every caller below, works in dataclasses.
# ---------------------------------------------------------------------------

_E = TypeVar("_E", bound=Enum)


def _fail(error: type[CorpusError], path: Path, where: str, problem: str) -> NoReturn:
    raise error(f"{path}: {where}: {problem}")


def _read_mapping(value: object, path: Path, where: str, error: type[CorpusError]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(error, path, where, f"expected a table, got {type(value).__name__}")
    return value


def _read_str(raw: Mapping[str, Any], key: str, *, path: Path, where: str, error: type[CorpusError]) -> str:
    if key not in raw:
        _fail(error, path, where, f"missing required field {key!r}")
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        _fail(error, path, where, f"{key!r} must be a non-empty string, got {value!r}")
    return value


def _read_optional_str(
    raw: Mapping[str, Any], key: str, *, path: Path, where: str, error: type[CorpusError]
) -> str | None:
    if key not in raw or raw[key] is None:
        return None
    return _read_str(raw, key, path=path, where=where, error=error)


def _read_enum(
    raw: Mapping[str, Any],
    key: str,
    enum_cls: type[_E],
    *,
    path: Path,
    where: str,
    error: type[CorpusError],
) -> _E:
    text = _read_str(raw, key, path=path, where=where, error=error)
    try:
        return enum_cls(text)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_cls)
        _fail(error, path, where, f"{key!r} must be one of {allowed}, got {text!r}")


def _read_enum_tuple(
    raw: Mapping[str, Any],
    key: str,
    enum_cls: type[_E],
    *,
    path: Path,
    where: str,
    error: type[CorpusError],
) -> tuple[_E, ...]:
    if key not in raw:
        _fail(error, path, where, f"missing required field {key!r}")
    values = raw[key]
    if not isinstance(values, list) or not values:
        _fail(error, path, where, f"{key!r} must be a non-empty list, got {values!r}")
    members: list[_E] = []
    allowed = ", ".join(member.value for member in enum_cls)
    for value in values:
        if not isinstance(value, str):
            _fail(error, path, where, f"{key!r} must hold strings, got {value!r}")
        try:
            members.append(enum_cls(value))
        except ValueError:
            _fail(error, path, where, f"{key!r} must be drawn from {allowed}, got {value!r}")
    return tuple(members)


def _reject_unknown_keys(
    raw: Mapping[str, Any], known: frozenset[str], *, path: Path, where: str, error: type[CorpusError]
) -> None:
    unknown = sorted(set(raw) - known)
    if unknown:
        _fail(error, path, where, f"unknown field(s) {', '.join(repr(key) for key in unknown)}")


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

_CASE_KEYS: Final = frozenset({"title", "why", "tags", "energyplus_version", "truth", "assertions"})
_ENTRY_KEYS: Final = frozenset(
    {
        "id",
        "title",
        "tags",
        "energyplus_version",
        "assertions",
        "input",
        "parse_outcome",
        "expected",
        "expected_diagnostics",
        "expected_validation",
        "expected_introspection",
        "expected_docs_url",
    }
)
_MANIFEST_KEYS: Final = frozenset({"$schema", "schema_version", "corpus_level", "oracle", "convention"})
_DIVERGENCE_KEYS: Final = frozenset({"case", "library", "assertion", "issue", "observed", "expected"})


def load_case(case_dir: Path) -> Case:
    """Read ``case_dir/case.toml`` into a :class:`Case`, with the id taken from the directory name.

    Beyond the field rules, this enforces the file rules the case directory must satisfy:
    ``truth = oracle`` requires ``expected.epJSON`` to be present, ``truth = convention`` forbids
    it, and every assertion in :data:`EXPECTATION_FILES` requires the file it names.
    """
    path = case_dir / CASE_FILE
    if not path.is_file():
        raise CaseError(f"{path}: missing. Every case directory needs a {CASE_FILE}")
    with path.open("rb") as handle:
        raw = _read_mapping(tomllib.load(handle), path, "case", CaseError)
    _reject_unknown_keys(raw, _CASE_KEYS, path=path, where="case", error=CaseError)

    case = Case(
        id=case_dir.name,
        title=_read_str(raw, "title", path=path, where="case", error=CaseError),
        why=_read_str(raw, "why", path=path, where="case", error=CaseError),
        tags=_read_enum_tuple(raw, "tags", Tag, path=path, where="case", error=CaseError),
        energyplus_version=_read_str(raw, "energyplus_version", path=path, where="case", error=CaseError),
        truth=_read_enum(raw, "truth", Truth, path=path, where="case", error=CaseError),
        assertions=_read_enum_tuple(raw, "assertions", Assertion, path=path, where="case", error=CaseError),
    )

    expected_path = case_dir / EXPECTED_EPJSON
    if case.truth is Truth.ORACLE and not expected_path.is_file():
        raise CaseError(
            f"{path}: truth = oracle but {EXPECTED_EPJSON} is missing from {case_dir}. "
            f"Generate it with tools/regenerate.sh and commit it"
        )
    if case.truth is Truth.CONVENTION and expected_path.is_file():
        raise CaseError(
            f"{path}: truth = convention but {expected_path} exists. A convention case has no "
            f"external expectation, and keeping one would let agreed convention pass as truth"
        )
    declared = {assertion.value for assertion in case.assertions}
    for assertion_value, (_, filename) in EXPECTATION_FILES.items():
        if assertion_value in declared and not (case_dir / filename).is_file():
            raise CaseError(f"{path}: the {assertion_value!r} assertion requires {filename} in {case_dir}")
    return case


def _load_entry(raw_entry: object, truth: Truth, path: Path, index: int) -> ManifestEntry:
    where = f"{truth.value}[{index}]"
    raw = _read_mapping(raw_entry, path, where, ManifestError)
    _reject_unknown_keys(raw, _ENTRY_KEYS, path=path, where=where, error=ManifestError)
    return ManifestEntry(
        id=_read_str(raw, "id", path=path, where=where, error=ManifestError),
        title=_read_str(raw, "title", path=path, where=where, error=ManifestError),
        tags=_read_enum_tuple(raw, "tags", Tag, path=path, where=where, error=ManifestError),
        energyplus_version=_read_str(raw, "energyplus_version", path=path, where=where, error=ManifestError),
        assertions=_read_enum_tuple(raw, "assertions", Assertion, path=path, where=where, error=ManifestError),
        input=_read_enum(raw, "input", InputFile, path=path, where=where, error=ManifestError),
        parse_outcome=_read_enum(raw, "parse_outcome", ParseOutcome, path=path, where=where, error=ManifestError),
        truth=truth,
        expected=_read_optional_str(raw, "expected", path=path, where=where, error=ManifestError),
        expected_diagnostics=_read_optional_str(
            raw, "expected_diagnostics", path=path, where=where, error=ManifestError
        ),
        expected_validation=_read_optional_str(raw, "expected_validation", path=path, where=where, error=ManifestError),
        expected_introspection=_read_optional_str(
            raw, "expected_introspection", path=path, where=where, error=ManifestError
        ),
        expected_docs_url=_read_optional_str(raw, "expected_docs_url", path=path, where=where, error=ManifestError),
    )


def load_manifest(path: Path) -> Manifest:
    """Read ``manifest.json`` into a :class:`Manifest`, deriving each entry's truth from its section."""
    if not path.is_file():
        raise ManifestError(f"{path}: missing")
    with path.open("r", encoding="utf-8") as handle:
        raw = _read_mapping(json.load(handle), path, "manifest", ManifestError)
    _reject_unknown_keys(raw, _MANIFEST_KEYS, path=path, where="manifest", error=ManifestError)

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        _fail(ManifestError, path, "manifest", f"'schema_version' must be an integer, got {schema_version!r}")

    sections: dict[Truth, tuple[ManifestEntry, ...]] = {}
    for truth in (Truth.ORACLE, Truth.CONVENTION):
        raw_section = raw.get(truth.value)
        if not isinstance(raw_section, list):
            _fail(ManifestError, path, "manifest", f"{truth.value!r} must be an array, got {raw_section!r}")
        sections[truth] = tuple(
            _load_entry(raw_entry, truth, path, index) for index, raw_entry in enumerate(raw_section)
        )

    corpus_level = raw.get("corpus_level")
    if corpus_level is not None and not isinstance(corpus_level, str):
        _fail(ManifestError, path, "manifest", f"'corpus_level' must be a string or null, got {corpus_level!r}")
    schema_ref = raw.get("$schema")
    if schema_ref is not None and not isinstance(schema_ref, str):
        _fail(ManifestError, path, "manifest", f"'$schema' must be a string, got {schema_ref!r}")

    return Manifest(
        oracle=sections[Truth.ORACLE],
        convention=sections[Truth.CONVENTION],
        corpus_level=corpus_level,
        schema_version=schema_version,
        schema_ref=schema_ref,
    )


def dump_manifest(manifest: Manifest, path: Path) -> None:
    """Write ``manifest`` back to ``path``, in the committed formatting: two-space indent, trailing newline."""
    text = json.dumps(manifest.to_json_obj(), indent=2, ensure_ascii=False)
    path.write_text(f"{text}\n", encoding="utf-8")


def load_divergences(path: Path) -> DivergenceRegister:
    """Read ``known-divergence.toml`` into a :class:`DivergenceRegister`.

    A missing file is an error, not an empty register: the corpus ships this file so it can go
    green on arrival, and a silently absent register would turn every accepted exception into a
    blocking failure without saying so.
    """
    if not path.is_file():
        raise DivergenceError(f"{path}: missing. The exception register is part of the corpus, even when empty")
    with path.open("rb") as handle:
        raw = _read_mapping(tomllib.load(handle), path, "register", DivergenceError)
    _reject_unknown_keys(raw, frozenset({"divergence"}), path=path, where="register", error=DivergenceError)

    raw_entries = raw.get("divergence", [])
    if not isinstance(raw_entries, list):
        _fail(DivergenceError, path, "register", f"'divergence' must be an array of tables, got {raw_entries!r}")

    entries: list[Divergence] = []
    for index, raw_entry in enumerate(raw_entries):
        where = f"divergence[{index}]"
        entry = _read_mapping(raw_entry, path, where, DivergenceError)
        _reject_unknown_keys(entry, _DIVERGENCE_KEYS, path=path, where=where, error=DivergenceError)
        entries.append(
            Divergence(
                case=_read_str(entry, "case", path=path, where=where, error=DivergenceError),
                library=_read_enum(entry, "library", Library, path=path, where=where, error=DivergenceError),
                assertion=_read_enum(entry, "assertion", Assertion, path=path, where=where, error=DivergenceError),
                issue=_read_str(entry, "issue", path=path, where=where, error=DivergenceError),
                observed=_read_str(entry, "observed", path=path, where=where, error=DivergenceError),
                expected=_read_str(entry, "expected", path=path, where=where, error=DivergenceError),
            )
        )
    return DivergenceRegister(entries=tuple(entries))


def _check_agreement(entry: ManifestEntry, case: Case, path: Path) -> None:
    """The manifest entry and the case's own ``case.toml`` must say the same thing.

    JSON Schema cannot express this: it never sees ``case.toml``. Two records of the same facts
    that are allowed to disagree eventually do, and the runner would then be testing the wrong
    declaration.
    """
    if entry.title != case.title:
        raise ManifestError(
            f"{path}: case {entry.id!r}: title {entry.title!r} does not match {case.title!r} in {CASE_FILE}"
        )
    if set(entry.tags) != set(case.tags):
        raise ManifestError(
            f"{path}: case {entry.id!r}: tags {[t.value for t in entry.tags]} do not match "
            f"{[t.value for t in case.tags]} in {CASE_FILE}"
        )
    if entry.energyplus_version != case.energyplus_version:
        raise ManifestError(
            f"{path}: case {entry.id!r}: energyplus_version {entry.energyplus_version!r} does not match "
            f"{case.energyplus_version!r} in {CASE_FILE}"
        )
    if entry.truth is not case.truth:
        raise ManifestError(
            f"{path}: case {entry.id!r}: sits in the {entry.truth.value} section but {CASE_FILE} declares "
            f"truth = {case.truth.value}"
        )
    if set(entry.assertions) != set(case.assertions):
        raise ManifestError(
            f"{path}: case {entry.id!r}: assertions {[a.value for a in entry.assertions]} do not match "
            f"{[a.value for a in case.assertions]} in {CASE_FILE}"
        )


def load_corpus(root: Path, *, require_divergences: bool = True) -> Corpus:
    """Load the whole corpus at ``root`` and check every rule that spans more than one file.

    Set ``require_divergences`` to ``False`` only for a corpus that has not written its exception
    register yet. Everything else is mandatory.
    """
    manifest_path = root / MANIFEST_FILE
    manifest = load_manifest(manifest_path)
    cases_root = root / CASES_DIR

    cases: list[Case] = []
    for entry in manifest.entries():
        case_dir = cases_root / entry.id
        if not case_dir.is_dir():
            raise ManifestError(f"{manifest_path}: case {entry.id!r} has no directory at {case_dir}")
        case = load_case(case_dir)
        _check_agreement(entry, case, manifest_path)
        if not (case_dir / entry.input.value).is_file():
            raise CaseError(f"{case_dir}: declared input {entry.input.value!r} is missing")
        cases.append(case)

    if cases_root.is_dir():
        indexed = {entry.id for entry in manifest.entries()}
        for case_dir in sorted(child for child in cases_root.iterdir() if child.is_dir()):
            if case_dir.name not in indexed:
                raise ManifestError(
                    f"{manifest_path}: {case_dir} is not indexed. An unindexed case never runs, which is "
                    f"indistinguishable from having no case at all"
                )

    divergence_path = root / DIVERGENCE_FILE
    if require_divergences or divergence_path.is_file():
        divergences = load_divergences(divergence_path)
    else:
        divergences = DivergenceRegister()
    for divergence in divergences.entries:
        if not any(entry.id == divergence.case for entry in manifest.entries()):
            raise DivergenceError(
                f"{divergence_path}: divergence names case {divergence.case!r}, which is not in the manifest. "
                f"A stale exception is removed by the change that fixes the bug"
            )

    return Corpus(root=root, manifest=manifest, cases=tuple(cases), divergences=divergences)
