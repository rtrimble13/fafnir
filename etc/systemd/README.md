# systemd unit templates

Templates for the scheduled jobs described in
[`doc/install_hetzner.md`](../../doc/install_hetzner.md) §8–§9. They are
*templates*, not installable units: the `@PLACEHOLDER@` tokens are substituted by
[`scripts/install_timers.sh`](../../scripts/install_timers.sh), which then runs
`systemd-analyze verify` on the result before it lands in
`/etc/systemd/system`.

| Unit | Default schedule (US Eastern) | Payload |
|---|---|---|
| `fafnir-daily` | Mon–Fri 22:30 | `scripts/daily_update.sh` |
| `fafnir-dq` | Mon–Fri 23:00 | `scripts/run_dq_checks.sh` |
| `fafnir-reconcile` | Sun 06:00 | `scripts/reconcile.sh` |
| `fafnir-dump` | Mon–Sat 04:00 | `scripts/backup_dump.sh` |
| `fafnir-backup-offsite` | Mon–Sat 04:45 | `scripts/backup_offsite.sh` |

`OnCalendar` carries an explicit `America/New_York`, so the slots stay on market
time across DST while the host clock stays UTC. That is the whole reason to
prefer these over the fixed-UTC `crontab.example`.

To edit a schedule after installation, prefer a drop-in over hand-editing the
generated unit (the next `install_timers.sh` run overwrites it):

```bash
sudo systemctl edit fafnir-daily.timer
# [Timer]
# OnCalendar=
# OnCalendar=Mon..Fri 23:15 America/New_York
```

The empty `OnCalendar=` first is required: timer settings are additive, so
without it you get *both* slots.
