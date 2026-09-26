"""A stand-in for a client's EHR / intake system (stdlib only).

    python scripts/mock_ehr.py --port 9000 --fail-first 2

* Verifies the HMAC signature -> 401 if wrong (try changing a client secret).
* Dedupes on Idempotency-Key -> safe under our retries.
* --fail-first N answers 503 to the first N attempts of every delivery, to watch
  the retry/backoff path work.
* GET /received lists everything accepted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.webhooks import IDEMPOTENCY_HEADER, SIGNATURE_HEADER, verify  # noqa: E402


def secret_for(client_id: str) -> str:
    return os.getenv(f"{client_id.upper()}_WEBHOOK_SECRET", f"dev-secret-{client_id}")


class EHRState:
    def __init__(self, fail_first: int = 0, reject: set[str] | None = None):
        self.fail_first = fail_first
        self.reject = reject or set()
        self.received: dict[str, dict] = {}
        self.attempts: dict[str, int] = defaultdict(int)
        self.lock = threading.Lock()


def make_handler(state: EHRState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter console
            sys.stderr.write(f"[mock-ehr] {fmt % args}\n")

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/received":
                with state.lock:
                    return self._send(200, {"count": len(state.received),
                                            "items": list(state.received.values())})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "webhooks":
                return self._send(404, {"error": "not found"})
            client_id = parts[1]
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

            if not verify(secret_for(client_id), body, self.headers.get(SIGNATURE_HEADER)):
                return self._send(401, {"error": "invalid signature"})
            key = self.headers.get(IDEMPOTENCY_HEADER, "")
            with state.lock:
                state.attempts[key] += 1
                if state.attempts[key] <= state.fail_first:
                    return self._send(503, {"error": "simulated outage"})
                if client_id in state.reject:
                    return self._send(422, {"error": "simulated validation failure"})
                if key in state.received:
                    return self._send(200, {"status": "duplicate", "idempotency_key": key})
                state.received[key] = json.loads(body)
            self._send(200, {"status": "accepted", "idempotency_key": key})

    return Handler


def make_server(port: int, state: EHRState) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--fail-first", type=int, default=0)
    ap.add_argument("--reject", action="append", default=[], help="client_id to answer 422 for")
    args = ap.parse_args()
    server = make_server(args.port, EHRState(args.fail_first, set(args.reject)))
    print(f"mock EHR listening on :{args.port} (fail_first={args.fail_first})")
    server.serve_forever()


if __name__ == "__main__":
    main()
