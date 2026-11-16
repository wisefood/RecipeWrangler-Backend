# Runtime and operations

Updated 30 October 2026. [The working plan](PLAN.md) is the continuation point. This guide consolidates setup, store ownership, activation and rollback; it is not an instruction to run a deployment during planning.

## Local development

```bash
uv sync --dev
make api
```

The API defaults to port 8001; `make api PORT=8002` overrides it. Copy `.env.example` to a local `.env` and supply the required service/model settings. Never embed credentials in documentation, images or generated reports. Provider/model names in historical reports are not necessarily the current configured runtime.

Backing-service compose files are in `neo4j-docker/`, `postgresql-docker/` and `elasticsearch-docker/`. Check actual filenames and existing running services before starting duplicates. API/OpenAPI is served from `recipe_wrangler.api.main:app`. Relevant API routes are summarized in [the component README](../src/recipe_wrangler/api/README.md); use the running OpenAPI schema for exact deployed contracts.

Run focused tests for changed behavior with `uv run pytest <modules> -q`; run the application suite when a release candidate is ready. The focused counts preserved in topic guides were reported by earlier runs, not rerun during documentation planning.

## Store ownership and synchronization

| Store | Authoritative responsibility |
|---|---|
| Neo4j | Recipe text/metadata, ingredients, semantic food/allergen/consumer evidence and discovery relationships |
| PostgreSQL | Composition/price records, profile traces, versioned matching runs, active pointers and projection queue |
| Elasticsearch | Projected searchable recipe catalogue and ingredient retrieval indexes |
| Local generated assets | Pinned parser/model/release bundles, input snapshots and audit evidence; not source modules |

Saved new-mechanism profiles activate and enqueue synchronization work in their PostgreSQL transaction. The worker retries graph, claim, catalogue and cache projection. Recipe-revision reconciliation finds supported imports/edits; an imported recipe with no calculated profile is `awaiting_profile`. Arbitrary manual DB changes bypassing save hooks require explicit queue/revision handling.

A successful PostgreSQL write alone does not establish fresh search/allergen data. Check active run IDs, graph occurrences, catalogue documents, projection failures and caches. Recipe soft-disable must be honored by every public read/search/recommendation path; privileged console access is distinct. Keep user-facing writes on established save hooks rather than add parallel materialization logic.

## Recorded 29 October matching deployment

The saved deployment uses release `2026-10-27-v11`, database mode `new`, API mechanism `occurrence_matching_v13_quality_fixes`, and catalogue alias `recipes -> recipes_matching_20261029`. The prior `recipes_v5`, legacy matching code, immutable matching runs and predecessor serving rows remain for rollback. The saved audit records 7,743 recipes, 30,972 regional profiles and 309,536 current graph occurrences.

The API container is `recipe-wrangler-api`, port 8001, restart policy `unless-stopped`. Pinned model/parser assets were packaged for offline startup; `.env` is mounted, not built into the image. Generated-image storage is the only public data mount in that recorded deployment.

Deployment commands preserved from the activation run:

```bash
make build IMGTAG=wisefood/recipe-wrangler:matching-2026-10-29
docker compose --env-file .env -f recipe-api-docker/docker-compose.yml up -d
curl -fsS http://127.0.0.1:8001/health/matching
```

Before rerunning, verify the current release, Docker configuration, build constraints, pinned-model cache and database access. The recorded build uses the released PostgreSQL assets and cached model rather than sending operational data/secrets into the build context.

Saved validation includes 125 focused regressions, offline model selection checks, a served recipe GET, an incremental temporary-recipe projection check followed by cleanup, and a full projection audit. Reference-only rows retained fingerprint `b413545a71a4db8a910e50845c8e4262`. These checks do not establish a full-suite pass, current database state, high nutrient completeness, or adaptation readiness.

Evidence is under `data/analysis/matching_production_2026-10-29/`: `activation.json`, `final_verification.json`, `incremental_smoke.json`, `rollback_rehearsal.json`, and related deployment/preflight records. New reports go to `analysis/<run-id>/`; do not relocate these historical paths while scripts and manifests still depend on them.

## Matching release 2 (4 November 2026, in progress)

**State 4 Nov 18:00 UTC:** v31 image deployed; promotion `matching-release-2-2026-11` active (30,972 profiles, 15:07 UTC; reports in
`analysis/matching-release-2/`: prepare/rehearsal/promote JSON). Projection worker draining 7,743 recipes to Neo4j/ES/cache (~12/min,
done ~02:00 UTC 5 Nov). Rollback: `promote_matching_production rollback --promotion-id matching-release-2-2026-11` (restores rows,
re-queues projection). Next steps: see the current-state paragraph in [PLAN.md](PLAN.md).

