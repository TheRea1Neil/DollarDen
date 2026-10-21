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
