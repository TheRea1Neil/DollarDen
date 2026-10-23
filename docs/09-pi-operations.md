# 09 — Pi Operations

Operational record and runbook for `nando` (192.168.86.105).

## Resource cleanup — 2026-10-21

The Pi has 3.7 GB RAM and was running several services unrelated to this
project. They were stopped to make room for Postgres and the backend.

| | Before | After |
|---|---|---|
| RAM used | 1763 MB | **863 MB** |
| RAM available | 2027 MB | **2927 MB** |

### What was stopped

All via `docker compose down` — containers and networks removed, **volumes and
bind mounts preserved**. Nothing was deleted.

| Service | Compose file | RAM freed |
|---|---|---|
| Formbricks (app, hub, cube, postgres, redis) | `/home/neil/formbricks-quickstart/docker-compose.yml` | ~870 MB |
| PhotoPrism + MariaDB | `/var/lib/casaos/apps/big-bear-photoprism/docker-compose.yml` | ~300 MB |
| CasaOS Postgres | `/var/lib/casaos/apps/postgresql/docker-compose.yml` | ~30 MB |

Formbricks had `restart: always`, so `stop` alone would not have survived a
Docker daemon restart — `down` was required.

### Data preserved

| Data | Location | Size |
|---|---|---|
| **PhotoPrism originals — 487 photos, back to 2017** | `/DATA/AppData/big-bear-photoprism/originals` | **3.8 GB** |
| PhotoPrism thumbnails (regenerable) | `/DATA/AppData/big-bear-photoprism/storage` | 3.6 GB |
| CasaOS Postgres data | `/DATA/AppData/postgresql/data` | 109 MB |
| Formbricks volumes | `formbricks-quickstart_postgres`, `_redis` | 95 MB |

### Backups taken first

`/home/neil/backups/pre-nnf-cleanup-2026-10-21/`

| File | Contents |
|---|---|
| `casaos-postgresql-all.sql.gz` | `pg_dumpall`, 6 databases (casaos, utahmusic-react, webdata, campaign-template, danserver, sportsmarxsurvey) |
| `formbricks-postgres-all.sql.gz` | `pg_dumpall`, 1 database |

Both verified with `gzip -t` and a `CREATE DATABASE` count.

### Restoring any of it

```bash
sudo docker compose -f /home/neil/formbricks-quickstart/docker-compose.yml up -d
sudo docker compose -f /var/lib/casaos/apps/big-bear-photoprism/docker-compose.yml up -d
sudo docker compose -f /var/lib/casaos/apps/postgresql/docker-compose.yml up -d
```

Volumes and bind mounts are intact, so these come back with their data.
CasaOS's app-management service did **not** resurrect them automatically —
verified after the stop.

### Disk is not a constraint

583 GB free of 917 GB. The cleanup was purely about RAM. Deleting the
PhotoPrism originals or thumbnails would reclaim disk nobody needs, so it was
not done.

## Still running

| Service | Port | Purpose |
|---|---|---|
| `wg-caddy` | 443 | TLS edge, Let's Encrypt via DuckDNS DNS-01 |
| `wg-easy` | 51820/udp | WireGuard server |
| `wg-duckdns` | — | Dynamic DNS updater |
| `wol-api` (systemd) | 51822 | Wake-on-LAN API for the AI host |
| `jellyfin` | 8096/8920 | Media |
| `big-bear-portainer` | 9000/9443 | Docker UI |
| `casaos-gateway` | **80** | Occupies port 80 — why certs use DNS-01 |

## Ports in use (pick around these)

Taken: 22, 80, 443, 139, 445, 1901, 7359, 8000, 8097, 8921, 9000, 9443,
51820/udp, 51822.

Allocated to this project: **5433** (Postgres), **8081** (backend API, behind
Caddy; not published publicly).

Note 5432 is now free since the CasaOS Postgres was stopped, but 5433 is used
anyway so re-enabling that service later cannot collide.

## Backup plan for this project

- Nightly `pg_dump` of the `nnf` database to `/home/neil/backups/nnf/`.
- Weekly copy off-box.
- `TOKEN_ENCRYPTION_KEY` backed up **separately**, in a password manager —
  the dump is useless without it.
- **Test a restore before linking a production bank** (docs/04).

---

## Deployment (as of 2026-10-21)

The backend is live at **`https://budget.example.org`**.

### Layout on the Pi

```
/home/neil/nnf/
├── backend/     rsynced from the repo
├── deploy/      docker-compose.yml
└── .env         mode 600, never in git
```

Caddy config lives with the existing stack at `/home/neil/wireguard/Caddyfile`;
the `finance.` site block was appended there. Backups: `Caddyfile.bak-*`.

### Containers

| Container | Purpose | Exposure |
|---|---|---|
| `nnf-postgres` | Postgres 16.15 (aarch64) | `127.0.0.1:5433` only |
| `nnf-api` | FastAPI on 8081 | via Caddy only, never published |

