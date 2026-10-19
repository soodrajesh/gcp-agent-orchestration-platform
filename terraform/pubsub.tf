resource "google_pubsub_topic" "runs" {
  name       = "ao-runs"
  depends_on = [google_project_service.api]
}
resource "google_pubsub_topic" "runs_dlq" {
  name       = "ao-runs-dlq"
  depends_on = [google_project_service.api]
}
# Inspect poison messages with: gcloud pubsub subscriptions pull ao-runs-dlq-sub --auto-ack
resource "google_pubsub_subscription" "runs_dlq" {
  name                       = "ao-runs-dlq-sub"
  topic                      = google_pubsub_topic.runs_dlq.id
  message_retention_duration = "604800s"
}

locals {
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "orchestrator_publishes" {
  topic  = google_pubsub_topic.runs.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.orchestrator.email}"
}
# Pub/Sub's own service agent mints the OIDC token for the push identity...
resource "google_service_account_iam_member" "pubsub_mints_push_token" {
  service_account_id = google_service_account.pubsub_push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_agent
}
# ...and moves exhausted messages to the dead-letter topic.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  topic  = google_pubsub_topic.runs_dlq.name
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}
resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  count        = local.deploy ? 1 : 0
  subscription = google_pubsub_subscription.runs_push[0].name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

resource "google_pubsub_subscription" "runs_push" {
  count                = local.deploy ? 1 : 0
  name                 = "ao-runs-push"
  topic                = google_pubsub_topic.runs.id
  ack_deadline_seconds = 300 # >= the longest step; matches the Cloud Run request timeout
  push_config {
    push_endpoint = "${local.orchestrator_url}/internal/advance"
    oidc_token {
      service_account_email = google_service_account.pubsub_push.email
      audience              = local.orchestrator_url
    }
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "120s"
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.runs_dlq.id
    max_delivery_attempts = 5
  }
  message_retention_duration = "86400s"
  depends_on                 = [google_cloud_run_v2_service.orchestrator]
}
