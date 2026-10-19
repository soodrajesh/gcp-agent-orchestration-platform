#!/usr/bin/env bash
# Tear everything down (services, Pub/Sub, Firestore data, BigQuery ledger, registry, build bucket,
# identities). Shows the destroy plan, then applies that saved plan.
#   ./scripts/down.sh            asks you to type the project id
#   ./scripts/down.sh --yes      no prompt
source "$(dirname "$0")/lib.sh"
need terraform
tf_init
warn "This destroys the stack in project $PROJECT_ID, including all run history in Firestore."
[ "${1:-}" = "--yes" ] || { read -r -p "Type the project id to confirm: " ans; [ "$ans" = "$PROJECT_ID" ] || die "aborted"; }
plan="$(mktemp -t aodestroy.XXXXXX)"
# The image only decides which resources exist (services are count=0 without one); use a placeholder
# so the plan covers the services too.
export TF_VAR_image="${TF_VAR_image:-placeholder}"
$TF plan -destroy -input=false -no-color -out="$plan" > "$plan.txt" 2>&1 || { cat "$plan.txt"; die "destroy plan failed"; }
grep -E '^Plan:|^  # ' "$plan.txt" | sed 's/^  # //'
$TF apply -input=false -no-color "$plan" | grep -E 'Destroy complete|Apply complete|Error' || true
rm -f "$plan" "$plan.txt"
ok "destroyed. The shared Terraform state bucket (${STATE_BUCKET}) is left in place."
