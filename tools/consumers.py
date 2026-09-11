"""The consumer register: its typed model, its reader, and the level extraction every tool shares.

`governance/consumers.toml` is the fourth governance artifact (feature 004, research R1). It is the
roster of every project in the workspace that resolves either library or teaches a reader how to
install one, and it says where each one's level is WRITTEN rather than what the level is (R2). A
tool that wants a level follows a Declaration to the file that holds it, and this module is the one
place that knows how to follow one.

Five tools import it, and none of them duplicates what is here:

    check_consumers.py        is the register well formed (the ten invariants in data-model.md)
    check_consumer_self.py    does one consumer still match what the register says about it
    sweep_consumers.py        what level is every consumer on, and who is missing from the roster
    rehearse.py               what does a candidate build do to one consumer
    adoption.py               in what order is a published level adopted

WHY THE CHECKER READS RAW TABLES AND EVERYTHING ELSE READS THE TYPED MODEL

`check_consumers.py` exists to report a malformed register, so it cannot assume a well-formed one;
it reads the parsed TOML the way `validate_governance.py` reads `parity.toml`. Every other tool runs
after that check has passed and gets `Register`, whose constructors fail loudly on a missing field
rather than defaulting one. A default would be the register saying something it does not say.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Vocabulary ─────────────────────────────────────────────────────────────────────────────────────

ROLES = frozenset({"builds", "delivers", "teaches"})
LIBRARIES = frozenset({"python", "javascript"})
ENTRY_POINTS: Mapping[str, frozenset[str]] = {
    "python": frozenset({"pypi"}),
    # Both first-class (FR-037). Neither is a deficiency and no tool may present one as one.
    "javascript": frozenset({"shared-name", "scoped"}),
}
MEANS = frozenset({"direct", "through-consumer", "image", "runtime-fetch", "prose"})
FORMS = frozenset({"exact", "range"})
#: The parity ledger's two kinds of absence, reused rather than reinvented (spec, Assumptions).
LAG_KINDS = frozenset({"not-yet", "deliberate"})
#: `through-provider` is for a consumer holding no code that calls the library: a candidate reaches
#: it only inside a provider's release, so rehearsing it IS rehearsing its providers.
INSTALLS = frozenset({"uv-wheel", "npm-tarball", "through-provider"})
SURFACE_STATUSES = frozenset({"serving", "retired"})
FORMATTING_ANSWERS = frozenset({"yes", "no", "inherited", "not-applicable"})

#: The packages whose version IS a library's level, by library. The second language publishes one
#: release across every workspace package at one version (`npm version --workspaces` in
#: idfkit-js/publish.yml), so the shared name and every scoped package carry the same number for the
#: same release. That is what makes FR-039's "one level across both doors" an observation.
GOVERNED_PACKAGES: Mapping[str, frozenset[str]] = {
    "python": frozenset({"idfkit"}),
    "javascript": frozenset({"idfkit", "@idfkit/core", "@idfkit/schemas", "@idfkit/weather", "@idfkit/language"}),
}
SCOPED_PACKAGES = GOVERNED_PACKAGES["javascript"] - {"idfkit"}

#: Placed outside the unification by the constitution. Recorded where a consumer resolves them, so a
#: coordinated bump can see them, and acted on by no gate (FR-041).
OUT_OF_SCOPE_PACKAGES = frozenset({"@idfkit/engine", "@idfkit/engine-assets"})

#: Packages that are consumers, delivered to a person by another consumer. Their version is what a
#: delivery path pins, and the library level follows from it (FR-018).
DELIVERED_PACKAGES: Mapping[str, str] = {"idfkit-mcp": "idfkit-mcp", "idfkit-lsp": "idfkit-lsp"}


# ── The model ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Declaration:
    """One place a level is written. Never the level itself (R2)."""

    path: str
    locator: str
    form: str
    #: The distribution whose version is written here. Absent means the library's own package. A
    #: delivery path pins a server rather than the library, so its declarations name the server.
    package: str | None = None
    #: The consumer whose level determines the library level this declaration delivers (FR-018).
    via: str | None = None

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> Declaration:
        return cls(
            path=table["path"],
            locator=table["locator"],
            form=table["form"],
            package=table.get("package"),
            via=table.get("via"),
        )


@dataclass(frozen=True, slots=True)
class Lag:
    """Why a consumer is off the current level. Absent is the normal state and means nothing."""

    kind: str
    issue: str | None = None
    reason: str | None = None
    parity_id: str | None = None

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> Lag:
        return cls(kind=table["kind"], issue=table.get("issue"), reason=table.get("reason"), parity_id=table.get("parity_id"))


@dataclass(frozen=True, slots=True)
class Resolution:
    """How one consumer arrives at one library."""

    library: str
    entry_point: str
    means: str
    declared_at: tuple[Declaration, ...]
    lag: Lag | None = None

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> Resolution:
        lag = table.get("lag")
        return cls(
            library=table["library"],
            entry_point=table["entry_point"],
            means=table["means"],
            declared_at=tuple(Declaration.from_table(d) for d in table.get("declared_at", [])),
            lag=Lag.from_table(lag) if lag else None,
        )


@dataclass(frozen=True, slots=True)
class Rehearsal:
    """How to build one consumer against a candidate of one library (contracts/rehearsal.md).

    One per library the consumer resolves, not one per consumer. The contract found the reason:
    `idfkit-plugin` resolves both languages, and a single `install` could name only one of a wheel
    and a tarball. `idfkit-lsp` makes the same point from the other side, with a different type
    checker and a different test runner per language in one repository.
    """

    library: str
    install: str
    #: Required unless `install` is `through-provider`. Runs first (R4).
    typecheck: str | None
    test: str | None
    workdir: str = "."
    prose_paths: tuple[str, ...] = ()
    #: Required when `test` is absent. A consumer with no suite says so rather than naming a
    #: command that tests nothing.
    no_test_reason: str | None = None

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> Rehearsal:
        return cls(
            library=table["library"],
            install=table["install"],
            typecheck=table.get("typecheck"),
            test=table.get("test"),
            workdir=table.get("workdir", "."),
            prose_paths=tuple(table.get("prose_paths", ())),
            no_test_reason=table.get("no_test_reason"),
        )


@dataclass(frozen=True, slots=True)
class OutOfScope:
    """A dependency the constitution places outside the unification: visible, and ungoverned."""

    package: str
    path: str
    locator: str
    form: str
    note: str

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> OutOfScope:
        return cls(
            package=table["package"], path=table["path"], locator=table["locator"], form=table["form"], note=table["note"]
        )


@dataclass(frozen=True, slots=True)
class Consumer:
    """One project that resolves either library, or teaches a reader to install one."""

    id: str
    repository: str
    role: str
    libraries: tuple[Resolution, ...]
    depends_on: tuple[str, ...] = ()
    rehearsals: tuple[Rehearsal, ...] = ()
    out_of_scope: tuple[OutOfScope, ...] = ()
    #: Feature 006's SC-009 answer, for a consumer that writes a model to disk.
    writes_models: bool | None = None
    preserves_formatting: str | None = None
    formatting_note: str | None = None

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> Consumer:
        return cls(
            id=table["id"],
            repository=table["repository"],
            role=table["role"],
            libraries=tuple(Resolution.from_table(r) for r in table.get("libraries", [])),
            depends_on=tuple(table.get("depends_on", ())),
            rehearsals=tuple(Rehearsal.from_table(r) for r in table.get("rehearsal", [])),
            out_of_scope=tuple(OutOfScope.from_table(o) for o in table.get("out_of_scope", [])),
            writes_models=table.get("writes_models"),
            preserves_formatting=table.get("preserves_formatting"),
            formatting_note=table.get("formatting_note"),
        )

    def resolution(self, library: str) -> Resolution | None:
        return next((r for r in self.libraries if r.library == library), None)

    def rehearsal(self, library: str) -> Rehearsal | None:
        return next((r for r in self.rehearsals if r.library == library), None)


@dataclass(frozen=True, slots=True)
class Surface:
    """A documentation host that answers a reader. Top-level: two are published by the libraries."""

    host: str
    status: str
    published_by: str
    states_level: bool | None = None
    redirects_to: str | None = None
    parity_id: str | None = None

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> Surface:
        return cls(
            host=table["host"],
            status=table["status"],
            published_by=table["published_by"],
            states_level=table.get("states_level"),
            redirects_to=table.get("redirects_to"),
            parity_id=table.get("parity_id"),
        )


@dataclass(frozen=True, slots=True)
class Register:
    """The whole roster, read from one file at one tag."""

    schema_version: str
    libraries: Mapping[str, str]
    consumers: tuple[Consumer, ...]
    surfaces: tuple[Surface, ...]
    source: str = "<memory>"

    @classmethod
    def from_toml(cls, data: Mapping[str, Any], source: str = "<memory>") -> Register:
        header = data["register"]
        return cls(
            schema_version=str(header["schema_version"]),
            libraries=dict(header["libraries"]),
            consumers=tuple(Consumer.from_table(c) for c in data.get("consumer", [])),
            surfaces=tuple(Surface.from_table(s) for s in data.get("surface", [])),
            source=source,
        )

    @classmethod
    def load(cls, path: Path) -> Register:
        with path.open("rb") as handle:
            return cls.from_toml(tomllib.load(handle), source=str(path))

    def consumer(self, consumer_id: str) -> Consumer:
        for consumer in self.consumers:
            if consumer.id == consumer_id:
                return consumer
        raise KeyError(f"{consumer_id!r} is not a consumer in {self.source}")

    def by_repository(self, repository: str) -> Consumer | None:
        return next((c for c in self.consumers if c.repository.lower() == repository.lower()), None)


# ── Levels ─────────────────────────────────────────────────────────────────────────────────────────

_VERSION = re.compile(r"v?\d+(?:\.\d+)*(?:[.\-]?(?:a|b|rc|alpha|beta|dev|post)[.\-]?\d*)*(?:\+[0-9A-Za-z.]+)?")
_PRE = re.compile(r"[.\-]?(alpha|beta|a|b|rc|dev|post)[.\-]?(\d*)")
_PRE_SHORT = {"alpha": "a", "beta": "b"}


def normalize(version: str) -> str:
    """One spelling per version, so `1.0.0-rc.4` and `1.0.0rc4` compare equal.

    Both spellings are live in this workspace for the same release: npm and the site's
    `[tool.idfkit.library] level` write the semver form, PyPI and `uv.lock` the PEP 440 one.
    """
    text = version.strip().lower().removeprefix("v")
    return _PRE.sub(lambda m: f"{_PRE_SHORT.get(m.group(1), m.group(1))}{m.group(2)}", text)


@dataclass(frozen=True, slots=True)
class Observed:
    """What a declaration actually says, once followed."""

    raw: str
    #: The exact version written, or None when the declaration is a range or names no version.
    level: str | None

    @property
    def is_exact(self) -> bool:
        return self.level is not None


def level_of(raw: str, package: str | None) -> Observed:
    """Read a level out of whatever string a locator reached.

    Four shapes are live in the workspace: a bare version (`0.3.0-rc.2`), a PEP 508 requirement
    (`idfkit==0.15.0`), a runner's `name@version` (`idfkit-mcp@0.9.3`), and a bare package name
    with no version at all (`idfkit-lsp`), which is the unpinned delivery path US3 closes.
    """
    text = raw.strip()
    if package and text.lower().startswith(package.lower()):
        rest = re.sub(r"^\[[^\]]*\]", "", text[len(package) :]).strip()
        for operator in ("==", "@"):
            if rest.startswith(operator) and _VERSION.fullmatch(rest[len(operator) :].strip()):
                return Observed(raw=text, level=rest[len(operator) :].strip())
        return Observed(raw=text, level=None)
    if _VERSION.fullmatch(text):
        return Observed(raw=text, level=text)
    return Observed(raw=text, level=None)


# ── Locators ───────────────────────────────────────────────────────────────────────────────────────

_SEGMENT = re.compile(r'\["(?P<quoted>[^"]+)"\]|\[(?P<index>\d+)\]|\[(?P<requirement>[^\]"]+)\]|(?P<key>[^.\[\]]+)')


class LocatorError(LookupError):
    """A locator that does not resolve. Reported, never defaulted."""


def _requirement_name(entry: str) -> str:
    return re.split(r"[\s\[<>=!~;@]", entry.strip(), maxsplit=1)[0].lower()


def _walk(document: Any, locator: str) -> Any:
    node = document
    position = 0
    for match in _SEGMENT.finditer(locator):
        if match.start() != position and locator[position] != ".":
            raise LocatorError(f"cannot parse locator {locator!r} at {locator[position:]!r}")
        position = match.end() + (1 if match.end() < len(locator) and locator[match.end()] == "." else 0)
        if (key := match.group("key") or match.group("quoted")) is not None:
            if not isinstance(node, Mapping) or key not in node:
                raise LocatorError(f"no key {key!r} in {locator!r}")
            node = node[key]
        elif (index := match.group("index")) is not None:
            if not isinstance(node, Sequence) or isinstance(node, str) or int(index) >= len(node):
                raise LocatorError(f"no index {index} in {locator!r}")
            node = node[int(index)]
        else:
            wanted = match.group("requirement")
            if not isinstance(node, Sequence) or isinstance(node, str):
                raise LocatorError(f"[{wanted}] selects from a list, and {locator!r} did not reach one")
            if "=" in wanted:
                # `[name=idfkit]`: the one table in a list whose field has that value. For a
                # declaration file that lists libraries as objects, like idfkit-lsp's levels.json.
                field_name, value = wanted.split("=", 1)
                hits = [e for e in node if isinstance(e, Mapping) and str(e.get(field_name)) == value]
            else:
                hits = [e for e in node if isinstance(e, str) and _requirement_name(e) == wanted.lower()]
            if len(hits) != 1:
                raise LocatorError(f"expected exactly one requirement on {wanted!r} in {locator!r}, found {len(hits)}")
            node = hits[0]
    if position < len(locator):
        raise LocatorError(f"cannot parse locator {locator!r} at {locator[position:]!r}")
    return node


def parse_document(path: str, text: str) -> Any:
    if path.endswith(".toml"):
        return tomllib.loads(text)
    if path.endswith(".json"):
        return json.loads(text)
    return text


def resolve(path: str, text: str, locator: str) -> str:
    """Follow *locator* inside the text of *path* and return the string it reaches.

    A locator is one of: a dotted key path into TOML or JSON (`project.dependencies[idfkit]`,
    `mcpServers.idfkit.args[0]`, `dependencies["@idfkit/core"]`), where `[N]` indexes a list and
    `[name]` selects the one requirement string naming that package; or `regex:` followed by a
    pattern with a named group `level`, for a file with no structure to walk.
    """
    if locator.startswith("regex:"):
        match = re.search(locator.removeprefix("regex:"), text, flags=re.MULTILINE)
        if match is None or "level" not in match.groupdict():
            raise LocatorError(f"{locator!r} matches nothing in {path}")
        return match.group("level")
    value = _walk(parse_document(path, text), locator)
    if not isinstance(value, str):
        raise LocatorError(f"{locator!r} in {path} reaches a {type(value).__name__}, not a string")
    return value


def read_declaration(root: Path, declaration: Declaration, library: str) -> Observed:
    """Follow one declaration inside a checkout at *root*."""
    target = root / declaration.path
    if not target.is_file():
        raise LocatorError(f"{declaration.path} does not exist")
    raw = resolve(declaration.path, target.read_text(encoding="utf-8"), declaration.locator)
    return level_of(raw, declaration.package or _default_package(declaration, library))


def _default_package(declaration: Declaration, library: str) -> str | None:
    # A key-path locator ending in the package name (`dependencies["@idfkit/core"]`) reaches a bare
    # version; one ending in a requirement selector (`[idfkit]`) reaches `idfkit==X`. The package is
    # needed only for the second, and the selector names it.
    selector = re.search(r'\[(?:"(?P<q>[^"]+)"|(?P<r>[^\]\d"][^\]"]*))\]$', declaration.locator)
    if selector:
        return selector.group("q") or selector.group("r")
    return "idfkit" if library == "python" else None


def agreement_key(declaration: Declaration, library: str) -> str:
    """Which declarations of one Resolution must agree with each other (FR-007).

    Declarations of the same package must agree. Every governed package of the second language
    shares one key, because they are one release at one version (FR-039), so a manifest holding
    `@idfkit/core` at one level and `@idfkit/schemas` at another is half adopted and fails. A
    delivery path's two servers do NOT share a key: `.mcp.json` pins idfkit-mcp and `.lsp.json`
    pins idfkit-lsp, two release series that requiring to agree would put in lockstep.
    """
    package = declaration.package or _default_package(declaration, library) or library
    if package in GOVERNED_PACKAGES.get(library, frozenset()):
        return f"{library}-release"
    return package


# ── Detection: what a checkout resolves, read from its manifests ────────────────────────────────────

SKIP_DIRECTORIES = frozenset({".git", ".venv", "venv", "node_modules", "dist", "build", "cdk.out", "site", "__pycache__"})


@dataclass(frozen=True, slots=True)
class Detected:
    """One place a checkout declares a dependency on a governed, delivered or out-of-scope package."""

    path: str
    package: str
    means: str
    #: `dependencies`, `devDependencies`, `peerDependencies`, `optional`, or `runtime`.
    section: str


_MANIFEST_NAMES = frozenset({"pyproject.toml", "package.json", ".mcp.json", ".lsp.json"})


def iter_manifests(root: Path) -> Iterator[Path]:
    """Every manifest the repository COMMITS, which is what CI checks out and what the sweep reads.

    Tracked files only, when *root* is a git checkout. A maintainer's machine carries worktrees,
    scratch harnesses and vendored copies that no build ever sees, and a self-check that failed on
    them would fail on one laptop and pass in CI, which is the disagreement FR-006 exists to end.
    """
    listing = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "ls-files", "-z"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if listing.returncode == 0:
        candidates = [root / name for name in listing.stdout.decode().split("\0") if name]
    else:
        candidates = [p for p in root.rglob("*") if p.is_file()]
    for path in sorted(candidates):
        if any(part in SKIP_DIRECTORIES for part in path.relative_to(root).parts[:-1]):
            continue
        if path.name in _MANIFEST_NAMES and path.is_file():
            yield path


def detect_text(path: str, text: str) -> list[Detected]:
    """Every dependency on a package this register cares about, in one manifest's text."""
    name = path.rsplit("/", 1)[-1]
    interesting = GOVERNED_PACKAGES["python"] | GOVERNED_PACKAGES["javascript"] | OUT_OF_SCOPE_PACKAGES | set(DELIVERED_PACKAGES)
    found: list[Detected] = []
    try:
        document = parse_document(path, text)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError):
        return found

    if name == "pyproject.toml":
        project = document.get("project", {})
        groups: list[tuple[str, list[Any]]] = [("dependencies", project.get("dependencies", []))]
        groups += [("optional", deps) for deps in project.get("optional-dependencies", {}).values()]
        groups += [("optional", deps) for deps in document.get("dependency-groups", {}).values()]
        for section, entries in groups:
            for entry in entries:
                if isinstance(entry, str) and _requirement_name(entry) in {"idfkit", *DELIVERED_PACKAGES}:
                    found.append(Detected(path, _requirement_name(entry), "direct", section))
    elif name == "package.json":
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            for package in document.get(section, {}) or {}:
                if package in interesting:
                    found.append(Detected(path, package, "direct", section))
    elif name in {".mcp.json", ".lsp.json"}:
        for value in _strings(document):
            package = _requirement_name(value.replace("@", "==", 1) if value.startswith("idfkit") else value)
            if package in DELIVERED_PACKAGES:
                found.append(Detected(path, package, "runtime-fetch", "runtime"))
    return found


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, Sequence):
        for value in node:
            yield from _strings(value)


def detect(root: Path) -> list[Detected]:
    found: list[Detected] = []
    for manifest in iter_manifests(root):
        found += detect_text(manifest.relative_to(root).as_posix(), manifest.read_text(encoding="utf-8"))
    return found


def detected_entry_point(detections: Sequence[Detected]) -> str | None:
    """Which door a second-language manifest came through, read from what it depends on at run time.

    An optional peer on the shared name does not make a consumer a shared-name consumer: the lsp
    model server peers on `idfkit` so that it can report what to install, and imports only scoped
    packages. What the code resolves decides the door.
    """
    runtime = {d.package for d in detections if d.section == "dependencies"}
    if runtime & SCOPED_PACKAGES:
        return "scoped"
    if "idfkit" in runtime:
        return "shared-name"
    return None
