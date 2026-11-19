# Running the AI on your own hardware

This is an account of one working setup, not a specification. It is
here so you can see the shape of the thing and judge what you would
need, not so you can copy it exactly.

**None of this is required.** With AI switched off the budget works
exactly as it does otherwise, and a hosted model is a supported choice
for people without a spare machine. What self-hosting buys is simple:
your transaction history never leaves your house.

## The idea

A budget does not need a model very often. A handful of transactions
arrive a day, each one wants a sentence of thought, and then nothing
happens for hours.

That is a poor fit for a machine left running, and a good fit for one
that sleeps and gets woken. In this setup the server lives on a
Raspberry Pi that is always on and draws a couple of watts; the model
lives on a desktop with a graphics card that is asleep almost all the
time, and is woken when there is something to do.

## What one real setup looks like

| | |
|---|---|
| Always on | Raspberry Pi 4 — API, Postgres, Plaid sync, reverse proxy |
| Asleep by default | A desktop with a Radeon RX 9060 XT, 16 GB VRAM |
| Model | `gemma4:12b` under Ollama, about 8 GB |
| Cold wake to answered | ~20 seconds |
| Warm answer | 4–10 seconds |

The desktop is an ordinary PC. The graphics card matters more than
anything else, and 16 GB of VRAM is comfortable for a 12B model. A
smaller model on 8 GB works; a larger one does not.

## Getting at the GPU

Ollama in a container, with the GPU passed through:

```bash
podman run -d --name ollama --restart always \
  --device /dev/dri \
  --security-opt seccomp=unconfined \
  -e OLLAMA_VULKAN=1 \
  -v "$HOME/ollama-models:/root/.ollama" \
  -p 11434:11434 \
  docker.io/ollama/ollama:latest
```

**Vulkan, not ROCm**, and that is worth explaining because the
official advice points the other way for AMD cards.

ROCm worked here for three weeks and scored better on paper. Then an
OS update brought a new kernel and the compute driver deadlocked while
loading the model into VRAM — an unkillable process, which also meant
the machine could not shut down. Vulkan reaches the same card through
a different path that the bug does not touch, and in practice it
turned out to be faster as well. The full story is in ADR 0010.

The lesson generalises past this one card: a container pins the
userspace half of a GPU driver and not the kernel half, so an OS
update can still break it.

## Waking it

The server needs the host up on demand. How is up to you — this is a
URL the server calls, not a mechanism it ships.

**Wake-on-LAN** is the obvious first choice and works whenever the
machine is suspended with a NIC that still has power.

**It is not enough on its own.** WoL cannot reach a machine that is
fully powered off, that lost power, or whose BIOS has WoL disabled.
This setup adds a relay across the motherboard's power switch header,
driven by a GPIO pin on the Pi — in parallel with the case button, so
that still works normally:

```
GPIO 17 (pin 11) -> relay IN      (active-high)
5V      (pin 2)  -> relay DC+
GND     (pin 6)  -> relay DC-
relay NO + COM   -> F_PANEL power switch pins, via a Y-splitter
```

A short press is a power button press. On a running machine that is an
ACPI shutdown, so the code refuses to press when the host is already
reachable, and will not press twice: repeatedly stabbing the power
button of a machine that is mid-boot is how filesystems get corrupted.

`deploy/pi/` is this implementation, and the reference for the
contract your own must honour:

```
POST {wake_url}          Authorization: Bearer {token}
  -> 200 once the host is reachable, or when the attempt has started
  -> 409 if it is already up
  -> 429 if asked again too soon

GET  {wake_url}/status   Authorization: Bearer {token}
  -> {"reachable": bool, "last_attempt_at": iso8601|null,
      "cooldown_seconds": int}
```

A smart plug, a hypervisor API, or a script on another machine would
all do. The server does not care what is behind the URL.

## Staying out of the way

Two things stop the budget interrupting the rest of the machine's
life.

**A GPU check.** Before running, the server asks how much VRAM is free
and how busy the card is. If a game is running, the work is deferred
and tried again later rather than stuttering someone's frame rate.
`deploy/deck/gpu-probe.py` is a small service that reports this from
sysfs.

**Quiet hours.** Nothing runs overnight, so the machine is not woken
at 3am to categorise a coffee. Transactions still arrive on schedule;
only the AI part waits until morning. Configurable, and switchable
off.

Both are specific to owning the machine, and both are skipped entirely
for a hosted model.

## What it is like to live with

The honest version: it works, and it is not free of maintenance.

An OS update broke the compute driver once and took an evening to
diagnose. The machine has to be reachable, which means a static
address or a DHCP reservation. When the AI is unavailable nothing
breaks — transactions wait in the review queue — but you do notice.

For the right person that is a fair trade for a budget whose data
never leaves the house. For everyone else there is an API key, and the
app does not think less of you for it.
