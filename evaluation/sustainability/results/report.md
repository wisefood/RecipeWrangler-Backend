# Sustainability reference data: reports

Combined on 13 Nov 2026 from four separate reports (release, merge, acquisition, embeddings).

## Sustainability reference database update — 3 November 2026

**Database import and reference-alias activation completed. Running API image has not
been redeployed, and stored recipe profiles have not been rewritten.** The workspace
calculator integration is implemented and verified against the active database.

### What was updated

Elasticsearch index `sustainability-references-20261103-v1`, exposed through the atomic
alias `sustainability_references`, contains **13,032 source references**, not 13,032
distinct foods. It is write-blocked after verification. No embeddings are needed.

| Source | References | Use in this release |
|---|---:|---|
| AGRIBALYSE 3.2 food-products / AOUT25 | 2,451 | 1,646 records pass the no-additional-consumer-preparation calculation policy; selection also requires unique food/form matching |
| CONCITO public displayed table 1.2 | 2,700 | Reference only; five market scenarios retained, consequential methodology kept separate |
| SU-EATABLE LIFE, figshare v2 | 517 | Reference only; kg/litre ambiguity and recommended aggregation remain unresolved; carbon and water kept distinct |
| Local legacy CSV snapshot | 7,364 | Reference only; provenance explicitly unverified; does not replace the live legacy collection |

Each reference retains source/version/native ID/market, citation, licence, original
units, method, boundary, source-file SHA-256 and source-specific quality information.
AGRIBALYSE's other indicators remain attached to their original units. Water-resource
deprivation and water footprint are not interchangeable. DQR is not a numerical
confidence interval. See the source investigation (acquisition section below) and
original merge review (merge section below) for citations and methodological differences.

The existing `sustainability_ingredients` collection still has **7,373 documents**.
Its count and a deterministic 20-document garlic sample were unchanged before/after
import and after activation. This is a bounded safety check, not a full live-content
checksum. No import request targeted that collection or existing recipe documents.

### Duplicate, identity and eligibility checks

- SQLite integrity and foreign-key checks passed; all four input-file SHA-256 values
  were rechecked against the catalogue before export.
- Source reference identity is `(source, version, native record ID, market)`, stored
  under a stable hash. Import uses Elasticsearch `create`, never silent overwrite.
  Equal repeats are allowed; conflicting repeated identities stop the import.
- Every one of the **13,032 live documents** was compared with its complete exported
  JSON. Counts match and there are no additional documents. Verification ran again
  before alias activation.
- Two conflicting legacy identity groups remain quarantined in the offline catalogue;
  neither was exported. Five repeated legacy records were already collapsed there.
  Existing live legacy records are preserved, including their historical limitations.
- Seven explicitly superseded AGRIBALYSE packaging-error rows stay excluded; source
  suffix IDs remain intact. The 447 token-reordering synonym groups remain unmerged.
  Different-source estimates remain alternative references, not duplicate copies to sum.
- Unit-ambiguous SEL entries, CONCITO method/market alternatives, and AGRIBALYSE
  records with additional consumer preparation are not eligible for the new calculation.
- Matching uses complete normalized names and a small explicit reviewed name map in
  `repositories/sustainability_references.py`; it does not remove form/product qualifiers
  or use fuzzy/model matching. Bare reviewed vegetable names assume raw form, and this
  is an input assumption rather than a verified preparation declaration. Other synonyms
  and FoodEx2 proxy links do not become automatic matches.

### Calculation policy and shadow comparison

Policy `agribalyse-3.2-no-preparation-exact-v1` is a French-reference consumer-product
estimate per kg consumed product, with no additional consumer preparation in the source
scenario. Input gram weights must represent the corresponding edible product form.
Recipe household cooking, recipe-specific waste/yield and non-French supply chains are
not modelled. Source values are never selected according to which is smaller.

