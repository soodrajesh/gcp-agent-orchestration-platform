# Dedicated, least-privilege build identity (the default compute SA is not used for builds).
resource "google_service_account" "build" {
  account_id   = "ao-build"
  display_name = "agent-orchestration image builds"
}
resource "google_project_iam_member" "build_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.build.email}"
}
resource "google_artifact_registry_repository_iam_member" "build_pushes" {
  repository = google_artifact_registry_repository.repo.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build.email}"
}

# Private source-staging bucket; US region keeps it inside the Cloud Storage free tier.
resource "google_storage_bucket" "build_staging" {
  name                        = "${var.project_id}-ao-build"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  lifecycle_rule {
    condition { age = 7 }
    action { type = "Delete" }
  }
}
resource "google_storage_bucket_iam_member" "build_reads_source" {
  bucket = google_storage_bucket.build_staging.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.build.email}"
}
resource "google_storage_bucket_iam_member" "admin_stages_source" {
  bucket = google_storage_bucket.build_staging.name
  role   = "roles/storage.objectCreator"
  member = "user:${var.admin_email}"
}
resource "google_service_account_iam_member" "admin_acts_as_build" {
  service_account_id = google_service_account.build.name
  role               = "roles/iam.serviceAccountUser"
  member             = "user:${var.admin_email}"
}
