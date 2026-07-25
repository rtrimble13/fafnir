# Fresh Install on a Hetzner Cloud Server

A complete, ordered walkthrough: from an empty Hetzner Cloud project to a hardened
PostgreSQL 16 host running fafnir on a nightly schedule.

**Target:** Hetzner Cloud server · Ubuntu 24.04 LTS · PostgreSQL 16 · Python 3.11+ ·
FMP **Professional** key.

The PostgreSQL, role, schema, scheduling-expression and backup/restore steps below
were validated against a real PostgreSQL 16.13 cluster on Ubuntu 24.04.4, and the
expected outputs shown are the actual ones. The Hetzner-side steps (server types,
prices, locations, volume IDs) depend on your project and on Hetzner's current
offering — confirm those in the Cloud Console.

> **Related docs:** [backfill.md](backfill.md) covers the historical load in depth
> (bandwidth, chunking, resumability); [operations.md](operations.md) is the
> steady-state runbook. This guide is the one to follow **first**, on a brand-new host.

---

## 0. What you will end up with

| Thing | Location | Owner |
|---|---|---|
| Code (git checkout) | `/opt/fafnir` | `fafnir:fafnir` |
| Virtualenv (`fafnir`, `duk`) | `/opt/fafnir/.venv` | `fafnir:fafnir` |
| Service-user home + `~/.fafnirrc` | `/var/lib/fafnir` | `fafnir:fafnir` |
| Secrets (env file) | `/etc/fafnir/fafnir.env` | `root:fafnir`, `0640` |
| Logs | `/var/log/fafnir` | `fafnir:adm`, `0750` |
| Postgres data | `/var/lib/postgresql/16/main` (or a Volume) | `postgres` |
| Postgres tuning | `/etc/postgresql/16/main/conf.d/10-fafnir.conf` | `root` |
| Schedule | `fafnir-daily.timer` (systemd) or cron | — |

Two OS users: **`deploy`** (your human sudo login) and **`fafnir`** (an unprivileged
system user that owns the code and runs the jobs). Three database roles, per the
[role model](architecture.md#role-model-least-privilege): `fafnir_ingest` (write),
`fafnir_read` (read core+mart), `fafnir_app` (read mart only — this is what `duk` uses).

**Nothing listens on the public internet.** Postgres binds to localhost; remote
`duk` access goes through an SSH tunnel (§11).

---

## 1. Provision the server

### 1.1 Sizing

fafnir stores **daily** bars, so it is small. The swing factor is how far back you
backfill. Estimates for the active US equity + ETF universe (~8k symbols):

| Backfill start | `core.daily_price` | + `landing` raw payloads | Total DB + WAL + one dump | Recommended disk |
|---|---|---|---|---|
| 2015 (~10 y) | ~1.5 GB | ~1 GB | ~6–8 GB | 40 GB |
| 1990 (~35 y) | ~4 GB | ~2–3 GB | ~15–20 GB | **80 GB** |

Empty schema (all migrations + seeds + 39 years of partitions and calendar) is
**16 MB**, so essentially all of the above is price history.

CPU and RAM matter less than you'd expect — the backfill is network-bound (throttled
to 280 FMP requests/min), not compute-bound.

Buy on specs, not on a type name — Hetzner revises its server lines and pricing
periodically (`hcloud server-type list`, or the console, is the authority):

| Workload | Specs to look for | Example types |
|---|---|---|
| Shallow backfill (2015+), single reader | 2 vCPU / 4 GB / 40 GB | `CX22`, `CAX11` |
| **Recommended baseline** (1990 backfill, room to grow) | **4 vCPU / 8 GB / 80 GB** | `CX32`, `CAX21`, `CPX31` |
| Heavy concurrent research / future intraday | 8 vCPU / 16 GB / 160 GB+ | `CX42`, `CAX31`, `CCX23` (dedicated) |

Notes:

- **Arm64 (`CAX`) works and is the cheapest per GB.** All of fafnir's dependencies
  (`psycopg[binary]`, `pandas`, `scipy`) publish `aarch64` manylinux wheels, and
  PostgreSQL 16 is a first-class Arm64 package. Pick x86 (`CX`/`CPX`) only if you
  plan to add dependencies with no Arm wheels.
