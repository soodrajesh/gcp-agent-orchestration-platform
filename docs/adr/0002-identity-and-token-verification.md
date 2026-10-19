# ADR 0002: Operator identity, and why not the human's own token

Status: accepted (2026-10-18). Found by testing the deployed service, not by design.

## Problem
The first design had the app read the caller's email from the Google ID token. Live:
- a human's own `gcloud auth print-identity-token` token reached the app but failed signature
  verification (Cloud Run's front end alters tokens issued to Google's own OAuth client);
- `gcloud run services proxy` delivered no `Authorization` header at all.
(Your `gcp-mcp-platform` repo documents the same platform behaviour.)

## Decision
- Operators call the API as service account `ao-operator`. A human obtains an **audience-bound**
  ID token by impersonating it (`roles/iam.serviceAccountTokenCreator`, granted to the admin only).
  Impersonation is recorded in Cloud Audit Logs against the human, so attribution exists, but at
  the log level: `decided_by` on an approval holds the operator SA, not a person.
- The app verifies signature, issuer, expiry **and audience** (`SERVICE_URL`) on every request.
  Role split: `OPERATORS` for the API, `PUSH_SA` for `/internal/advance`; neither can use the other's
  endpoints (live-tested).
- Cloud Run IAM (`run.invoker`) is the first gate, the in-app check the second. No `allUsers`.
- `scripts/ui_proxy.py` is a local proxy that attaches the impersonated token, replacing
  `gcloud run services proxy` for the run viewer.
- `/healthz` is reserved by Cloud Run's front end (returns 404 before reaching the app); the app
  uses `/health`.

## Consequences / limits
- Approvals are not attributable to an individual inside the application. If that matters, put
  IAP in front for the human path and keep the OIDC path for machines.
- IAM bindings on a fresh stack can take 1-2 minutes to propagate; the first impersonation right
  after `up.sh` may get a 403.

## Also found while testing (fixed)
- The lease owner was per Engine instance. One Cloud Run instance serves many concurrent
  requests, so concurrent deliveries shared an owner and bypassed the lease. Owner is now unique
  per `advance()` call (regression test `test_one_engine_instance_excludes_its_own_concurrent_calls`).
- TTL on `expires_at` was inert: the field did not exist and JSON-mode serialisation would have
  stored a string. It is now a native timestamp (`test_expires_at_is_stored_as_a_native_timestamp`).
