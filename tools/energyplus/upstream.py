"""The upstream release listing, and the rule that decides which entries are releases.

Research R1: an upstream release is a non-draft release object whose tag matches `vX.Y.Z` exactly,
whose `prerelease` flag is false, and which has at least one asset. The flag alone does not qualify
a release, because upstream publishes test builds with `prerelease: false`.

An asset name is never constructed from a version. It embeds a build hash, so an asset is found by
its role's pattern among the names upstream published.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .roster import AssetRole, Release

SHA256_PREFIX = "sha256:"


class UpstreamError(RuntimeError):
    """The upstream listing could not be read, or an asset could not be resolved."""


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    size: int
    #: 64 hexadecimal characters, or `None` where upstream published no digest.
    sha256: str | None
    url: str


@dataclass(frozen=True, slots=True)
class Published:
    """One entry of the upstream listing, whether or not it qualifies as a release."""

    tag: str
    draft: bool
    prerelease: bool
    published_at: datetime
    assets: tuple[Asset, ...]

    def refusal(self) -> str | None:
        """Why this entry is not a release, or `None` when it is one."""
        if self.draft:
            return "it is a draft"
        if Release.parse_tag(self.tag) is None:
            return f"its tag {self.tag!r} is not exactly vX.Y.Z, so it is a candidate or a test build"
        if self.prerelease:
            return "upstream marks it as a prerelease"
        if not self.assets:
            return "it has no assets"
        return None


@dataclass(frozen=True, slots=True)
class UpstreamRelease:
    release: Release
    tag: str
    commit: str
    published_at: datetime
    prerelease: bool
    assets: tuple[Asset, ...]

    def asset(self, role: AssetRole) -> Asset:
        pattern = role.pattern(self.release)
        found = [asset for asset in self.assets if fnmatch.fnmatchcase(asset.name, pattern)]
        if len(found) != 1:
            raise UpstreamError(f"{self.tag}: {len(found)} assets match {pattern!r}, expected exactly 1")
        return found[0]

    def has(self, role: AssetRole) -> bool:
        """The role resolves to one asset, and that asset carries a digest."""
        try:
            return self.asset(role).sha256 is not None
        except UpstreamError:
            return False


class Releases(Protocol):
    """Where the listing comes from: GitHub in a run, a recorded file in a test."""

    def payload(self) -> str: ...

    def commit(self, tag: str) -> str: ...


class GhReleases:
    """The upstream repository, through `gh api`. The repository is public."""

    def __init__(self, repository: str) -> None:
        self.repository = repository

    def _api(self, *args: str) -> str:
        result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            raise UpstreamError(result.stderr.strip() or "gh api failed")
        return result.stdout

    def payload(self) -> str:
        return self._api(f"repos/{self.repository}/releases?per_page=50")

    def commit(self, tag: str) -> str:
        return self._api(f"repos/{self.repository}/commits/{tag}", "--jq", ".sha").strip()


class FixtureReleases:
    """A recorded listing, and a recorded table of the commit each tag points to."""

    def __init__(self, listing: Path, commits: Path | None = None) -> None:
        self.listing = listing
        self.commits = commits

    def payload(self) -> str:
        return self.listing.read_text(encoding="utf-8")

    def commit(self, tag: str) -> str:
        if self.commits is None:
            return ""
        return str(json.loads(self.commits.read_text(encoding="utf-8")).get(tag, ""))


def listing(source: Releases) -> tuple[Published, ...]:
    """Every entry upstream lists, newest first as upstream orders them."""
    entries = []
    for raw in json.loads(source.payload()):
        assets = tuple(
            Asset(
                name=asset["name"],
                size=int(asset["size"]),
                sha256=(asset.get("digest") or "").removeprefix(SHA256_PREFIX) or None,
                url=asset.get("browser_download_url", ""),
            )
            for asset in raw.get("assets", ())
        )
        published = raw.get("published_at")
        if published is None:
            continue
        entries.append(
            Published(
                tag=raw["tag_name"],
                draft=bool(raw.get("draft")),
                prerelease=bool(raw.get("prerelease")),
                published_at=datetime.fromisoformat(published.replace("Z", "+00:00")),
                assets=assets,
            )
        )
    return tuple(entries)


def releases(source: Releases) -> tuple[UpstreamRelease, ...]:
    """The entries that qualify as releases, ascending."""
    kept = []
    for entry in listing(source):
        release = Release.parse_tag(entry.tag)
        if release is None or entry.refusal() is not None:
            continue
        kept.append(UpstreamRelease(release, entry.tag, source.commit(entry.tag), entry.published_at, entry.prerelease, entry.assets))
    return tuple(sorted(kept, key=lambda upstream: upstream.release))


def find(found: tuple[UpstreamRelease, ...], release: Release) -> UpstreamRelease | None:
    return next((upstream for upstream in found if upstream.release == release), None)


def refusal(source: Releases, release: Release) -> str | None:
    """Why `release` cannot be adopted, or `None` when upstream published it as a release."""
    entries = [entry for entry in listing(source) if entry.tag.startswith(release.tag())]
    exact = next((entry for entry in entries if entry.tag == release.tag()), None)
    if exact is not None:
        return exact.refusal()
    if entries:
        return f"upstream lists only {', '.join(entry.tag for entry in entries)}, none of which is the release {release.tag()}"
    return f"upstream has published no release tagged {release.tag()}"


def awaiting(found: tuple[UpstreamRelease, ...], newest_supported: Release) -> tuple[UpstreamRelease, ...]:
    """The releases greater than the newest supported one, ascending."""
    return tuple(upstream for upstream in found if upstream.release > newest_supported)
