locals {
  deploy               = var.image != ""
  num                  = data.google_project.this.number
  orchestrator_url     = "https://ao-orchestrator-${local.num}.${var.region}.run.app"
  incident_analyst_url = "https://ao-incident-analyst-${local.num}.${var.region}.run.app"
  ops_mcp_url          = "https://ao-ops-mcp-${local.num}.${var.region}.run.app"
  demo_target_url      = "https://ao-demo-target-${local.num}.${var.region}.run.app"
  rollback_allowed     = "ao-demo-target" # the ONLY service the platform may roll back
}

# --- shared shape for the small stateless services ------------------------------------------
# (Terraform has no function for this, so each service spells it out; kept deliberately uniform.)

resource "google_cloud_run_v2_service" "incident_analyst" {
  count               = local.deploy ? 1 : 0
  name                = "ao-incident-analyst"
  location            = var.region
  deletion_protection = false
  template {
    service_account                  = google_service_account.incident_analyst.email
    timeout                          = "180s"
    max_instance_request_concurrency = 10
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      args  = ["sh", "-c", "exec uvicorn agentorch.agents.incident_analyst:app --host 0.0.0.0 --port $${PORT}"]
      resources {
        limits   = { cpu = "1", memory = "1Gi" }
        cpu_idle = false # CPU stays allocated so the batch span exporter can run between requests
      }
      env {
        name  = "OTEL_TRACES_SAMPLER"
        value = "always_on" # Cloud Run marks most requests unsampled; a parent-based sampler would drop them all
      }
      env {
        name  = "OTEL_BSP_SCHEDULE_DELAY"
        value = "1000"
      }
      env {
        name  = "ENABLE_TRACING"
        value = "1"
      }
      env {
        name  = "AGENT_PUBLIC_URL"
        value = local.incident_analyst_url
      }
      env {
        name  = "MCP_URL"
        value = local.ops_mcp_url
      }
      env {
        name  = "AGENT_MODEL"
        value = var.agent_model
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
    }
  }
  depends_on = [google_project_service.api, google_artifact_registry_repository.repo]
}

resource "google_cloud_run_v2_service" "ops_mcp" {
  count               = local.deploy ? 1 : 0
  name                = "ao-ops-mcp"
  location            = var.region
  deletion_protection = false
  template {
    service_account                  = google_service_account.ops_mcp.email
    timeout                          = "60s"
    max_instance_request_concurrency = 10
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      args  = ["sh", "-c", "exec uvicorn agentorch.mcp_ops:app --host 0.0.0.0 --port $${PORT}"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = false # CPU stays allocated so the batch span exporter can run between requests
      }
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "OTEL_TRACES_SAMPLER"
        value = "always_on" # Cloud Run marks most requests unsampled; a parent-based sampler would drop them all
      }
      env {
        name  = "OTEL_BSP_SCHEDULE_DELAY"
        value = "1000"
      }
      env {
        name  = "ENABLE_TRACING"
        value = "1"
      }
      env {
        name  = "OPS_ALLOWED_SERVICES"
        value = local.rollback_allowed
      }
      env {
        name  = "MCP_CALLERS"
        value = google_service_account.incident_analyst.email
      }
      env {
        name  = "SERVICE_URL"
        value = local.ops_mcp_url
      }
    }
  }
  depends_on = [google_project_service.api, google_artifact_registry_repository.repo]
}

# The service incidents happen to. Healthy by default; scripts/demo_incident.py deploys a bad
# revision next to it. Re-running up.sh redeploys a healthy revision (i.e. heals the demo).
resource "google_cloud_run_v2_service" "demo_target" {
  count               = local.deploy ? 1 : 0
  name                = "ao-demo-target"
  location            = var.region
  deletion_protection = false
  template {
    service_account = google_service_account.demo_target.email
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.image
      args  = ["sh", "-c", "exec uvicorn agentorch.demo_target:app --host 0.0.0.0 --port $${PORT}"]
      resources {
        limits   = { cpu = "1", memory = "256Mi" }
        cpu_idle = true
      }
      env {
        name  = "APP_VERSION"
        value = "v1-healthy"
      }
    }
  }
  # Revisions of this service belong to the scenario scripts (demo_incident.py / demo_reset.py),
  # not to Terraform: without this, an apply would "fix" a live incident by redeploying v1.
  lifecycle {
    ignore_changes = [template, traffic, client, client_version]
  }
  depends_on = [google_project_service.api, google_artifact_registry_repository.repo]
}

