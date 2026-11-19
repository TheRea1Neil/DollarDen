"""Developer documentation the server serves about itself.

Self-hosting this means wiring up hardware — a machine to wake, a way
to wake it, a way to tell whether it is up. Everyone's will be
different: Wake-on-LAN, a relay across the power switch, a smart plug,
a hypervisor API, a script on another box.

The person doing that wiring will quite likely have an assistant
helping them, and that assistant should be able to FETCH the exact
contract rather than infer it from a README it cannot see. So the
specs live here, served over the API and offered as an MCP tool,
structured rather than prose.

**These are generated from what the code actually calls.** A document
that drifts from the implementation is worse than no document: it
sends someone to build the wrong thing and then blames their wiring.
`tests/test_devdocs.py` asserts the two agree.

Unauthenticated on purpose. There is nothing here specific to an
install — it is the same text on every server — and requiring a
credential would mean an assistant could not read it until after the
setup it is meant to help with.
"""
from __future__ import annotations

from typing import Any

WAKE_CONTRACT: dict[str, Any] = {
    "topic": "wake-contract",
    "title": "Waking the AI host",
    "summary": (
        "This server can call a URL of yours to bring the machine that "
        "runs the model out of sleep or power-off. It does not care what "
        "is behind that URL — Wake-on-LAN, a relay across the power "
        "switch, a smart plug, a hypervisor API — only that it answers "
        "the shape below."
    ),
    "when_it_is_used": (
        "Before sending work to the model, if the host is not already "
        "reachable. If nothing is configured the server does not try to "
        "wake anything: an unreachable host simply means the work waits "
        "in the review queue, which is a supported way to run."
    ),
    "settings": [
        {"name": "POWER_RELAY_URL",
         "what": "Base URL of your wake service. Leave empty to disable."},
        {"name": "POWER_RELAY_TOKEN",
         "what": "Sent as `Authorization: Bearer <token>` on every call."},
        {"name": "POWER_RELAY_BOOT_SECONDS",
         "what": "How long to wait for a cold boot after a successful press "
                 "before giving up. Default 150."},
        {"name": "AI_HOST_URL",
         "what": "Where the model is served, e.g. http://10.0.0.5:11434. "
                 "Reachability of this address is how the server decides "
                 "whether a wake is needed at all."},
    ],
    "endpoints": [
        {
            "method": "POST",
            "path": "{POWER_RELAY_URL}/press",
            "purpose": "Wake or power on the host. Idempotent in effect, "
                       "not in action: see the cooldown note.",
            "headers": {"Authorization": "Bearer {POWER_RELAY_TOKEN}"},
            "request_body": None,
            "responses": {
                "200": {
                    "meaning": "The press happened, or the host is coming up.",
                    "body": {"pressed": True},
                },
                "409": {
                    "meaning": "The host is already reachable, so nothing was "
                               "done. IMPORTANT: this is a refusal, not a "
                               "failure — on a running machine a power-button "
                               "press is a shutdown.",
                    "body": {"pressed": False, "reason": "host already up"},
                },
                "429": {
                    "meaning": "Asked again too soon. Return how long to wait.",
                    "body": {"pressed": False, "retry_after_seconds": 120},
                },
            },
        },
        {
            "method": "GET",
            "path": "{POWER_RELAY_URL}/status",
            "purpose": "Whether the host is up, and whether a press is allowed "
                       "right now. Used by the admin portal and for "
                       "diagnostics; the server never needs it to decide to "
                       "wake.",
            "headers": {"Authorization": "Bearer {POWER_RELAY_TOKEN}"},
            "responses": {
                "200": {
                    "meaning": "Current state.",
                    "body": {
                        "deck_reachable": False,
                        "last_press_at": "2026-11-17T05:21:48+00:00",
                        "cooldown_remaining_seconds": 0,
                    },
                },
            },
        },
    ],
    "rules": [
        "**Refuse to press when the host is already up.** A short press on a "
        "running machine is an ACPI shutdown. Answer 409 instead.",
        "**Enforce a cooldown.** The server will not press twice on its own, "
        "but your service should refuse anyway: repeatedly pressing the power "
        "button of a machine that is mid-boot is how filesystems get "
        "corrupted. The reference implementation uses 180 seconds.",
        "**Decide reachability with TCP, not ping.** A host can answer ICMP "
        "while the thing you care about is not listening yet, and plenty of "
        "machines drop ICMP entirely. The reference checks ports 22 and "
        "11434.",
        "**Never expose this without authentication.** It is a remote power "
        "switch. Bind it to your own network.",
        "**Answer quickly.** Return as soon as the press is done rather than "
        "blocking until the host is up; the server polls for readiness "
        "itself.",
    ],
    "also_supported": {
        "wake_on_lan": {
            "settings": ["WOL_API_URL", "WOL_API_TOKEN",
                         "AI_WAKE_TIMEOUT_SECONDS"],
            "endpoint": {
                "method": "POST",
                "path": "{WOL_API_URL}/wake?wait={seconds}",
                "headers": {"Authorization": "Bearer {WOL_API_TOKEN}"},
                "note": "Tried first when configured. If it fails or the host "
                        "does not come up, the relay above is tried next — a "
                        "failing wake request is the strongest evidence the "
                        "machine is fully off, which is exactly when a relay "
                        "helps.",
            },
        },
    },
    "reference_implementation": (
        "deploy/pi/ in this repository: a Python service driving a GPIO relay "
        "wired across a motherboard's power switch header, with the cooldown "
        "and the already-up refusal implemented. Read it as an example, not a "
        "requirement."
    ),
    "minimal_example": (
        "from flask import Flask, request, jsonify\n"
        "import subprocess, time, socket\n\n"
        "app = Flask(__name__)\n"
        "TOKEN, HOST, COOLDOWN = 'change-me', '10.0.0.5', 180\n"
        "last_press = 0.0\n\n"
        "def up():\n"
        "    for port in (22, 11434):\n"
        "        try:\n"
        "            with socket.create_connection((HOST, port), 2):\n"
        "                return True\n"
        "        except OSError:\n"
        "            pass\n"
        "    return False\n\n"
        "@app.post('/press')\n"
        "def press():\n"
        "    global last_press\n"
        "    if request.headers.get('Authorization') != f'Bearer {TOKEN}':\n"
        "        return '', 401\n"
        "    if up():\n"
        "        return jsonify(pressed=False, reason='host already up'), 409\n"
        "    left = COOLDOWN - (time.time() - last_press)\n"
        "    if left > 0:\n"
        "        return jsonify(pressed=False, retry_after_seconds=int(left)), 429\n"
        "    last_press = time.time()\n"
        "    subprocess.run(['your-wake-command'], check=False)\n"
        "    return jsonify(pressed=True)\n"
    ),
}

