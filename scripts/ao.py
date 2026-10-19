"""Operator CLI.  Reads ORCH_URL and OPERATOR_SA (see scripts/lib.sh / terraform outputs).

ao.py run "alert text" [--run-id ID]      start a triage run, print it
ao.py show RUN_ID                          print status, steps, usage
ao.py approve RUN_ID STEP [--reject]       decide a gated step
ao.py state                                platform state: kill switch, spend today vs the daily cap
ao.py killswitch on|off                    stop (or resume) the whole platform
"""

import argparse
import json
import os

from agentorch.operator import OrchestratorClient


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("alert")
    r.add_argument("--run-id")
    s = sub.add_parser("show")
    s.add_argument("run_id")
    a = sub.add_parser("approve")
    a.add_argument("run_id")
    a.add_argument("step")
    a.add_argument("--reject", action="store_true")
    sub.add_parser("state")
    k = sub.add_parser("killswitch")
    k.add_argument("position", choices=["on", "off"])
    args = ap.parse_args()
    c = OrchestratorClient(os.environ["ORCH_URL"], os.environ["OPERATOR_SA"])
    if args.cmd == "run":
        out = c.start("triage", {"alert": args.alert}, args.run_id)
    elif args.cmd == "show":
        out = c.get(args.run_id)
    elif args.cmd == "approve":
        out = c.decide(args.run_id, args.step, not args.reject)
    elif args.cmd == "state":
        out = c.request("GET", "/admin/state").json()
    else:
        resp = c.request("POST", "/admin/killswitch", json={"enabled": args.position == "on"})
        resp.raise_for_status()
        out = resp.json()
    print(json.dumps(out, indent=2))


main()
