#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage:
  scripts/check-compatibility.sh [--baseline VERSION] [--consumer PATH]

Checks that this working tree stays compatible with an already-released
dr-store, in both directions, by installing that release into a throwaway
virtual environment and exchanging artifact bundles between the two.

A test suite imports exactly one dr_store, so it cannot express these checks;
they need two versions resident at once. This script is that layer. It is not
run by CI or by any hook: it needs network access to reach PyPI and spends a
minute or so building the baseline environment. Run it before publishing a
release, and whenever a change touches the on-disk bundle format, the public
API surface, or anything the compatibility claims in .defs/contracts.toml rest
on.

Options:
  --baseline VERSION  Released dr-store to check against. Defaults to
                      DEFAULT_BASELINE below, which should name the oldest
                      release whose recorded bundles must still read.
  --consumer PATH     Also validate a consumer checkout that resolves this
                      working tree (an editable [tool.uv.sources] path):
                      runs its test suite and its reuse of
                      scripts/test-postgres.sh command mode.

Checks:
  1. local writer    -> baseline reader   (bundles this tree writes stay readable)
  2. baseline writer -> local reader      (bundles that release wrote still read)
  3. public API surface present in both
  4. consumer suite                       (with --consumer)
  5. consumer reuse of command mode       (with --consumer)
USAGE
}

# The oldest release whose recorded bundles must still read unchanged. Raise
# this only when a format break is deliberately accepted and contracted.
DEFAULT_BASELINE="0.2.0"

baseline="${DEFAULT_BASELINE}"
consumer=""

while [[ "$#" -gt 0 ]]; do
    case "$1" in
    -h | --help)
        usage
        exit 0
        ;;
    --baseline)
        if [[ "$#" -lt 2 ]]; then
            printf '%s\n' '--baseline requires a version.' >&2
            exit 2
        fi
        baseline="$2"
        shift 2
        ;;
    --consumer)
        if [[ "$#" -lt 2 ]]; then
            printf '%s\n' '--consumer requires a path.' >&2
            exit 2
        fi
        consumer="$2"
        shift 2
        ;;
    *)
        printf 'Unrecognized argument: %s\n' "$1" >&2
        usage >&2
        exit 2
        ;;
    esac
done

script_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
    pwd -P
)"
repository_root="$(
    cd -- "${script_directory}/.."
    pwd -P
)"

if [[ -n "${consumer}" ]]; then
    if [[ ! -d "${consumer}" ]]; then
        printf 'Consumer path is not a directory: %s\n' "${consumer}" >&2
        exit 2
    fi
    consumer="$(
        cd -- "${consumer}"
        pwd -P
    )"
fi

temporary_root=""
temporary_parent=""

cleanup() {
    local cleanup_target="${temporary_root:-}"

    if [[ -z "${cleanup_target}" ]]; then
        return 0
    fi
    if [[ ! -d "${cleanup_target}" || -L "${cleanup_target}" ]]; then
        printf 'Refusing to clean invalid temporary directory: %s\n' \
            "${cleanup_target}" >&2
        return 1
    fi
    if [[ "$(dirname -- "${cleanup_target}")" != "${temporary_parent}" ]]; then
        printf 'Refusing to clean temporary directory outside its parent: %s\n' \
            "${cleanup_target}" >&2
        return 1
    fi
    rm -rf -- "${cleanup_target}"
}
trap cleanup EXIT

# Explicit template: `mktemp -t PREFIX` without one is a BSD extension that
# GNU mktemp rejects. Both paths are resolved before the cleanup guard compares
# them, since /tmp is a symlink on macOS.
temporary_base="/tmp"
temporary_base="$(cd -- "${temporary_base}" && pwd -P)"
temporary_root="$(
    mktemp -d "${temporary_base}/dr-store-compatibility.XXXXXXXX"
)"
temporary_root="$(cd -- "${temporary_root}" && pwd -P)"
temporary_parent="$(dirname -- "${temporary_root}")"

