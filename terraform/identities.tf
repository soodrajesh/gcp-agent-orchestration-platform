data "google_project" "this" {}

resource "google_service_account" "orchestrator" {
  account_id   = "ao-orchestrator"
  display_name = "agent orchestrator (Cloud Run)"
}
resource "google_service_account" "incident_analyst" {
  account_id   = "ao-incident-analyst"
  display_name = "incident-analyst agent (Cloud Run)"
}
resource "google_service_account" "pubsub_push" {
  account_id   = "ao-pubsub-push"
  display_name = "Pub/Sub push identity -> orchestrator"
}

# Orchestrator: read/write run documents only.
resource "google_project_iam_member" "orchestrator_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}
# Specialist: may call Gemini; nothing else.
resource "google_project_iam_member" "incident_analyst_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.incident_analyst.email}"
}

# Operators call the API as this SA. A human's own Google ID token cannot be verified inside the
# app (Cloud Run's front end alters it), so humans impersonate this identity to mint an
# audience-bound token; every impersonation is recorded in Cloud Audit Logs against the human.
resource "google_service_account" "operator" {
  account_id   = "ao-operator"
  display_name = "operator identity for the orchestrator API"
}
resource "google_service_account_iam_member" "admin_impersonates_operator" {
  service_account_id = google_service_account.operator.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:${var.admin_email}"
}

# --- Phase 3: ops MCP server and the demo target ---------------------------------------------
resource "google_service_account" "ops_mcp" {
  account_id   = "ao-ops-mcp"
  display_name = "read-only ops MCP server (Cloud Run)"
}
# Reads error logs. logging.viewer is project-wide (a log view would narrow it): the server itself
# only ever queries allow-listed services, enforced in code and tested.
resource "google_project_iam_member" "ops_mcp_logs" {
  project = var.project_id
  role    = "roles/logging.viewer"
  member  = "serviceAccount:${google_service_account.ops_mcp.email}"
}

# The demo target runs as an identity with NO permissions at all.
resource "google_service_account" "demo_target" {
  account_id   = "ao-demo-target"
  display_name = "demo target service (no permissions)"
}
