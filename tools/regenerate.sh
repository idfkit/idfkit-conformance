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
# Separately, for every case that declares the post-operation-epjson assertion, it runs
# the same converter on cases/<id>/after.idf, the input with the case's operations made
# textually, and places the result at cases/<id>/expected.after.epJSON. This happens
# whatever the case's truth, because the two files answer different questions: a
# convention case still has no expected.epJSON, since nothing external rules on how its
# input is read, while its post-operation expectation is still produced by the oracle
# from a committed input rather than written by hand.
#
# A case with truth = "convention" that declares no post-operation-epjson has nothing
# to regenerate. It is skipped without error (FR-020).
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
    sed -n '3,41p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

# Succeed when the case.toml's assertions list names the given assertion. Like
# read_toml_string, this ignores triple-quoted blocks, so prose in `why` cannot count.
declares_assertion() {
    local assertion="$1" file="$2"
    awk -v want="\"$assertion\"" '
        { line = $0; probe = line; fences = gsub(/"""/, "", probe) }
        inblock { if (fences % 2 == 1) inblock = 0; next }
        {
            if (fences % 2 == 1) { inblock = 1; next }
            if (line ~ /^[ \t]*assertions[ \t]*=/ && index(line, want) > 0) { found = 1; exit }
        }
        END { exit found ? 0 : 1 }
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
    post_operation=0
    if declares_assertion post-operation-epjson "$manifest"; then
        post_operation=1
    fi
    if [[ "$truth" != "oracle" && "$post_operation" -eq 0 ]]; then
        # truth = "convention" has no external authority to ask, and forbids
        # expected.epJSON. Not an error.
        skipped_convention+=("$case_id")
        continue
    fi

    version="$(read_toml_string energyplus_version "$manifest")"
    if [[ -z "$version" ]]; then
        echo "FAIL  $case_id: an oracle expectation is owed but there is no energyplus_version" >&2
        failed+=("$case_id: missing energyplus_version")
        continue
    fi

    # Each conversion is a source in the case directory and the expectation it produces.
    conversions=()
    if [[ "$truth" == "oracle" ]]; then
        conversions+=("input.idf expected.epJSON")
    fi
    if [[ "$post_operation" -eq 1 ]]; then
        conversions+=("after.idf expected.after.epJSON")
    fi

    for conversion in "${conversions[@]}"; do
        source_name="${conversion%% *}"
        target_name="${conversion##* }"
        label="$case_id/$target_name"
        input="$case_dir/$source_name"
        if [[ ! -f "$input" ]]; then
            if [[ "$source_name" == "after.idf" ]]; then
                echo "FAIL  $case_id: declares post-operation-epjson but has no after.idf" >&2
                failed+=("$case_id: missing after.idf")
            else
                # The oracle converts IDF to epJSON. A case whose input is already epJSON
                # has nothing to regenerate here.
                skipped_no_input+=("$case_id")
            fi
            continue
        fi

        if ! converter="$(find_converter "$version")"; then
            skipped_no_install+=("$label ($version)")
            missing_versions+=("$version")
            continue
        fi

        out_dir="$tmp_dir/$case_id/${source_name%.idf}"
        rm -rf "$out_dir"
        mkdir -p "$out_dir"

        if ! "$converter" -f epJSON -o "$out_dir/" "$input" >"$out_dir/converter.log" 2>&1; then
            echo "FAIL  $label: ConvertInputFormat $version exited non-zero" >&2
            sed 's/^/      /' "$out_dir/converter.log" >&2
            failed+=("$label: converter rejected $source_name")
            continue
        fi

        produced="$out_dir/${source_name%.idf}.epJSON"
        if [[ ! -f "$produced" ]]; then
            echo "FAIL  $label: ConvertInputFormat $version produced no epJSON" >&2
            sed 's/^/      /' "$out_dir/converter.log" >&2
            failed+=("$label: no output produced")
            continue
        fi

        expected="$case_dir/$target_name"
        if [[ -f "$expected" ]] && cmp -s "$produced" "$expected"; then
            unchanged+=("$label")
            echo "ok    $label ($version) unchanged"
        else
            cp "$produced" "$expected"
            updated+=("$label")
            echo "ok    $label ($version) written"
        fi
    done
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
    echo "Convention cases with no post-operation-epjson, which have nothing to regenerate:"
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