API code: image `wisefood/recipe-wrangler:matching-2026-11-04-v30` deployed 4 Nov (user-run compose update); v31
(`matching-2026-11-04-v31`) built and pending deployment. Each image is the 29 Oct image (pinned by digest
`sha256:ef7228…`) plus ten matching source files copied into `site-packages` and `/app/src`; no package operations.
Overlay equality was verified against the workspace (711 ingredient resolutions, 40 recipe calculations, identical).
Rollback of the code: set the compose image back to `wisefood/recipe-wrangler:matching-2026-10-29` and `up -d`.
The compose file before the change is saved in `backups/pre_v30_backfill_20261104/`.

Profiles: v31 backfill writes inactive runs (`scripts/composition/backfill_matching_profiles.py --shard i --shards 4`,
2 CPUs / 8 GB each, watchdog on host memory and live search latency). Served profiles change only through
`promote_matching_production.py prepare --mechanism-version … / promote [--rehearse]`, which now allows a second
promotion within the same release, backs up every replaced row, and queues projection for every affected recipe in the
same transaction; `rollback` restores the backups and queues projection again. Pre-backfill dumps:
`backups/pre_v30_backfill_20261104/` (profiles table 32,680 rows verified, active pointers, deployment, promotions).

## Rollback procedure preserved from activation

Promotion ID: `matching-production-2026-10-29`. Rollback SQL was rehearsed on four regional profiles within an outer transaction; restored rows matched and that rehearsal committed no changes.

Stop writes/API before rollback. The command refuses if active pointers have changed since promotion. Reconcile subsequent imports/saves rather than overwrite them.

```bash
docker compose --env-file .env -f recipe-api-docker/docker-compose.yml stop
PYTHONPATH=src uv run python -m scripts.composition.promote_matching_production rollback \
  --promotion-id matching-production-2026-10-29 \
  --report data/analysis/matching_production_2026-10-29/rollback.json
```

Then restore matching mode/release from `previous_matching_environment.json`, restore the `recipes` alias to `recipes_v5`, restore saved nutrition-claim edges, mark rolled-back matching occurrences noncurrent, invalidate recipe caches, and restart the API. Backups are in PostgreSQL `matching_production_backups`; prior claim/settings evidence is in the same activation directory. This is a multi-store procedure; the SQL command alone is not the complete rollback.

## Imports, snapshots and figures

Supported commands and side effects are in [scripts/README.md](../scripts/README.md). Prefer a read-only snapshot and inspect flags before executing a script; a name containing `audit` or `verify` is not proof that it has no writes or provider calls. Historical scripts can use outdated corpus assumptions or fixed output paths.

Native backup/restore tooling lives in `scripts/maintenance/`; restores and purges can destroy shared state. Keep source/reference records separate from generated profiles, maintain a snapshot/release manifest, and inspect both input and output hashes for a proposed data refresh.


## Documentation maintenance

Update the relevant topic guide for changed behavior and [PLAN.md](PLAN.md) for progress, decisions and next action. Run outputs go to local `analysis/<task>-<date>/`; results worth sharing go to `evaluation/<step>/`. Do not add dated handoff files or link docs into `analysis/`.

## Shared-server resource safeguards

The user's latest revision on 31 October replaces universal small RAM caps with a **10 GiB host MemAvailable headroom target**. Choose an aggregate job cgroup ceiling from fresh available memory minus that reserve, monitor availability/pressure and shrink or stop our own job if other workloads consume the headroom. A starting ceiling does not reserve RAM against other users. Avoid low virtual-address-space caps when physical job memory is enforced. Use GPU where the task benefits, subject to the existing task-specific VRAM/compute sharing controls; CPU-only work disables visibility. Routine checks remain one job, up to two CPU cores/threads, 900 CPU seconds and a thirty-minute wall ceiling; bounded database diagnostics use sixty-second timeouts. Resource measurements and policy revisions retain the earlier eight-GiB smoke as historical evidence. Use monitored smoke tests, check available resources before substantial work, and stop your own job if it causes pressure. CPU affinity does not constrain database/search-server compute; also bound query time and batch size. Do not increase limits automatically, stop unrelated services, or change packages/drivers/CUDA/kernel without the required explicit approval. See the corpus run for measured usage and its bounded-command wrapper.

## Pending rollouts

Prepared locally, not applied to stored data or deployed. Details and gates are in the local history (`analysis/docs-history-2026-11-12/docs/OPERATIONS.md`).

- Pending local allergen-contract rollout
- Pending source-join validation
- Pending filter/planner rollout
- Pending recipe allergen edit contract
- Pending allergen classification v2 — 31 October 2026
- Pending FoodOn link policy v2 — 31 October 2026
- Read-only reconciliation and propagation prerequisites — 31 October 2026
- Verified prerequisites and cache retry — local, 31 October 2026
- Prepared composition/reconciliation rollout — 1 November 2026
- Source-complete nutrition reprofile package
- Transactional source reprofile primitives
- Read-only source reprofile operator preflight