The workspace calculator uses this policy only if **every positive-weight ingredient**
has a unique eligible reference. Otherwise it retains the **entire existing legacy
recipe calculation**, and attaches supported AGRIBALYSE alternatives to ingredient details.
It does not fill holes in an AGRIBALYSE total using legacy, CONCITO or SEL values.
Legacy partial totals now carry explicit partial status; their numerical compatibility
fields are retained. Per-ingredient source/method/boundary/version information survives
the existing profile-detail storage path. Invalid, negative or non-finite weights and
mismatched ingredient/weight lengths are rejected.

One PostgreSQL read-only transaction, with a 60-second statement timeout, fetched the
first 100 EU profiles ordered by recipe ID. They have 100 distinct recipe IDs and
**1,060 ingredient positions**. This is an exposed development sample, not a randomized
representative benchmark or independent environmental ground truth.

| Shadow result | Count |
|---|---:|
| Ingredient positions with a source-pinned AGRIBALYSE match | 120 / 1,060 |
| Recipes completely supported by the new policy | 0 / 100 |
| Recipe totals changed under the policy | 0 / 100 |
| New mixed-source recipe totals | 0 |

These results justify a safe reference-data activation, **not a claim of increased
recipe coverage or improved environmental accuracy**. Full-recipe use remains limited
by conservative matching and the preparation/basis restrictions. Broader food/form review
and source-consistent factor preparation are needed before replacing general recipe scores.

Live conformance tests against the active alias confirmed fresh garlic maps to
**AGRIBALYSE 11000, Garlic, fresh, 0.383 kg CO2 eq/kg**, with citation
<https://doi.org/10.57745/XTENSJ>, release `3.2-food-AOUT25`, source SHA and DQR 2.88.
A 10 g garlic probe returns **0.00383 kg CO2e**. It is a synthetic calculation probe,
not an independently measured recipe evaluation. Garlic powder maps separately to
record **11023**; garlic salt, garlic in oil and cooked garlic cannot use the fresh factor.

### Verification, operational evidence and rollback

- **112 tests passed** across catalogue, release selection, profiling and adaptation
  regression modules: test log (archived locally). No local model or
  paid LLM was used. Existing adaptation factor matching is unchanged and continues to
  use its legacy reference policy; this release does not silently mix new factors into it.
- All phases used one CPU-only private cgroup at a time, two CPU cores/200% quota,
  1 GiB RAM/no swap, 900 CPU seconds and 15-minute wall limits. Host availability,
  job memory/swap and host memory pressure were monitored for publication phases.
  Successful phase receipts record peak RSS, sampled cgroup memory, elapsed time and
  exit status; observed minimum host MemAvailable across them exceeded 75 GiB.
- Publication helper: `scripts/sustainability/publish_release.py`.
- Immutable exported records, source manifest, source checksums, read-only shadow input,
  comparisons, previous alias and phase receipts are under
  `evaluation/sustainability/data/sustainability-references-20261103-v1/`.
  Logs are `evaluation/sustainability/logs/release-*.log`.
- Previous reference alias was absent (`previous-alias.json` is `{}`). Rollback of this
  release removes only alias `sustainability_references` from index
  `sustainability-references-20261103-v1` using `POST /_aliases`. The new calculator then
  falls back to legacy behavior. Retain the immutable index and export for investigation;
  no legacy data restoration or deletion is necessary. Before any rollback, verify the
  alias still points to this release so a later release is not inadvertently displaced.

### API deployment boundary

`recipe-wrangler-api` currently runs `wisefood/recipe-wrangler:matching-2026-10-29`.
Docker inspection confirmed only `.env` and generated images are bind-mounted; workspace
Python source is not mounted. Consequently **the running API still executes its prior
calculator**, even though the new reference database and alias are live. This task made
no image/package changes or service restarts. A code rollout must include both
`repositories/sustainability_references.py` and the updated `tools/sustainability_calculator.py`.
The new lookup honors optional `SUSTAINABILITY_REFERENCE_INDEX`, defaulting to the live alias.
The existing recipe/profile scores were not backfilled. Deploying this integration and
expanding source-consistent coverage are distinct follow-up operations; the source database
is ready and the workspace integration has passed a live conformance check.

