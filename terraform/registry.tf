resource "google_artifact_registry_repository" "repo" {
  repository_id = "agentorch"
  location      = var.region
  format        = "DOCKER"
  description   = "agent-orchestration-platform images"
  # Keep the repo inside the 0.5 GB free storage: only the newest few images are retained.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions { keep_count = 3 }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition { older_than = "172800s" }
  }
  depends_on = [google_project_service.api]
}
