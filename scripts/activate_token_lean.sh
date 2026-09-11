#!/usr/bin/env bash
set -euo pipefail

VERIFY_ONLY=0
case "${1:-}" in
  "") ;;
  --verify-only) VERIFY_ONLY=1 ;;
  *) echo "Usage: $0 [--verify-only]" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
CONFIG="$HERMES_HOME_DIR/config.yaml"
BACKUP_DIR="$HERMES_HOME_DIR/.token-lean-backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_DIR/config.yaml.$STAMP"
REPORT_DIR="$HERMES_HOME_DIR/token-economy/reports"
REPORT="$REPORT_DIR/activation-$STAMP.json"
ACTIVATED=0
HAD_CONFIG=0
SERVICE_RESTART_ATTEMPTED=0

# Measurements must represent an ordinary fresh CLI session, not inherit a
# dispatcher/worker identity from the shell that launched this verifier.
unset HERMES_KANBAN_TASK HERMES_SESSION_SOURCE HERMES_SESSION_PLATFORM HERMES_PLATFORM

fail() { echo "ERROR: $*" >&2; exit 1; }

branch="$(git -C "$ROOT" branch --show-current)"
[[ "$branch" == hermesx/token-lean-* ]] || fail "refusing activation from unexpected branch: $branch"
MIN_UPSTREAM_BASE="53e32d0581"
git -C "$ROOT" merge-base --is-ancestor "$MIN_UPSTREAM_BASE" HEAD   || fail "refusing activation from stale token-lean branch; latest required upstream base $MIN_UPSTREAM_BASE is absent"
tracked_dirty="$(git -C "$ROOT" status --porcelain --untracked-files=no)"
[[ -z "$tracked_dirty" ]] || fail "refusing activation from a dirty tracked tree; commit the token-lean rollout first"
for required in   agent/token_economy.py   agent/token_economy_store.py   tools/token_lean_profile.py   tools/lean_tool_schemas.py   tools/token_economy_tools.py   scripts/activate_token_lean.sh; do
  [[ -f "$ROOT/$required" ]] || fail "missing token-lean rollout file: $required"
done

mkdir -p "$BACKUP_DIR" "$REPORT_DIR"
if [[ -f "$CONFIG" ]]; then
  cp -p "$CONFIG" "$BACKUP"
  HAD_CONFIG=1
else
  : > "$BACKUP.missing"
fi

rollback() {
  rc=$?
  if [[ "$ACTIVATED" != "1" ]]; then
    echo "Token-lean activation did not complete; restoring original config." >&2
    if [[ "$HAD_CONFIG" == "1" ]]; then
      cp -p "$BACKUP" "$CONFIG"
    else
      rm -f "$CONFIG"
    fi
    if [[ "$SERVICE_RESTART_ATTEMPTED" == "1" ]]; then
      echo "Restoring Hermes gateway on the original config..." >&2
      systemctl --user restart hermes-gateway-freebrain.service >/dev/null 2>&1 || true
    fi
  fi
  exit "$rc"
}
trap rollback EXIT

UV=""
for candidate in "$HOME/.local/bin/uv" "$(command -v uv 2>/dev/null || true)"; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then UV="$candidate"; break; fi
done
[[ -n "$UV" ]] || fail "uv is required but was not found"

printf 'Hermes token-lean source: %s\n' "$ROOT"
printf 'Branch: %s\n' "$(git -C "$ROOT" branch --show-current)"
printf 'Commit: %s\n' "$(git -C "$ROOT" rev-parse --short=12 HEAD)"
printf 'Config backup: %s\n' "$BACKUP"

# Keep the existing runtime extras/lazy-installed provider packages and add the
# locked dev + Hindsight extras needed by this verifier's regression surface.
# `uv sync` is exact by default; --inexact is load-bearing here because activation
# must not uninstall optional capabilities that are not selected by this
# verification run. Hindsight is explicit because the suite exercises its real
# embedded-client/background-retain paths and security.allow_lazy_installs may be
# false on production machines, so tests must not depend on a runtime lazy install.
UV_PROJECT_ENVIRONMENT="$ROOT/.venv" "$UV" sync --extra dev --extra hindsight --locked --inexact
PY="$ROOT/.venv/bin/python"
HERMES="$ROOT/.venv/bin/hermes"
[[ -x "$PY" && -x "$HERMES" ]] || fail "Hermes virtualenv is incomplete after uv sync"
"$PY" -m pytest --version >/dev/null

# A global MoA primary multiplies one user turn into several model requests and
# invalidates token-lean guarantees. Preserve the /moa capability, but require an
# explicit operator opt-in before activating token economy with MoA as the main
# provider. This reads only the provider slug; credentials are never printed.
PRIMARY_PROVIDER="$(PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PY" - <<'PYMODEL'
from hermes_cli.config import load_config_readonly
from agent.token_economy import configured_primary_provider
print(configured_primary_provider(load_config_readonly()))
PYMODEL
)"
printf 'Primary provider: %s\n' "$PRIMARY_PROVIDER"
if [[ "${PRIMARY_PROVIDER,,}" == "moa" ]]; then
  case "${HERMES_TOKEN_LEAN_ALLOW_MOA:-0}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) fail "global model.provider=moa fans each turn out to multiple models; choose a single-model primary first, or explicitly set HERMES_TOKEN_LEAN_ALLOW_MOA=1 to accept that token cost. /moa remains available for intentional use." ;;
  esac
