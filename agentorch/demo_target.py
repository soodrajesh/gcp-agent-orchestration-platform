"""A tiny service to have incidents on. FAIL=1 makes every request return 500 and log an error,
like a bad deploy. `agentorch.demo_target:app`."""

import json
import os
import sys

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

VERSION = os.environ.get("APP_VERSION", "v1")
FAIL = os.environ.get("FAIL") == "1"


async def index(request):
    if FAIL:
        # Cloud Run parses JSON on stdout: `severity` becomes the log severity.
        print(
            json.dumps(
                {
                    "severity": "ERROR",
                    "message": f"KeyError: 'customer_id' in orders.process_checkout (release {VERSION})",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return PlainTextResponse("internal error", status_code=500)
    return PlainTextResponse(f"ok {VERSION}\n")


app = Starlette(routes=[Route("/", index)])
