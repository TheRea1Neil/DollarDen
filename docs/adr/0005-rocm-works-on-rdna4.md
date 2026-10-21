# ADR 0005 — ROCm works on RDNA4; no Vulkan fallback needed

**Status:** accepted · **Date:** 2026-10-21 · **Resolves:** the Phase 0 GPU spike

## Context

The plan flagged GPU acceleration as the single biggest technical unknown.
The AI host is an RX 9060 XT (Navi 44, **RDNA4**, gfx1200-class). ROCm support
for RDNA4 is recent, `rocminfo` was **not installed** on the host, and SteamOS
has an immutable rootfs. The fallback ladder was ROCm → Vulkan → CPU, and a
bad outcome would have demoted AI features to optional-only.

## What was measured

Ollama was run as a rootless **podman** container (`ollama/ollama:rocm`,
3.17 GB) with `--device /dev/kfd --device /dev/dri`. Both device nodes are
world-accessible (`crw-rw-rw-`), so no group changes were needed despite the
`deck` user not being in `render`.

Ollama 0.40.7 reported:

```
source=types.go:32 msg="inference compute" id=0 filter_id=0
  library=ROCm compute=gfx1200 name=ROCm0 description="AMD Radeon Graphics"
  libdirs=ollama,rocm_v7_2 driver=0.0 pci_id=0000:07:00.0
  type=discrete total="15.9 GiB" available="15.8 GiB"
```

## Decision

Use the **ROCm** backend. Do not pursue Vulkan or CPU fallbacks.

## Why this is the good outcome

- `compute=gfx1200` means the RDNA4 target is recognised, not coerced — no
  `HSA_OVERRIDE_GFX_VERSION` hack required.
- `rocm_v7_2` libraries ship inside the container, so the immutable host
  rootfs never needs ROCm installed. SteamOS updates cannot break it.
- 15.8 GiB of 15.9 GiB is available to the runtime, confirming the full
  framebuffer is usable rather than a reduced BAR window.
- `type=discrete` confirms it bound to the dGPU.

## Consequences

- ADR 0003 (AI is optional) **stands regardless** — the host still sleeps, and
  can be offline or mid-game. But the pessimistic branch of the roadmap is
  closed: AI features are viable.
- Run Ollama via podman with models under `/home`, managed by a systemd user
  unit so it survives reboot and wake.
- Still to measure: actual tokens/sec for `qwen2.5vl:7b`, and vision latency
  on a real receipt. Recorded here when the benchmark completes.
