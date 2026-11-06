#!/usr/bin/env bash
# The gate. The Stop hook, /harness:commit, the reviewer and CI all run this.
# Keep it fast (aim under 2 minutes) and deterministic. Add a step here
# whenever a mistake can be caught mechanically. That beats a line in CLAUDE.md.
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n== %s\n' "$1"; }
fail() { echo "VERIFY FAILED: $1" >&2; exit 1; }

# Uncomment the block for this repo's stack; delete the others.

# --- Go ---
# step "gofmt";  [ -z "$(gofmt -l .)" ] || { gofmt -l .; fail "gofmt"; }
# step "vet";    go vet ./...                || fail "go vet"
# step "lint";   golangci-lint run           || fail "golangci-lint"
# step "test";   go test -race ./...         || fail "go test"

# --- Python (uv) ---
step "format"; uv run ruff format --check . || fail "ruff format"
step "lint";   uv run ruff check .          || fail "ruff check"
step "types";  uv run mypy .                || fail "mypy"
step "test";   uv run pytest -q             || fail "pytest"

# --- Optional path fence: scripts/check-scope.sh <path> exits non-zero for a forbidden path ---
# step "scope"
# if [ -x scripts/check-scope.sh ]; then
#   base="$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main)"
#   git diff --name-only "$base" | while read -r f; do scripts/check-scope.sh "$f" || exit 1; done || fail "scope"
# fi

