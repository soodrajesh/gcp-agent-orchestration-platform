variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "admin_email" {
  type        = string
  description = "Human operator: may invoke the orchestrator and approve runs"
}
variable "image" {
  type        = string
  default     = ""
  description = "Container image for both services. Empty on the first (registry-only) apply."
}
variable "agent_model" {
  type        = string
  default     = "gemini-2.5-flash"
  description = "Analyst model. Chosen by evals/run.py: flash-lite scored 54%, flash 96% (docs/eval-results.md)"
}
variable "daily_budget_usd" {
  type        = number
  default     = 1.0
  description = "Platform-wide daily model-spend cap; the orchestrator halts runs when it is reached"
}