if [[ "${temporary_parent}" != "${temporary_base}" \
    || "$(basename -- "${temporary_root}")" != dr-store-compatibility.* ]]; then
    printf 'mktemp returned an unsafe directory: %s\n' \
        "${temporary_root}" >&2
    exit 1
fi

baseline_root="${temporary_root}/baseline"
bundles="${temporary_root}/bundles"
mkdir -p -- "${baseline_root}" "${bundles}"

printf '== dr-store compatibility check ==\n'
printf 'working tree : %s\n' "${repository_root}"
printf 'baseline     : dr-store %s\n' "${baseline}"
if [[ -n "${consumer}" ]]; then
    printf 'consumer     : %s\n' "${consumer}"
fi
printf '\n'

# The baseline environment deliberately holds nothing but the released
# dr-store: it stands in for a consumer that has not upgraded.
printf -- '-- building the baseline environment --\n'
uv venv -q -- "${baseline_root}/.venv"
baseline_python="${baseline_root}/.venv/bin/python"
VIRTUAL_ENV="${baseline_root}/.venv" uv pip install -q "dr-store==${baseline}"
resolved_baseline="$(
    "${baseline_python}" -c \
        'import importlib.metadata as m; print(m.version("dr-store"))'
)"
if [[ "${resolved_baseline}" != "${baseline}" ]]; then
    printf 'Baseline environment resolved %s, expected %s.\n' \
        "${resolved_baseline}" "${baseline}" >&2
    exit 1
fi
printf 'baseline dr-store %s installed\n\n' "${resolved_baseline}"

local_python="$(
    cd -- "${repository_root}"
    uv run --frozen python -c 'import sys; print(sys.executable)'
)"
local_version="$(
    "${local_python}" -c \
        'import importlib.metadata as m; print(m.version("dr-store"))'
)"
printf 'working-tree dr-store %s\n\n' "${local_version}"

write_bundle() {
    # $1 python, $2 destination root, $3 label recorded in the payload,
    # remaining arguments: artifact names to create.
    local python="$1" destination="$2" label="$3"
    shift 3
    mkdir -p -- "${destination}"
    "${python}" - "${destination}" "${label}" "$@" <<'PYTHON'
import pathlib
import sys

from dr_store import ArtifactBundlePublication

destination, label, *names = sys.argv[1:]
publication = ArtifactBundlePublication.allocate(
    pathlib.Path(destination), prefix="compat"
)
for name in names:
    writer = publication.open_artifact(name)
    writer.write(f"artifact {name} written by {label}".encode())
    writer.finalize()
publication.publish({"written_by": label})
print(publication.path)
PYTHON
}

read_bundle() {
    # $1 python, $2 bundle path, remaining arguments: artifact names to verify.
    local python="$1" bundle="$2"
    shift 2
    "${python}" - "${bundle}" "$@" <<'PYTHON'
import pathlib
import sys

from dr_store import ArtifactBundleReader, BundleReadLimits

bundle_path, *names = sys.argv[1:]
limits = BundleReadLimits(
    manifest_max_bytes=1 << 20,
    manifest_max_depth=64,
    max_artifacts=64,
    max_bytes_per_artifact=1 << 20,
    max_total_artifact_bytes=4 << 20,
)
bundle = pathlib.Path(bundle_path)
manifest = ArtifactBundleReader(bundle, limits=limits).audit()
declared = sorted(descriptor.name for descriptor in manifest.artifacts)
if declared != sorted(names):
    raise SystemExit(f"declared {declared}, expected {sorted(names)}")

written_by = manifest.payload["written_by"]
for name in names:
    delivered: list[bytes] = []
    ArtifactBundleReader(bundle, limits=limits).consume_and_verify_artifact(
        name, lambda reader: delivered.append(reader.read())
    )
    expected = f"artifact {name} written by {written_by}".encode()
    if delivered != [expected]:
        raise SystemExit(f"artifact {name!r} delivered {delivered!r}")
print(f"format {manifest.format}, {len(names)} artifact(s) verified")
PYTHON
}

