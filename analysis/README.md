# analysis/

Local run outputs: one folder per run, `analysis/<task>-<date>/`, with a short report, a manifest (inputs, code hashes, cohort, settings, cost) and machine-readable tables. Everything here except this file and `documentation_inventory.csv` is gitignored.

- Docs do not link here. Results worth sharing are copied into `evaluation/<step>/` (data and results) and summarised in that step's README.
- Before deleting a folder, check that no script, test or doc references it by name.
- `docs-history-2026-11-12/`: full copies of the docs and plan before the 12 November consolidation.
- `evaluation-archive-2026-11-12/`: working files removed from `evaluation/` during the same cleanup.
- `nutrition-repairs-2026-11-12/`: 10-11 November baseline repairs with rollback backups (summary in `evaluation/nutrition/REPAIRS.md`).
- `documentation_inventory.csv`: maps pre-30-October document paths to their maintained replacements.
