#!/usr/bin/env bash
# test-session-start.sh — Tests for the session-start hook's config sanity checks
# Tests: legacy key detection, github_org drift, workspace resolution
# Usage: bash tests/test-session-start.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SESSION_START="$REPO_ROOT/hooks/scripts/linear-session-start.py"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf "${GREEN}  PASS${NC} %s\n" "$1"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf "${RED}  FAIL${NC} %s\n" "$1"
  if [ -n "${2:-}" ]; then
    printf "       %s\n" "$2"
  fi
}

section() {
  printf "\n${YELLOW}── %s ──${NC}\n" "$1"
}

# ---------- fixtures ----------

TEMP_DIR=""
setup_test_repo() {
  local config="$1"
  local state="$2"
  local remote_org="${3:-crystal-peak}"

  TEMP_DIR=$(mktemp -d)
  mkdir -p "$TEMP_DIR/test-repo/.claude"
  mkdir -p "$TEMP_DIR/state-dir"

  # Init git repo with a remote pointing to the specified org
  (
    cd "$TEMP_DIR/test-repo"
    git init -q
    git commit --allow-empty -m "init" -q
    git remote add origin "https://github.com/${remote_org}/test-repo.git" 2>/dev/null || true
  )

  # Write repo config
  echo "$config" > "$TEMP_DIR/test-repo/.claude/linear-sync.json"

  # Write state
  echo "$state" > "$TEMP_DIR/state-dir/state.json"
}

cleanup() {
  if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
    rm -rf "$TEMP_DIR"
  fi
}

trap cleanup EXIT

# Run session-start hook with test fixtures
run_session_start() {
  local cwd="$TEMP_DIR/test-repo"
  local input
  input=$(python3 -c "
import json
print(json.dumps({'cwd': '$cwd'}))
")
  local output=""
  local exit_code=0
  output=$(printf '%s' "$input" | STATE_FILE_OVERRIDE="$TEMP_DIR/state-dir/state.json" python3 "$SESSION_START" 2>&1) || exit_code=$?
  echo "$exit_code|$output"
}

output_contains() {
  echo "$1" | grep -q "$2"
}

# ==========================================================================
#  LEGACY KEY DETECTION
# ==========================================================================

section "Legacy Config Key Detection"

setup_test_repo '{
  "workspace": "test-ws",
  "teamId": "abc123",
  "teamKey": "TEST",
  "projectId": "proj123",
  "labelId": "label123",
  "label": "repo:test"
}' '{
  "workspaces": {
    "test-ws": {"name": "Test", "mcp_server": "linear"}
  },
  "repos": {}
}'

RESULT=$(run_session_start)
OUTPUT="${RESULT#*|}"

if output_contains "$OUTPUT" "Legacy config keys"; then
  pass "detects legacy config keys (teamId, teamKey, projectId, labelId)"
else
  fail "should detect legacy config keys" "$OUTPUT"
fi
cleanup

# ==========================================================================
#  GITHUB_ORG DRIFT
# ==========================================================================

section "GitHub Org Drift Detection"

# Config says 'old-org' but git remote points to 'crystal-peak'
setup_test_repo '{
  "workspace": "test-ws",
  "team": "TEST",
  "label": "repo:test",
  "github_org": "old-org"
}' '{
  "workspaces": {
    "test-ws": {"name": "Test", "mcp_server": "linear"}
  },
  "repos": {}
}' "crystal-peak"

RESULT=$(run_session_start)
OUTPUT="${RESULT#*|}"

if output_contains "$OUTPUT" "github_org mismatch"; then
  pass "detects github_org drift (config: old-org, remote: crystal-peak)"
else
  fail "should detect github_org drift" "$OUTPUT"
fi
cleanup

# No drift when orgs match
setup_test_repo '{
  "workspace": "test-ws",
  "team": "TEST",
  "label": "repo:test",
  "github_org": "crystal-peak"
}' '{
  "workspaces": {
    "test-ws": {"name": "Test", "mcp_server": "linear"}
  },
  "repos": {}
}' "crystal-peak"

RESULT=$(run_session_start)
OUTPUT="${RESULT#*|}"

if output_contains "$OUTPUT" "github_org mismatch"; then
  fail "should NOT detect drift when orgs match" "$OUTPUT"
else
  pass "no false positive when github_org matches remote"
fi
cleanup

# ==========================================================================
#  CLEAN CONFIG (no warnings)
# ==========================================================================

section "Clean Config (No Warnings)"

setup_test_repo '{
  "workspace": "test-ws",
  "project": "Test Project",
  "team": "TEST",
  "label": "repo:test-repo",
  "github_org": "crystal-peak"
}' '{
  "workspaces": {
    "test-ws": {"name": "Test", "mcp_server": "linear"}
  },
  "repos": {
    "test-repo": {"workspace": "test-ws"}
  }
}'

RESULT=$(run_session_start)
OUTPUT="${RESULT#*|}"

if output_contains "$OUTPUT" "Legacy config keys" || output_contains "$OUTPUT" "github_org mismatch"; then
  fail "clean config should produce no warnings" "$OUTPUT"
else
  pass "clean config produces no sanity warnings"
fi
cleanup

# ==========================================================================
#  SUMMARY
# ==========================================================================

printf "\n${YELLOW}═══════════════════════════════════════${NC}\n"
printf "Results: ${GREEN}%d passed${NC}, ${RED}%d failed${NC}\n" "$PASS_COUNT" "$FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
