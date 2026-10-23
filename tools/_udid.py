"""The booted simulator's udid, or a clear error."""
import json
import sys

devices = json.load(sys.stdin)["devices"]
for runtime in devices.values():
    for dev in runtime:
        if dev.get("state") == "Booted":
            print(dev["udid"])
            raise SystemExit
raise SystemExit("no booted simulator — run: tools/sim boot")
