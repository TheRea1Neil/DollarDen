#!/usr/bin/env python3
"""Press the deck's power button, through a relay wired to the Pi.

Wake-on-LAN cannot help when the machine is at S5 (soft off) with WoL
disabled, after a power cut, or when SteamOS has frozen hard. This is
the physical fallback: a relay across the motherboard's PWR_SW header,
in parallel with the case button, which still works normally.

    GPIO 17 (physical pin 11) -> relay IN   (jumper on H, active-high)
    5V      (physical pin 2)  -> relay DC+
    GND     (physical pin 6)  -> relay DC-
    relay NO + COM           -> F_PANEL pins 5 & 7 via a Y-splitter

Two gestures, exactly as a person would make them:

    short (0.5s)  powers on from off; on a RUNNING machine this is an
                  ACPI request and shuts it DOWN
    long  (5.0s)  holds the button to cut power on a frozen machine

That asymmetry is the whole danger. A short press is not "turn on", it
is "toggle", and firing one blindly at a machine that is already up
turns it off. So `on` refuses unless the host is actually unreachable,
and every action is rate-limited and logged.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import sys
import time
from datetime import UTC, datetime

PIN = int(os.environ.get("DECK_POWER_PIN", "17"))
HOST = os.environ.get("DECK_HOST", "192.168.86.196")
PORTS = (22, 11434)

SHORT_PRESS = 0.5
LONG_PRESS = 5.0

STATE_DIR = pathlib.Path(os.environ.get("DECK_POWER_STATE", "/var/lib/deck-power"))
LOG_PATH = STATE_DIR / "actions.log"

# Nothing may press the button twice inside this window. The point of
# the relay is to help when waking failed; a loop of presses on a
# machine that is mid-boot would power-cycle it repeatedly, which is
# how you corrupt a filesystem.
COOLDOWN_SECONDS = int(os.environ.get("DECK_POWER_COOLDOWN", "180"))


def host_is_up(host: str = HOST, ports: tuple[int, ...] = PORTS,
               timeout: float = 1.5) -> bool:
    """Any open port means the machine is running.

    Ports rather than ping: a frozen SteamOS can still answer ICMP from
    the kernel while every service is dead, and "it pings" would then
    read as "it is fine".
    """
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _log(action: str, **fields: object) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.now(UTC).isoformat(), "action": action, **fields}
    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def last_action_at() -> datetime | None:
    try:
        lines = LOG_PATH.read_text().strip().split("\n")
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("pressed"):
            return datetime.fromisoformat(entry["at"])
    return None


def cooldown_remaining() -> float:
    last = last_action_at()
    if last is None:
        return 0.0
    elapsed = (datetime.now(UTC) - last).total_seconds()
    return max(0.0, COOLDOWN_SECONDS - elapsed)


def press(duration: float) -> None:
    """Close the relay for `duration`, then open it. Always open it."""
    from gpiozero import OutputDevice

    # initial_value=False drives the pin LOW before anything else, so
    # constructing this object can never itself trigger a press.
    relay = OutputDevice(PIN, active_high=True, initial_value=False)
    try:
        relay.on()
        time.sleep(duration)
    finally:
        # Finally, not after: an exception or a Ctrl-C mid-press would
        # otherwise leave the button held down forever, which reads as
        # a hard power-off and then a boot loop.
        relay.off()
        relay.close()


def _refuse(reason: str, **fields: object) -> int:
    print(f"refused: {reason}", file=sys.stderr)
    _log("refused", pressed=False, reason=reason, **fields)
    return 2


def cmd_on(args: argparse.Namespace) -> int:
    up = host_is_up()
    if up and not args.force:
        return _refuse(
            "the deck is already reachable — a short press would ask it to "
            "SHUT DOWN. Use --force only if you mean that.",
            host_up=True,
        )
    remaining = cooldown_remaining()
    if remaining > 0 and not args.force:
        return _refuse(
            f"pressed {COOLDOWN_SECONDS - remaining:.0f}s ago; waiting "
            f"{remaining:.0f}s more. Repeated presses power-cycle a booting "
            f"machine.",
            cooldown_remaining=round(remaining),
        )

    print(f"short press ({SHORT_PRESS}s) on GPIO{PIN}…")
    press(SHORT_PRESS)
    _log("short_press", pressed=True, host_up_before=up)
    print("done")

    if args.wait:
        deadline = time.time() + args.wait
        while time.time() < deadline:
            if host_is_up():
                waited = args.wait - (deadline - time.time())
                print(f"deck answered after ~{waited:.0f}s")
                return 0
            time.sleep(3)
        print(f"deck did not answer within {args.wait}s", file=sys.stderr)
        return 1
    return 0


def cmd_force_off(args: argparse.Namespace) -> int:
    remaining = cooldown_remaining()
    if remaining > 0 and not args.force:
        return _refuse(f"pressed recently; waiting {remaining:.0f}s more.")
    print(f"HOLDING power button for {LONG_PRESS}s — this cuts power…")
    press(LONG_PRESS)
    _log("long_press", pressed=True)
    print("done")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    last = last_action_at()
    state = {
        "deck_host": HOST,
        "deck_reachable": host_is_up(),
        "gpio_pin": PIN,
        "last_press_at": last.isoformat() if last else None,
        "cooldown_remaining_seconds": round(cooldown_remaining()),
        "log": str(LOG_PATH),
    }
    print(json.dumps(state, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deck-power", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    on = sub.add_parser("on", help="short press: power on (or ACPI shutdown)")
    on.add_argument("--wait", type=int, default=0,
                    help="seconds to wait for the deck to answer")
    on.add_argument("--force", action="store_true",
                    help="press even if the deck is up or in cooldown")
    on.set_defaults(func=cmd_on)

    off = sub.add_parser("force-off",
                         help="hold 5s: cut power on a frozen machine")
    off.add_argument("--force", action="store_true", help="ignore cooldown")
    off.set_defaults(func=cmd_force_off)

    st = sub.add_parser("status", help="what the relay and the deck are doing")
    st.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