resource "google_cloud_run_v2_service" "orchestrator" {
  count               = local.deploy ? 1 : 0
  name                = "ao-orchestrator"
  location            = var.region
  deletion_protection = false
  template {
    service_account                  = google_service_account.orchestrator.email
    timeout                          = "300s"
    max_instance_request_concurrency = 10
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      resources {
        limits   = { cpu = "1", memory = "1Gi" }
        cpu_idle = false # CPU stays allocated so the batch span exporter can run between requests
      }
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "TOPIC"
        value = google_pubsub_topic.runs.name
      }
      env {
        name  = "SPECIALIST_URL"
        value = local.incident_analyst_url
      }
      env {
        name  = "OTEL_TRACES_SAMPLER"
        value = "always_on" # Cloud Run marks most requests unsampled; a parent-based sampler would drop them all
      }
      env {
        name  = "OTEL_BSP_SCHEDULE_DELAY"
        value = "1000"
      }
      env {
        name  = "ENABLE_TRACING"
        value = "1"
      }
      env {
        name  = "DAILY_BUDGET_USD"
        value = tostring(var.daily_budget_usd)
      }
      env {
        name  = "ROLLBACK_ALLOWED_SERVICES"
        value = local.rollback_allowed
      }
      env {
        name  = "AGENT_MODEL"
        value = var.agent_model
      }
      env {
        name  = "OPERATORS"
        value = google_service_account.operator.email
      }
      env {
        name  = "SERVICE_URL"
        value = local.orchestrator_url
      }
      env {
        name  = "PUSH_SA"
        value = google_service_account.pubsub_push.email
      }
    }
  }
  depends_on = [google_project_service.api, google_artifact_registry_repository.repo]
}

# --- who may call whom (Cloud Run IAM is the first gate; the app re-checks identity as the second)
locals {
  invokers = local.deploy ? {
    "orchestrator-calls-analyst" = { svc = "ao-incident-analyst", sa = google_service_account.orchestrator.email }
    "analyst-calls-mcp"          = { svc = "ao-ops-mcp", sa = google_service_account.incident_analyst.email }
    "operator-invokes-orch"      = { svc = "ao-orchestrator", sa = google_service_account.operator.email }
    "pubsub-invokes-orch"        = { svc = "ao-orchestrator", sa = google_service_account.pubsub_push.email }
    "operator-invokes-target"    = { svc = "ao-demo-target", sa = google_service_account.operator.email }
  } : {}
  # names are only known after creation, so resolve them via the services map
  svc_names = local.deploy ? {
    "ao-incident-analyst" = google_cloud_run_v2_service.incident_analyst[0].name
    "ao-ops-mcp"          = google_cloud_run_v2_service.ops_mcp[0].name
    "ao-orchestrator"     = google_cloud_run_v2_service.orchestrator[0].name
    "ao-demo-target"      = google_cloud_run_v2_service.demo_target[0].name
  } : {}
}

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = local.invokers
  name     = local.svc_names[each.value.svc]
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value.sa}"
}

# --- the rollback blast radius: exactly one service, nothing project-wide ----------------------
# run.developer on the target only (update traffic); the MCP server only needs to read it.
resource "google_cloud_run_v2_service_iam_member" "orchestrator_manages_target" {
  count    = local.deploy ? 1 : 0
  name     = google_cloud_run_v2_service.demo_target[0].name
  location = var.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.orchestrator.email}"
}
resource "google_cloud_run_v2_service_iam_member" "ops_mcp_reads_target" {
  count    = local.deploy ? 1 : 0
  name     = google_cloud_run_v2_service.demo_target[0].name
  location = var.region
  role     = "roles/run.viewer"
  member   = "serviceAccount:${google_service_account.ops_mcp.email}"
}
# Updating a service requires acting as its runtime identity; that identity has no permissions.
resource "google_service_account_iam_member" "orchestrator_acts_as_target" {
  service_account_id = google_service_account.demo_target.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.orchestrator.email}"
}
