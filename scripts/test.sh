#!/usr/bin/env bash
# Offline suite first (free), then the live checks against the deployed stack.
source "$(dirname "$0")/lib.sh"
need terraform
log "offline tests"
"$ROOT/.venv/bin/python" -m pytest -q -W ignore
log "live tests against $PROJECT_ID / $REGION"
tf_init
export ORCH_URL="$($TF output -raw orchestrator_url)" OPERATOR_SA="$($TF output -raw operator_sa)" PROJECT_ID REGION
"$ROOT/.venv/bin/python" -W ignore "$ROOT/scripts/live_test.py"
