locals {
  apis = [
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudtrace.googleapis.com",
    "bigquery.googleapis.com",
    "telemetry.googleapis.com",
    "logging.googleapis.com",
  ]
}

resource "google_project_service" "api" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false # other stacks in this project share these APIs
}
