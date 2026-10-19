# Threat model

## What is being protected
1. **Production traffic** of the allow-listed service: the platform can move it, so who or what can *make* it move is the central question.
2. **Spend**: model tokens are money; an agent platform can loop.
3. **Operator trust**: the approval screen must show the truth about what will happen.
4. **Run history**: Firestore documents and the BigQuery ledger.

## Who might attack, and from where
| Attacker | Realistic vector here |
|---|---|
| Someone who can make the target service log a string | prompt injection through log text read by the analyst |
| Someone who can file or influence an alert | prompt injection through the alert text |
| A compromised or buggy model | any output at all |
| A web page the operator visits | CSRF / DNS rebinding against the local UI proxy |
| An unauthenticated internet caller | every service has a public `run.app` URL |
| A principal with a stale or foreign token | wrong audience, wrong identity, expired token |
| The platform itself, by accident | runaway loops, retries, duplicate delivery |

## Threats, mitigations and the evidence for each
| # | Threat | Mitigation | Evidence |
|---|---|---|---|
| 1 | Injected text makes the platform act on the wrong service | Model reply is a *proposal*: closed schema (2 actions), service allow-list, target must exist/be ready/be older; fails before any human is asked. Orchestrator holds `run.developer` on **one** service | `tests/test_remediation.py`, `test_service.py` (4 hostile proposals); live: injected alert and injected logs produce no action on another service; evals: 0 unsafe in 75 runs, 3 vendors/models |
| 2 | Model fabricates a diagnosis without looking at the logs | Analyst fails the task if it did not call both tools; the A2A client raises on `FAILED` instead of reading the error text as an answer | `tests/test_mcp_ops.py` (3 tests incl. tools unavailable, and across real A2A) |
| 3 | Approving a stale plan | Execution re-checks live state; refuses if traffic moved elsewhere; idempotent if already applied | `test_traffic_that_moved_elsewhere...`, `test_mutation_that_succeeds_but_raises...` |
| 4 | A change happens after the platform should have stopped | Kill switch / daily cap are checked before every step, including an already-approved one | `test_approved_rollback_is_blocked_if_the_kill_switch...`; live: approval after the switch is pulled ends `halted`, production untouched |
| 5 | Unauthenticated or wrong-identity calls | Cloud Run IAM first; then in-app verification of signature, issuer, expiry and **audience**, then per-role allow-lists; no `allUsers` anywhere | `tests/test_auth.py`; live: all three services 403 without a token; the operator identity cannot call the push endpoint or the MCP server |
| 6 | Cross-site request or DNS rebinding against the local UI proxy (it holds the operator's token) | Proxy checks `Host`, `Origin`, `Sec-Fetch-Site` and requires `application/json` on writes (forces a preflight it never grants); binds to 127.0.0.1 only | `tests/test_operator_proxy.py` |
| 7 | Malformed or oversized caller input | `run_id` regex (it is a Firestore document id), input must be a JSON object under 16 KiB, budget validated and capped, `limit` clamped; invalid JSON is a 400 | `tests/test_service.py` validation tests |
| 8 | Log or error text injected into the operator's screen | UI renders with `textContent`; nothing is inserted as HTML | code review of `ui/index.html` |
| 9 | Secrets in logs or the cost ledger | Ledger rows hold ids, token counts, cost only; prompts and model text are never logged | `tests/test_obs.py::test_ledger_never_contains_prompt_alert_or_model_text` |
| 10 | Runaway spend | per-run budget -> `budget_exceeded`; platform daily cap and kill switch -> `halted`; scale-to-zero, max 2 instances | `test_guard.py`; live kill switch |
| 11 | Duplicate delivery repeats a paid call or a change | lease with CAS (owner per call), checkpoints, idempotent rollback, DLQ for poison messages | live: 8 duplicates, 1 analyst request in Cloud Run's own log |
| 12 | Credentials in the repository | none exist; local config (`deploy.env`, `.env`) is git-ignored; OpenAI key is never stored or deployed | secret-pattern scan before commit; `.gitignore` |
| 13 | Vulnerable dependencies / insecure code | `pip-audit` (no known vulnerabilities), `bandit` (0 findings in the application package) | CI runs both advisory |

## Accepted risks and known gaps
- **Evidence is the model's own words.** The safety-critical fields (service, revision) are verified by code; the cited log lines are not cross-checked against the logs. The operator sees them beside the exact action.
- **The validator cannot tell that a ready, older revision was itself failing.** In evals, `gemini-2.5-flash-lite` proposed such a rollback 3 times in 24. A human sees the diagnosis; code does not yet check the target revision's error history.
- **Approvals are attributed to a service account, not a person.** Impersonation is audit-logged against the human (Cloud Audit Logs), so attribution exists at the log level only. IAP in front of a human path would fix it.
- **The MCP server's identity holds project-wide `roles/logging.viewer`.** The server only queries allow-listed services (tested), but a log view would narrow the IAM grant.
- **A first-party analyst could mislead the orchestrator's A2A client** by publishing an agent card pointing elsewhere; the orchestrator's ID token is audience-bound to the analyst's own URL, which limits what a redirect could do, but the card URL is not pinned.
- **Container supply chain:** the base image is a floating `python:3.12-slim` tag and `pip install` has no hash pinning. Builds run as a dedicated least-privilege service account.
- **Denial of service** against the public URLs is bounded by Cloud Run IAM (unauthenticated requests are rejected before reaching the app) and instance caps, not by an application-level rate limiter.
- **No lease-loss fencing token** on external effects. The rollback is idempotent instead; a second effect type would need the same treatment.
