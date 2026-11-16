# Sustainability

**No accuracy evaluation exists.** There is no open English recipe dataset with independent full-recipe footprint ground truth. Public sets (RecipeEmission, GreenFoodLens) are modelled estimates, and shared reference factors cannot give an independent accuracy claim. Last updated 4 November 2026.

## What was done

| Item | Result |
|---|---|
| Reference data | 13,032 factors from AGRIBALYSE 3.2, SU-EATABLE LIFE, CONCITO and FoodEx2 mappings, merged by a source-aware policy into one versioned Elasticsearch index. 112 tests pass. |
| Recipe-level shadow run | 100 profiles: 120 of 1,060 ingredient positions matched, no complete recipes under the single-source policy |
| Embeddings | 10,834 distinct source names embedded (BGE) for all 13,032 references |
| Deployment | API image not redeployed. Stored recipe scores unchanged. |

Current app method: sum of ingredient kg x kg CO2e/kg from "Sustainable FooDB". It measures climate impact only. Missing factors count as zero, a known limitation.

## Files

| Path | Contents |
|---|---|
| `results/report.md` | Release policy, shadow results and rollback; source merge and quarantined conflicts; acquisition counts, licences, units; embedding build |
| `data/` | Reference data used by `scripts/sustainability/` (catalogue `catalogue-pilot-v3.sqlite`, `sustainability-references-*`, `sustainability-vectors-*`, public source downloads; about 200 MB, local only and gitignored). Reference data, not evaluation gold. |

Offline lookup:

```bash
python3 scripts/sustainability/reference_catalogue.py lookup \
  --database evaluation/sustainability/data/catalogue-pilot-v3.sqlite \
  --name garlic --source AGRIBALYSE
```