# The names an earlier release admitted are part of what compatibility means:
# publication may refuse a name later, but bundles already recorded with it
# must keep reading. The baseline writes them; this tree only has to read them.
ordinary_artifact="projection-samples.jsonl"
baseline_only_artifact=".dr-store-document-recorded-by-baseline"

printf -- '-- 1. local writer -> baseline reader --\n'
forward_bundle="$(
    cd -- "${repository_root}"
    write_bundle "${local_python}" "${bundles}/forward" \
        "local ${local_version}" "${ordinary_artifact}"
)"
read_bundle "${baseline_python}" "${forward_bundle}" "${ordinary_artifact}"
printf 'bundles this tree writes stay readable by %s\n\n' "${baseline}"

printf -- '-- 2. baseline writer -> local reader --\n'
backward_bundle="$(
    write_bundle "${baseline_python}" "${bundles}/backward" \
        "baseline ${baseline}" \
        "${ordinary_artifact}" "${baseline_only_artifact}"
)"
read_bundle "${local_python}" "${backward_bundle}" \
    "${ordinary_artifact}" "${baseline_only_artifact}"
printf 'bundles %s wrote still read here, including names it admitted\n\n' \
    "${baseline}"

printf -- '-- 3. public API surface --\n'
surface_script="${temporary_root}/surface.py"
cat >"${surface_script}" <<'PYTHON'
import dr_store

missing = [name for name in dr_store.__all__ if not hasattr(dr_store, name)]
if missing:
    raise SystemExit(f"exported but absent: {missing}")
for name in sorted(dr_store.__all__):
    print(name)
PYTHON
baseline_surface="${temporary_root}/surface-baseline.txt"
local_surface="${temporary_root}/surface-local.txt"
"${baseline_python}" "${surface_script}" >"${baseline_surface}"
"${local_python}" "${surface_script}" >"${local_surface}"

# Names the baseline exported must still resolve; new names are additive and
# fine. A rename shows up here as a removal, which is what makes comparing
# names rather than counts worth the extra step.
dropped="$(comm -23 "${baseline_surface}" "${local_surface}")"
printf 'baseline exports %s names, working tree exports %s\n' \
    "$(wc -l <"${baseline_surface}" | tr -d ' ')" \
    "$(wc -l <"${local_surface}" | tr -d ' ')"
if [[ -n "${dropped}" ]]; then
    printf 'Public names present in %s are gone from this tree:\n' \
        "${baseline}" >&2
    printf '%s\n' "${dropped}" >&2
    printf 'Removing a public name is a deliberate break; contract it.\n' >&2
    exit 1
fi
printf 'every baseline export is still present\n\n'

if [[ -z "${consumer}" ]]; then
    printf 'compatibility with dr-store %s verified\n' "${baseline}"
    printf 'pass --consumer PATH to also validate a consumer checkout\n'
    exit 0
fi

printf -- '-- 4. consumer suite --\n'
consumer_resolved="$(
    cd -- "${consumer}"
    uv run python -c 'import dr_store; print(dr_store.__file__)'
)"
if [[ "${consumer_resolved}" != "${repository_root}/"* ]]; then
    printf 'Consumer resolves dr_store from %s,\n' "${consumer_resolved}" >&2
    printf 'which is outside this working tree. Point it at this checkout\n' >&2
    printf 'with an editable [tool.uv.sources] entry before validating.\n' >&2
    exit 1
fi
printf 'consumer resolves dr_store from this working tree\n'
(
    cd -- "${consumer}"
    uv run pytest -q
)
printf '\n'

printf -- '-- 5. consumer reuse of command mode --\n'
# Command mode runs in the caller's directory, so a consumer's own relative
# paths resolve against the consumer. Exercising it from there is the check.
(
    cd -- "${consumer}"
    "${script_directory}/test-postgres.sh" -- uv run pytest -q
)
printf '\n'

printf 'compatibility with dr-store %s verified, consumer %s validated\n' \
    "${baseline}" "${consumer}"