- **Traffic is not a constraint.** Current cloud plans include 20 TB/month; a full
  1990 backfill moves ~4–6 GB. Your **FMP** 50 GB/month cap is the real limit — see
  [backfill.md](backfill.md#6-full-backfill--a-single-run-of-a-few-hours).
- **Location:** `ash` (Ashburn, VA) or `hil` (Hillsboro, OR) put you closest to FMP's
  US endpoints and shave latency off a 30k-request backfill. EU locations (`fsn1`,
  `nbg1`, `hel1`) are fine too — the throttle dominates, not RTT.
- **Local disk vs Volume:** the server's built-in NVMe is faster and simpler. But a
  server **disk upgrade is irreversible** (you can never scale that server back down),
  so if you expect to grow — or want to snapshot the data independently of the OS —
  put the data directory on a **Volume** (§3.7). Volumes are network-attached: slightly
  higher latency, resizable up at any time.

### 1.2 Create the server

Add your SSH public key first (**Security → SSH Keys** in the Cloud Console), so the
server never has a root password.

Console path: **Servers → Add Server** → Location → Image **Ubuntu 24.04** → Type
(from §1.1) → **SSH key** (select yours) → optionally enable **Backups** → Create.

Equivalent with the [`hcloud` CLI](https://github.com/hetznercloud/cli):

```bash
hcloud context create fafnir            # paste a project API token
hcloud ssh-key create --name laptop --public-key-from-file ~/.ssh/id_ed25519.pub

hcloud server create \
  --name fafnir-db \
  --type cx32 \
  --image ubuntu-24.04 \
  --location ash \
  --ssh-key laptop \
  --enable-backup
```

List what is actually available before you commit to the flags above:
`hcloud server-type list`, `hcloud location list`, `hcloud image list --type system`.

Note the public IPv4 shown on the server's page; `<SERVER_IP>` below refers to it.

### 1.3 Cloud Firewall (do this before you log in)

Hetzner's Cloud Firewall runs **outside** the server, so it protects you even if the
host misconfigures. Create one firewall and attach it to the server:

| Direction | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 22 | `<your-home-ip>/32` | SSH (tunnels ride on this) |
| Inbound | ICMP | — | `0.0.0.0/0`, `::/0` | ping / MTR (optional) |

Leave outbound unrestricted (fafnir needs HTTPS to FMP and the apt/PyPI mirrors).

**Never open 5432.** Postgres stays on localhost; §11 tunnels it over SSH.

```bash
hcloud firewall create --name fafnir-fw
hcloud firewall add-rule fafnir-fw --direction in --protocol tcp --port 22 \
  --source-ips <your-home-ip>/32
hcloud firewall apply-to-resource fafnir-fw --type server --server fafnir-db
```

> If your home IP is dynamic, either widen the source to your ISP range, use a
> Hetzner Cloud Network + a jump host, or re-run the `add-rule` when it changes. If
> you lock yourself out, the console's **Rescue → Console** (VNC) always gets you back in.

### 1.4 First login

```bash
ssh root@<SERVER_IP>
```

---

## 2. Base OS hardening

All of §2 runs as `root` on the server.

```bash
# 2.1 Patch, set a hostname, keep the clock on UTC (leave it UTC -- §8 handles market time)
apt-get update && apt-get -y upgrade
hostnamectl set-hostname fafnir-db
timedatectl set-timezone UTC
timedatectl                                  # expect: Time zone: UTC (UTC, +0000)

# 2.2 A human sudo user (SSH keys copied from root)
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

# Give deploy a password -- sudo needs one, and `--disabled-password` left the
# account locked (sudo would fail with "a password is required"). SSH password
# login stays disabled by 2.3, so this password is only ever used for sudo.
passwd deploy

# 2.3 SSH: keys only, no root login
cat > /etc/ssh/sshd_config.d/10-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
EOF
sshd -t && systemctl reload ssh
```

> Prefer an unattended box with no sudo password? Skip `passwd deploy` and grant
> `NOPASSWD` instead — it is the only other combination that works with a locked
> account:
> ```bash
> echo 'deploy ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/deploy
> chmod 440 /etc/sudoers.d/deploy
> visudo -c -f /etc/sudoers.d/deploy        # must print "parsed OK"
> ```

**Verify in a second terminal before you close this one** — key login works and the
account can escalate:

```bash
ssh deploy@<SERVER_IP> 'id -nG | tr " " "\n" | grep -qw sudo && echo sudo-group-ok'
ssh -t deploy@<SERVER_IP> 'sudo true && echo sudo-ok'      # prompts unless NOPASSWD
```

```bash
# 2.4 Host firewall (defence in depth behind the Cloud Firewall)
apt-get install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable
ufw status verbose

# 2.5 Brute-force protection + unattended security updates
apt-get install -y fail2ban unattended-upgrades
systemctl enable --now fail2ban
dpkg-reconfigure -plow unattended-upgrades     # answer Yes

# 2.6 Swap -- Hetzner images ship with none. 2 GB keeps a small instance from
#     OOM-killing Postgres during pip builds or a large ANALYZE.
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
free -h
```

From here on, log in as `deploy` and use `sudo`.

---

## 3. Install PostgreSQL 16

### 3.1 Add the PGDG repository

Ubuntu 24.04 ships PostgreSQL 16, but the PGDG repo is what you want: point releases
land faster and the 17/18 upgrade path is there when you choose to take it.

```bash
sudo apt-get install -y curl ca-certificates
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt-get update
sudo apt-get install -y postgresql-16 postgresql-client-16
```

`pg_stat_statements` (used in §12) ships inside the `postgresql-16` package — no
separate `-contrib` install is needed.

### 3.2 Re-create the cluster with data checksums

The cluster the package creates for you has **`data_checksums = off`**. For a
warehouse whose first pillar is correctness, turn them on — silent page corruption
should be an error, not a wrong number in a backtest. Do this **now**, while the
cluster is empty:

```bash
sudo -u postgres psql -tAc "SHOW data_checksums;"        # off  <-- the default
sudo pg_dropcluster --stop 16 main
sudo pg_createcluster --start 16 main --locale=C.UTF-8 --encoding=UTF8 -- --data-checksums

sudo -u postgres psql -tAc "SHOW data_checksums;"        # on
sudo -u postgres psql -tAc "SHOW server_encoding;"       # UTF8   <-- verify, don't assume
sudo -u postgres psql -tAc "SHOW password_encryption;"   # scram-sha-256
```

> **Pin the locale explicitly** — that is not belt-and-braces. `pg_createcluster`
> takes the locale from its environment, and `sudo` scrubs `LANG`/`LC_ALL`, so on a
> cloud image where the locale is unset you get an **`SQL_ASCII` / `C`** cluster. That
> breaks the seeds outright (`fafnir db seed` fails with a foreign-key error on
> `ref.exchange`) and would mangle every non-ASCII company name that made it in. If
> `SHOW server_encoding` says anything but `UTF8`, drop the cluster and redo this step
> — encoding cannot be changed in place.

> Already have data in the cluster? Don't drop it — stop the cluster and run
> `sudo -u postgres /usr/lib/postgresql/16/bin/pg_checksums --enable -D /var/lib/postgresql/16/main`
> instead (offline, rewrites every page).

### 3.3 Tune for the instance size

Ubuntu's `postgresql.conf` ends with `include_dir = 'conf.d'`, so a drop-in file
cleanly overrides the defaults without editing the packaged config.

```bash
sudo tee /etc/postgresql/16/main/conf.d/10-fafnir.conf > /dev/null <<'CONF'
# fafnir tuning -- profile: 4 vCPU / 8 GB RAM
listen_addresses = 'localhost'
max_connections = 50

shared_buffers = 2GB                    # ~25% of RAM
effective_cache_size = 6GB              # ~75% of RAM (a hint, not an allocation)
maintenance_work_mem = 512MB            # index builds, VACUUM
work_mem = 32MB                         # per sort/hash node -- see note below

wal_compression = on
min_wal_size = 1GB
max_wal_size = 4GB                      # fewer checkpoints during bulk loads
checkpoint_completion_target = 0.9
wal_buffers = 16MB

random_page_cost = 1.1                  # NVMe/SSD, not spinning rust
effective_io_concurrency = 200

max_worker_processes = 4                # = vCPU count
max_parallel_workers = 4
max_parallel_workers_per_gather = 2
max_parallel_maintenance_workers = 2

# core.daily_price is append-mostly: analyze often, vacuum less.
autovacuum_vacuum_scale_factor = 0.05
autovacuum_analyze_scale_factor = 0.02
default_statistics_target = 200

shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = all

timezone = 'UTC'
log_min_duration_statement = '1000ms'
log_checkpoints = on
log_autovacuum_min_duration = '0'
log_temp_files = 0
log_line_prefix = '%m [%p] %q%u@%d '
CONF
sudo systemctl restart postgresql@16-main
```

Scale by RAM: for **4 GB** use `shared_buffers = 1GB`, `effective_cache_size = 3GB`,
`work_mem = 16MB`, `maintenance_work_mem = 256MB`, and drop the parallel workers to
`2`. For **16 GB**, double the 8 GB numbers.

`work_mem` is **per sort/hash node**, not per connection — 50 connections running
complex plans can each use several multiples of it. 32 MB with `max_connections = 50`
is deliberately conservative for a research box.

**Verify the config parsed and applied:**

```bash
sudo -u postgres psql -tAc "SELECT count(*) FROM pg_file_settings WHERE error IS NOT NULL;"   # 0
sudo -u postgres psql -c "SELECT name, setting, unit FROM pg_settings
  WHERE name IN ('shared_buffers','effective_cache_size','work_mem','max_wal_size',
                 'random_page_cost','default_statistics_target');"
```

Expect `shared_buffers = 262144` (8 kB pages = 2 GB) and
`effective_cache_size = 786432` (= 6 GB).

### 3.4 Create the roles and the database

Passwords: generate them now and keep them somewhere safe (`openssl rand -base64 24`).
`fafnir_ingest` is the **database owner** — it must own the objects so the nightly
`fafnir db ensure-horizon` can attach new yearly partitions to `core.daily_price`.

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE fafnir_ingest LOGIN PASSWORD 'REPLACE_ingest';
CREATE ROLE fafnir_read   LOGIN PASSWORD 'REPLACE_read';
CREATE ROLE fafnir_app    LOGIN PASSWORD 'REPLACE_app';
CREATE DATABASE fafnir OWNER fafnir_ingest;
SQL

# Query-statistics view used in §10. Creating an extension needs superuser, so do it
# now while you are still the postgres role.
sudo -u postgres psql -d fafnir -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
```

### 3.5 Password-free local auth for the nightly job (recommended)

Map the OS user `fafnir` to the database role `fafnir_ingest` over the Unix socket.
The nightly job then needs **no database password anywhere on disk**.

```bash
# The OS user is created in §4.1; this ident map refers to it.
echo 'fafnirmap  fafnir  fafnir_ingest' | sudo tee -a /etc/postgresql/16/main/pg_ident.conf

# IMPORTANT: this line must sit ABOVE the generic "local all all peer" rule.
sudo sed -i '/^# TYPE  DATABASE/a local   all             fafnir_ingest                           peer map=fafnirmap' \
  /etc/postgresql/16/main/pg_hba.conf

sudo systemctl reload postgresql@16-main
grep -nE '^(local|host)' /etc/postgresql/16/main/pg_hba.conf
```

pg_hba is first-match-wins, so this rule must appear before `local all all peer` —
otherwise peer auth demands an OS user literally named `fafnir_ingest` and fails. The
database column is `all` rather than `fafnir` so the same credential-free path also
works for maintenance databases, such as the restore drill in §9.4.

Keep the default `host all all 127.0.0.1/32 scram-sha-256` line — that is the path
`duk` uses with `fafnir_app` (§11).

### 3.6 Confirm nothing is exposed

```bash
sudo ss -lntp | grep 5432        # loopback only: 127.0.0.1:5432 and/or [::1]:5432
```

Any non-loopback bind address (`0.0.0.0:5432`, `*:5432`, the server's public IP) means
`listen_addresses` did not take effect — fix the drop-in from §3.3 and restart.

### 3.7 Optional: move the data directory to a Volume

Skip this if you sized the server's local disk for your backfill (§1.1).

```bash
hcloud volume create --name fafnir-data --size 100 --server fafnir-db --format ext4
```

On the server (`<VOLUME_ID>` is shown in the console / `hcloud volume list`):

```bash
DEV=/dev/disk/by-id/scsi-0HC_Volume_<VOLUME_ID>
sudo mkdir -p /mnt/fafnir-data
echo "$DEV /mnt/fafnir-data ext4 defaults,discard,nofail 0 0" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount -a && df -h /mnt/fafnir-data
```

`nofail` matters: without it, a Volume that isn't attached yet at boot leaves the
server stuck in emergency mode. `discard` lets the volume reclaim freed blocks.

Move the cluster (it is still empty, so this is instant):

```bash
sudo systemctl stop postgresql@16-main
sudo rsync -a /var/lib/postgresql/16/main/ /mnt/fafnir-data/16/main/
sudo chown -R postgres:postgres /mnt/fafnir-data/16
sudo chmod 700 /mnt/fafnir-data/16/main
echo "data_directory = '/mnt/fafnir-data/16/main'" \
  | sudo tee /etc/postgresql/16/main/conf.d/20-datadir.conf
sudo systemctl start postgresql@16-main
sudo -u postgres psql -tAc "SHOW data_directory;"    # /mnt/fafnir-data/16/main
```

Only after verifying the new path: `sudo rm -rf /var/lib/postgresql/16/main`.

---

## 4. Install fafnir

### 4.1 Users, directories, checkout

```bash
sudo apt-get install -y git python3 python3-venv python3-dev tmux

sudo git clone https://github.com/rtrimble13/fafnir.git /opt/fafnir
sudo useradd --system --create-home --home-dir /var/lib/fafnir --shell /bin/bash fafnir
sudo chown -R fafnir:fafnir /opt/fafnir
sudo install -d -o fafnir -g adm -m 0750 /var/log/fafnir
```

### 4.2 Virtualenv

Ubuntu 24.04 marks its system Python as *externally managed* — a bare
`pip install -e .` fails with `error: externally-managed-environment`. Use a venv:

```bash
sudo -u fafnir -H python3 -m venv /opt/fafnir/.venv
sudo -u fafnir -H /opt/fafnir/.venv/bin/pip install --upgrade pip
sudo -u fafnir -H /opt/fafnir/.venv/bin/pip install -e /opt/fafnir

sudo -u fafnir -H /opt/fafnir/.venv/bin/fafnir --version    # 0.1.0
sudo -u fafnir -H /opt/fafnir/.venv/bin/duk --version       # 1.1.0
```

(`-H` gives pip a writable cache under `/var/lib/fafnir` instead of the calling user's
home.)

Add `dev` extras (`pip install -e '/opt/fafnir[dev]'`) only if you intend to run the
test suite on the server.

### 4.3 Configuration file

Copy the template to the service user's home and change three things: an absolute
`log_dir`, the `calendar_start_year`, and the database `host`.

```bash
sudo -u fafnir cp /opt/fafnir/etc/fafnirrc /var/lib/fafnir/.fafnirrc
sudo chmod 600 /var/lib/fafnir/.fafnirrc
sudo -u fafnir sed -i \
  -e 's|^log_dir = .*|log_dir = "/var/log/fafnir"|' \
  -e 's|^calendar_start_year = .*|calendar_start_year = 1990|' \
  -e 's|^host = .*|host = "/var/run/postgresql"|' \
  /var/lib/fafnir/.fafnirrc
```

> ### Set `calendar_start_year` **before** you seed
> It is the earliest year fafnir builds partitions and the trading calendar for, and
> it defaults to the `--floor-year` of `fafnir db ensure-horizon`. Set it to your
> backfill start (1990 here). If you seed first and change it later, `ensure-horizon`
> only extends the calendar **forward** — it will not fill in the years behind you;
> you would have to re-run `fafnir db seed`. See
> [backfill.md §3](backfill.md#3-configure-fafnirrc-and-the-environment).

### 4.4 Environment file (secrets)

```bash
sudo install -d -m 755 /etc/fafnir
sudo tee /etc/fafnir/fafnir.env > /dev/null <<'ENV'
# Socket + peer auth (§3.5): no database password needed.
FAFNIR_DSN="host=/var/run/postgresql port=5432 dbname=fafnir user=fafnir_ingest"
FMP_API_KEY="your_fmp_pro_key"
FAFNIR_SQL_DIR="/opt/fafnir/sql"
PATH="/opt/fafnir/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ENV
sudo chgrp fafnir /etc/fafnir/fafnir.env
sudo chmod 640 /etc/fafnir/fafnir.env
```

Why each line matters:

- **`FAFNIR_SQL_DIR`** — the migrator locates `sql/` by walking up from the package or
  the cwd. Setting it explicitly makes `fafnir db migrate` work from any directory.
- **`PATH`** — cron gives you `/usr/bin:/bin` only, so the venv must be prepended or
  `fafnir: command not found` is all you get.
- Quoting the DSN is required: the cron/`systemd` recipes source this file with
  `set -a; . /etc/fafnir/fafnir.env`, and an unquoted DSN containing spaces breaks.

> ### Password gotcha: `FAFNIR_DB_PASSWORD` is ignored when `FAFNIR_DSN` is set
> `FAFNIR_DSN` is used **verbatim**. The password is only merged in when the DSN is
> assembled from the `[database]` parts in `~/.fafnirrc`. So if you use TCP + password
> auth instead of §3.5, pick one of these — the third combination fails with
> `fe_sendauth: no password supplied`:
>
> | Pattern | Works |
> |---|---|
> | `FAFNIR_DSN="... user=fafnir_ingest"` + `PGPASSWORD=...` (libpq reads it) | ✅ |
> | No `FAFNIR_DSN`; `[database]` parts in `~/.fafnirrc` + `FAFNIR_DB_PASSWORD=...` | ✅ |
> | Embed `password=...` directly in `FAFNIR_DSN`, or use a `~/.pgpass` (`0600`) | ✅ |
> | `FAFNIR_DSN` without a password + `FAFNIR_DB_PASSWORD=...` | ❌ |

---

## 5. Create the schema

### 5.1 The privileges this needs (none beyond §3.4)

The whole schema is created by `fafnir_ingest` as an ordinary, non-superuser role —
that is the point of pre-creating the roles and giving it the database in §3.4:

- It **must** own the objects. Ownership is what lets the nightly
  `fafnir db ensure-horizon` attach new yearly partitions to `core.daily_price`, so
  running migrations as `postgres` would break maintenance later.
- Two statements in migration `0001` would otherwise want more privilege, and both are
  handled inside the migration. `CREATE ROLE` is skipped because §3.4 already created
  the roles (if they are missing, the migration stops with an error telling you the
  exact SQL to run). The three `COMMENT ON ROLE` statements — catalog documentation
  only — are best-effort: PostgreSQL requires superuser for those, so they are skipped
  with a `NOTICE` instead of failing the install.

If you want the role comments in the catalog, apply them any time as a superuser; see
the block at the top of
[`sql/migrations/0001_schemas_and_roles.up.sql`](../sql/migrations/0001_schemas_and_roles.up.sql).

### 5.2 Migrate, seed, set the horizon

```bash
sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; cd /opt/fafnir; fafnir db migrate'
```

Expected: `Applied: 0001, 0002, 0003, 0004, 0005, 0006, 0007, 0008`. Then the seeds and
the partition/calendar horizon:

```bash
sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; cd /opt/fafnir
  fafnir db seed            # exchanges + trading calendar from calendar_start_year
  fafnir db ensure-horizon  # yearly partitions, floor = calendar_start_year
  fafnir db status'
```

With `calendar_start_year = 1990` both steps take about a second and report roughly:

```
Seeded trading calendar 1990-2027 for 5 exchanges (9572 open days each)
Horizon ensured through 2028: 29 partition(s), 1255 calendar rows
```

The horizon year is `current year + horizon_extra_years` (2), so it moves with the
calendar. The partition count is how many were **newly created**: migration `0005`
pre-creates 2018–2027, so a 1990 install adds 1990–2017 plus the horizon year.

Keep the `-H`: it forces `HOME=/var/lib/fafnir` so the CLI finds `~/.fafnirrc`. Some
`sudoers` policies preserve the *calling* user's `HOME`, and then the CLI silently
falls back to built-in defaults — including `calendar_start_year = 2015`, which is
almost certainly not what you want. (systemd `User=` and cron both set `HOME` from
`/etc/passwd`, so §8's units need no equivalent.)

### 5.3 Checkpoint

```bash
sudo -u postgres psql -d fafnir -tAc "SELECT count(*) FROM pg_inherits
  JOIN pg_class p ON p.oid=inhparent WHERE p.relname='daily_price';"
sudo -u postgres psql -d fafnir -tAc "SELECT min(trade_date), max(trade_date), count(*)
  FROM ref.trading_calendar;"
sudo -u postgres psql -d fafnir -tAc "SELECT pg_size_pretty(pg_database_size('fafnir'));"
sudo -u postgres psql -d fafnir -c "SELECT version, name, applied_at FROM meta.schema_migration ORDER BY version;"
```

Expected on a 1990 install, with the rolling horizon at 2028: **40** partitions
(1990–2028 = 39 years, plus the `DEFAULT` catch-all), calendar
`1990-01-02 → 2028-12-29` (49,115 rows = 9,823 open days × 5 exchanges), database size
**~16 MB**, and migrations `0001`–`0008` applied.

Now confirm the least-privilege model actually holds — this is the check that catches
a migration run by the wrong role:

```bash
for role in fafnir_read fafnir_app; do
  for tbl in ref.exchange mart.security_latest mart.v_daily_price_adjusted core.daily_price; do
    printf '%-12s %-30s ' "$role" "$tbl"
    sudo -u postgres env PGOPTIONS="-c role=$role" \
      psql -d fafnir -tAc "SELECT count(*) FROM $tbl;" 2>&1 | head -1
  done
done
```

```
fafnir_read  ref.exchange                   5
fafnir_read  mart.security_latest           0
fafnir_read  mart.v_daily_price_adjusted    0
fafnir_read  core.daily_price               0
fafnir_app   ref.exchange                   5
fafnir_app   mart.security_latest           0
fafnir_app   mart.v_daily_price_adjusted    0
fafnir_app   core.daily_price               ERROR:  permission denied for schema core
```

`fafnir_read` must read all four. `fafnir_app` must read `ref` and `mart` but be
**denied** on `core.daily_price` (`permission denied for schema core`) — that is
correct, not a bug.

---

## 6. Smoke test against FMP

Validate the API key and the whole pipeline on a tiny slice before the real load.
Open a shell that carries the environment, and stay in it for §6–§7:

```bash
sudo -u fafnir -H bash
set -a; . /etc/fafnir/fafnir.env; set +a
cd /opt/fafnir

fafnir ingest securities --limit 50
fafnir ingest prices  --symbols AAPL,MSFT --from 1990-01-01
fafnir ingest actions --symbols AAPL,MSFT
fafnir adjust --symbol AAPL
fafnir db refresh-marts
fafnir status
duk -S db ph AAPL --adj --close -n 5
```

`fafnir status` should show ~50 securities and a non-zero price-row count whose latest
date is the last trading day. If splits/dividends came back as zero, check the FMP
field mapping before committing to a full run — see
[ingestion.md](ingestion.md).

`duk` here picks up `FAFNIR_DSN` from the env file, so it reads as `fafnir_ingest`.
That is fine for a smoke test; §11 sets up the least-privilege `fafnir_app` path that
day-to-day reading should use.

Bandwidth so far (against the 50 GB/month FMP budget):

```bash
psql "$FAFNIR_DSN" -c "SELECT endpoint, status, rows_inserted,
  pg_size_pretty(bytes_downloaded) FROM ops.ingestion_run ORDER BY started_at DESC LIMIT 10;"
```

---

## 7. Full backfill

Run it detached — a dropped SSH session must not kill a multi-hour load. Per-symbol
watermarks make it resumable, so re-running the same command after an interruption
picks up where it stopped.

```bash
tmux new -s backfill
set -a; . /etc/fafnir/fafnir.env; set +a
cd /opt/fafnir
scripts/initial_backfill.sh 1990-01-01 2>&1 | tee -a /var/log/fafnir/backfill.log
# detach: Ctrl-b then d      reattach: tmux attach -t backfill
```

Budget ~3–5 hours and ~4–6 GB of FMP traffic for a 1990 start. Watch it from another
session:

```bash
sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; fafnir status'
df -h /var/lib/postgresql                    # or /mnt/fafnir-data
psql "$FAFNIR_DSN" -tAc "SELECT pg_size_pretty(sum(bytes_downloaded)) FROM ops.ingestion_run
  WHERE started_at >= date_trunc('month', now());"
```

Depth, chunking strategy, and the bandwidth math are covered in
[backfill.md](backfill.md#6-full-backfill--a-single-run-of-a-few-hours). Verify with
`fafnir dq run` and the queries in [backfill.md §7](backfill.md#7-verify) when it finishes.

---

## 8. Schedule the nightly update

The server clock is UTC (§2.1) but the data settles on **US market time**, which moves
with DST. A systemd timer handles that correctly; fixed-UTC cron does not.

### 8.1 systemd timer (recommended)

```bash
sudo tee /etc/systemd/system/fafnir-daily.service > /dev/null <<'UNIT'
[Unit]
Description=fafnir daily incremental update
After=postgresql.service
Wants=postgresql.service

[Service]
Type=oneshot
User=fafnir
Group=fafnir
WorkingDirectory=/opt/fafnir
EnvironmentFile=/etc/fafnir/fafnir.env
ExecStart=/opt/fafnir/scripts/daily_update.sh
StandardOutput=append:/var/log/fafnir/daily.log
StandardError=append:/var/log/fafnir/daily.log
TimeoutStartSec=6h
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
UNIT

sudo tee /etc/systemd/system/fafnir-daily.timer > /dev/null <<'UNIT'
[Unit]
Description=Run the fafnir daily update after US settlement

[Timer]
OnCalendar=Mon..Fri 22:30 America/New_York
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
UNIT

sudo systemd-analyze verify /etc/systemd/system/fafnir-daily.{service,timer}
sudo systemctl daemon-reload
sudo systemctl enable --now fafnir-daily.timer
systemctl list-timers fafnir-daily.timer
```

`OnCalendar` accepts a timezone, so 22:30 ET stays 22:30 ET across DST changes.
`Persistent=true` runs a missed occurrence (server rebooting, say) once the machine
is back. Confirm the interpretation without waiting:

```bash
systemd-analyze calendar "Mon..Fri 22:30 America/New_York"
# Normalized form: Mon..Fri *-*-* 22:30:00 America/New_York
# Next elapse: ... 02:30:00 UTC     <-- note the +1 day in UTC
```

Test the unit immediately rather than waiting for the timer:

```bash
sudo systemctl start fafnir-daily.service
sudo systemctl status fafnir-daily.service
tail -40 /var/log/fafnir/daily.log
```

Add the DQ sweep and weekly reconciliation from
[`etc/crontab.example`](../etc/crontab.example) the same way if you want them as
separate units.

### 8.2 cron alternative

If you prefer cron, install it for the `fafnir` user (`sudo apt-get install -y cron`,
then `sudo crontab -u fafnir -e`) and use `etc/crontab.example` as the starting point.
Because the host runs UTC, convert the times — and note that the **weekday shifts**,
since a US evening slot is the next day in UTC:

| Intent (US Eastern) | UTC in EDT (Mar–Nov) | UTC in EST (Nov–Mar) |
|---|---|---|
| Mon–Fri 22:30 daily update | `30 2 * * 2-6` | `30 3 * * 2-6` |
| Mon–Fri 23:00 DQ sweep | `0 3 * * 2-6` | `0 4 * * 2-6` |

Fixed-UTC cron drifts by an hour twice a year. That is harmless here — the loaders are
incremental and idempotent, and a missed day is caught up on the next run — but the
systemd timer avoids the question. Some cron builds honour `CRON_TZ=America/New_York`
at the top of the crontab; verify with `man 5 crontab` on your host before relying on it.

---

## 9. Backups

Three independent layers. The whole warehouse is rebuildable from `landing` + the
sources, but rebuilding costs hours and FMP bandwidth — dumps are cheaper.

### 9.1 Nightly logical dump

```bash
sudo install -d -o fafnir -g fafnir -m 0750 /var/backups/fafnir
sudo tee /usr/local/bin/fafnir-dump > /dev/null <<'EOF'
#!/usr/bin/env bash
# Whole-database custom-format dump, 14-day retention.
set -euo pipefail
OUT="/var/backups/fafnir/fafnir_$(date -u +%F).dump"
pg_dump -h /var/run/postgresql -U fafnir_ingest -d fafnir -Fc -Z6 -f "$OUT"
find /var/backups/fafnir -name 'fafnir_*.dump' -mtime +14 -delete
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
EOF
sudo chmod 755 /usr/local/bin/fafnir-dump
sudo -u fafnir /usr/local/bin/fafnir-dump      # test it now
```

Dump **all** schemas, not just `core`/`landing`. It is tempting to skip `mart` because
it is derived and `meta` because it is only bookkeeping, but a restore without them is
not a working warehouse: `meta.schema_migration` is what stops `fafnir db migrate` from
re-applying every migration, and without the `mart` views there is nothing for
`fafnir db refresh-marts` to refresh. The extra bytes are negligible — the derived
`mart` is a screening snapshot, not a copy of the price history.

Role definitions live outside a database dump. Recreate them with §3.4 on a new host,
or capture them with `sudo -u postgres pg_dumpall --globals-only > globals.sql`.

Schedule the dump with a second timer (`OnCalendar=Mon..Sat 04:00 America/New_York`,
after the nightly load) using the same pattern as §8.1.

### 9.2 Off-server copy

A dump on the same disk does not survive losing the server. Push it to a Hetzner
**Storage Box** (SFTP/rsync/BorgBackup) or **Object Storage** (S3-compatible):

```bash
# Storage Box, key-based, from the fafnir user's crontab/timer:
rsync -a --delete /var/backups/fafnir/ \
  u123456@u123456.your-storagebox.de:/home/fafnir-backups/
```

### 9.3 Hetzner server backups / snapshots

Enable **Backups** on the server (§1.2) for whole-machine recovery, and take a
**Snapshot** before risky changes (major version upgrades, `fafnir db rollback`).
These are crash-consistent block images of the running server, *not* logical database
backups — keep §9.1 as well. Snapshots are billed per GB of used disk.

### 9.4 Practise the restore

An untested backup is a hope, not a backup:

```bash
sudo -u postgres createdb -O fafnir_ingest fafnir_restore_test
sudo -u fafnir pg_restore -h /var/run/postgresql -U fafnir_ingest \
  -d fafnir_restore_test /var/backups/fafnir/fafnir_$(date -u +%F).dump

for q in "SELECT count(*) FROM meta.schema_migration" \
         "SELECT count(*) FROM core.daily_price" \
         "SELECT count(*) FROM ref.trading_calendar" \
         "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent
            WHERE p.relname='daily_price'"; do
  sudo -u postgres psql -d fafnir_restore_test -tAc "$q;"
done

sudo -u postgres dropdb fafnir_restore_test
```

Expect the migration count (8), your price-row count, the calendar rows, and the
partition count to match the live database.

> Restoring as `fafnir_ingest` ends with `errors ignored on restore: 2` — both are the
> `pg_stat_statements` extension, which only a superuser may create. Harmless for the
> data; if you are rebuilding a real host, re-create it afterwards as in §3.4.

---

## 10. Monitoring

```bash
# Health, from the service user's environment
sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; fafnir status'
sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; cd /opt/fafnir && scripts/run_dq_checks.sh'

# The two Hetzner-specific things to watch
df -h /var/lib/postgresql /mnt/fafnir-data 2>/dev/null   # disk: an out-of-space Postgres stops writing
systemctl list-timers 'fafnir-*'                          # did last night actually run?
journalctl -u fafnir-daily.service --since '2 days ago'
```

What to watch, and the SQL for each, is in
[operations.md](operations.md#monitoring): freshness, quarantine spikes, FMP bandwidth,
failed runs. `pg_stat_statements` (enabled in §3.3) gives you the slow-query view:

```sql
SELECT calls, round(mean_exec_time) AS avg_ms, round(total_exec_time) AS total_ms,
       left(query, 90) AS query
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;
```

Add log rotation so `/var/log/fafnir` cannot fill the disk:

```bash
sudo tee /etc/logrotate.d/fafnir > /dev/null <<'EOF'
/var/log/fafnir/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 0640 fafnir adm
}
EOF
sudo logrotate --debug /etc/logrotate.d/fafnir     # dry run
```

Hetzner also shows CPU / traffic / disk graphs per server in the console, and can send
you alerts on them.

> **Email alerts:** Hetzner restricts outbound SMTP (ports 25/465/587) on new cloud
> accounts by default, so `MAILTO=` in a crontab may silently go nowhere. Either
> request the unblock through a support ticket, or use an API-based notification
> service over HTTPS.

---

## 11. Reading the warehouse from your laptop

Postgres is not exposed (§3.6), so tunnel it over the SSH port you already allow:

```bash
# On your laptop -- forwards local port 15432 to the server's 127.0.0.1:5432 listener.
ssh -N -L 15432:127.0.0.1:5432 deploy@<SERVER_IP>
```

In a second local shell, point `duk` at the tunnel using the **read-only** `fafnir_app`
role:

```bash
export FAFNIR_DSN="host=127.0.0.1 port=15432 dbname=fafnir user=fafnir_app"
export PGPASSWORD='REPLACE_app'          # or a ~/.pgpass entry (chmod 600)

duk -S db ph AAPL --adj --close -n 10
duk -S db ls --sector Technology -n 10
```

Or put it in `~/.dukrc` on the laptop — see [duk.md](duk.md#configuration). Note that
`duk` takes a **full DSN only** (`FAFNIR_DSN`, or `[database].dsn` in `~/.dukrc`); it
does not assemble one from the `host`/`port`/`dbname`/`user` parts that `~/.fafnirrc`
accepts:

```toml
[database]
dsn = "host=127.0.0.1 port=15432 dbname=fafnir user=fafnir_app"

[general]
default_source = "db"
```

A `~/.pgpass` line then keeps the password out of your shell history and environment:

```
127.0.0.1:15432:fafnir:fafnir_app:REPLACE_app
```

**Other options:**

- **Apps on another Hetzner server:** put both servers in a Cloud **Network**
  (private, free), add the private subnet to `listen_addresses` and a
  `hostssl fafnir fafnir_app 10.0.0.0/16 scram-sha-256` line to `pg_hba.conf`. Traffic
  stays off the public internet; the Cloud Firewall still needn't open 5432 publicly.
- **Direct TLS from the internet:** only with the Cloud Firewall restricted to specific
  source IPs, a real certificate (not the packaged snakeoil one), `hostssl` in
  `pg_hba.conf`, and clients using `sslmode=verify-full`. The SSH tunnel is less work
  and fewer footguns.

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `db migrate`: *the fafnir roles do not exist and the current user lacks CREATEROLE* | Migration `0001` cannot create the roles it grants to | Run the `CREATE ROLE` statements from §3.4 as a superuser, then re-run `db migrate` |
| `db migrate`: `permission denied for database fafnir` | The database is owned by `postgres`, not the migrating role | `sudo -u postgres psql -c "ALTER DATABASE fafnir OWNER TO fafnir_ingest;"` (§3.4) |
| `db seed`: foreign-key error, `Key (exchange_code)=(\x4e4153444151) is not present` | Cluster was created `SQL_ASCII` because the locale was unset (§3.2) | `SHOW server_encoding` — if not `UTF8`, drop and recreate the cluster with `--locale=C.UTF-8 --encoding=UTF8` |
| `error: externally-managed-environment` from pip | Ubuntu 24.04 system Python is PEP 668 managed | Install into `/opt/fafnir/.venv` (§4.2) |
| `fafnir: command not found` in cron/systemd | venv not on `PATH` | Set `PATH=` in `/etc/fafnir/fafnir.env` (§4.4) |
| `fe_sendauth: no password supplied` | `FAFNIR_DB_PASSWORD` set alongside `FAFNIR_DSN` | Use `PGPASSWORD`, `~/.pgpass`, or peer auth — see the table in §4.4 |
| `Peer authentication failed for user "fafnir_ingest"` | The `pg_hba` peer rule landed *below* `local all all peer`, or the ident map name is wrong | §3.5 — reorder so the `fafnir_ingest` rule comes first, then reload |
| `Could not locate sql/migrations` | Running outside the checkout | Export `FAFNIR_SQL_DIR=/opt/fafnir/sql` |
| Calendar/partitions start at 2015, not your backfill year | `~/.fafnirrc` not found (missing `-H`/`HOME`), or seeded before setting `calendar_start_year` | §5.2; then re-run `fafnir db seed` and `fafnir db ensure-horizon` |
| `permission denied for schema core` from `duk` | `fafnir_app` reads `mart` + `ref` only | Correct by design — use `mart.*` views, or connect as `fafnir_read` |
| Nightly job never ran | Timer not enabled, or clock/timezone confusion | `systemctl list-timers 'fafnir-*'`; `systemd-analyze calendar '<expr>'` |
| Postgres won't start after a config edit | Bad value in the drop-in | `journalctl -u postgresql@16-main -n 50`; `sudo -u postgres psql -c "SELECT * FROM pg_file_settings WHERE error IS NOT NULL;"` |
| Server stuck at boot after adding a Volume | `fstab` entry without `nofail` | Console → **Rescue**, fix `/etc/fstab` (§3.7) |
| `No space left on device`; Postgres read-only | Disk full (usually WAL + a dump on the same disk) | Delete old dumps, then grow: resize the Volume, or resize the server (irreversible) |
| Locked out by SSH/ufw changes | Firewall or `sshd_config` mistake | Cloud Console → **Console** (VNC) or **Rescue** system |

Rebuilding from scratch: `fafnir db rollback --steps N` unwinds migrations, but never
roll back past `core.daily_price` without a current dump (§9). To start completely
over, `sudo -u postgres dropdb fafnir` and return to §3.4 — you keep the OS, Postgres,
and venv work.

---

## 13. Post-install checklist

```bash
# --- Host -------------------------------------------------------------------
timedatectl | grep 'Time zone'                       # UTC
sudo ufw status | head -3                            # active, 22/tcp only
free -h | grep -i swap                               # 2 GB
ssh -o BatchMode=yes root@<SERVER_IP> true           # must FAIL (root login disabled)

# --- PostgreSQL -------------------------------------------------------------
sudo -u postgres psql -tAc "SHOW data_checksums;"                     # on
sudo -u postgres psql -tAc "SHOW server_encoding;"                    # UTF8
sudo ss -lntp | grep 5432                                             # 127.0.0.1 only
sudo -u postgres psql -tAc "SELECT count(*) FROM pg_file_settings WHERE error IS NOT NULL;"   # 0
sudo -u postgres psql -tAc "SELECT rolsuper, rolcreaterole FROM pg_roles
  WHERE rolname='fafnir_ingest';"                                     # f|f -- least privilege

# --- fafnir -----------------------------------------------------------------
sudo -u fafnir -H bash -c 'set -a; . /etc/fafnir/fafnir.env; set +a; cd /opt/fafnir
  fafnir db status        # 0001..0008 all "applied", no DRIFT
  fafnir status           # securities > 0, latest date = last trading day
  fafnir dq run'          # flag counts you can explain

# --- Automation & backups ---------------------------------------------------
systemctl list-timers 'fafnir-*'                     # next elapse looks right
ls -lh /var/backups/fafnir/                          # a dump exists

# --- From your laptop, with the §11 tunnel up -------------------------------
duk -S db ph SPY --adj -n 5                          # reads as fafnir_app
```

Steady-state operation, reconciliation, and recovery from here on:
**[operations.md](operations.md)**.
