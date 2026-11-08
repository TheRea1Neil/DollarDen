#!/usr/bin/env python3
"""Expose the power relay to the nnf container, over HTTP.

The API runs in Docker and has no access to the Pi's GPIO. Rather than
mounting /dev/gpiochip0 into the container -- which hands a web service
permanent physical control of a power switch -- this sits on the host
and offers exactly three verbs. Same shape as the GPU probe on the
deck, for the same reason: a narrow, auditable surface.

Bearer token required on everything but /health. The Pi is on a home
LAN with no firewall, so the token is what stands between a stray
request and someone's PC turning off.
"""
from __future__ import annotations

import json
import os
import pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import deck_power

PORT = int(os.environ.get("DECK_POWER_API_PORT", "11436"))
TOKEN = os.environ.get("DECK_POWER_TOKEN", "")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if not TOKEN:
            return False
        got = self.headers.get("authorization", "")
        return got == f"Bearer {TOKEN}"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.rstrip("/") or "/"
        if path == "/health":
            self._send(200, {"ok": True, "service": "deck-power-api"})
            return
        if not self._authorised():
            self._send(401, {"error": "unauthorized"})
            return
        if path == "/status":
            last = deck_power.last_action_at()
            self._send(200, {
                "deck_reachable": deck_power.host_is_up(),
                "last_press_at": last.isoformat() if last else None,
                "cooldown_remaining_seconds":
                    round(deck_power.cooldown_remaining()),
            })
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorised():
            self._send(401, {"error": "unauthorized"})
            return
        path = self.path.rstrip("/") or "/"

        # Only "on", and only when the deck is unreachable. The API has
        # no business shutting the machine down or holding the button:
        # a short press on a RUNNING deck is a shutdown request, and
        # force-off is destructive. Both stay on the terminal, where a
        # person is deciding.
        if path != "/press":
            self._send(404, {"error": "not found"})
            return

        if deck_power.host_is_up():
            self._send(409, {"error": "deck is already reachable",
                             "pressed": False})
            return
        remaining = deck_power.cooldown_remaining()
        if remaining > 0:
            self._send(429, {"error": "pressed recently", "pressed": False,
                             "retry_after_seconds": round(remaining)})
            return

        try:
            deck_power.press(deck_power.SHORT_PRESS)
        except Exception as exc:  # noqa: BLE001
            deck_power._log("api_press_failed", pressed=False, error=str(exc))
            self._send(500, {"error": str(exc), "pressed": False})
            return
        deck_power._log("short_press", pressed=True, via="api")
        self._send(200, {"pressed": True})

    def log_message(self, *a) -> None:  # quieter journal
        return


def main() -> int:
    if not TOKEN:
        print("DECK_POWER_TOKEN is not set; refusing to start", flush=True)
        return 1
    pathlib.Path(deck_power.STATE_DIR).mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"deck-power-api listening on :{PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