fi

# Regression surface: token economy itself plus every subsystem whose prompt,
# tool visibility, memory, result budgeting, and Kanban lifecycle it changes.
echo "Running token-economy regression suite..."
"$PY" -m pytest -q \
  tests/tools/test_token_lean_profile.py \
  tests/tools/test_lean_tool_schemas.py \
  tests/tools/test_tool_search.py \
  tests/tools/test_file_tools.py \
  tests/agent/test_token_economy.py \
  tests/agent/test_token_economy_store.py \
  tests/agent/test_token_economy_prune_order.py \
  tests/agent/test_token_economy_system_prompt.py \
  tests/agent/test_token_economy_memory_projection.py \
  tests/agent/test_prompt_builder.py \
  tests/agent/test_memory_provider.py \
  tests/plugins/memory/test_hindsight_provider.py \
  tests/hermes_cli/test_lean_worker_controls.py \
  tests/hermes_cli/test_prompt_size.py \
  tests/agent/test_kanban_stop.py

set_cfg() { "$HERMES" config set "$1" "$2" >/dev/null; }

measure_prompt() {
  local cwd="$1" out="$2"
  (
    cd "$cwd"
    PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PY" - "$out" <<'PY'
import json, sys
from pathlib import Path
from hermes_cli.prompt_size import compute_prompt_breakdown
out = Path(sys.argv[1])
data = compute_prompt_breakdown("cli")
out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  )
}

# Baseline against the SAME user config, explicitly disabling only the master
# rollout flag. This is deliberately not a synthetic upstream number.
set_cfg token_economy.enabled false
BASELINE="$REPORT_DIR/baseline-$STAMP.json"
measure_prompt "$HOME" "$BASELINE"

# Persist only the token_economy namespace. Runtime projection deliberately
# overrides legacy memory/tool-search/context settings while the master flag is
# on; turning token_economy.enabled=false reveals those original settings intact.
# No provider/model credentials or routing settings are touched.
set_cfg token_economy.enabled true
set_cfg token_economy.ledger_enabled true
set_cfg token_economy.session_mode auto
set_cfg token_economy.tool_result_externalize_chars 256
set_cfg token_economy.tool_result_receipt_chars 160
set_cfg token_economy.retain_tool_result_turns 1
set_cfg token_economy.live_tool_result_chars 4000
set_cfg token_economy.live_tool_turn_chars 8000
set_cfg token_economy.task_state_enabled true
set_cfg token_economy.task_state_projection_chars 900
set_cfg token_economy.compact_prompt true
set_cfg token_economy.llm_title_upgrade false
set_cfg token_economy.background_review_enabled false
set_cfg token_economy.memory_prompt_injection false
set_cfg token_economy.context_file_max_chars 4000
set_cfg token_economy.short_context_ceiling 8000
set_cfg token_economy.work_context_ceiling 16000
set_cfg token_economy.autonomous_context_ceiling 24000

# Verify the persisted token-economy settings and the effective runtime tool projection
# without printing unrelated config or secrets.
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PY" - <<'PY'
from hermes_cli.config import load_config_readonly
from tools.tool_search import load_config_readonly as load_tool_search_readonly
cfg = load_config_readonly()
te = cfg.get("token_economy") or {}
expected = {
    "enabled": True, "ledger_enabled": True, "session_mode": "auto", "tool_result_externalize_chars": 256,
    "tool_result_receipt_chars": 160, "retain_tool_result_turns": 1,
    "live_tool_result_chars": 4000, "live_tool_turn_chars": 8000,
    "task_state_enabled": True, "task_state_projection_chars": 900,
    "compact_prompt": True, "llm_title_upgrade": False, "background_review_enabled": False,
    "memory_prompt_injection": False,
    "context_file_max_chars": 4000, "short_context_ceiling": 8000,
    "work_context_ceiling": 16000, "autonomous_context_ceiling": 24000,
}
for key, value in expected.items():
    assert te.get(key) == value, (key, te.get(key), value)
ts = load_tool_search_readonly()
assert ts.enabled == "on"
assert ts.listing == "off"
assert ts.compact_direct is True
assert ts.compact_bridge is True
assert "clarify" not in ts.effective_defer_tools
print("TOKEN_LEAN_CONFIG_OK")
PY

SHORT="$REPORT_DIR/short-$STAMP.json"
WORK="$REPORT_DIR/work-$STAMP.json"
set_cfg token_economy.session_mode short
measure_prompt "$HOME" "$SHORT"
set_cfg token_economy.session_mode work
measure_prompt "$ROOT" "$WORK"
set_cfg token_economy.session_mode auto

