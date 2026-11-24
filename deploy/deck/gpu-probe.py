#!/usr/bin/env python3
"""Report the gaming PC's GPU headroom over HTTP.

The budget backend needs one question answered before it loads an 8 GB
model onto a card that may be mid-game: IS THERE ROOM RIGHT NOW.

Nothing already running can answer it. The Wake-on-LAN API lives on the
Pi and only probes whether port 11434 is open. Ollama's own /api/ps
under-reports badly -- it claimed 1411 MB for a model that actually
took 9489 MB, measured against sysfs on this machine.

So this reads the kernel's own figures, which need no ROCm tooling:

    /sys/class/drm/card0/device/gpu_busy_percent
    /sys/class/drm/card0/device/mem_info_vram_used
    /sys/class/drm/card0/device/mem_info_vram_total

HTTP rather than SSH because the backend runs in a container on the Pi
and already speaks HTTP-with-a-bearer-token to both Ollama and the WoL
API. One more of those needs no key material mounted into the image.

Install: deploy/deck/install.sh
Remove:  systemctl --user disable --now dollarden-gpu-probe
         rm ~/.config/systemd/user/dollarden-gpu-probe.service \
            ~/.local/bin/dollarden-gpu-probe.py ~/.config/dollarden-gpu-probe.token
"""
from __future__ import annotations

import json
import os
import pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DOLLARDEN_GPU_PROBE_PORT", "11435"))
# Under ~/.config, not /etc: SteamOS keeps / read-only and sudo needs a
# password, so this installs entirely as a user service. `loginctl
# Linger=yes` is already set on the deck, so it runs with nobody logged
# in — which matters, because the machine is usually woken remotely.
TOKEN_FILE = pathlib.Path(os.environ.get(
    "DOLLARDEN_GPU_PROBE_TOKEN_FILE",
    os.path.expanduser("~/.config/dollarden-gpu-probe.token")))
DRM = pathlib.Path("/sys/class/drm")


def _read_int(p: pathlib.Path) -> int | None:
    try:
        return int(p.read_text().strip())
    except Exception:
        return None


def gpu_state() -> dict:
    """First card that reports a busy percentage is the one we care about."""
    for card in sorted(DRM.glob("card*/device")):
        busy = _read_int(card / "gpu_busy_percent")
        if busy is None:
            continue
        used = _read_int(card / "mem_info_vram_used") or 0
        total = _read_int(card / "mem_info_vram_total") or 0
        return {
            "ok": True,
            "card": card.parent.name,
            "busy_percent": busy,
            "vram_used_mb": used // 1048576,
            "vram_total_mb": total // 1048576,
            "vram_free_mb": (total - used) // 1048576,
            "gtt_used_mb": (_read_int(card / "mem_info_gtt_used") or 0) // 1048576,
        }
    return {"ok": False, "error": "no DRM card exposes gpu_busy_percent"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True, "service": "dollarden-gpu-probe"})
            return
        if self.path.rstrip("/") != "/gpu":
            self._send(404, {"error": "not found"})
            return

        want = ""
        try:
            want = TOKEN_FILE.read_text().strip()
        except Exception:
            self._send(500, {"error": "probe token not readable"})
            return
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not want or got != want:
            self._send(401, {"error": "unauthorized"})
            return

        self._send(200, gpu_state())

    def log_message(self, *_args) -> None:
        """Quiet. This is polled; journald does not need every hit."""


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
