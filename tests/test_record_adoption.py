"""Recording an adoption edits one lag in place, keeps every comment, and never writes a bad register."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from adoption_statements import governance_level_at, release_tags
from consumers import Register
from record_adoption import RecordError, close_lag, decline

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "governance" / "consumers.toml").read_text(encoding="utf-8")
PARITY = {c["id"] for c in tomllib.loads((ROOT / "governance" / "parity.toml").read_text())["capability"]}


def _lag(text: str, consumer: str, library: str) -> object:
    resolution = Register.from_toml(tomllib.loads(text)).consumer(consumer).resolution(library)
    assert resolution is not None
    return resolution.lag


def test_closing_a_lag_removes_that_lag_and_nothing_else() -> None:
    edited = close_lag(TEXT, "idfkit-mcp", "python", PARITY)
    assert _lag(edited, "idfkit-mcp", "python") is None
    assert _lag(edited, "idfkit-docs", "python") is not None
    assert TEXT.count("\n#") == edited.count("\n#"), "a comment was lost"
    assert len(TEXT.splitlines()) - len(edited.splitlines()) == 3


def test_closing_one_language_leaves_the_other() -> None:
    edited = close_lag(TEXT, "idfkit-lsp", "javascript", PARITY)
    assert _lag(edited, "idfkit-lsp", "javascript") is None
    assert _lag(edited, "idfkit-lsp", "python") is not None


def test_closing_where_there_is_no_lag_changes_nothing() -> None:
    assert close_lag(TEXT, "idfkit-developers", "python", PARITY) == TEXT


def test_declining_writes_a_deliberate_lag() -> None:
    edited = decline(TEXT, "idfkit-shoebox", "javascript", "Follows the library only when the page needs a new capability.", PARITY)
    lag = _lag(edited, "idfkit-shoebox", "javascript")
    assert lag is not None and lag.kind == "deliberate"  # type: ignore[attr-defined]


def test_declining_replaces_a_not_yet_lag() -> None:
    edited = decline(TEXT, "idfkit-docs", "python", "Moves only when EnergyPlus adds a schema version.", PARITY)
    lag = _lag(edited, "idfkit-docs", "python")
    assert lag is not None and lag.kind == "deliberate" and lag.issue is None  # type: ignore[attr-defined]


def test_a_decline_citing_a_version_is_refused() -> None:
    with pytest.raises(RecordError, match="policy"):
        decline(TEXT, "idfkit-docs", "python", "Stays on 0.12.1.", PARITY)


def test_a_decline_citing_the_entry_point_is_refused() -> None:
    with pytest.raises(RecordError, match="entry point"):
        decline(TEXT, "envelop", "javascript", "Stays on the scoped packages.", PARITY)


def test_an_unknown_consumer_is_refused() -> None:
    with pytest.raises(RecordError):
        close_lag(TEXT, "nobody", "python", PARITY)


def test_release_tags_try_the_semver_spelling_first() -> None:
    assert release_tags("1.0.0rc4") == ["v1.0.0-rc.4", "v1.0.0rc4"]
    assert release_tags("0.15.0") == ["v0.15.0"]


def test_governance_level_is_read_from_the_library_at_its_tag() -> None:
    files = {("idfkit/idfkit", "v1.0.0-rc.4"): '[tool.idfkit.governance]\nlevel = "governance-2026.17"\n'}
    assert governance_level_at("python", "1.0.0rc4", lambda repo, path, ref: files.get((repo, ref))) == "governance-2026.17"
    with pytest.raises(LookupError):
        governance_level_at("python", "9.9.9", lambda repo, path, ref: None)
