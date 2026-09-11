"""The rehearsal's own rules, without running anyone's build: attribution, candidates, and no-candidate."""

from __future__ import annotations

from pathlib import Path

from rehearse import EXIT_NO_CANDIDATE, Run, _attribute, _package_of_tarball, find_candidate, main

ROOT = Path(__file__).resolve().parent.parent


def _run(code: int, *errors: str) -> Run:
    return Run("cmd", code, "", tuple(errors))


def test_green_then_red_is_the_library() -> None:
    assert _attribute(_run(0), _run(1, "a.py:3 - error: x")) == "library"


def test_red_at_both_with_the_same_errors_is_the_consumer() -> None:
    assert _attribute(_run(1, "lexer.py:25 - error: y"), _run(1, "lexer.py:25 - error: y")) == "consumer"


def test_a_new_error_behind_an_old_one_is_still_the_library() -> None:
    # idfkit-docs is red at its declared level for reasons unrelated to idfkit. A candidate that
    # breaks a call there must not hide behind those.
    declared = _run(1, "lexer.py:25 - error: y")
    candidate = _run(1, "lexer.py:25 - error: y", "schema_utils.py:18 - error: unknown import")
    assert _attribute(declared, candidate) == "library"


def test_red_then_green_is_the_consumer_and_not_a_pass() -> None:
    assert _attribute(_run(1, "x"), _run(0)) == "consumer"


def test_tarball_names_map_to_packages() -> None:
    assert _package_of_tarball("/tmp/idfkit-core-0.0.0.tgz") == "@idfkit/core"
    assert _package_of_tarball("idfkit-language-0.3.0-rc.3.tgz") == "@idfkit/language"
    assert _package_of_tarball("idfkit-0.0.0.tgz") == "idfkit"


def test_a_wheel_candidate_is_found_and_named(tmp_path: Path) -> None:
    (tmp_path / "idfkit-1.0.0rc4-py3-none-any.whl").write_bytes(b"")
    candidate = find_candidate(tmp_path, "python", "f709abdf")
    assert candidate is not None and candidate.version == "1.0.0rc4" and candidate.sha == "f709abdf"


def test_an_empty_candidate_directory_is_no_candidate(tmp_path: Path) -> None:
    assert find_candidate(tmp_path, "python", "x") is None
    assert find_candidate(tmp_path, "javascript", "x") is None


def test_no_candidate_fails_and_never_passes(tmp_path: Path) -> None:
    code = main(
        [
            "--register", str(ROOT / "governance" / "consumers.toml"),
            "--naming", str(ROOT / "governance" / "naming.toml"),
            "--consumer", "idfkit-mcp",
            "--library", "python",
            "--candidate", str(tmp_path / "missing"),
            "--root", str(tmp_path),
        ]
    )
    assert code == EXIT_NO_CANDIDATE


def test_a_through_provider_consumer_rehearses_through_its_providers(tmp_path: Path) -> None:
    code = main(
        [
            "--register", str(ROOT / "governance" / "consumers.toml"),
            "--naming", str(ROOT / "governance" / "naming.toml"),
            "--consumer", "idfkit-plugin",
            "--library", "python",
            "--root", str(tmp_path),
        ]
    )
    assert code == 0
