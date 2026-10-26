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

`fafnir-daily` carries the fundamentals too (#106). In Sharadar mode it loads SF1's
nightly delta after the Sharadar step and refreshes the factor panel with the other
marts, so there is no fundamentals unit. If the panel's refresh passes 15 minutes on
production, it moves to a pair of its own, `fafnir-fundamentals.service.in` and
`.timer.in`, ordered `After=fafnir-daily.service` as `fafnir-dq` is, and
[operations.md](../../doc/operations.md#fundamentals-in-the-night) says why.
`scripts/backfill_fundamentals.sh` is never scheduled: it is a by-hand re-run.

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