## Source-aware sustainability merge pilot

Historical pilot report. The subsequent database release (release section above)
imported and activated the reference index; it records the current production boundary.

3 November 2026. **Offline prototype built and checked. Production collection and recipe
calculations are unchanged.** Current artifact: `data/catalogue-pilot-v3.sqlite`.
Earlier attempts are retained as development evidence and must not be used as the current pilot.

### What a garlic lookup should return

One food identity can have multiple explicitly sourced environmental references. A duplicate
copy of the same reference is different from an estimate made with another method, market,
unit or lifecycle boundary. Keep the latter; do not sum or average those alternatives.

The pilot stores `garlic` once in its food table and links fresh/raw/clove naming variants through
a small explicit reviewed alias set. Garlic powder, garlic salt, garlic in oil, garlic mayonnaise,
garlic cheese and garlic bread remain separate identities. No model or fuzzy similarity was used
to assert that other names are equivalent.

Example lookup, run from the repository root:

```bash
python3 scripts/sustainability/reference_catalogue.py lookup \
  --database evaluation/sustainability/data/catalogue-pilot-v3.sqlite \
  --name garlic --source AGRIBALYSE
```

The result identifies **Garlic, fresh**, source **AGRIBALYSE**, release
**3.2-food-AOUT25**, native record **11000**, climate value **0.383 kg CO2 eq/kg de produit**,
French reference market, consumer-product boundary, DQR **2.88**, source DOI, workbook row and
SHA-256. See the saved response (archived locally).

An unfiltered query returns all compatible-name references with their sources and a
`multiple_references` status, with **no automatic selection**. `--market` restricts an explicit
market; it does not invent an Irish factor from another country's estimate. `--metric` defaults
to `climate_change`; a water-only record cannot answer a carbon query. `water_footprint` is
available separately, with source units retained.

#### Actual sources found for garlic

| Source | Record / scope | Climate value | Qualification |
|---|---|---:|---|
| Current live legacy collection | document `garlic`, source ID `34c683de-be38-49e3-89ae-c9ccdf1c83bf` | 0.67 | Runtime assumes kg CO2e/kg; metadata contains category/factor/ingredient but no study citation or version |
| AGRIBALYSE | 11000, Garlic, fresh, FR | 0.383 | Per kg consumer product with declared lifecycle assumptions |
| CONCITO | Ra00136, DK/ES/FR/GB/NL | 1.248 / 0.961 / 1.210 / 1.197 / 1.211 | Per kg product; separate market scenarios and consequential method |
| SU-EATABLE LIFE | GARLIC | 0.708 | Header has kg-or-litre ambiguity; high uncertainty; source recommends a better subtypology value, so not approved as a recipe factor |

These values do not demonstrate that one garlic is environmentally better than another. Their
boundaries/methods differ. The selected source must accompany any eventual recipe contribution.
The two legacy aliases `garlic` and `garlic clove` are retained as traceable source records linked
to the same food; their identical 0.67 values are not independent evidence or summed contributions.

### Live database inspection

One read-only lexical Elasticsearch query used `size=20`, query timeout 60 seconds and
`track_total_hits=100`, with a collection filter. No embeddings were generated. Elasticsearch
reported 94 garlic-containing documents, took 30 ms and did not time out. Retrieved examples
included garlic spread/cheese/salt, illustrating why word containment is insufficient for identity.
The complete query and bounded result are saved in merge-investigation.json (archived locally).
This is not a full live-index deduplication or source-integrity audit.

### Pilot contents

| Source | Active reference records | Carbon records | Notes |
|---|---:|---:|---|
| Existing local Sustainable FooDB CSV | 7,364 | 7,364 | Explicitly labelled legacy/unverified provenance |
| AGRIBALYSE food-products workbook | 2,451 | 2,451 | All native suffix IDs preserved; seven labelled packaging-error predecessors excluded from active lookup |
| CONCITO public table | 2,700 | 2,700 | 540 products × five markets; source/method/market retained |
| SU-EATABLE LIFE | 517 | 324 | Also 320 water records; carbon and water join only on exact original source item labels |
| **Total** | **13,032** | **12,839** | References are not unique ingredient concepts and do not represent measured recipe coverage |

