#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage:
  scripts/test-postgres.sh [PYTEST_ARG...]
  scripts/test-postgres.sh -- COMMAND [ARG...]

Starts a scratch password-authenticated PostgreSQL server, exports
DR_STORE_POSTGRES_DSN and DR_STORE_REQUIRE_POSTGRES, then runs work against it
and tears the server down on success, failure, and interrupt.

A leading '--' selects command mode: the rest of the line is a command run
against the scratch server, and its exit code is propagated. This lets consumer
repositories reuse the scratch-server mechanics without duplicating them, for
example:

  scripts/test-postgres.sh -- uv run pytest tests/evaluation -q

Otherwise the arguments are dr-store's own suite: 'uv run pytest -q' over the
given paths, or over 'tests' when no arguments are supplied. Every argument
reaches pytest verbatim, including a later '--' and pytest's own '-h'; this
usage text prints only when '-h' or '--help' is the sole argument.
USAGE
}

command_mode=0
declare -a supplied_command=()
if [[ "$#" -eq 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
    usage
    exit 0
fi
if [[ "$#" -gt 0 && "$1" == "--" ]]; then
    command_mode=1
    shift
    supplied_command=("$@")
    if [[ "${#supplied_command[@]}" -eq 0 ]]; then
        printf '%s\n' 'A command is required after "--".' >&2
        usage >&2
        exit 2
    fi
    set --
fi

script_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
    pwd -P
)"
repository_root="$(
    cd -- "${script_directory}/.."
    pwd -P
)"

postgres_bin="${DR_STORE_POSTGRES_BIN:-}"
if [[ -z "${postgres_bin}" ]] && command -v initdb >/dev/null 2>&1; then
    postgres_bin="$(dirname -- "$(command -v initdb)")"
fi
if [[ -z "${postgres_bin}" ]] && command -v brew >/dev/null 2>&1; then
    for formula in postgresql@18 postgresql@17 postgresql@16; do
        candidate="$(brew --prefix "${formula}" 2>/dev/null || true)/bin"
        if [[ -x "${candidate}/initdb" ]]; then
            postgres_bin="${candidate}"
            break
        fi
    done
fi
if [[ -z "${postgres_bin}" || ! -d "${postgres_bin}" ]]; then
    printf '%s\n' \
        'PostgreSQL 16-18 tools were not found; set DR_STORE_POSTGRES_BIN.' >&2
    exit 1
fi
postgres_bin="$(cd -- "${postgres_bin}" && pwd -P)"
for tool in initdb pg_ctl createdb psql; do
    if [[ ! -x "${postgres_bin}/${tool}" ]]; then
        printf 'Required PostgreSQL tool is not executable: %s/%s\n' \
            "${postgres_bin}" "${tool}" >&2
        exit 1
    fi
done

temporary_base="/tmp"
temporary_base="$(cd -- "${temporary_base}" && pwd -P)"
temporary_root="$(
    mktemp -d "${temporary_base}/dr-store-postgres.XXXXXXXX"
)"
temporary_root="$(cd -- "${temporary_root}" && pwd -P)"
temporary_parent="$(dirname -- "${temporary_root}")"
data_directory="${temporary_root}/data"
socket_directory="${temporary_root}/socket"
server_started=0

cleanup() {
    local cleanup_target="${temporary_root:-}"

    if [[ -z "${cleanup_target}" \
        || ! -d "${cleanup_target}" \
        || -L "${cleanup_target}" \
        || "$(dirname -- "${cleanup_target}")" != "${temporary_parent}" \
        || "$(basename -- "${cleanup_target}")" != dr-store-postgres.* ]]; then
        printf 'Refusing to clean invalid PostgreSQL test directory: %s\n' \
            "${cleanup_target}" >&2
        return 1
    fi

    if [[ "${server_started}" -eq 1 ]]; then
        if ! "${postgres_bin}/pg_ctl" \
            -D "${data_directory}" -m immediate -w stop >/dev/null; then
            printf 'PostgreSQL did not stop; preserving %s\n' \
                "${cleanup_target}" >&2
            return 1
        fi
        server_started=0
    fi

    rm -rf -- "${cleanup_target:?}"
}

on_exit() {
    local exit_status=$?

    trap - EXIT
    if ! cleanup && [[ "${exit_status}" -eq 0 ]]; then
        exit_status=1
    fi
    exit "${exit_status}"
}

trap on_exit EXIT

if [[ "${temporary_parent}" != "${temporary_base}" \
    || "$(basename -- "${temporary_root}")" != dr-store-postgres.* \
    || "${temporary_root}" == *"'"* \
    || "${temporary_root}" == *$'\n'* ]]; then
    printf 'mktemp returned an unsafe directory: %s\n' \
        "${temporary_root}" >&2
    exit 1
fi

mkdir -- "${socket_directory}"
password_file="${temporary_root}/password"
umask 077
python3 -c \
    'import secrets; print(secrets.token_urlsafe(36))' >"${password_file}"
postgres_password="$(<"${password_file}")"
if [[ -z "${postgres_password}" ]]; then
    printf '%s\n' 'Failed to generate a PostgreSQL test password.' >&2
    exit 1
fi

"${postgres_bin}/initdb" \
    -D "${data_directory}" \
    --username=postgres \
    --pwfile="${password_file}" \
    --auth-local=scram-sha-256 \
    --auth-host=reject \
    --encoding=UTF8 \
    --no-locale >/dev/null
printf "listen_addresses = ''\nunix_socket_directories = '%s'\n" \
    "${socket_directory}" >>"${data_directory}/postgresql.conf"

if ! "${postgres_bin}/pg_ctl" \
    -D "${data_directory}" \
    -l "${temporary_root}/postgres.log" \
    -w start >/dev/null; then
    sed -n '1,160p' "${temporary_root}/postgres.log" >&2
    exit 1
fi
server_started=1

export PGHOST="${socket_directory}"
export PGPORT=5432
export PGUSER=postgres
export PGPASSWORD="${postgres_password}"

if PGPASSWORD=dr-store-deliberately-wrong-password \
    "${postgres_bin}/psql" -d postgres -XAtqc 'SELECT 1' \
        >/dev/null 2>&1; then
    printf '%s\n' 'PostgreSQL unexpectedly accepted an incorrect password.' >&2
    exit 1
fi
"${postgres_bin}/psql" -d postgres -XAtqc 'SELECT 1' >/dev/null
"${postgres_bin}/createdb" dr_store_test

encoded_socket="$(
    python3 -c \
        'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' \
        "${socket_directory}"
)"
export DR_STORE_POSTGRES_DSN="postgresql://postgres@/dr_store_test?host=${encoded_socket}"
export DR_STORE_REQUIRE_POSTGRES=1

cd -- "${repository_root}"
if [[ "${command_mode}" -eq 1 ]]; then
    "${supplied_command[@]}"
    exit
fi

if [[ "$#" -eq 0 ]]; then
    set -- tests
fi
uv run pytest -q "$@"

printf '%s\n' \
    'password-authenticated PostgreSQL backend integration passed'
