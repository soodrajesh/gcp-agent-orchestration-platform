output "orchestrator_url" { value = local.orchestrator_url }
output "incident_analyst_url" { value = local.incident_analyst_url }
output "ops_mcp_url" { value = local.ops_mcp_url }
output "demo_target_url" { value = local.demo_target_url }
output "image_repo" { value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}" }
output "build_sa" { value = google_service_account.build.email }
output "build_bucket" { value = google_storage_bucket.build_staging.name }
output "operator_sa" { value = google_service_account.operator.email }
