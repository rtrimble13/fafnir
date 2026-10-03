## Summary

<!-- What changes, and why. One paragraph; link the ADR or plan section it implements. -->

Closes #

## Docs

<!-- Documentation lands in the same PR as the behaviour it describes. The docs-gate
     check fails a PR that changes src/, sql/, scripts/ or etc/ and touches no docs;
     if nothing a reader relies on changed, add the `no-docs-needed` label instead.
     Tick what this PR updated; strike through (~~like this~~) what does not apply. -->

- [ ] Data dictionary (`doc/data_dictionary.md`)
- [ ] ADR (new, amended, or a status note in `doc/adr/`)
- [ ] Runbook / operations (`doc/operations.md`, `doc/install_hetzner.md`)
- [ ] Ingestion / backfill (`doc/ingestion.md`, `doc/backfill.md`)
- [ ] duk / agent (`doc/duk.md`, `doc/agent.md`)
- [ ] fafnir-dba skill references (`.claude/skills/fafnir-dba/`)
- [ ] `etc/` templates (`etc/fafnirrc`, `etc/crontab.example`, `etc/systemd/`)
- [ ] Living-plan row (`doc/plans/sharadar-adoption.md`: status and PR number)

## Migrations

<!-- Delete this section if the PR adds none. -->

- [ ] Up and down both tested (`test_rollback_then_remigrate` passes)
- [ ] Least privilege: grants for the new objects; `test_migrations_least_privilege.py` extended

## Production impact

<!-- Pick one. -->

- [ ] None: merging and deploying `main` is enough
- [ ] Operator step required: #<!-- the owner:operator issue that carries it -->
