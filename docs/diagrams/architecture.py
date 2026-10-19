#!/usr/bin/env python3
"""Generates docs/img/architecture.svg (PNG via docs/diagrams/render.py).

    python3 docs/diagrams/architecture.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from archlib import Diagram

OUT = os.path.join(os.path.dirname(__file__), "..", "img", "architecture.svg")

W, H = 2000, 1550
d = Diagram(W, H, "Agent Orchestration Platform on Google Cloud",
            "ADK analyst over A2A · logs read through MCP · model proposes, code validates, a human approves · "
            "Firestore-checkpointed runs · spend cap + kill switch")



def tile(nid, cx, cy, kind, cat):
    """A tile whose caption is drawn separately (so connectors never cross the text)."""
    d.node(nid, cx, cy, "", kind, cat)


def caption(x, y, bold, subs, anchor="end"):
    d.text(x, y, bold, 12.5, "#202124", "600", anchor)
    for i, line in enumerate(subs):
        d.text(x, y + 14 + i * 12, line, 10.5, "#5f6368", "400", anchor)


d.group(170, 100, 1760, 800, "Google Cloud project  ·  us-central1 / global (Vertex AI)  ·  every service private (Cloud Run IAM + in-app token check)",
        "#1a73e8", dash=False, fill="#f8faff", label_w=860)

# ── nodes ────────────────────────────────────────────────────────────────────────────────
d.node("op", 100, 430, "Operator", "user", "actor", "impersonates ao-operator\n(audience-bound ID token)")

tile("pubsub", 560, 220, "bolt", "network")            # caption to the LEFT: arrows leave downwards
caption(520, 214, "Pub/Sub  ao-runs", ["push subscription,", "OIDC to the orchestrator"])
d.node("dlq", 900, 220, "ao-runs-dlq", "bolt", "network", "after 5 failed\ndeliveries")

tile("orch", 560, 430, "run", "compute")                # caption BELOW-LEFT: the arrow to Firestore leaves downwards
caption(525, 478, "ao-orchestrator", ["Cloud Run · run engine", "plan validation · API · viewer"])
d.node("fs", 560, 620, "Firestore", "db", "data", "runs (TTL 30d) · checkpoints\nspend counter · kill switch")

tile("an", 980, 430, "run", "compute")
caption(945, 478, "ao-incident-analyst", ["Cloud Run · ADK agent", "A2A server"])
d.node("mcp", 980, 620, "ao-ops-mcp", "run", "compute", "Cloud Run · read-only MCP\nrecent_errors · list_revisions")
d.node("vx", 1390, 430, "Vertex AI", "sparkle", "ai", "gemini-2.5-flash")

tile("log", 1390, 620, "registry", "ops")               # caption ABOVE: the target's arrow arrives from below
caption(1390, 540, "Cloud Logging", ["ERROR logs · step_usage JSON"], anchor="middle")
d.node("tgt", 1390, 810, "ao-demo-target", "run", "compute", "Cloud Run · the service\nthat has the incidents")

d.node("trace", 1780, 430, "Cloud Trace", "pipeline", "ops", "one trace across\nthree services")
d.node("bq", 1780, 620, "BigQuery", "db", "data", "agentorch_costs\nper-step cost ledger")

# ── flows ────────────────────────────────────────────────────────────────────────────────
d.edge("op", "orch", "h", num=1, label="alert · approve / reject · kill switch")

# 2 publish / 3 push: two parallel verticals between the orchestrator and Pub/Sub
d.path([(540, 400), (540, 250)], num=2)
d.path([(580, 250), (580, 400)], num=3)
d.text(522, 328, "publish run_id", 11, "#3c4043", anchor="end")
d.text(598, 328, "push (OIDC)", 11, "#3c4043")
d.edge("pubsub", "dlq", "h", dash=True, label="poison messages")

d.edge("orch", "fs", "v", num=4, label="state, checkpoints")
d.edge("orch", "an", "h", num=5, label="A2A task + ID token (diagnose)")
d.edge("an", "vx", "h", num=6, label="Gemini 2.5 Flash")
d.edge("an", "mcp", "v", num=7, label="MCP + ID token\n(must call both tools)")
d.edge("mcp", "log", "h", num=8, label="read ERROR logs\n(allow-listed service only)")
d.edge("tgt", "log", "v", num=9, label="writes logs")

# 10: the only action: human-approved, re-validated traffic shift, to ONE service
d.path([(590, 455), (700, 455), (700, 810), (1360, 810)], num=10, color="#d93025",
       label="human-approved traffic shift: re-validated against live state, this service only", lab_at=(1030, 810))

d.edge("log", "bq", "h", num=11, label="log sink:\nstep_usage rows")
d.path([(980, 400), (980, 300), (1780, 300), (1780, 400)], num=12, dash=True,
       label="spans: traceparent carried over A2A and MCP", lab_at=(1380, 300))

# ── notes ────────────────────────────────────────────────────────────────────────────────
d.badge(230, 940, "13", "#1e8e3e")
d.text(250, 945, "Each service has its own service account: orchestrator = Firestore + publish + run.developer on the one target; analyst = Vertex AI + call MCP;", 12, "#3c4043")
d.text(250, 963, "MCP = read logs/revisions; target = no permissions at all. No service-account keys, no allUsers binding, no project-level owner/editor.", 12, "#3c4043")

d.band(170, 1000, 1760, 150, "THE MODEL PROPOSES, CODE DISPOSES  ·  see docs/threat-model.md and docs/adr/0003", "#d93025", "#fff8f7")
d.text(200, 1042, "The analyst's reply is untrusted (it reads log text an attacker may influence). It is parsed into a closed schema (rollback | none), then checked by code against LIVE Cloud Run state:", 12.5, "#3c4043")
d.text(200, 1064, "allow-listed service, target revision exists / ready / older. Anything else fails the run before a human is asked. A human approves the exact action; execution re-checks state and is idempotent.", 12.5, "#3c4043")
d.text(200, 1086, "Before every step, even an approved one: per-run budget, platform daily spend cap, kill switch. A model that never called its tools is refused, and a failed A2A task is never read as an answer.", 12.5, "#3c4043")
d.text(200, 1108, "Steps are at-least-once: finished steps are checkpointed and never re-run; the lease has a heartbeat; the rollback is idempotent. Evidence strings are the model's words and are not cross-checked against logs.", 12.5, "#3c4043")

d.legend(1500, 1190, "Numbered flows", [
    ("1", "alert / decision in (operator identity)"),
    ("2", "run_id published"),
    ("3", "Pub/Sub pushes it back (OIDC)"),
    ("4", "state, checkpoints, spend, kill switch"),
    ("5", "diagnose: A2A to the analyst"),
    ("6", "analyst calls Gemini"),
    ("7", "analyst calls the MCP tools"),
    ("8", "MCP reads error logs"),
    ("9", "target writes its logs"),
    ("10", "approved rollback (the only action)"),
    ("11", "cost ledger to BigQuery"),
    ("12", "trace spans across services"),
    ("13", "one service account per service"),
], w=420)
d.key(170, 1210)

d.save(OUT)
print("wrote", OUT)