There are **10,548 normalized food-name keys**, not 10,548 independently verified ingredient
identities. Public sources have 3,423 name keys, 238 shared with the accepted legacy names and
3,185 not present under those legacy keys. This is a candidate-enrichment inventory, not a claim
of 3,185 genuinely new foods or a measured coverage increase.

FoodEx2-to-AGRIBALYSE links are stored separately (2,122 distinct link rows) with their original
proxy/action/conversion flags. They add mapping evidence, not new impact measurements or automatic
identity aliases. Agricultural farm-gate records and the HESTIA sample are not mixed into the
consumer-product factor catalogue.

### Deduplication and conflict rules

1. **Food names:** conservative normalized keys; the stated garlic aliases are explicitly reviewed.
   Do not drop processing, product, mixture or form qualifiers to increase coverage.
2. **Reference identity:** unique `(source, version, native record ID, market)`, with a deterministic
   hash. Same-record reimports are skipped. Different sources/markets remain traceable estimates.
3. **Conflicting values:** public-source conflicts stop the import. Conflicting legacy identity keys
   are quarantined entirely; neither the first nor last value becomes an active answer.
4. **Corrections:** seven AGRIBALYSE records explicitly labelled `software version - with packaging
   error` have `packaging fixed` counterparts. Only corrected records are active; excluded original
   labels/rows are retained in the build report.
5. **Source codes and labels:** preserve AGB variant IDs such as `13167_1` and `13167_2` (powder and
   rehydrated). Normalize zero padding only for known numeric AGB mapping IDs.
6. **Case-sensitive source distinctions:** SU-EATABLE LIFE lists `CUCUMBER (G)` and `CUCUMBER (g)`
   with different factors (also pepper/tomato). Source item keys retain case and punctuation so
   these cannot overwrite one another. Broad food-name grouping does not assert equal production
   variants, and no factor is selected automatically from an ambiguous group.
7. **Units:** keep all metrics and original units. SEL kg/litre ambiguity is blocked, not silently
   converted. AGRIBALYSE water-resource impact and SEL water footprint remain different metrics.

The full legacy CSV yielded five repeated equivalent records and two conflicting identity groups:
`cap n crunch cereal` has incompatible category labels; `breadcrumb bread` has factors 0.78 and
0.70. Both conflict groups are quarantined. Quarantine affects only the offline pilot, not production.

There are **447 candidate synonym groups** identified by token reordering, such as `almond butter`
and `butter almond`. They are queued for review in merge-review-v3.json (archived locally),
not automatically merged. This is not an exhaustive semantic duplicate detector. More identity
review is required before claiming a fully deduplicated production ingredient catalogue.

### Verification and resources

- 18 focused tests passed: idempotence, provenance, cross-source preservation, conflicting values,
  quarantine, market selection, unit ambiguity, metric filtering, suffix IDs, case-sensitive source
  variants and garlic compound/form exclusion. Test log (archived locally).
- Complete input replay inserted **zero** records and left the SQLite SHA-256 **unchanged**.
  SQLite integrity/foreign-key checks passed. Review log (archived locally).
- CPU-only private cgroups: 1 GiB RAM/no swap, two-core affinity/200% quota, one job at a time,
  900 CPU-second/15-minute wall limits. Host availability, cgroup memory/swap and memory pressure
  were monitored during builds. No model or embedding was loaded. Build/replay telemetry is
  stored beside the database in `.build-report.json` and `.replay-report.json`.
- Intermediate failed/superseded builds remain logged. Investigation caught lost AGB suffix IDs
  and case-folded SEL source distinctions before this final pilot. Use **v3** only.

### Production integration direction