Both on the `nnf` network; `nnf-api` also joins `wg` so Caddy can reach it.

### Deploying a change

```bash
# from the repo root on the Mac
rsync -az --delete --exclude .venv --exclude __pycache__ \
  backend deploy .env nando:/home/neil/nnf/

ssh nando
cd /home/neil/nnf/deploy
sudo docker compose -f /home/neil/nnf/deploy/docker-compose.yml \
  --env-file /home/neil/nnf/.env build nnf-api
sudo docker compose -f /home/neil/nnf/deploy/docker-compose.yml \
  --env-file /home/neil/nnf/.env run --rm nnf-api alembic upgrade head
sudo docker compose -f /home/neil/nnf/deploy/docker-compose.yml \
  --env-file /home/neil/nnf/.env up -d --force-recreate nnf-api
```

**Use absolute paths for `-f` and `--env-file`.** Relative paths break when
the command runs from anywhere other than `deploy/`.

**Build in the foreground.** A `systemd-run --user` wrapper around
`sudo docker compose build` reported `Result=success` while producing an image
that still contained the previous `pyproject.toml`. The cause was not
identified; running the build directly and watching `--progress plain` output
is reliable. Verify what actually landed rather than trusting the exit code:

```bash
sudo docker run --rm --entrypoint sh nnf-nnf-api:latest -c "pip list | grep -i <pkg>"
```

### Smoke test after deploying

```bash
curl https://budget.example.org/api/health   # {"status":"ok"}
curl https://budget.example.org/api/ready    # database:true
curl -o /dev/null -w '%{http_code}\n' \
  https://budget.example.org/wp-json/        # 404 at the edge
```

Verified working end to end over the public internet: register → 201,
`/me` with token → 200, `/me` without → 401, refresh → 200 and rotates,
replaying a used refresh token → 401.

## The AI host (deck) after a reboot

Ollama runs as a rootless podman container on the gaming PC, with models
bind-mounted from `/home/deck/ollama-models` (not the 5 GB SteamOS root).

It did **not** survive a reboot: the container had `restart=no`, so waking
the PC gave you a machine that was up but had no Ollama on it, and every
AI job failed until someone SSHed in and started it by hand. Fixed:

```bash
podman update --restart=always ollama
systemctl --user enable --now podman-restart.service   # needs linger, already on
```

`podman-restart.service` runs `podman start --all --filter
should-start-on-boot=true` at login.

That was not sufficient on its own. The container passes `/dev/dri/card0`
through for GPU access, and the unit runs at session start **before
amdgpu has created that device node**, so at real boot it failed with

```
crun: cannot stat `/dev/dri/card0`: No such file or directory
```

exit 125, with nothing retrying — the machine came up with no Ollama on
it. Surfaced after a SteamOS 3.9.4 update, though the update only caused
the reboot that exposed it. `renderD128` existed at boot; `card0` did
not.

The fix is a drop-in that waits for the device:

```
~/.config/systemd/user/podman-restart.service.d/wait-for-gpu.conf
  [Unit]  After=graphical-session.target
  [Service] ExecStartPre=... wait up to 60s for /dev/dri/card0 ...
```

Bounded, and it proceeds anyway on timeout so a machine with no GPU
still boots. There is deliberately no `Restart=`: systemd does not
honour it on `Type=oneshot`, and a line that looks like a retry but is
not would be worse than none.

**Worth knowing:** my earlier "verified" test ran the unit manually on a
warm system, where the device already existed. That proved the unit
worked and said nothing about the boot race. Testing the mechanism is
not testing the timing.

### A wedge under sustained 14B inference

On 2026-10-22, during an allocation experiment, the deck entered a state
where:

- `sshd` accepted TCP but reset before sending its banner (could not fork)
- Ollama returned `mkdir .../manifests-v2: input/output error` on any disk
  access, while `/api/ps` still answered from memory
- **journald stopped recording twelve minutes before the reboot**

The machine stayed pingable throughout. A reboot cleared it completely.

It was **not** the disk. NVMe SMART afterwards: `critical_warning 0`,
`media_errors 0`, `num_err_log_entries 0`, `available_spare 100%`, and the
previous boot's kernel log had no I/O errors at all. It was not disk space
either — models live on `/home`, which had 716 GB free.

The last thing journald recorded was `qwen3:14b` at `n_gen = 10650` and
still generating: a runaway thinking loop on the one scenario that model
could not terminate on. No OOM kill was logged, but logging had already
failed, so absence of evidence means little here.

Root cause is not established. What is established is that it coincided
with sustained 14B inference, and that `qwen3:14b` has since been removed
(ADR 0006 — it was not adopted anyway). If it recurs without that model
present, suspect hardware and run a long SMART self-test.
