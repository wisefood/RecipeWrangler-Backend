# Instruction extraction experiments

## RecipeBERT traditional: initial probe, 4 November 2026

**Downloaded and tested successfully with the existing environment.** This is an
offline compatibility and development probe, not a validated time-adaptation system.
No production recipe, database, API image, shared matcher, or installed package was
changed. The ReciFine package was not installed.

### Model and reproducibility

- Model: [nuhuibrahim/recifinegold-recipebert-trad](https://huggingface.co/nuhuibrahim/recifinegold-recipebert-trad).
- Pinned revision: `7686d9b1fda9123a768e2e80c70cb6a438b8d370`.
- Author code revision: `9c22c310cb07c4193aee85ce26e9897f3920b80c`.
- Runner: `scripts/instructions/recifine_probe.py`.
- Completed bundle: `evaluation/instructions/recifine-traditional-v2/`.
- Acquisition manifest: `acquisition.json`; model files: `model/`; pinned author
  preprocessing/schema files: `upstream/`; frozen inputs: `inputs.json`.
- Results: `evaluate-predictions.json`, `evaluate-summary.json`,
  `evaluate-receipt.json`; corresponding `smoke-*` files preserve the initial smoke.
- Logs: `evaluation/instructions/logs/{acquire,smoke,evaluate,casecheck}-v2.log`.

Acquisition downloaded 17 files totalling 436,117,443 bytes. The safetensors file
was verified against its Hugging Face LFS SHA-256; local file digests were saved.
No `.bin` checkpoints, training pickles or remote Python model code were loaded.
All model/tokenizer inference was offline with local-files-only loading.

Installed environment: PyTorch 2.10.0, Transformers 5.0.0, safetensors 0.7.0,
tokenizers 0.22.2. The checkpoint config records Transformers 4.57.6. Compatibility
worked, but exact original-environment reproduction is not established.

The checkpoint's 21 output names are generic `LABEL_n` values. The runner derives
the documented ordering from the pinned author's schema: `O`, followed by sorted
BIO labels for ten entity types. It preserves case, tokenizes whitespace words into
WordPieces, predicts at the first subtoken, and uses 256 positions with zero segment
IDs, matching inspected author preprocessing settings. Inputs exceeding 254
WordPieces are explicitly rejected instead of silently truncated. Original text and
character offsets are retained. Author code was inspected, not executed or installed.

The model card declares **CC BY-NC 4.0**. Attribution and the noncommercial condition
must remain visible when deciding how this research artifact can be integrated.

### What was tested

| Input group | Count | Purpose |
|---|---:|---|
| Authored diagnostic sentences | 12 | Targeted foods, tools, timing, state, negation, batches and storage examples |
| Local source recipe steps | 36 | First three FoodHero recipes and first three Safefood dinner recipes, at most eight steps each |
| Published author examples | 20 | First 20 saved predictions, to check agreement with the released checkpoint |
| Total | 68 | All processed; no over-length cases |

The six local recipes are Agua Fresca, Almond Rice Pudding, Ants on a Log, Homemade
burgers, Beef tacos with chilli sauce, and Beef chilli con carne. File hashes, titles,
URLs, original steps and sampling policy are frozen in `inputs.json`. Sampling is
purposive and small, not representative. No training-data independence is claimed.

The authored inputs contain **21 selected expected mentions; 20 were recovered**
under exact type/text matching after case/punctuation normalization. These are
incomplete positive checks, not full gold annotation: they do not measure overall
precision, recall or accuracy, and do not penalize all extra/wrong entities.

Five regression tests in `tests/test_recifine_probe.py` passed: label ordering,
original-text offsets, orphan BIO continuation handling, adjacent actions and
preservation of negation/numeric distinctions in targeted comparisons.

### Useful extractions and observed gaps

| Instruction | Actual observations |
|---|---|
| Simmer the beans in a saucepan for 20 minutes. | Correct action, beans, saucepan and duration spans |
| Drain and rinse the canned beans. | Both actions, canned state and beans extracted |
| Soak the dried beans overnight. | Soak, dried state, beans and overnight extracted; no numeric duration invented |
| Cook the rice for 15 to 20 minutes, until tender. | Action, rice, duration range and tender state extracted |
| Preheat the oven to 180 degrees C. | Oven recognized; temperature split into `180 degrees` as TOOL STATE and `C.` as TOOL |
| Meanwhile, chop the carrots. | Chop and carrots extracted; no explicit overlap relation produced |
| Do not boil the sauce. | `Do not boil` retained as a discontinuous-action span; no typed prohibition predicate produced |
| Bake in two batches for 12 minutes per batch. | Batch count and duration found, but final `batch.` misclassified as TOOL; repetition relation absent |
| Local FoodHero storage instructions | Storage deadlines sometimes extracted as DURATION; one `up to 3 days` span was missed |
| Local burger cooking instruction | `10 to 12 minutes` found; `each side` is not represented as a timing multiplier |

The local recipes also show `heat` labelled TOOL, standalone `until` labelled
DURATION, and step number `3.` labelled QUANTITY. Punctuation remains attached to
some word-level spans. These are concrete reasons to validate/normalize entities,
classify timing semantics, and build relations before scheduling. A DURATION entity
can describe a cooking interval, qualitative endpoint, storage deadline or shelf life;
it must not automatically become recipe elapsed time.

### Published-output comparison remains unresolved

Against the authors' saved predictions, the case-preserving run agreed on **242/283
token labels**, with **4/20 complete sequences identical**. This is agreement with
published predictions, not accuracy against gold. Labels were semantically plausible,
but the result does not establish exact reproduction or the cause of disagreements.
Do not transfer the authors' reported F1 to our environment or corpus.

An additional fixed-input casing comparison was attempted under the same limits.
It was stopped by the host-swapping safeguard before completion. Partial stdout is
preserved, with `casecheck-receipt.json` reporting exit 75; no completed casing
summary or selected best configuration exists. Do not infer a winner from partial
outputs. Preprocessing/version/checkpoint lineage remain follow-up questions.

### Resource evidence

All phases used one private systemd cgroup, two-core affinity, 200% CPU quota and
zero job swap. Acquisition used 1 GiB aggregate RAM; model runs used 4 GiB, numerical
thread limits of two, batch size one, 900 CPU seconds and a 15-minute wall limit.
Fresh host memory/load and GPU memory/utilization/temperature/power were captured
before launch. The RAM ceilings respected half physical RAM and the computed
available-minus-10-GiB ceiling. GPU visibility was disabled; no GPU compute was used.

| Successful phase | Elapsed | Peak process RSS | Exit |
|---|---:|---:|---:|
| Acquisition retry | 16.92 s | 32,208 KiB | 0 |
| Two-sentence model smoke | 4.25 s | 1,183,264 KiB | 0 |
| Main 68-case probe | 17.22 s | 1,200,868 KiB | 0 |

The main probe used about 31.2 CPU seconds; minimum host MemAvailable was
62,538,792,960 bytes. Job swap stayed zero, host swap counters were unchanged and
memory pressure averages stayed zero. Monitoring checked host availability, swap,
pressure and job memory each second and stopped only our jobs on safeguard events.
The first acquisition stopped after a small host swap-in increase (receipt retained
under `recifine-traditional-v1/`); the same-budget retry succeeded. Later casing
diagnostics stopped on host swap-out. No limits were increased and no other jobs
were touched. Process RSS and sampled cgroup charges are separate measurements.

### What to do next

Use this as a candidate entity extractor. First resolve reproducibility and annotate
a small independent in-house sample with complete span labels. Then evaluate
action-to-food/tool/duration attachment, intermediate-food coreference, explicit
negation, timing scope (including storage), repetition, endpoints and dependencies.
Only then feed supported action graphs to a resource-aware scheduler. This model
does not independently discover equivalent quantities, cooking transformations,
numeric durations for unstated actions or safe time-saving substitutions.

### Running a fresh probe

Use a new output directory; existing receipts are never overwritten. Prefix each
phase with a private systemd service, e.g. `systemd-run --user --wait --pipe --collect`
and `-p MemoryMax=4G -p MemorySwapMax=0 -p CPUQuota=200% -p RuntimeMaxSec=900
-p WorkingDirectory=$PWD`. Use `MemoryMax=1G`
for acquisition. Set `RECIFINE_PROBE_OUTPUT` to the fresh workspace directory and
thread environment variables as recorded in the logs. Run `.venv/bin/python
scripts/instructions/recifine_probe.py acquire`, then `smoke`, then `evaluate`.
The runner checks cgroup enforcement and fresh headroom before model loading.
Reassess current host conditions before any future run; saved headroom is not a
reservation. No further model runs are needed merely to inspect these results.