The current application still uses its existing Elasticsearch collection and legacy calculator.
The new lookup is a standalone offline CLI, not a deployed API or an active fallback. This pilot
does not change recipe scores, aliases, source selection or existing broth/salt overrides.

For integration, bind each accepted ingredient identity/form to source record IDs and a versioned
method/basis policy. Return the source name, release, native ID, citation, mapping type, original
unit, lifecycle boundary and uncertainty with every matched factor and recipe contribution.
Store conflicting or unknown mappings as unresolved; never make a missing contribution zero
without an explicit partial-total status. Do not choose the smallest number across sources.

Next review should resolve common synonym groups and high-impact unmatched terms, select a
consistent ingredient lifecycle basis, and measure additional exact/reviewed/proxy coverage on
exposed development recipes. Preserve raw reference releases and validate source selection before
any production import or sustainability adaptation claim.

## Initial reference acquisition and inspection

3 November 2026. **Complete initial investigation, not production ingestion or accuracy evaluation.**
The Japanese/Chinese recipe resources are excluded. Original application data and other agents'
evaluation additions are preserved. No model, embedding, LLM, production query or package installation.

### Acquired evidence

23 downloaded artifacts, 29,048,298 bytes (27.70 MiB), passed local SHA-256, file-size,
XLSX archive, PDF-signature and schema checks. This includes source metadata and documentation;
it does not mean 23 distinct datasets. See integrity checks (archived locally),
source inspection (archived locally), acquisition manifests and logs (archived locally).

| Source | Actually downloaded and inspected | Licence evidence | Result |
|---|---|---|---|
| AGRIBALYSE | Official simplified CSV; food-products workbook (`PublieAOUT25`); organic-agriculture workbook (`PublieNov24`); English agricultural methodology; Dataverse and ADEME metadata | Dataverse `etalab 2.0`; ADEME `lov2` | Food workbook has 2,458 numeric product rows, **2,451 distinct AGB codes**, 2,425 distinct CIQUAL codes; every inspected product row has an English name |
| SU-EATABLE LIFE | Official Figshare article version 2 and its XLSX, including source studies/statistics/user sheets | Figshare CC BY 4.0 | **324 carbon items**, **320 water items** with numeric user-sheet values; source references, uncertainty and recommended aggregation levels present |
| FoodEx2 environmental bridges | All five release files: three mapping XLSX, decomposition/waste XLSX and matching R source; R code **not executed** | Zenodo CC BY 4.0 | DOI lookup resolved to record **20025350**, explicitly pinned by the file URLs/metadata; AGRIBALYSE mapping has 5,354 rows over 4,575 distinct FoodEx2 codes |
| CONCITO v1.2 | Official public table HTML, extracted CSV, methodology PDF | Source page links CC BY 4.0 | **2,700 market-product rows = 540 products × DK/ES/FR/GB/NL**, with agriculture, ILUC, processing, packaging, transport and retail columns |
| HESTIA | Access/API/release documentation, public release metadata and one original plus stored recalculated Spanish grape assessment | Official documentation CC BY 4.0 | Pinned sample release **2025-05-01**; stored sample has **71 impact indicators and 495 emission/resource-use indicators**. No HESTIA models run locally or requested for execution |

CONCITO's CSV is our extraction of the official displayed table, not an official downloaded
workbook. Displayed three-decimal values are retained. Raw HTML and derived SHA-256 remain available.
Metadata for HESTIA also advertises enabled releases 2026-03-10 and 2026-04-30; our sample uses
the documented older public release, not a claim to have downloaded the latest full database.

Acquisition manifests record requested and resolved URLs, source filenames, bytes, hashes,
server Last-Modified where supplied and schema samples. Later runs also record explicit UTC
request/retrieval timestamps. First-run files retain their local acquisition mtimes in the
integrity ledger; those mtimes are local evidence, not source publication dates.

### Important findings before integration

#### AGRIBALYSE: strongest immediate candidate, but use actual release counts

