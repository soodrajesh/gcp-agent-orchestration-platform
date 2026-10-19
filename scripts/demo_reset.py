"""Heal the demo target: new healthy revision, 100% of traffic, tracking latest again.

PROJECT_ID=... REGION=... .venv/bin/python scripts/demo_reset.py
"""

from demo_incident import reset_healthy

reset_healthy()
print("ao-demo-target is healthy again")
