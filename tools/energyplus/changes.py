"""The change summary between two EnergyPlus releases (research R12).

Computed from the `Energy+.schema.epJSON` of the previous supported release and of the new one:
object types present in only one of them and, for types in both, fields present in only one. A
rename is reported as one removal and one addition and is never inferred, because an inferred rename
would put a guess into the document a maintainer reads to judge risk.

Renamed output variables are read from upstream's `Report Variables` transition table for the pair.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import tarfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sweep_consumers import Source

from .roster import AssetRole, Release
from .upstream import UpstreamRelease

SCHEMA_NAME = "Energy+.schema.epJSON"


@dataclass(frozen=True, slots=True)
class FieldChange:
    object_type: str
    field: str


@dataclass(frozen=True, slots=True)
class Rename:
    previous: str
    current: str


@dataclass(frozen=True, slots=True)
class ChangeSummary:
    previous: Release
    release: Release
    types_added: tuple[str, ...]
    types_removed: tuple[str, ...]
    fields_added: tuple[FieldChange, ...]
    fields_removed: tuple[FieldChange, ...]
    variables_renamed: tuple[Rename, ...]
    #: Stated when a part could not be computed. A summary is never silently incomplete.
    notes: tuple[str, ...] = ()


def _fields(definition: Mapping[str, Any]) -> frozenset[str]:
    """The field names of one object type, from its `patternProperties` or its `properties`."""
    names: set[str] = set()
    for inner in (definition.get("patternProperties") or {}).values():
        names.update((inner.get("properties") or {}).keys())
    names.update((definition.get("properties") or {}).keys())
    return frozenset(names)


def compare(previous: Release, release: Release, before: str, after: str) -> ChangeSummary:
    """The summary from the text of two schemas. Output variables are added by `with_variables`."""
    old = json.loads(before).get("properties") or {}
    new = json.loads(after).get("properties") or {}
    shared = sorted(set(old) & set(new))
    added = [FieldChange(name, field) for name in shared for field in sorted(_fields(new[name]) - _fields(old[name]))]
    removed = [FieldChange(name, field) for name in shared for field in sorted(_fields(old[name]) - _fields(new[name]))]
    return ChangeSummary(previous, release, tuple(sorted(set(new) - set(old))), tuple(sorted(set(old) - set(new))), tuple(added), tuple(removed), ())


def renamed_variables(table: str) -> tuple[Rename, ...]:
    """The renames in a `Report Variables A to B.csv` table: old name, new name, in the first two columns."""
    renames = []
    for row in csv.reader(io.StringIO(table)):
        if len(row) < 2 or not row[0].strip() or row[0].lstrip().startswith(("!", "old", "Old")):
            continue
        old, new = row[0].strip(), row[1].strip()
        if old.isdigit() or not new or old == new or re.fullmatch(r"[\d.]+", old):
            continue
        renames.append(Rename(old, new))
    return tuple(renames)


# ── Obtaining the inputs ───────────────────────────────────────────────────────────────────────────


class Fetch(Protocol):
    """HTTP, injectable. `schema_from_archive` streams a release archive and returns one member."""

    def text(self, url: str) -> str | None: ...

    def schema_from_archive(self, url: str) -> str | None: ...


class HttpFetch:
    def text(self, url: str) -> str | None:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError):
            return None

    def schema_from_archive(self, url: str) -> str | None:
        try:
            with urllib.request.urlopen(url, timeout=600) as response, tarfile.open(fileobj=response, mode="r|gz") as archive:  # noqa: S310
                for member in archive:
                    if member.name.endswith("/" + SCHEMA_NAME):
                        handle = archive.extractfile(member)
                        return handle.read().decode("utf-8") if handle else None
        except (urllib.error.URLError, TimeoutError, tarfile.TarError):
            return None
        return None


def bundled_schema(source: Source, repository: str, directory: str, release: Release) -> str | None:
    """The schema the first library bundles for `release`, or `None` when it bundles none."""
    path = f"{directory}/{release.directory()}/{SCHEMA_NAME}.gz"
    reader = getattr(source, "read_bytes", None)
    if reader is None:
        return None
    try:
        return gzip.decompress(reader(repository, path)).decode("utf-8")
    except (FileNotFoundError, OSError):
        return None


def summarise(
    previous: Release, upstream: UpstreamRelease, before: str | None, after: str | None, fetch: Fetch, upstream_repository: str
) -> ChangeSummary:
    """The summary for an upstream release, with every part that could not be computed stated."""
    release = upstream.release
    notes: list[str] = []
    if after is None and upstream.has(AssetRole.LINUX_X86_64):
        after = fetch.schema_from_archive(upstream.asset(AssetRole.LINUX_X86_64).url)
    if before is None or after is None:
        missing = "previous" if before is None else "new"
        notes.append(f"The schema of the {missing} release could not be read, so object types and fields were not compared.")
        summary = ChangeSummary(previous, release, (), (), (), (), ())
    else:
        summary = compare(previous, release, before, after)
    name = f"Report Variables {previous.dashed()} to {release.dashed()}.csv".replace(" ", "%20")
    table = fetch.text(f"https://raw.githubusercontent.com/{upstream_repository}/{upstream.tag}/src/Transition/SupportFiles/{name}")
    if table is None:
        notes.append("Upstream's output variable transition table for this pair could not be read.")
        renames: tuple[Rename, ...] = ()
    else:
        renames = renamed_variables(table)
    return ChangeSummary(
        summary.previous, summary.release, summary.types_added, summary.types_removed, summary.fields_added, summary.fields_removed, renames, tuple(notes)
    )
