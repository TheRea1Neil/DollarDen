# ADR 0010 — Vulkan, not ROCm, for inference on the deck

**Status:** accepted · **Date:** 2026-11-16 · **Supersedes ADR 0005**

## Context

ADR 0005 chose the ROCm backend and closed the question: *"Do not pursue
Vulkan or CPU fallbacks."* It was well measured and right about what it
measured. One of its reasons was wrong:

> `rocm_v7_2` libraries ship inside the container, so the immutable host
> rootfs never needs ROCm installed. **SteamOS updates cannot break it.**

The container half of ROCm is indeed pinned. The other half is not: the
AMDKFD compute interface lives in the **kernel**, which SteamOS updates.

On 2026-11-16 the AI host stopped answering and then hung on shutdown.
Cause, caught live three times on clean boots:

```
kfd_ioctl_map_memory_to_gpu            ← ROCm mapping the model into VRAM
  amdgpu_amdkfd_gpuvm_map_memory_to_gpu
    amdgpu_vm_validate
      __mutex_lock                     ← never returns
```

`task llama-server is blocked on a mutex likely owned by task
llama-server` — a self-deadlock in the driver, in kernel state `D`. It
could not be killed (`SIGKILL` included), so shutdown hung and the
machine had to be forced off through the relay. SteamOS had installed
kernel `7.3.1-valve2-1-neptune-73` three days earlier, replacing
`7.2.7-valve2-2-neptune-72`. Full write-up: `docs/16-deck-power.md`.

Not uptime (28 h, and a 5-minute-old boot reproduced it), not memory
pressure (16 GB VRAM, 1.4 GB used, and a 1.7 GB model hung identically),
not the container (unchanged since 2026-10-21).

## What was measured

The host's **Vulkan** stack was healthy throughout — `vulkaninfo`
reported `AMD Radeon RX 9060 XT (RADV GFX1200)`, API 1.4.350 — because
RADV goes through the DRM render node, not `/dev/kfd`. The deadlock is
in the compute path specifically, and Vulkan does not use it.

`ollama/ollama:latest` ships `/usr/lib/ollama/vulkan/libggml-vulkan.so`
plus the Vulkan loader and mesa RADV, and the binary honours
`OLLAMA_VULKAN`. The `:rocm` image carries only `libggml-hip.so`.

```
msg="inference compute" library=Vulkan name=Vulkan0
  description="AMD Radeon Graphics (RADV GFX1200)" libdirs=ollama,vulkan
  type=discrete total="15.9 GiB" available="15.4 GiB"
```

| Workload | ROCm (ADR 0005) | Vulkan |
|---|---|---|
| Cold load + answer, `gemma4:12b` | — | **17.0 s** (15.7 s load) |
| Three load/unload cycles | deadlocks on the first | 0 s, 5 s, 5 s |
| Tasks left in `D` state | 1, unkillable | **none** |
| Kernel blocked-task warnings | yes | **none** |

Real jobs end to end through the app: the five that failed during the
incident were re-queued and all completed in **15 seconds**, including
the Spotify charge that started it.

## Decision

Run inference on the **Vulkan** backend:

```bash
podman run -d --name ollama --restart always \
  --device /dev/dri \
  --security-opt seccomp=unconfined \
  -e OLLAMA_VULKAN=1 \
  -v /home/deck/ollama-models:/root/.ollama \
  -p 11434:11434 \
  docker.io/ollama/ollama:latest
```

`/dev/kfd` is **deliberately not passed**. The deadlocking interface is
not merely unused, it is unreachable — which is the point. Omitting it
is the enforcement, not a tidy-up.

## Consequences

- The GPU gate is unaffected: `deploy/deck/gpu-probe.py` reads
  `gpu_busy_percent` and `mem_info_vram_used` from sysfs, and
  `model_already_loaded` asks ollama's `/api/ps`. Neither touches ROCm.
- The `:rocm` image is kept on the host, unused, as a way back.
- ADR 0004's single-model choice and ADR 0007's OCR-free receipts are
  untouched — same models, same API, different compute backend.
- ADR 0003 (AI is optional) was the reason this incident cost a handful
  of categorisations rather than the budget. It keeps earning its place.
- The general lesson is narrower than "ROCm is bad": a pinned userspace
  does **not** pin a kernel interface. Anything riding a kernel ioctl is
  exposed to OS updates no matter how it is packaged.