The downloaded workbook identifies impacts as **per kg of product consumed by the consumer,
including intermediate losses**. It contains summary, stage and ingredient detail sheets. Its
summary provides CIQUAL/AGB codes, English/French labels, season/air-freight/delivery/packaging/
preparation fields, DQR quality and climate plus other environmental indicators.

The old public documentation's headline 2,516 must not substitute for the actual file counts.
The CSV parser encountered 14,956 rows, but **12,505 have blank AGB codes**. The 2,451 nonblank
codes align in count with the workbook's distinct AGB codes. The raw `distinct_agb=2452` statistic
in the initial inspection includes the empty string; this is explicitly corrected here rather
than rewriting historical output. Full row/content equivalence between CSV and XLSX is not established.

The workbook has 2,458 product rows across 2,451 codes, so an importer must handle duplicate codes
and scenario attributes deliberately rather than silently taking the first row. Preparation
includes no preparation, microwave, boiling, oven, pan and refrigerated scenarios. Some values
contain trailing spaces. DQR spans 1.0–4.79 (lower is better in the workbook definition).

Use the stage breakdown to construct a consistent ingredient basis. Do not add household cooking
emissions on top of totals already including the same preparation. The organic workbook explicitly
uses **per kg agricultural product at farm gate**: it cannot be pooled with consumed-product factors.

#### SU-EATABLE LIFE: useful transparent proxies, not universally precise item factors

Carbon user-sheet uncertainty labels: **144 low / 180 high**. Water labels: **293 low / 27 high**.
The recommended-value column often directs users to a typology/subtypology instead of the item.
Preserve those directions and statistics; an exact ingredient name does not cancel uncertainty.

Critically, headers specify factors **per kg OR litre**, with equivalent g/cc forms in some
hierarchy columns. A mass-based loader cannot assume every value is per kg. Resolve product
functional units and densities explicitly before using liquid factors. Water consumption and
AGRIBALYSE's water-resource impact indicator are different measures and must remain separate.

#### FoodEx2: preserve proxy/actions and normalize identifiers carefully

AGRIBALYSE links use `matching_action`, `match_found`, `proxy`, `type_of_proxy`, and `cf_required`.
Rows include non-leaf averages, unmatched, cut-off and disaggregate actions. Repeated FoodEx2 codes
do not imply duplicate erroneous rows: a mapping may need several components or actions.

The sheet has 1,322 distinct target AGB strings; 1,320 match workbook strings directly. The two
remaining strings, `0001` and `0006`, correspond to workbook numeric IDs `1` (beetroot juice)
and `6` (virgin coconut oil). This is a demonstrated zero-padding issue, not evidence of missing foods.
Define a source-specific normalization without stripping meaningful zeros from arbitrary IDs.

Boolean cells appear as `TRUE`/`FALSE`, `1`/`0` and missing values. A real XLSX adapter must preserve
cell types and parse these explicitly; Python truthiness of a nonempty `"FALSE"` string is unsafe.
The quick source inspection intentionally retains these raw forms. No impact factors are supplied
by the mappings themselves, and the ecoinvent mapping does not grant access to its licensed factors.

#### CONCITO: useful market scenarios, separate method

Retain product ID, market and stage columns; do not average markets into a fabricated EU value.
CONCITO's consequential method differs from attributional accounting. Compare candidate substitutions
within compatible source/method/market scenarios. The public factors are suitable for initial
cross-method sensitivity analysis; they are not automatically IE/HU/SI-specific estimates.

#### HESTIA: accessible richer evidence, higher adapter effort

The original public sample contains geography, period, sources, allocation, product, observations
and quality information but no computed impact list. The stored `dataState=recalculated` response
contains environmental indicators and model/version provenance. These are downloaded stored results,
not an instruction to execute any models. The schema includes array-valued product observations,
functional-unit and allocation information; it cannot be treated as a flat food-name/factor table.

### Downloads that did not succeed