# Fail-closed budget gate. chars/4 estimates are intentionally simple and stable;
# /context waste reconciles them to provider-measured usage after live turns.
"$PY" - "$BASELINE" "$SHORT" "$WORK" "$REPORT" "$ROOT" <<'PY'
import json, subprocess, sys
from pathlib import Path
base_p, short_p, work_p, report_p = map(Path, sys.argv[1:5])
root = Path(sys.argv[5])
commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
branch = subprocess.check_output(["git", "-C", str(root), "branch", "--show-current"], text=True).strip()
base = json.loads(base_p.read_text())
short = json.loads(short_p.read_text())
work = json.loads(work_p.read_text())

def row(d):
    return {
        "system_tokens": int(d["system_prompt"]["estimated_tokens"]),
        "tool_tokens": int(d["tools"]["estimated_tokens"]),
        "fixed_tokens": int(d["fixed_total_estimated_tokens"]),
        "tool_count": int(d["tools"]["count"]),
        "tool_names": list(d["tools"].get("names") or []),
        "system_bytes": int(d["system_prompt"]["bytes"]),
        "tool_bytes": int(d["tools"]["json_bytes"]),
        "skills_bytes": int(d["skills_index"]["bytes"]),
        "memory_bytes": int(d["memory"]["bytes"]),
        "user_profile_bytes": int(d["user_profile"]["bytes"]),
    }

b, s, w = row(base), row(short), row(work)
reduction = (1.0 - s["fixed_tokens"] / b["fixed_tokens"]) if b["fixed_tokens"] else 0.0
short_expected_tools = {"clarify", "tool_search", "tool_describe", "tool_call"}
work_expected_tools = {"terminal", "read_file", "write_file", "patch", "search_files", "clarify",
                       "tool_search", "tool_describe", "tool_call"}
checks = {
    "short_fixed_le_5000": s["fixed_tokens"] <= 5000,
    "short_tools_le_650": s["tool_tokens"] <= 650,
    "short_tool_surface_exact": set(s["tool_names"]) == short_expected_tools and s["tool_count"] == 4,
    "short_skills_index_zero": s["skills_bytes"] == 0,
    "work_fixed_le_8000": w["fixed_tokens"] <= 8000,
    "work_tools_le_2500": w["tool_tokens"] <= 2500,
    "work_tool_surface_exact": set(w["tool_names"]) == work_expected_tools and w["tool_count"] == 9,
    "work_skills_index_zero": w["skills_bytes"] == 0,
    "short_reduction_ge_60pct": reduction >= 0.60,
    "short_memory_projection_zero": s["memory_bytes"] == 0 and s["user_profile_bytes"] == 0,
}
report = {
    "schema_version": 1,
    "branch": branch,
    "commit": commit,
    "baseline": b, "short": s, "work": w,
    "short_reduction_percent": round(reduction * 100.0, 2),
    "checks": checks,
}
report_p.write_text(json.dumps(report, indent=2) + "\n")
print("Token budget:")
print(f"  baseline fixed : ~{b['fixed_tokens']:,} tok ({b['tool_tokens']:,} tool)")
print(f"  short fixed    : ~{s['fixed_tokens']:,} tok ({s['tool_tokens']:,} tool), reduction={reduction*100:.1f}%")
print(f"  work fixed     : ~{w['fixed_tokens']:,} tok ({w['tool_tokens']:,} tool)")
for name, ok in checks.items(): print(f"  {'PASS' if ok else 'FAIL'} {name}")
if not all(checks.values()):
    raise SystemExit("token budget regression gate failed")
print("TOKEN_LEAN_BUDGET_OK")
PY

if [[ "$VERIFY_ONLY" == "1" ]]; then
  if [[ "$HAD_CONFIG" == "1" ]]; then
    cp -p "$BACKUP" "$CONFIG"
  else
    rm -f "$CONFIG"
  fi
  ACTIVATED=1
  trap - EXIT
  printf 'TOKEN_LEAN_VERIFY_OK\n'
  printf 'Report: %s\n' "$REPORT"
  printf 'Report SHA-256: %s\n' "$(sha256sum "$REPORT" | awk '{print $1}')"
  printf 'Original config restored; Hermes gateway was not restarted.\n'
  exit 0
fi

# Only a fully tested and measured configuration reaches the live gateway.
SERVICE_RESTART_ATTEMPTED=1
systemctl --user restart hermes-gateway-freebrain.service
if ! systemctl --user is-active --quiet hermes-gateway-freebrain.service; then
  systemctl --user --no-pager --full status hermes-gateway-freebrain.service || true
  fail "Hermes gateway did not become active"
fi

ACTIVATED=1
trap - EXIT
printf 'TOKEN_LEAN_ACTIVATION_OK\n'
printf 'Report: %s\n' "$REPORT"
printf 'Report SHA-256: %s\n' "$(sha256sum "$REPORT" | awk '{print $1}')"
printf 'Original config backup: %s\n' "$BACKUP"
printf 'Run: exec hermes\n'
printf 'Then /new; after one turn run /context waste, and later /insights waste.\n'
