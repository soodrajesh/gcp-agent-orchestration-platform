#!/usr/bin/env bash
# Build and deploy the whole stack, or update an existing one. Safe to re-run.
# Every Terraform change is planned, summarised (including any destroys), then applied from the
# saved plan file, so nothing is applied that was not shown.
source "$(dirname "$0")/lib.sh"
need gcloud; need terraform

apply_plan() { # <label>: plan, show the resource list, refuse unexpected destroys, apply the saved plan
  local plan; plan="$(mktemp -t aoplan.XXXXXX)"
  $TF plan -input=false -no-color -out="$plan" >"$plan.txt" 2>&1 || { cat "$plan.txt"; die "plan failed"; }
  grep -E '^Plan:|^No changes|^  # ' "$plan.txt" || true
  if grep -qE '^Plan: .* [1-9][0-9]* to destroy' "$plan.txt" && [ "${ALLOW_DESTROY:-0}" != 1 ]; then
    die "plan would destroy resources; review the list above, then re-run with ALLOW_DESTROY=1 if intended"
  fi
  $TF apply -input=false -no-color "$plan" | grep -E 'Apply complete|Error' || true
  rm -f "$plan" "$plan.txt"
}

tf_init

# Phase 1 only on a fresh project. With no image the Cloud Run services are count=0, so running it
# against an existing stack would plan to destroy them.
if gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  log "1/3 foundation already exists, skipping"
else
  log "1/3 foundation: APIs, Firestore, registry, identities, Pub/Sub, BigQuery ledger (no image yet)"
  TF_VAR_image="" apply_plan foundation
fi

log "2/3 build image with Cloud Build (dedicated ao-build identity)"
IMG="${IMAGE_BASE}/ao:$(date +%Y%m%d-%H%M%S)"
built=0
for attempt in 1 2 3; do  # fresh stacks: the build identity's IAM grants can take a minute to propagate
  if gcloud builds submit "$ROOT" --project "$PROJECT_ID" --region "$REGION" --config "$ROOT/cloudbuild.yaml" \
       --substitutions "_IMAGE=$IMG" --gcs-source-staging-dir "gs://${PROJECT_ID}-ao-build/source" \
       --service-account "projects/$PROJECT_ID/serviceAccounts/ao-build@${PROJECT_ID}.iam.gserviceaccount.com" \
       >/dev/null 2>"$ROOT/.build.err"; then built=1; break; fi
  warn "build attempt $attempt failed ($(tail -1 "$ROOT/.build.err" | cut -c1-120)); retrying in 30s"; sleep 30
done
rm -f "$ROOT/.build.err"
[ "$built" = 1 ] || die "image build failed"
ok "image $IMG"

log "3/3 deploy services, push subscription and observability"
TF_VAR_image="$IMG" apply_plan services

ok "orchestrator: $($TF output -raw orchestrator_url)"
echo "Operator tools need:"
echo "  export ORCH_URL=$($TF output -raw orchestrator_url) OPERATOR_SA=$($TF output -raw operator_sa) PROJECT_ID=$PROJECT_ID REGION=$REGION"
warn "Impersonation grants can take 1-2 minutes to propagate on a fresh stack (first call may return 403)."