GPU_PROBE: dict[str, Any] = {
    "topic": "gpu-probe",
    "title": "Reporting the graphics card's state",
    "summary": (
        "Optional. If you tell the server how busy the card is, it will hold "
        "AI work back rather than stuttering a game you are playing on the "
        "same machine. With nothing configured the server simply runs the "
        "work."
    ),
    "settings": [
        {"name": "GPU_PROBE_URL", "what": "Base URL. Empty disables the check."},
        {"name": "GPU_PROBE_TOKEN", "what": "Bearer token."},
        {"name": "AI_MIN_VRAM_MB",
         "what": "Free VRAM below which work is deferred. Default 10500."},
        {"name": "AI_MAX_GPU_BUSY_PERCENT",
         "what": "Utilisation above which work is deferred. Default 60."},
    ],
    "endpoints": [{
        "method": "GET",
        "path": "{GPU_PROBE_URL}/gpu",
        "headers": {"Authorization": "Bearer {GPU_PROBE_TOKEN}"},
        "responses": {"200": {"body": {
            "ok": True, "busy_percent": 12,
            "vram_free_mb": 14500, "vram_total_mb": 16304,
        }}},
    }],
    "rules": [
        "**Being unreachable means go, not stop.** A missing probe must never "
        "become a silent block on every transaction, so the server treats an "
        "unreachable probe as permission to proceed.",
        "**Read it from the system, not from a model.** The reference reads "
        "sysfs: gpu_busy_percent and mem_info_vram_used.",
    ],
    "reference_implementation": "deploy/deck/gpu-probe.py in this repository.",
}

TOPICS: dict[str, dict[str, Any]] = {
    WAKE_CONTRACT["topic"]: WAKE_CONTRACT,
    GPU_PROBE["topic"]: GPU_PROBE,
}


def index() -> dict[str, Any]:
    return {
        "topics": [
            {"topic": t["topic"], "title": t["title"], "summary": t["summary"]}
            for t in TOPICS.values()
        ],
        "note": "Fetch one with GET /api/docs/{topic}. These describe things "
                "you build and this server calls; everything here is "
                "optional, and the budget works without any of it.",
    }


def get(topic: str) -> dict[str, Any] | None:
    return TOPICS.get(topic.strip().lower())
