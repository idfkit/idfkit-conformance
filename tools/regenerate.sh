#!/usr/bin/env bash
#
# regenerate.sh - regenerate every case's committed expectation from the oracle.
#
# THE RULE: an expectation is never hand-written and never copied from either library.
# It is produced only by EnergyPlus ConvertInputFormat, the external authority (FR-012).
# If you are tempted to edit an expected.epJSON by hand, the case is wrong, not the oracle.
#
# This is a MAINTAINER task. CI never runs it and needs no EnergyPlus installation
# (FR-013): expectations are generated offline here and committed alongside the cases.
#
# For every cases/<id>/case.toml with truth = "oracle", this reads energyplus_version,
# locates the matching install, and runs:
#
#     ConvertInputFormat -f epJSON -o <tmp>/ cases/<id>/input.idf
#
# placing the result at cases/<id>/expected.epJSON. Versions that are not installed
# locally are skipped, and every skip is reported at the end: a full set of installs is
# roughly 13.6 GB, so working from a partial set is the normal case, not a failure.
#
# A case with truth = "convention" has no oracle. It is skipped without error (FR-020).
#
# Usage:
#   tools/regenerate.sh                 regenerate every oracle case
#   tools/regenerate.sh <id> [<id>...]  regenerate only the named cases
#   tools/regenerate.sh --help
#
# Environment:
#   ENERGYPLUS_INSTALL_ROOT   colon-separated list of directories holding EnergyPlus
#                             installs, each named EnergyPlus-<MAJOR>-<MINOR>-<PATCH>.
#                             Defaults to "/Applications:/usr/local", which covers the
#                             macOS and Linux installer layouts.

set -euo pipefail

readonly DEFAULT_INSTALL_ROOT="/Applications:/usr/local"
INSTALL_ROOT="${ENERGYPLUS_INSTALL_ROOT:-$DEFAULT_INSTALL_ROOT}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly CASES_DIR="$REPO_ROOT/cases"

usage() {
    sed -n '3,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

# Read one single-line string value out of a TOML file, ignoring anything inside a
# triple-quoted block so that prose in `why` cannot masquerade as a key.
read_toml_string() {
    local key="$1" file="$2"
    awk -v key="$key" '
        { line = $0; probe = line; fences = gsub(/"""/, "", probe) }
        inblock { if (fences % 2 == 1) inblock = 0; next }
        {
            if (fences % 2 == 1) { inblock = 1; next }
            if (line ~ "^[ \t]*" key "[ \t]*=") {
                if (match(line, /"[^"]*"/)) {
                    print substr(line, RSTART + 1, RLENGTH - 2)
                    exit
                }
            }
        }
    ' "$file"
}

# Echo the path to ConvertInputFormat for an EnergyPlus version, or fail if absent.
find_converter() {
    local version="$1"
    local dashed="${version//./-}"
    local root candidate
    local IFS=':'
    for root in $INSTALL_ROOT; do
        [[ -n "$root" ]] || continue
        candidate="$root/EnergyPlus-$dashed/ConvertInputFormat"
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# Case ids to process: the arguments, or every directory under cases/.
collect_case_ids() {
    if (($# > 0)); then
        printf '%s\n' "$@"
        return
    fi
    [[ -d "$CASES_DIR" ]] || return 0
    find "$CASES_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort
}

updated=()
unchanged=()
skipped_convention=()
skipped_no_install=()
skipped_no_input=()
failed=()
missing_versions=()

case_ids=$(collect_case_ids "$@")

if [[ -n "$case_ids" ]]; then
    tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/idfkit-conformance.XXXXXX")"
    trap 'rm -rf "$tmp_dir"' EXIT
fi

while IFS= read -r case_id; do
    [[ -n "$case_id" ]] || continue
    case_dir="$CASES_DIR/$case_id"
    manifest="$case_dir/case.toml"

    if [[ ! -f "$manifest" ]]; then
        echo "skip  $case_id: no case.toml"
        continue
    fi

    truth="$(read_toml_string truth "$manifest")"
    if [[ "$truth" != "oracle" ]]; then
        # truth = "convention" has no external authority to ask, and forbids
        # expected.epJSON. Not an error.
        skipped_convention+=("$case_id")
        continue
    fi

    version="$(read_toml_string energyplus_version "$manifest")"
    if [[ -z "$version" ]]; then
        echo "FAIL  $case_id: truth = \"oracle\" but no energyplus_version" >&2
        failed+=("$case_id: missing energyplus_version")
        continue
    fi

    input="$case_dir/input.idf"
    if [[ ! -f "$input" ]]; then
        # The oracle converts IDF to epJSON. A case whose input is already epJSON has
        # nothing to regenerate here.
        skipped_no_input+=("$case_id")
        continue
    fi

    if ! converter="$(find_converter "$version")"; then
        skipped_no_install+=("$case_id ($version)")
        missing_versions+=("$version")
        continue
    fi

    out_dir="$tmp_dir/$case_id"
    rm -rf "$out_dir"
    mkdir -p "$out_dir"

    if ! "$converter" -f epJSON -o "$out_dir/" "$input" >"$out_dir/converter.log" 2>&1; then
        echo "FAIL  $case_id: ConvertInputFormat $version exited non-zero" >&2
        sed 's/^/      /' "$out_dir/converter.log" >&2
        failed+=("$case_id: converter rejected the input")
        continue
    fi

    produced="$out_dir/input.epJSON"
    if [[ ! -f "$produced" ]]; then
        echo "FAIL  $case_id: ConvertInputFormat $version produced no epJSON" >&2
        sed 's/^/      /' "$out_dir/converter.log" >&2
        failed+=("$case_id: no output produced")
        continue
    fi

    expected="$case_dir/expected.epJSON"
    if [[ -f "$expected" ]] && cmp -s "$produced" "$expected"; then
        unchanged+=("$case_id")
        echo "ok    $case_id ($version) unchanged"
    else
        cp "$produced" "$expected"
        updated+=("$case_id")
        echo "ok    $case_id ($version) written"
    fi
done <<<"$case_ids"

# Report. Skips are the expected outcome on a partial set of installs, so they are
# always listed, never implied.
echo
echo "Summary"
echo "  install roots searched: $INSTALL_ROOT"
echo "  written:   ${#updated[@]}"
echo "  unchanged: ${#unchanged[@]}"
echo "  skipped (truth = convention): ${#skipped_convention[@]}"
echo "  skipped (no input.idf):       ${#skipped_no_input[@]}"
echo "  skipped (EnergyPlus missing): ${#skipped_no_install[@]}"
echo "  failed:    ${#failed[@]}"

if ((${#missing_versions[@]} > 0)); then
    echo
    echo "EnergyPlus versions not installed locally, so their cases were left untouched:"
    printf '%s\n' "${missing_versions[@]}" | sort -u | sed 's/^/  /'
    echo
    echo "Cases left untouched:"
    printf '  %s\n' "${skipped_no_install[@]}"
fi

if ((${#skipped_convention[@]} > 0)); then
    echo
    echo "Convention cases, which have no oracle and no expected.epJSON:"
    printf '  %s\n' "${skipped_convention[@]}"
fi

if ((${#skipped_no_input[@]} > 0)); then
    echo
    echo "Oracle cases with no input.idf to convert:"
    printf '  %s\n' "${skipped_no_input[@]}"
fi

if ((${#failed[@]} > 0)); then
    echo
    echo "Failures:"
    printf '  %s\n' "${failed[@]}"
    exit 1
fi