The conventional-agriculture workbook (Dataverse file 666339) and English food-products methodology
PDF (602259) each timed out on the initial attempt and one same-budget retry. Failure records remain
in `downloads-01-manifest.json` and `downloads-02-manifest.json`; no resource limits were increased.
The primary food-products workbook, organic workbook and agricultural methodology were retrieved.
Do not describe the two failed files as downloaded. A later retry can use the saved official plan.

### Resource and validation evidence

Private systemd services enforced 1 GiB RAM, zero swap, 200% CPU quota and 15-minute wall ceilings;
Python affinity selected two cores and acquisition CPU time was limited to 900 seconds. One worker,
150 MiB per file and 300 MiB per acquisition run; aggregate downloaded material is below 300 MiB.
GPU visibility was disabled. Initial host preflight showed about 76 GiB MemAvailable and GPU
97% busy / 77°C. No GPU workload was started.

The startup smoke check exited 0 in 16 ms with peak Python RSS 12,096 KiB. Main download/inspection
and integrity jobs exited 0. Initial acquisitions took 71.554 s; retry/documentation took 61.084 s;
offline source inspection took 1.353 s at 47,792 KiB peak RSS; final integrity checks took 0.249 s.
The main acquisition sampled cgroup memory peaked at 41,279,488 bytes, host availability stayed
above 73 GiB, job swap was zero, host swap counters were unchanged and pressure averages were zero.
Other acquisition runs retain their individual telemetry; do not infer a reservation against later
allocations by other users.

The first startup attempt failed because systemd defaulted to the home directory; it exited before
downloading anything. The retry explicitly set WorkingDirectory. The original failure log is preserved.
No full application tests were run because no runtime application behavior changed. Offline checks
validated successful download hashes/sizes, spreadsheet ZIP contents, PDF signatures, script syntax,
CONCITO table schema and a nonempty published HESTIA impact sample.

### Next recommended work

1. Build a small offline, reviewed AGRIBALYSE/CIQUAL/FoodEx2 pilot with explicit units, stages and
   duplicate-ID policy; keep raw source files immutable.
2. Recover original Sustainable FooDB source provenance and compare the existing live index only
   through a separately bounded read-only audit. No such live audit has happened here.
3. Measure added exact/reviewed/proxy coverage on exposed development recipes before forecasting
   corpus-wide gains. More reference rows do not by themselves establish useful coverage.
4. Specify comparable per-serving environmental changes for adaptation, preserving uncertainty and
   nutrition/cost trade-offs. Do not promote category fallbacks into precise product-variant claims.
5. Keep RecipeEmission/GreenFoodLens as a separate future evaluation-access task; the present work
   downloads enrichment sources, not an independent recipe-footprint gold benchmark.

## Sustainability embedding preparation — 4 November 2026

Completed offline vector preparation for the immutable reference release. No Elasticsearch
index/alias was created or changed, no API image was deployed, and no recipe score was
rewritten. Shared nutrition matcher code and other agents' changes were preserved.

### Artifact and contract

Bundle: `evaluation/sustainability/data/sustainability-vectors-20261104-v1/`.

- `vectors.jsonl`: 13,032 separate reference documents, 130,344,730 bytes.
- `manifest.json`: source hashes, model/revision, counts, output checksum and integration status.
- `smoke-receipt.json` and `build-receipt.json`: preflight, resource telemetry and exit status.
- Build log: `evaluation/sustainability/logs/embedding-build-20261104-v1.log`.
- Producer: `scripts/sustainability/prepare_embeddings.py`; regression tests:
  `tests/test_sustainability_embeddings.py` (8 passed).

The producer used the existing `get_embeddings_batch` helper, model
`BAAI/bge-small-en-v1.5`, pinned revision
`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`, 384 dimensions, CPU batch size 8.
The model was already cached; offline mode prevented model downloads. No packages
were installed or changed. Input is the verbatim source food name, matching the
existing nutrition export's name-only embedding convention, without an instruction
prefix or appended environmental factors. Food/form descriptors already in names
are retained. Shared helper defaults apply; retrieval should use cosine similarity.

