# The nightly report email

A summary of last night's automations and the DQ queue, written by the on-host
agent and emailed each morning. It is the *push* half of
[operations.md §Monitoring](operations.md#monitoring): `scripts/monitor.sh`
answers "how is it?" when you go and ask, and this answers it when you do not.

It changes nothing. It reads what the night left behind, summarises it, and
sends mail.

## Why a systemd timer and not something else

Worth stating, because two easier-looking options do not work here:

- **Claude's own scheduler** (an in-session cron) holds jobs in memory for the
  life of one session. It cannot outlive the terminal it was created in, so it
  cannot own a daily email.
- **A cloud-hosted schedule** has nothing to connect to. The MCP server reaches
  Postgres over the local Unix socket under peer authentication — the OS identity
  of the process *is* the credential
  ([ADR 0008](adr/0008-remote-duk-access-and-mcp.md)). There is no password for
  an off-host agent to present, by design.

The evidence is on this machine, so the scheduler is too, and it looks like its
neighbours in [`etc/systemd/`](../etc/systemd/) rather than inventing a second
way to schedule things.

## The pieces

| Path | What it is |
|---|---|
| [`scripts/collect_facts.sh`](../scripts/collect_facts.sh) | Gathers one night's evidence to stdout. Read-only, deterministic. |
| [`scripts/nightly_report.sh`](../scripts/nightly_report.sh) | Collect → summarise → send. The unit's `ExecStart`. |
| [`etc/agent/fafnir-report.service.example`](../etc/agent/fafnir-report.service.example) | The unit. Runs as the **agent** user, not `fafnir`. |
| [`etc/agent/fafnir-report.timer.example`](../etc/agent/fafnir-report.timer.example) | Tue–Sat 06:15 America/New_York. |
| [`etc/agent/report.env.example`](../etc/agent/report.env.example) | `/etc/fafnir/report.env` — recipient and sender. No secret. |
| [`etc/agent/msmtprc.example`](../etc/agent/msmtprc.example) | The agent's `~/.msmtprc`, mode 600. The one file with a credential. |

These are `.example` files under `etc/agent/`, not `@PLACEHOLDER@` templates
under `etc/systemd/`, because
[`scripts/install_timers.sh`](../scripts/install_timers.sh) installs units that
run as `fafnir`. This one runs as the agent, and its account, home and Claude
credentials are agent deployment facts ([agent.md](agent.md)) rather than
pipeline configuration.

## Where it sits in the night

```
fafnir-daily.timer          Mon–Fri 22:30 ET
fafnir-dq.timer             Mon–Fri 23:00 ET   After=fafnir-daily.service
fafnir-dump.timer           Mon–Sat 04:00 ET
fafnir-backup-offsite.timer Mon–Sat 04:45 ET
fafnir-report.timer         Tue–Sat 06:15 ET   ← this
```

**Tue–Sat**, not Mon–Fri: the jobs it reports on run Mon–Fri *night*, so Friday
night's run is Saturday's email and Monday morning has nothing new to say.
06:15 ET leaves `fafnir-dq` its full two-hour `TimeoutStartSec` and still lands
before the working day.

## Install

Steps 1–4 need root. Substitute your agent account for `claude` throughout.

### 1. A mail transport

```bash
sudo apt-get update && sudo apt-get install -y msmtp msmtp-mta
```

First the state directory, as the agent user. Three things write into it before
`nightly_report.sh` ever runs its own `mkdir`: the unit's `StandardOutput=`, the
unit's `StandardError=`, and msmtp's `logfile`. systemd opens the unit's output
files *before* `ExecStart`, so the script cannot create the directory it is
already being logged into:

```bash
mkdir -p ~/fafnir-report
```

Then the credential, as the agent user:

```bash
cp etc/agent/msmtprc.example ~/.msmtprc
chmod 600 ~/.msmtprc          # msmtp refuses to run on a looser mode
$EDITOR ~/.msmtprc            # host, from, user, password
```

Use an app password or a send-only credential, never the account password.
Prove the hop works before wiring anything to a timer:

```bash
msmtp --debug --from=default -t <<'EOF'
To: you@example.com
Subject: fafnir msmtp test

body
EOF
```

> **Outbound SMTP is not a given.** Hetzner blocks ports 25/465/587 on new cloud
> accounts (see [install_hetzner.md §10](install_hetzner.md)), and a blocked port
> *hangs* rather than refusing — which reads as a hung script, not a firewall.
> Check the port before you debug anything else:
> ```bash
> timeout 8 bash -c 'exec 3<>/dev/tcp/smtp.example.com/587' && echo open
> ```
> If SMTP stays blocked, replace the `msmtp` call at the end of
> `nightly_report.sh` with a `curl` POST to a transactional-email API; nothing
> above that line changes.

### 2. Recipient

```bash
sudo install -m 644 etc/agent/report.env.example /etc/fafnir/report.env
sudo $EDITOR /etc/fafnir/report.env
```

Mode 644 is correct: the file names addresses and holds no secret. Note it is a
*different* file from `/etc/fafnir/fafnir.env`, which the agent is denied
outright ([agent.md](agent.md#what-it-can-and-cannot-do)) and which this does
not need.

It also carries `FAFNIR_DSN=dbname=fafnir user=claude_ops` — the agent's own
read-only role, peer-authenticated, so still no secret. `monitor.sh`'s `runs` and
`bandwidth` checks query Postgres, and their only other source of a DSN is that
denied `fafnir.env`. Without the line they skip themselves, and because a skipped
section neither warns nor fails, the run still summarises as clean. That is the
one failure this report cannot tolerate, so `collect_facts.sh` also states the
gap outright when `FAFNIR_DSN` is unset.

### 3. Journal access for the agent

```bash
sudo usermod -aG adm claude
```

Without this the agent is in no privileged group, `journalctl -u 'fafnir-*'`
returns *"No data available"*, and `/var/log/fafnir` (0750 `fafnir:adm`) is
unreadable. The report still works — it falls back to unit exit status — but it
is blind to log detail, and it will say so in the mail rather than quietly
omitting it.

Note what this deliberately does **not** grant: membership of the `fafnir`
group. `/var/backups/fafnir` is 0750 `fafnir:fafnir` and stays unreadable, which
is why `collect_facts.sh` omits `monitor.sh`'s `backups` section — run as the
agent, it can only ever report a false *"no dump found"*. The dump is covered
honestly by `fafnir-dump.service`'s exit status. A report that cries wolf every
morning is worse than no report, and widening the agent's access to silence one
is the wrong trade.

### 4. The units

```bash
sudo install -m 644 etc/agent/fafnir-report.service.example \
     /etc/systemd/system/fafnir-report.service
sudo install -m 644 etc/agent/fafnir-report.timer.example \
     /etc/systemd/system/fafnir-report.timer
sudo systemctl daemon-reload
sudo systemctl enable --now fafnir-report.timer
systemctl list-timers fafnir-report.timer
```

### 5. Fire it once

```bash
sudo systemctl start fafnir-report.service
cat ~/fafnir-report/report.log
```

## How it is put together

**Collection is deterministic shell; only the summary is the model.**
`collect_facts.sh` gathers a fixed set of probes, and the model is handed the
result. It is not sent to go and look. That keeps the nightly cost flat, makes
two nights comparable, and means a bad report can be reproduced from the
evidence file rather than re-run against a warehouse that has since moved.

**It reports movement, not totals.** `nightly_report.sh` copies the previous
run's evidence aside before collecting tonight's, and feeds both in, fenced.
An open-flag count that is identical every morning is not news; a count that
moved is. The baseline advances **only after a successful send** — a morning
where the mail failed must not also lose the comparison that would have
explained it.

**A failed summariser still sends.** If `claude -p` exits non-zero the script
mails the raw evidence with the stderr attached. Silence is the one failure
mode a monitoring email cannot have, because it is indistinguishable from a
quiet night.

**The model runs with no tools.** `--allowedTools ""` — everything it needs is
already in the prompt, so a headless run cannot stall on a permission prompt at
06:15. If you later want it to chase a flag down, add the read-only fafnir MCP
tools here; do not give it the Bash tool.

`--max-turns` is 3 rather than 1 for the same reason the fallback exists: at 1,
identical input intermittently exits *Reached max turns (1)*, which costs that
morning its summary and mails the raw evidence instead. With no tools enabled
there is no loop for a tight limit to bound, so 1 buys nothing but the flake.

**The prompt carries the warehouse's semantics.** It tells the model that
`price_*` flags repeat per re-detection by design, so their flag counts are not
counts of distinct problems — the same caveat
[operations.md](operations.md#monitoring) makes to a human reader. Without it
the report leads with its largest number, which is the least meaningful one.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No mail, unit succeeded | `msmtp` wrote to `~/fafnir-report/msmtp.log` and failed there | read that log; re-run the step 1 test send |
| Unit hangs until `TimeoutStartSec` | outbound SMTP blocked — a blocked port hangs, it does not refuse | test `/dev/tcp/<host>/587`; unblock, or move to an API transport |
| `msmtp: cannot use configuration file … permissions` | `~/.msmtprc` is not 600 | `chmod 600 ~/.msmtprc` |
| Mail body starts `UNSUMMARISED` | `claude -p` exited non-zero; the raw evidence was sent instead | the stderr is in the mail; usually expired credentials, below |
| …and that stderr reads *Reached max turns* | `--max-turns` too low for that night's evidence | it is 3 in the shipped script; raise it if a long night trips it again |
| *Invalid API key* / *unauthorized* in that stderr | the agent's OAuth token could not refresh | re-authenticate as the agent user (`claude` interactively, once) |
| `HOME` errors, or credentials not found | `Environment=HOME=` missing from the unit | systemd sets no `HOME` for a oneshot; the shipped unit sets it |
| Report says journal evidence is missing | agent not in `adm` | step 3 |
| Report claims no backup dump exists | `monitor.sh backups` was re-added to `collect_facts.sh` | remove it; `/var/backups/fafnir` is unreadable to the agent by design |
| `sudo: no tty present` / `a password is required` | the sudoers rule is absent or the binary path differs | [agent.md](agent.md) step 4 |
| Unit fails instantly, *Failed to open standard output file … No such file or directory* | `~/fafnir-report` does not exist; systemd opens the log before `ExecStart` can create it | `mkdir -p ~/fafnir-report` (step 1) |
| Evidence says `runs` and `bandwidth` did NOT run | `FAFNIR_DSN` missing from `/etc/fafnir/report.env`; the agent cannot read `fafnir.env` to fall back on | add `FAFNIR_DSN=dbname=fafnir user=claude_ops` (step 2) |
| Every morning reads as day one | `last_facts.txt` is not persisting | check `FAFNIR_REPORT_STATE_DIR` is writable by the agent |

## Turning it off

```bash
sudo systemctl disable --now fafnir-report.timer
```

The agent's read access is unaffected, and so is the nightly pipeline — this
unit is downstream of everything and nothing depends on it.
