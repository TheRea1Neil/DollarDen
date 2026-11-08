# 16 — Powering the deck from the Pi

A relay wired across the deck's power switch, driven by a GPIO on
`nando`. Built 2026-11-08, tested against the real hardware the same
day: the deck went from fully off to answering in **33 seconds**.

This is the fallback for the cases Wake-on-LAN cannot reach. WoL only
works if the NIC still has power — so it is useless after a power cut,
on a machine at soft off with WoL disabled in the BIOS, or when
SteamOS has frozen hard. Those are exactly the situations where the
budget quietly stops categorising anything.

## The commands

On the Pi, as `neil`:

```bash
deck-power status        # is it up, when was the button last pressed
deck-power on            # short press: powers on from off
deck-power on --wait 90  # ...and wait for it to answer
deck-power force-off     # HOLD 5s: cuts power on a frozen machine
```

From this Mac, without logging in:

```bash
ssh nando 'deck-power status'
ssh nando 'deck-power on --wait 90'
```

Every action, including every refusal and its reason, is appended to
`/var/lib/deck-power/actions.log` as one JSON object per line.

## The thing to understand before using it

**A short press is a toggle, not an "on" switch.** It is wired to the
same header as the case button, so on a machine that is already
running it is an ACPI shutdown request.

That single fact is the whole safety model:

- `deck-power on` **refuses** when the deck answers on the network,
  because the press would shut it down. `--force` overrides, and
  should only be used when shutting it down is what you want.
- Any press starts a **180-second cooldown**. Repeatedly pressing the
  button of a machine that is mid-boot power-cycles it, which is how
  filesystems get corrupted.
- `force-off` is a five-second hold. It cuts power with no filesystem
  flush. There is no web path to it at all — see below.

Reachability is tested by opening TCP ports (22, 11434), not by ping.
A frozen SteamOS can still answer ICMP from the kernel while every
service is dead, so "it pings" would read as "it is fine".

## How the app uses it

Only as a last resort, inside `ensure_awake`:

1. Is the port open? Then nothing happens.
2. Send Wake-on-LAN, wait `AI_WAKE_TIMEOUT_SECONDS`.
3. Still nothing → press the power button **once**.
4. Wait `DECK_POWER_BOOT_SECONDS` (150s — a cold boot is POST plus a
   full SteamOS start, far longer than a resume).
5. Still nothing → tell the user, and **stop**. It is never pressed
   twice on its own.

The HTTP shim enforces the same rules independently, so a bug in the
app cannot talk it into misbehaving: it offers `POST /press` only,
refuses when the deck is reachable (409) or inside the cooldown (429),
and has no force-off verb at all. Holding down a power button is a
decision for a person at a terminal, not for a web request retrying
in the background.

## Wiring

```
Gigabyte B550 F_PANEL pins 5 & 7 (PWR_SW)
        └── Y-splitter
              ├── case button  (still works normally)
              └── relay NO + COM

AEDIKO 5V 1-channel relay, jumper on H (active-high)
        DC+ ── Pi pin 2  (5V)
        DC- ── Pi pin 6  (GND)
        IN  ── Pi pin 11 (BCM GPIO 17)
```

GPIO 17 idles as an input with a pull-down (`pinctrl get 17` reports
`ip pd | lo`), so a Pi reboot cannot fire the relay. The script drives
the pin low before anything else and releases it in a `finally`, so an
exception or a Ctrl-C mid-press cannot leave the button held down.

## Layout

| | |
|---|---|
| `/usr/local/bin/deck-power` | symlink to the CLI |
| `/opt/deck-power/` | the two scripts |
| `/etc/deck-power.env` | the API token, mode 640, group `gpio` |
| `/var/lib/deck-power/actions.log` | audit trail |
| `deck-power-api.service` | the shim the container talks to, port 11436 |

Reinstall with `sudo deploy/pi/install.sh`. It regenerates nothing it
does not have to — an existing token is kept.

## Troubleshooting

**Nothing happens, no click.** Check the jumper is on `H`. With it on
`L` the relay is inverted and idles closed, which would hold the power
button down permanently.

**The deck powers off instead of on.** It was already running, and
something used `--force`. Nothing in the app can do this.

**`refused: pressed Ns ago`.** The cooldown. Wait, or pass `--force`
if you are sure.

**The service will not start.** It refuses to run without
`DECK_POWER_TOKEN`; check `/etc/deck-power.env` and that the unit can
read it (`systemctl status deck-power-api`).

**The app says the switch could not be used.** Check
`DECK_POWER_URL` and `DECK_POWER_TOKEN` in `~/nnf/.env` match
`/etc/deck-power.env`, and that the container can reach port 11436 on
the host.
