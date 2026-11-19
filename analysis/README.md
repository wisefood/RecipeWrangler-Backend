# analysis/

Local run outputs: one folder per run, `analysis/<task>-<date>/`, with a short report, a manifest (inputs, code hashes, cohort, settings, cost) and machine-readable tables. Everything here except this file and `documentation_inventory.csv` is gitignored.

- A few scripts and docs reference folders here by name. Results worth sharing are copied into `evaluation/<step>/` (data and results) and summarised in that step's README.
- Before deleting a folder, check that no script, test or doc references it by name.
- When a run is accepted: copy its summary to `evaluation/` or `docs/`, archive the folder, then delete the scratch. Do not keep superseded, preview or dry-run outputs once the result is in Postgres or Neo4j.
- 19 Nov 2026 cleanup: 103 unreferenced runs from 30 Oct to 17 Nov (about 5 GB) were archived to `backups/archives/analysis_archive_2026-11-19.tar.zst` (963 MB) and removed. Restore one with `zstd -dc backups/archives/analysis_archive_2026-11-19.tar.zst | tar -xf - <folder>`.
- `docs-history-2026-11-12/`: full copies of the docs and plan before the 12 November consolidation.
- `evaluation-archive-2026-11-12/`: working files removed from `evaluation/` during the same cleanup.
- `nutrition-repairs-2026-11-12/`: 10-11 November baseline repairs with rollback backups (summary in `evaluation/nutrition/REPAIRS.md`).
- `documentation_inventory.csv`: maps pre-30-October document paths to their maintained replacements.
