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

**Shutdown hangs on the motherboard's power-down screen, and the only
way out is holding the button.** A process is in kernel state `D`
(uninterruptible sleep). Nothing can kill it — not `SIGKILL`, not
systemd — so shutdown waits for it forever. `force-off` is the correct
and only remedy; see the incident below for the one that caused this.

## 2026-11-16: the AI stopped answering and shutdown hung

Worth recording in full, because every symptom pointed somewhere other
than the cause.

**What was seen.** The deck woke to categorise a Spotify charge and
never answered. Five jobs failed in a row, each taking five to ten
minutes. Then shutdown hung and the machine had to be forced off.

**What it actually was.** A self-deadlock in the AMD graphics driver,
in the compute memory-mapping path:

```
kfd_ioctl_map_memory_to_gpu            ← ROCm asks to map the model into VRAM
  amdgpu_amdkfd_gpuvm_map_memory_to_gpu
    amdgpu_vm_validate
      __mutex_lock                     ← never returns
```

The kernel's own words: `task llama-server:44076 is blocked on a mutex
likely owned by task llama-server:44076`. It is stuck behind itself.

**Why nothing noticed.** Ollama's listener lives in the parent `ollama
serve` process; the model runs in a child `llama-server`. Only the
child wedged. So the TCP connect succeeded, `/api/version` returned
200, and `ensure_awake` concluded — correctly, on the evidence it had —
that the machine was up. Then every request hung until it timed out.

This is why there is no "better ping" fix. No probe short of actually
generating a token can tell a healthy ollama from one whose runner is
deadlocked, so the app does not try: it bounds the request
(`AI_REQUEST_TIMEOUT_SECONDS`, 120s), reports a timeout as its own
failure — `AIHostStalled`, "switched on but stopped answering… may
need restarting" — and then stops sending work to that host for
`AI_STALL_BACKOFF_SECONDS` (15 min). Without that last part the serial
queue walks every remaining job into the same wall, one full timeout
each. Five jobs became forty minutes of nothing.

**Why shutdown hung.** Same `D` state. Unkillable, so systemd waited.

**It was not long uptime.** The machine had been up 28 hours, not
weeks, and after a completely clean boot the very first model load
deadlocked again within five minutes — twice, with both a 8.2 GB model
and a 1.7 GB one. So a scheduled restart does not prevent it. It only
clears it after the fact, which `force-off` already does on demand.

**Not memory pressure either**, though the stack looks like it: 16 GB
of VRAM with 1.4 GB in use, and the 1.7 GB model hung the same way.

**What correlates.** The GPU is a Radeon RX 9060 XT (Navi 44, RDNA4,
`gfx1200`) — recent silicon on recent ROCm. SteamOS installed kernel
`7.3.1-valve2-1-neptune-73` on 2026-11-13 18:51, replacing
`7.2.7-valve2-2-neptune-72`. The ollama container has not changed since
2026-10-21.

The honest caveat: three jobs *did* succeed on 7.3.1 during the
2026-11-13→15 boot. Everything after the 2026-11-15 17:08 reboot has
failed. So the new kernel is strongly implicated but not proven sole
cause, and the working boot's kernel command line had already rotated
out of the journal, so the two could not be compared.

**Useful commands.**

```bash
# Is a task wedged in the driver? This is the signature.
ps -eo pid,stat,wchan:24,comm | awk '$2 ~ /D/'
#    3768 D    amdgpu_vm_validate       llama-server

journalctl -k -b 0 | grep -E "blocked for more than|amdgpu_vm_validate"

# Does it actually generate, or merely listen? The port lies.
curl -s --max-time 120 http://deck:11434/api/chat \
  -d '{"model":"gemma3:12b","messages":[{"role":"user","content":"say OK"}],"stream":false}'
```

A `podman restart` will NOT fix it — the runner cannot be killed:

```
StopSignal SIGTERM failed to stop container ollama in 10 seconds, resorting to SIGKILL
Error: given PID did not die within timeout
```

Only a power cycle clears it: `sudo deck-power force-off`, wait out the
cooldown, `sudo deck-power on`.

### How it was fixed

By leaving that code path unreachable rather than hoping it improves.

The deadlock is in the **compute** interface (`/dev/kfd`, AMDKFD), which
is what ROCm uses. Vulkan reaches the same GPU through the DRM render
node instead, and the host's Vulkan stack was healthy all along —
`vulkaninfo` reported `AMD Radeon RX 9060 XT (RADV GFX1200)` while
ollama was wedged.

So inference moved to the Vulkan backend (ADR 0010). `/dev/kfd` is no
longer passed to the container at all:

```bash
podman run -d --name ollama --restart always \
  --device /dev/dri \
  --security-opt seccomp=unconfined \
  -e OLLAMA_VULKAN=1 \
  -v /home/deck/ollama-models:/root/.ollama \
  -p 11434:11434 \
  docker.io/ollama/ollama:latest
```

Note `:latest`, not `:rocm` — the Vulkan backend library ships only in
the former (`/usr/lib/ollama/vulkan/libggml-vulkan.so`), along with the
Vulkan loader and mesa RADV. The `:rocm` image is kept on the host,
unused, as a way back.

Result: cold load and answer in 17 s, three consecutive load/unload
cycles with nothing left in `D` state and no kernel warnings, and the
five jobs that failed during the incident re-queued and finished in 15
seconds. It is also faster than ROCm was in practice.

Boot persistence is unchanged and was verified: `podman-restart.service`
is enabled for the `deck` user, lingering is on, and the container keeps
`restart=always`.

**If the AI is dead after a SteamOS update, check this first:**

```bash
podman logs ollama 2>&1 | grep "inference compute"
# want: library=Vulkan ... description="AMD Radeon Graphics (RADV GFX1200)"
# a line saying library=ROCm means the old container came back
```
