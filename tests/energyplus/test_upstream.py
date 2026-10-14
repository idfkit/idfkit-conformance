"""The qualification rule, against the listing recorded on 2026-10-13."""

from __future__ import annotations

import re

import pytest
from energyplus.roster import AssetRole, Release
from energyplus.upstream import Asset, FixtureReleases, UpstreamError, UpstreamRelease, awaiting, find, listing, refusal, releases
from support import FIXTURES

SOURCE = FixtureReleases(FIXTURES / "releases-2026-10-13.json", FIXTURES / "tag-commits-2026-10-13.json")
SUPPORTED = Release(26, 1, 0)
NEXT = Release(26, 2, 0)


def test_the_fixture_holds_the_entries_the_rule_must_separate() -> None:
    tags = {entry.tag: entry for entry in listing(SOURCE)}
    assert {"v26.2.0", "v26.2.0-RC3", "v26.2.0-IOFreeze", "fix-ems-vars", "v26.1.0-LWRadTest2"} <= set(tags)
    assert tags["v26.1.0-LWRadTest2"].prerelease is False


def test_exactly_one_release_awaits_adoption() -> None:
    waiting = awaiting(releases(SOURCE), SUPPORTED)
    assert [upstream.tag for upstream in waiting] == ["v26.2.0"]
    assert waiting[0].commit == "4bd7a1f26fb749a83257855aba6760e26c8a3c8f"
    assert waiting[0].published_at.isoformat() == "2026-09-30T13:54:03+00:00"


def test_a_test_build_published_as_final_does_not_qualify() -> None:
    assert all(upstream.tag != "v26.1.0-LWRadTest2" for upstream in releases(SOURCE))
    assert all(Release.parse_tag(upstream.tag) == upstream.release for upstream in releases(SOURCE))


def test_releases_are_ascending() -> None:
    found = [upstream.release for upstream in releases(SOURCE)]
    assert found == sorted(found) and SUPPORTED in found


def test_a_role_resolves_to_the_name_with_the_build_hash() -> None:
    upstream = find(releases(SOURCE), NEXT)
    assert upstream is not None
    assert upstream.asset(AssetRole.LINUX_X86_64).name == "EnergyPlus-26.2.0-4bd7a1f26f-Linux-Ubuntu24.04-x86_64.tar.gz"
    assert upstream.asset(AssetRole.LINUX_ARM64).name == "EnergyPlus-26.2.0-4bd7a1f26f-Linux-Ubuntu24.04-arm64.tar.gz"
    assert upstream.asset(AssetRole.CHECKSUMS).size == 2430
    assert upstream.has(AssetRole.LINUX_X86_64)


def test_every_digest_is_sixty_four_hexadecimal_characters() -> None:
    upstream = find(releases(SOURCE), NEXT)
    assert upstream is not None and len(upstream.assets) == 23
    assert all(asset.sha256 and re.fullmatch(r"[0-9a-f]{64}", asset.sha256) for asset in upstream.assets)


def _release(*names: str) -> UpstreamRelease:
    upstream = find(releases(SOURCE), NEXT)
    assert upstream is not None
    return UpstreamRelease(NEXT, "v26.2.0", "", upstream.published_at, False, tuple(Asset(name, 1, None, "") for name in names))


def test_an_asset_that_matches_no_name_or_two_names_is_an_error() -> None:
    with pytest.raises(UpstreamError, match="0 assets"):
        _release("sha256sums.txt").asset(AssetRole.LINUX_X86_64)
    same = _release("EnergyPlus-26.2.0-aaa-Linux-Ubuntu24.04-x86_64.tar.gz", "EnergyPlus-26.2.0-bbb-Linux-Ubuntu24.04-x86_64.tar.gz")
    with pytest.raises(UpstreamError, match="2 assets"):
        same.asset(AssetRole.LINUX_X86_64)
    assert not same.has(AssetRole.LINUX_X86_64)


OLDER = "EnergyPlus-26.2.0-aaa-Linux-Ubuntu22.04-x86_64.tar.gz"
NEWER = "EnergyPlus-26.2.0-aaa-Linux-Ubuntu24.04-x86_64.tar.gz"


def test_of_two_ubuntu_builds_the_oldest_fills_the_role() -> None:
    assert _release(NEWER, OLDER).asset(AssetRole.LINUX_X86_64).name == OLDER


def test_a_stated_ubuntu_release_selects_its_build() -> None:
    assert _release(NEWER, OLDER).asset(AssetRole.LINUX_X86_64, "24.04").name == NEWER


def test_a_stated_ubuntu_release_that_upstream_did_not_build_is_an_error() -> None:
    with pytest.raises(UpstreamError, match="for Ubuntu 26.04"):
        _release(NEWER, OLDER).asset(AssetRole.LINUX_X86_64, "26.04")
    assert not _release(NEWER).has(AssetRole.LINUX_X86_64, "22.04")


def test_an_asset_without_a_digest_does_not_satisfy_a_role() -> None:
    assert not _release("EnergyPlus-26.2.0-aaa-Linux-Ubuntu24.04-x86_64.tar.gz").has(AssetRole.LINUX_X86_64)


def test_a_refusal_states_its_cause() -> None:
    assert refusal(SOURCE, NEXT) is None
    assert "no release tagged v99.9.0" in (refusal(SOURCE, Release(99, 9, 0)) or "")
    candidate = next(entry for entry in listing(SOURCE) if entry.tag == "v26.2.0-RC3")
    assert "not exactly vX.Y.Z" in (candidate.refusal() or "")
