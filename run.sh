#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

pipenv run ./manage.py sendalerts &
alerts_pid=$!

cleanup() {
    kill "$alerts_pid" 2>/dev/null || true
    wait "$alerts_pid" 2>/dev/null || true
}

trap cleanup EXIT
trap 'exit 130' INT

pipenv run ./manage.py runserver 0.0.0.0:8000 "$@"
