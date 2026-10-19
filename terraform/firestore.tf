# The (default) database is the one the Firestore free quota is documented against.
# Single region keeps the free-quota assumption simple; the location cannot be changed later.
resource "google_firestore_database" "default" {
  name                    = "(default)"
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  deletion_policy         = "DELETE" # down.sh must be able to tear the stack down
  delete_protection_state = "DELETE_PROTECTION_DISABLED"
  depends_on              = [google_project_service.api]
}

# Runs auto-expire so the 1 GiB free storage is never a concern.
resource "google_firestore_field" "run_ttl" {
  collection = "runs"
  field      = "expires_at"
  ttl_config {}
  index_config {} # TTL field needs no single-field index
  depends_on = [google_firestore_database.default]
}
