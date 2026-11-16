# Dietary ingredient evidence

`dietary_ingredients.json` contains an English ingredient lookup derived from
**Open Food Facts contributors**. Contains information from Open Food Facts,
made available under the [Open Database License](https://opendatacommons.org/licenses/odbl/1-0/).
Individual database contents are under the
[Database Contents License](https://opendatacommons.org/licenses/dbcl/1-0/).
See the [upstream data licensing documentation](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorials/license-be-on-the-legal-side/).
This derived lookup is distributed under ODbL; its data licence is separate
from application code. No upstream application code or product images are included.

Source: `openfoodfacts/openfoodfacts-server`, commit
`12008812f6aa465b901af4080d1357b1dba51546`,
[`taxonomies/food/ingredients.txt`](https://github.com/openfoodfacts/openfoodfacts-server/blob/12008812f6aa465b901af4080d1357b1dba51546/taxonomies/food/ingredients.txt).
Source SHA-256: `7e42ae5bc64d9288c2d5341fb64776b56de8d5035c8947481cb03cb9d210df7c`.
The JSON embeds this provenance and the explicit property roots for each diet.

Regenerate with `scripts/preprocessing/build_dietary_ingredient_evidence.py`
using the pinned source file and a manifest containing its SHA-256, URL and commit.
The derived data contain 4,875 English ingredient entries and synonyms, with
resolved vegan/vegetarian properties. These are ingredient facts, not independent
recipe evaluation labels or manufacturer certification.

Direct declared milk/egg/fish/shellfish constituents also supply negative dietary
evidence. For example, the source's white chocolate entry explicitly lists milk,
which resolves an otherwise ambiguous inherited vegan property. Contradictory
explicit suitability and constituent declarations remain uncertain for review.
This does not replace the separate allergen classifier or establish allergen absence.

Inheritance stops at an explicit property, including `maybe`. Conflicting
parents, cycles and ambiguous aliases cannot prove suitability. Runtime lookup
accepts a whole ingredient name or synonym, narrowly removes preparation
prefixes, and recognises a few documented spelling equivalences. It does not use
substring/fuzzy matches to transfer suitability to mixtures. Missing and `maybe`
properties supply no definite evidence and cannot be confirmed just by a broad
ontology origin. Prepared mixtures also require whole-food dietary evidence;
plant origin alone does not establish their full composition. Existing independent composition rules
remain in force. In particular our named-ingredient policy treats plain sugar
as suitable without certifying processing aids, and permits named dairy for
vegetarian unless animal rennet is explicitly stated.

No product-specific formulations, allergen absence or cross-contact claims are
inferred. Existing definite blockers and possible blocking-allergen evidence
take precedence over positive ingredient evidence.
