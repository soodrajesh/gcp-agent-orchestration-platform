"""Shared test helpers. A plain module (not conftest) so tests can import it under any pytest invocation."""

import socket

PROPOSAL = (
    '{"diagnosis":"bad deploy: errors began with the newest revision",'
    '"evidence":["KeyError: customer_id in orders.process_checkout"],'
    '"action":{"type":"rollback","service":"ao-demo-target","target_revision":"rev-2"}}'
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