There are **10,834 distinct exact source-name strings**. Their vectors were computed
once per string, then attached to all corresponding references. This computation
deduplication does not merge food identities, markets, methods or sources.

| Source | Preserved references |
|---|---:|
| AGRIBALYSE | 2,451 |
| Local legacy snapshot | 7,364 |
| CONCITO | 2,700 |
| SU-EATABLE LIFE | 517 |

Each JSONL row has `_id = reference_id` and an `_source` containing `source_id`,
`document`, `embedding`, model/revision and cosine-space declaration. Metadata
retains the complete original reference payload (native ID, source/version/market,
units, method, lifecycle boundary, scenario, factors and provenance), the reference
release and original calculation eligibility. All rows explicitly carry
`candidate_only: true` and `facet_annotation_status: pending_shared_matcher`.
Eligibility of a catalogue record does not establish suitability for an occurrence.

Source export SHA-256:
`d2afe3f6f498d090df339be489e0a1b93f2c54b5de10380f3956a77c2b01cbce`.
Vector export SHA-256:
`1a5d3b4075a9ba0abaf51feff6d7baffa198adc72bc0e702e9080d025e454df0`.

### Verification and resources

Source checksum/count/unique identities were checked before embedding. Every output
record was reread and compared with its intended document, including full provenance
and vector. All vectors have 384 finite elements and nonzero norm. All 13,032 records
were verified; no source references were dropped. Eight offline tests cover record
separation, provenance/eligibility retention, corrupt source exports, duplicate IDs,
and invalid vectors. This verifies artifact integrity, not retrieval quality or
environmental accuracy.

Both smoke and build used one private systemd cgroup at a time: two-core affinity,
200% CPU quota, two numerical threads, 4 GiB aggregate RAM, zero job swap, 900 CPU
seconds and a 15-minute wall limit. The fresh host-memory calculation supported that
ceiling under both the half-physical-RAM rule and the 10 GiB headroom target.
Preflight captured CPU load and GPU VRAM/utilization/temperature/power; GPU visibility
was disabled. The monitor sampled host availability, memory pressure, swap counters
and job memory/swap, with termination safeguards for our own job.

| Phase | Elapsed | Peak process RSS | Minimum host MemAvailable | Exit |
|---|---:|---:|---:|---:|
| Four-description smoke | 3.97 s | 1,076,236 KiB | 74,400,280,576 bytes | 0 |
| Full build and verification | 36.64 s | 1,281,956 KiB | 73,836,408,832 bytes | 0 |

Build sampled cgroup memory peaked at 928,985,088 bytes. Job swap stayed zero;
host swap counters were unchanged and memory pressure averages stayed zero.
The cached checkpoint loader reported an unexpected `embeddings.position_ids` key;
loading completed and output dimension/finite-vector checks passed. No dependency
change was attempted.

### Next integration boundary

Wait for the shared matcher interface to stabilize. Reuse its observed ingredient
identity/facets, and annotate source records separately; unknown facets must remain
unknown. Preserve descriptor attachment and distinguish intended recipe cooking from
the ingredient's observed state. Do not copy a selected reference's properties into
observed ingredient facts.

Add a sustainability catalogue adapter and a separate versioned search index with an
explicit mapping for this bundle. The logical collection name in the export is
`sustainability_references_prepared_20261104_v1`; it is not an active index or alias.
The existing generic vector importer/mapping has not been validated for the nested
reference metadata, so this is an offline bundle rather than a claim of drop-in import.

Retrieve candidates lexically and/or by vector, gate by supported food/form facets,
then apply environmental source/version/market/unit/boundary/scenario eligibility.
Keep source-native identifiers and reviewed CIQUAL/FoodEx2 bridges; do not invent
CoFID equivalences. Do not average alternative source factors or select the lowest
factor. Retain current all-positive-mass, single-policy recipe selection until an
explicitly evaluated policy replaces it. Measure retrieval and accepted-match quality
before activating this path. No model rerun is needed just to attach structured facets
while the source-name embedding contract remains unchanged.
