# Can-Do switch data (temporary)

The per-environment inputs of the merged Can-Do switch's nightly progress pass ([pangeachat/.github#832](https://github.com/pangeachat/.github/issues/832)): `can-do-map.json` (old Can-Do row id → merged row id) and `can-do-statements.json` (Can-Do row id → statement), as described in `scripts/migrate_course_can_dos.py`. Row ids differ between environments, so each environment has its own folder. `prod/` holds the files the production switch used (2026-11-18). They hold Can-Do ids and statements only, never a room, learner or teacher note.

**This folder is deleted** with the migration endpoints (`synapse_pangea_chat/migrate_course_can_dos/`), the operator script, `scripts/nightly_can_do_progress.py`, its test and `.github/workflows/nightly-can-do-progress.yml`, after the last progress pass (the runbook skill, 2-step-choreographer `migrate-plans-to-merged-can-dos`). Until then the map stays exactly as the switch used it.
