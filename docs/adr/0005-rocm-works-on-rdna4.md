# ADR 0005 — ROCm works on RDNA4; no Vulkan fallback needed

**Status:** SUPERSEDED by ADR 0010 (2026-11-16) · **Date:** 2026-10-21 · **Resolves:** the Phase 0 GPU spike

> **Superseded.** The measurements below stand; one conclusion did not.
> "SteamOS updates cannot break it" was wrong — the container pins ROCm's
> *userspace*, but AMDKFD is a **kernel** interface, and the 7.3.1 kernel
> deadlocks in it while mapping a model into VRAM. See ADR 0010 and
> `docs/16-deck-power.md`. Inference now runs on Vulkan.

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

## Measured performance (`qwen2.5vl:7b`, 5.97 GB)

| Workload | Result |
|---|---|
| Cold model load into VRAM | **2.8 s** |
| Sustained text generation | **58 tok/s** (301 tokens in 5.2 s) |
| Prompt prefill | **~1030 tok/s** |
| Structured JSON categorisation | 158 tokens in 3.2 s (49 tok/s) |
| Receipt image -> structured line items | **13.2 s** end to end (450 tokens) |

Combined with the ~10 s Wake-on-LAN cold start, a worst-case receipt parse on
a sleeping host is roughly **26 s** (10 s wake + 2.8 s load + 13 s inference),
and about 13 s on a warm host. Comfortably inside the async job design.

### Categorisation accuracy

A single grocery transaction against five categories returned schema-valid
JSON, the correct category, `confidence: 1`, and amounts summing exactly to
the transaction total.

### Receipt accuracy

A 520x760 synthetic receipt with 11 items across three categories, plus
section headers and tax:

- **10/10 line items extracted with exactly correct amounts**
- subtotal 7282, tax 528, total 7810 — all correct
- every item assigned to the right category
- `--- HOUSEHOLD ---` style section headers correctly ignored
- tax reported separately rather than smeared across items

One miss: the `date` field came back `null` despite `10/20/2026` being legible
on the image. It was optional in the schema. **Fix: make `date` required**, or
fall back to the transaction's own date, which is authoritative anyway.

## Consequences

- ADR 0003 (AI is optional) **stands regardless** — the host still sleeps, and
  can be offline or mid-game. But the pessimistic branch of the roadmap is
  closed: AI features are viable and fast.
- Run Ollama via podman with models under `/home`, managed by a systemd user
  unit so it survives reboot and wake.
- The OCR-free receipt design (ADR 0004) is **validated against real output**,
  not just assumed.
