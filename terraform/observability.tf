# --- Per-step cost ledger: Cloud Logging -> BigQuery -----------------------------------------------
# The engine logs one JSON line per completed step (event=step_usage: run, step, tokens, cost). A log
# sink routes just those lines into a partitioned BigQuery table. Prompts and model output are never
# logged (tested), so the ledger holds numbers only.
resource "google_bigquery_dataset" "costs" {
  dataset_id                      = "agentorch_costs"
  location                        = var.region
  description                     = "agent-orchestration-platform per-step token and cost ledger"
  delete_contents_on_destroy      = true
  default_partition_expiration_ms = 7776000000 # 90 days
  depends_on                      = [google_project_service.api]
}

resource "google_logging_project_sink" "step_usage" {
  name                   = "ao-step-usage"
  destination            = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.costs.dataset_id}"
  filter                 = "resource.type=\"cloud_run_revision\" AND jsonPayload.event=\"step_usage\""
  unique_writer_identity = true
  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_bigquery_dataset_iam_member" "sink_writes" {
  dataset_id = google_bigquery_dataset.costs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.step_usage.writer_identity
}

# --- Cloud Trace: each service may write spans (one trace follows a run across the services) -------
resource "google_project_iam_member" "trace_agent" {
  for_each = {
    orchestrator     = google_service_account.orchestrator.email
    incident_analyst = google_service_account.incident_analyst.email
    ops_mcp          = google_service_account.ops_mcp.email
  }
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${each.value}"
}

# OTLP spans go to the Telemetry API (ADK's exporter), which has its own writer role.
resource "google_project_iam_member" "telemetry_traces_writer" {
  for_each = {
    orchestrator     = google_service_account.orchestrator.email
    incident_analyst = google_service_account.incident_analyst.email
    ops_mcp          = google_service_account.ops_mcp.email
  }
  project = var.project_id
  role    = "roles/telemetry.tracesWriter"
  member  = "serviceAccount:${each.value}"
}

# Platform guard documents (kill switch, daily spend counters): daily counters expire on their own.
resource "google_firestore_field" "platform_ttl" {
  collection = "platform"
  field      = "expires_at"
  ttl_config {}
  index_config {}
  depends_on = [google_firestore_database.default]
}
