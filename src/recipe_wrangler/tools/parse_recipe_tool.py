# Purpose: LLM-based parser from raw recipe text to structured fields.

from typing import Any, List, Optional
import os
import re
import unicodedata
from fractions import Fraction

from langchain.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, ValidationError

from recipe_wrangler.schemas import RecipeState
from recipe_wrangler.utils.seasoning_quantities import salt_and_pepper_parts as _salt_and_pepper_parts


def _plain_recipe_text(text: str) -> str:
    """The model corrupts non-ASCII it must copy ('3⁄4' -> '3¾', '1⁄4' -> '1⁄'), so send plain text."""
    def fraction(match: "re.Match[str]") -> str:
        plain = unicodedata.normalize("NFKC", match.group(2)).replace("⁄", "/")
        return f"{match.group(1)} {plain}" if match.group(1) else plain
    text = _fold_accents(text).replace("®", "").replace("©", "")
    return re.sub(r"(?:(\d)\s*)?([½⅓⅔¼¾⅛⅜⅝⅞])", fraction, text)


def _parser_llm(model_name: str):
    """Build the configured parser model and structured-output method."""
    source = os.getenv("WEIGHT_LLM_SOURCE", "groq").strip().lower()
    # Output is always a short structured-JSON entry list — a handful of
    # ingredients at ~40-60 tokens each. No real recipe needs anywhere close
    # to 2000 completion tokens. Capping it bounds a runaway generation (seen
    # in practice: a single stuck request that never hits a stop token can
    # sit "Running" on the vLLM server for many minutes, during which every
    # OTHER queued request just times out client-side with no server-side
    # error at all — cascading silent failures across dozens of unrelated
    # recipes). A hard ceiling here turns that into a fast, visible failure
    # for the one bad request instead of stalling everything behind it.
    max_tokens = 2000
    if source == "vllm":
        llm = ChatOpenAI(
            model=model_name,
            temperature=0.0,
            max_retries=2,
            max_tokens=max_tokens,
            base_url=os.getenv("VLLM_BASE_URL", "http://localhost:8007/v1"),
            api_key=os.getenv("VLLM_API_KEY", "none"),
        )
        return llm, "function_calling"
    if source == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set.")
        llm = ChatOpenAI(
            model=model_name,
            temperature=0.0,
            max_retries=2,
            max_tokens=max_tokens,
            base_url=os.getenv(
                "OPENROUTER_BASE_URL",
                "https://openrouter.ai/api/v1",
            ),
            api_key=api_key,
        )
        return llm, "function_calling"
    if source == "groq":
        llm = ChatGroq(model=model_name, temperature=0.0, max_retries=2, max_tokens=max_tokens)
        method = (
            os.getenv("PARSE_LLM_STRUCTURED_METHOD", "json_schema").strip()
            or "json_schema"
        )
        return llm, method
    raise ValueError(
        "WEIGHT_LLM_SOURCE must be 'groq', 'openrouter', or 'vllm'."
    )


def _aligned_match_names(
    ingredient_names: List[str],
    ingredient_match_names: object,
) -> List[str]:
    """Return a non-empty matching phrase aligned to every display name."""
    if not isinstance(ingredient_match_names, list):
        return list(ingredient_names)
    if len(ingredient_match_names) != len(ingredient_names):
        return list(ingredient_names)
    return [
        str(match_name or "").strip() or ingredient_name
        for ingredient_name, match_name in zip(
            ingredient_names,
            ingredient_match_names,
        )
    ]



_LEADING_DECIMAL_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<quantity>\d+(?:\.\d+)?)\s*(?P<unit>.*)$"
)




def _split_shared_measurement(measurement: str) -> tuple[str, str]:
    """Split an explicit shared quantity equally; discard quantity-free prose."""
    match = _LEADING_DECIMAL_MEASUREMENT_RE.match(str(measurement or ""))
    if not match:
        return "", ""
    half = _format_quantity(float(match.group("quantity")) / 2.0)
    value = f"{half} {match.group('unit').strip()}".strip()
    return value, value


def split_salt_and_pepper_rows(
    ingredient_names: List[str],
    measurements: List[str],
    ingredient_match_names: Optional[List[str]] = None,
    weights: Optional[List[float]] = None,
) -> tuple[List[str], List[str], List[str], Optional[List[float]]]:
    """Split standalone salt-and-pepper rows while keeping arrays aligned.

    Most source rows are quantity-free ("to taste"); those become two blank
    measurements so the weight tool applies its seasoning fallback separately.
    If one numeric quantity/weight covers both foods, divide it equally rather
    than assigning the full amount twice.
    """
    names_out: List[str] = []
    measurements_out: List[str] = []
    match_names_out: List[str] = []
    weights_out: Optional[List[float]] = [] if weights is not None else None
    aligned_match_names = (
        ingredient_match_names
        if ingredient_match_names is not None
        and len(ingredient_match_names) == len(ingredient_names)
        else ingredient_names
    )

    for index, name in enumerate(ingredient_names):
        match_name = aligned_match_names[index]
        parts = _salt_and_pepper_parts(match_name) or _salt_and_pepper_parts(name)
        measurement = measurements[index] if index < len(measurements) else ""
        weight = weights[index] if weights is not None and index < len(weights) else None
        if parts is None:
            names_out.append(name)
            measurements_out.append(measurement)
            match_names_out.append(match_name)
            if weights_out is not None:
                weights_out.append(float(weight or 0.0))
            continue

        salt_name, pepper_name = parts
        split_measurements = _split_shared_measurement(measurement)
        names_out.extend((salt_name, pepper_name))
        measurements_out.extend(split_measurements)
        match_names_out.extend((salt_name, pepper_name))
        if weights_out is not None:
            half_weight = float(weight or 0.0) / 2.0
            weights_out.extend((half_weight, half_weight))

    return names_out, measurements_out, match_names_out, weights_out


class ParsedRecipeText(BaseModel):
    title: str
    ingredient_lines: List[str] = Field(min_length=1)
    directions: List[str]
    total_time: float = Field(ge=0)
    serves: int = Field(ge=0)


_RECIPE_TEXT_SYSTEM_PROMPT = (
    "Split this recipe text into sections. Do not interpret or convert anything.\n"
    "- title: a heading line naming the dish. If the text starts directly with ingredients, "
    "title is '' and that first line is an ingredient, even a bare food name such as "
    "'fudge (optional)' or 'cupcakes (CHOCOLATE)'. Every line of an ingredient-only text is "
    "an ingredient.\n"
    "- ingredient_lines: one string per ingredient, copied word for word with its amount, "
    "unit, parentheses and notes. Keep optional and garnish items. If several ingredients "
    "share one line, separated by commas, give each its own string and keep preparation "
    "words with their ingredient: '200g shrimp, peeled, 2 tbsp butter' -> '200g shrimp, "
    "peeled' and '2 tbsp butter'. Options joined by 'or' stay in one string, even when each "
    "has its own amount: '1 tbsp oil or 1 tbsp butter'. Leave out section headings such as "
    "'For the sauce'. Do "
    "not add an ingredient that only appears in the directions or serving notes.\n"
    "- directions: the method steps as written; [] if there are none.\n"
    "- total_time: total minutes only if the text states it, otherwise 0.\n"
    "- serves: servings only if the text states it, such as 'serves 4' or 'makes 8 "
    "portions', otherwise 0. Never infer it from the ingredient amounts."
)


@tool
def parse_recipe_tool(recipe: str) -> dict:
    """Parses a raw recipe text into structured fields."""
    model_name = (os.getenv("PARSE_LLM") or "llama-3.3-70b-versatile").strip()
    if model_name == "meta-llama/llama-4-maverick-17b-128e-instruct":
        # Legacy value kept in some environments; remap to a model we can serve.
        model_name = "llama-3.1-8b-instant"
    llm, structured_method = _parser_llm(model_name)
    prompt = ChatPromptTemplate.from_messages(
        [("system", _RECIPE_TEXT_SYSTEM_PROMPT), ("human", "Recipe: {recipe}")]
    )
    chain = prompt | llm.with_structured_output(ParsedRecipeText, method=structured_method)
    fallback_chain = prompt | llm.with_structured_output(ParsedRecipeText, method="function_calling")
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            sections = (chain if last_exc is None else fallback_chain).invoke({"recipe": _plain_recipe_text(recipe)})
            break
        except ValidationError as exc:
            last_exc = exc
        except Exception as exc:
            # json_schema is not supported by every Groq model.
            if structured_method == "json_schema" and "response format `json_schema`" in str(exc):
                last_exc = exc
            else:
                raise
    else:
        raise last_exc

    lines = [line.strip() for line in sections.ingredient_lines if line.strip()]
    entries = parse_ingredient_lines(lines)
    names, measurements, originals = prepare_entries_for_weights(entries, lines)
    # ponytail: whole-line lookup; a step the model re-splits stays folded, sentence alignment if that matters.
    source = {_plain_recipe_text(line).strip(): line.strip() for line in recipe.splitlines() if line.strip()}
    return {
        "title": source.get(sections.title.strip(), sections.title),
        "ingredient_names": names,
        "ingredient_match_names": names,
        "measurements": measurements,
        "ingredient_original_texts": originals,
        "directions": [source.get(step.strip(), step) for step in sections.directions],
        "total_time": sections.total_time,
        "serves": sections.serves,
    }


_MEASUREMENT_ONLY_SYSTEM_PROMPT = (
    "Parse these ingredient lines into structured entries: 'name', 'measurement', "
    "'display', and 'note' for each real ingredient. The lines come from a scraper "
    "and don't reliably "
    "line up with real ingredients: sometimes one line is split across two array entries "
    "(a stray word like 'rinsed' or 'chopped' sitting alone), sometimes a section heading "
    "is glued onto the first ingredient of that section, sometimes one line lists several "
    "ingredients together. Reconstruct the REAL ingredients regardless of how the input "
    "was split — merge fragments back together, split glued-together ingredients apart, "
    "and drop section headings ('SLAW', 'For the sauce') and stray connector words that "
    "aren't ingredients on their own.\n\n"
    "A line can also contain a parenthetical aside about an ALTERNATIVE ingredient or "
    "method that is not actually used in this recipe, e.g. '3 tbsp vegetable oil "
    "((4 slices of bacon is the traditional method))' or '1 cup butter (or use "
    "margarine instead)'. That aside is NOT a separate ingredient — never create an "
    "entry for it (no 'bacon' entry, no 'margarine' entry here). If it is a genuine "
    "food choice, keep it in the note of the ingredient the line is actually about "
    "('or margarine'). One entry per real line, not one per mention.\n\n"
    "RULES FOR 'name':\n"
    "- The core food noun, keeping any word that changes what the food actually IS "
    "(a different nutrition identity) — only drop words that describe how it's "
    "prepared or served, not what it is.\n"
    "- Example: '1 cup applesauce, unsweetened' -> name 'applesauce' ('unsweetened' "
    "is a prep/style detail here, not a different food).\n"
    "- Do NOT strip a word if removing it changes the food to something with "
    "meaningfully different nutrition: 'sour cream' is not 'cream' (very different "
    "fat content) — name stays 'sour cream', never bare 'cream'. Likewise keep "
    "'heavy cream', 'whipping cream', 'buttermilk', 'condensed milk', 'evaporated "
    "milk', 'brown sugar' vs 'sugar', 'whole wheat flour' vs 'flour', 'smoked "
    "cheese' vs 'cheese' — these name a different ingredient, not a style of the "
    "same one. When in doubt, keep the word in the name rather than move it to note.\n"
    "- A fat, sugar or salt level changes the nutrition and stays in the name: 'low-fat "
    "cheddar', 'low fat milk', 'lean rashers', 'reduced-salt stock', 'skimmed milk'.\n"
    "- A parenthesis naming the specific kind of a generic food is food identity: "
    "'1 sheet bread (lavash)' -> name 'lavash bread'; '1 cup cheese (gruyere, or emmental, "
    "grated)' -> name 'gruyere cheese', note 'or emmental, grated'; 'cupcakes (CHOCOLATE)' "
    "-> name 'chocolate cupcakes'. A product form also stays in the name: 'olive oil spray', "
    "'lemon twist' and 'spinach wrap' are not 'olive oil', 'lemon' and 'spinach'.\n"
    "- Several named kinds of the same food sharing one amount use the first as the name: "
    "'750g haddock, salmon or cod fillets' -> name 'haddock fillets', measurement '750.0 g', "
    "note 'or salmon or cod'.\n"
    "- Remove brand/manufacturer names, but never drop the edible ingredient: "
    "'Kraft grated parmesan cheese' -> 'parmesan cheese'; 'Coca-Cola' -> 'cola'. "
    "This also applies when the brand is the RETAILER the recipe site itself belongs "
    "to, prefixed onto an otherwise ordinary ingredient throughout the whole recipe "
    "(e.g. every line on a supermarket's own site starting with 'SuperValu ...', "
    "'Tesco ...', 'Dunnes ...') — strip that store-brand prefix the same as any "
    "other brand: 'SuperValu Brown Sugar' -> 'brown sugar'; 'SuperValu Fresh Basil' "
    "-> 'basil' (with 'fresh' in note, per the rule above); 'SuperValu Signature "
    "Tastes Feta' -> 'feta'. Never leave a store/retailer name as part of 'name'. "
    "The brand can also be fused directly into a product-line name rather than "
    "just prefixed onto a generic noun — strip the brand there too and reduce it "
    "to the actual food category: 'Alpro Rice Original' -> 'rice milk' (not "
    "'Alpro Rice Original', not 'Rice Original'). If dropping the brand+marketing "
    "name would leave nothing recognizable as a food, name it by what it actually "
    "is (a drink, a spread, a cheese, etc.), never keep the product's brand name "
    "as a stand-in for that.\n\n"
    "A line can also list several ingredients under ONE shared quantity as a choice "
    "of options, e.g. '1 ½ cups shredded vegetables (zucchini, potato, carrots, bell "
    "pepper, celery, sweet potato or yam)'. Do NOT create a separate entry per "
    "option with no measurement of its own — create ONE entry for the shared item "
    "('vegetables'), give it the shared quantity, and list the options in 'note' "
    "('choose from zucchini, potato, carrots, bell pepper, celery, sweet potato, or "
    "yam'). One real quantity should produce one priced/measured entry, not many "
    "empty ones. This also applies when a heading gives a total for a longer list "
    "with no 'or' at all, e.g. a heading 'SALAD (1.5 cups total per person)' "
    "followed by a comma-separated list 'leafy greens, tomatoes, cucumbers, olives, "
    "onions, garlic, avocado, cheese, beans' with no per-item amounts — this is the "
    "same pattern (one stated total, many named options with no individual "
    "quantities) and must become ONE entry (e.g. name 'salad vegetables', "
    "measurement '1.5 cups', note listing the options), never a guessed number "
    "invented per item. If you cannot find a real quantity for a specific named "
    "option, that option belongs in note, not as its own zero, invented, or "
    "duplicated-total entry.\n\n"
    "A DIFFERENT pattern is a real 'or' alternative WITH ITS OWN quantity attached "
    "to at least one side, e.g. '1 clove garlic, minced or ¼ teaspoon garlic "
    "powder', or '3 green onions, chopped or ¼ cup chopped onion'. This is not a "
    "shared-total list — it means 'use one of these two, not both'. Always "
    "produce EXACTLY ONE entry for it, never two, and never mix the name from one "
    "side with the quantity from the other: the entry uses the FIRST-listed side, and "
    "its note keeps the other side with its own quantity. '1 clove garlic, minced or "
    "¼ teaspoon garlic powder' -> name 'garlic', measurement '1.0 clove', note 'minced, "
    "or ¼ teaspoon garlic powder'. '3 green onions, chopped or ¼ cup chopped onion' -> "
    "name 'green onions', measurement '3.0', note 'chopped, or ¼ cup chopped onion'. Never "
    "output a second entry named 'garlic powder' or 'onion' for these lines, and "
    "never give 'green onions' a cup measurement that actually belongs to the "
    "onion alternative.\n\n"
    "RULES FOR 'note':\n"
    "- Preserve every explicit source qualifier somewhere in the output: food form, "
    "colour, freshness, edible part, preparation and purpose. Keep identity terms "
    "such as 'white sugar', 'all-purpose flour', 'vegetable oil', 'basil leaves' "
    "and 'candy cane' in name; put freshness and purposes such as 'for coating' "
    "in note. When combining repeated ingredient amounts, keep the notes from "
    "ALL contributing lines, including what a reserved portion is used for. "
    "Do not add qualifiers absent from the source.\n"
    "- Everything about HOW the ingredient is prepared or served that does NOT change "
    "what food it is: chopping/cutting style, texture, temperature, 'to taste', 'for "
    "garnish', 'optional', ripeness, freshness, brand. "
    "Example: '1/4 cup fresh thyme, roughly chopped' -> name 'thyme', measurement "
    "'0.25 cup', note 'fresh, roughly chopped'.\n"
    "- Include only notes explicitly supported by the source text. A missing quantity "
    "does not imply 'to taste', 'as needed', 'optional', or 'for garnish'. Never "
    "invent these instructions from the ingredient type or an unspecified amount.\n"
    "- Empty string '' if the line has no such notes: 'whipped cream' -> name "
    "'whipped cream', measurement '', display '', note ''. 'salt' -> name "
    "'salt', measurement '', display '', note ''. Explicit instructions must "
    "still be preserved: 'salt to taste' -> name 'salt', measurement '', note "
    "'to taste'; '1 tbsp sugar (optional)' -> measurement '1.0 tbsp', note "
    "'optional'.\n\n"
    "- Explicit cutting/preparation phrases such as 'freshly grated', 'roughly "
    "chopped', or 'finely minced' belong in note, not name: '1/2 teaspoon freshly "
    "grated nutmeg' -> name 'nutmeg', measurement '0.5 teaspoon', note 'freshly "
    "grated'. Preserve food-identity terms such as 'ground beef', 'evaporated "
    "milk', and 'whole wheat flour'; do not strip them as preparation notes.\n\n"
    "RULES FOR 'display':\n"
    "- The natural, human-readable way to show this quantity in a recipe, exactly as "
    "someone would expect to read it — this is what a person sees, 'measurement' is "
    "what a calculator uses, and they are allowed to differ.\n"
    "- Most of the time they're the same: '2.5 tbsp' display, '2.5 tbsp' measurement.\n"
    "- 'display' is quantity phrasing ONLY — number, unit, size word — never the "
    "ingredient's name and never a brand, even if the source line has them glued "
    "together with no separator other than a dash: some sources format every line "
    "as 'N - Item Name' with no unit (e.g. '0.5 - Banana', '227 g SuperValu "
    "Strawberries'). Extract just the quantity for display, exactly like you would "
    "for measurement: '0.5 - Banana' -> measurement '0.5', display '0.5' (not "
    "'0.5 - Banana'); '227 g SuperValu Strawberries' -> measurement '227.0 g', "
    "display '227 g' (not '227 g SuperValu Strawberries'). The name never belongs "
    "in display.\n"
    "- When 'measurement' had to be computed (a range averaged, a compound quantity "
    "converted and summed, a container count expressing a total), 'display' keeps "
    "the recipe's original, more natural phrasing instead of the computed number: "
    "'4 - 6\" links Hungarian Kolbász' -> measurement '5.0 links', display '4-6 "
    "links'; '2 tbsp + 100ml corn oil' -> measurement '130.0 ml', display '2 tbsp + "
    "100 ml'; '2 cans (15.5 ounces) corn' -> measurement '31.0 oz' (2 x 15.5, the "
    "real total), display '2 cans (15.5 oz each)'.\n"
    "- Empty string '' only when 'measurement' is also empty (nothing to display).\n\n"
    "RULES FOR 'measurement':\n"
    "- A single numeric float followed by the unit (e.g., '2.5 tbsp', '1.0 cup').\n"
    "- Preserve the original unit (tablespoon, teaspoon, ml, g, clove, small, medium, "
    "large) — never drop it.\n"
    "- For countable items with a physical SIZE word (small/medium/large, or a "
    "dimension like '6-inch'), keep that size word in the measurement: '2 small "
    "onions' -> measurement '2.0 small'. This is only for size — a quality, "
    "source, or freshness descriptor (free-range, organic, fresh, kosher) is NOT "
    "a size word and belongs in note, not measurement: '1 free-range egg' -> "
    "measurement '1.0', note 'free-range' (not measurement '1.0 free-range').\n"
    "- A real range of amounts (e.g., '2-3 tbsp', two separate words either side "
    "with the same unit) becomes its mean average ('2.5 tbsp').\n"
    "- '1-N unit' or '1- N unit' immediately followed by a container word (jar, "
    "can, tin, bag, box, package) is NOT a range — it means one container of that "
    "size. Keep N, not an average: '1-28 oz jar tomato juice' -> name 'tomato "
    "juice', measurement '28.0 oz'; '1- 14 oz can beans' -> name 'beans', "
    "measurement '14.0 oz'; '1-24 oz (680 ml) jar tomatoes' -> name 'tomatoes', "
    "measurement '24.0 oz'. Never average the '1' into it.\n"
    "- A container word (can, tin, jar, package, box, bag) with a count but NO "
    "stated size at all is still a real quantity — not knowing the exact oz/g "
    "size of the container is not the same as not knowing the quantity. Keep "
    "the count with the container word as the unit, never blank it: '1 can "
    "low-sodium tomato soup' -> measurement '1.0 can'; '1 package low-sodium "
    "sazon seasoning' -> measurement '1.0 package'; '1 can of low-sodium black "
    "beans' -> measurement '1.0 can'. This applies however the source phrases "
    "it ('1 can X', '1 can of X', 'X (1 can)') and regardless of whether other "
    "text like 'low-sodium' or 'or other beans if desired' sits nearby.\n"
    "- 'M cans/jars/tins/bags (N unit each)' (M is more than 1) means M containers "
    "of N each — the real total quantity is M x N, and 'measurement' must be that "
    "computed total, not just the container count: '2 cans (15.5 ounces) corn' -> "
    "measurement '31.0 oz' (2 x 15.5), not '2.0 cans'. Put the natural phrasing in "
    "'display' instead: display '2 cans (15.5 oz each)'.\n"
    "- The SAME 'M x N unit' pattern is written with a literal 'x' instead of "
    "parentheses just as often, especially for tins/tubs/bags and for individual "
    "meat/fish portions: '1 x 400g tin of black beans' means ONE container of "
    "400g, so measurement is '400.0 g' (1 x 400), not '1.0 g' — do not just grab "
    "the leading '1' and glue the unit onto it, actually multiply: '2 x 400g tins "
    "chopped tomatoes' -> measurement '800.0 g' (2 x 400); '4 x 175g (6oz) chicken "
    "breasts' -> measurement '700.0 g' (4 x 175); '4 x 100g / 4oz chicken breast "
    "fillets' -> measurement '400.0 g' (4 x 100, ignore the redundant '/ 4oz' "
    "restatement of the SAME per-item size, don't treat it as a second quantity). "
    "If the source separately states a drained/net weight that overrides the raw "
    "tin size (e.g. '2 x 400g (drained weight 240g) tins of black beans'), use "
    "that stated override per container instead: measurement '480.0 g' (2 x 240), "
    "not 2 x 400. Put the original 'N x Mg' phrasing in 'display'.\n"
    "- No unit present (e.g., '2 eggs') -> return only the number ('2.0').\n"
    "- Fractions become decimals ('1/2' -> '0.5'). This includes unicode "
    "fraction characters (½ ¼ ¾ ⅓ ⅔ ⅛), whether alone or glued directly to a "
    "whole number with no space: '1½ Tablespoons' -> '1.5 Tablespoons'; '2½ "
    "Tablespoons' -> '2.5 Tablespoons'; '⅓ cup' -> '0.33 cup'. Never leave a "
    "unicode fraction character in the measurement unconverted.\n"
    "- The number and unit are often glued with no space in the source ('800g "
    "potatoes', '250ml milk', '30g yeast') — this is still a real quantity, extract "
    "it the same as if it had a space: '800g potatoes' -> name 'potatoes', "
    "measurement '800.0 g'. Never drop it just because there's no space.\n"
    "- A unit can appear AFTER the ingredient name, including inside a "
    "parenthetical explanation of the leading amount. Attach that explicit "
    "unit to the amount rather than leaving it only in note: '1/2 matcha powder "
    "(level tsp of, green tea also has caffeine)' -> name 'matcha powder', "
    "measurement '0.5 tsp', display '1/2 level tsp', note 'level, green tea also "
    "has caffeine'. '2 sugar (heaped tablespoons)' -> measurement '2.0 "
    "tablespoons', note 'heaped'. Only use a unit that describes this ingredient's "
    "amount; '2 eggs (serve with 1 cup milk)' stays measurement '2.0', never "
    "'2.0 cups'. If the measurement already has its own unit, do not replace it "
    "with a later secondary quantity or an alternative ingredient's unit.\n"
    "- A bare word-quantity with no digit ('pinch of salt', 'a dash of pepper', 'a "
    "handful of herbs') means quantity 1 of that word: 'pinch of salt' -> name "
    "'salt', measurement '1.0 pinch'.\n"
    "- A quantity is still the measurement even when a size, percentage, or other "
    "descriptor sits between the number and the noun, in parentheses or not: "
    "'4 (10-inch) flour tortillas' -> name 'flour tortillas', measurement '4.0', note "
    "'10-inch'; '8 corn tortillas (6-inch)' -> name 'corn tortillas', measurement "
    "'8.0', note '6-inch'; '1/4 cup nonfat or 1% milk' -> name 'milk', measurement "
    "'0.25 cup', note 'nonfat or 1%'. Never drop the leading number because of what "
    "comes after it.\n"
    "- A count number immediately followed, with no dash between them, by a "
    "separate size expressed as a number+unit (e.g. 'N inch') describes each "
    "item's size, not a second quantity — keep the count as the measurement and "
    "put the size in note: '2  6 inch sausage links' -> measurement '2.0', note "
    "'6-inch'.\n"
    "- A real range still averages even with a stray quote/inch mark stuck to the "
    "second number: '4 - 6\" links Hungarian Kolbász' is a range from 4 to 6 -> "
    "measurement '5.0 links'.\n"
    "- Two numbers in different units for the SAME quantity (a unit conversion, "
    "often with the second number/unit in parentheses, e.g. '8 (200) oz (g.)' "
    "meaning '8 oz (200 g)' scrambled by a scraper) are not two separate amounts. "
    "Prefer whichever one is metric (g, kg, ml, l) — it's the more precise, "
    "standard unit — and drop the other: '8 (200) oz (g.) egg noodles' -> name "
    "'egg noodles', measurement '200.0 g'. If neither is metric, keep the first.\n"
    "- A compound quantity written as 'X unit1 + Y unit2' (two amounts added "
    "together, e.g. '2 tbsp + 100ml') is one total quantity. Convert BOTH amounts "
    "to metric (g or ml) using standard conversions (1 tbsp = 15 ml, 1 cup = 240 "
    "ml, 1 tsp = 5 ml, 1 oz = 28 g, 1 lb = 454 g) and sum them in metric — even if "
    "neither original amount was metric — rather than picking one of the "
    "original, non-metric units as the target: '2 tbsp + 100ml corn oil' -> 2 "
    "tbsp is 30 ml, +100 ml = 130 ml -> name 'corn oil', measurement '130.0 ml', "
    "note ''. The exact same compound-addition pattern is also written with the "
    "word 'plus' instead of a '+' symbol — this is NOT a different case, sum it "
    "the same way, never drop the second amount just because there's no '+' "
    "character: '3/4 cup plus 1 tablespoon cashew nuts' -> 3/4 cup is 180 ml, "
    "+15 ml = 195 ml -> name 'cashew nuts', measurement '195.0 ml', note ''. "
    "Round any such computed/converted result to a practical cooking "
    "precision, not raw decimal arithmetic — nearest 5 for a gram/ml amount over "
    "20, nearest 1 for a gram/ml amount under 20, nearest 0.5 for a spoon/cup "
    "count. Never output something like '8.7 tbsp' or '132.4 ml' — round it to "
    "'130.0 ml'.\n"
    "- 'to taste', 'as needed', 'optional', 'for garnish', or any line with no real "
    "quantity -> empty string '' as the measurement. Never '0' or '0.0' for these — a "
    "zero quantity means 'contains none of this ingredient', not 'unspecified amount'. "
    "But these words do NOT override an actual number that IS present in the same "
    "line — 'optional'/'to taste'/'for garnish' only means blank the measurement "
    "when there is no digit or fraction to extract in the first place. If a real "
    "quantity is stated, keep it and put the word in note instead: '1 tbsp brown "
    "sugar (optional)' -> measurement '1.0 tbsp', note 'optional' (not measurement "
    "'').\n"
    "- Some sources format EVERY plain-count ingredient as 'N - Item Name' with a "
    "dash and no unit at all (e.g. '6 - Cherry Tomatoes', '0.5 - Lemon'). Treat N "
    "exactly like any other bare count: '6 - Cherry Tomatoes' -> measurement "
    "'6.0'; '0.5 - Lemon' -> measurement '0.5'. The one exception is when this "
    "same source uses a literal '0' as its placeholder for 'no quantity specified' "
    "on seasoning/condiment items (this is a source-data convention, not a real "
    "zero amount): '0 - Olive Oil', '0 - Black Pepper', '0 - Salt' -> measurement "
    "'' (empty), same as any other no-quantity line. Only literal '0' triggers "
    "this — '0.5', '1', '6', etc. in the same 'N - Item' format are real counts "
    "and must be kept, never blanked.\n"
    "- The 'N - Item Name' format can also have a per-item weight trailing AFTER "
    "the name instead of a unit up front — this is the same 'M containers of N "
    "each' multiplication as elsewhere, just written differently, and the total "
    "still belongs in measurement: '1 - Goji Berries 150g' -> measurement "
    "'150.0 g' (1 x 150, not '1.0 g'); '2 - Salmon Darnes (approx 175g each from "
    "the Fish Counter)' -> measurement '350.0 g' (2 x 175, not '2.0'). Put the "
    "original count in display, and any non-quantity detail (e.g. 'from the Fish "
    "Counter') in note.\n"
    "- A second exception to the same 'N - Item' rule: a loose/ground seasoning "
    "that is measured by volume or weight, not counted by the piece — salt, "
    "ground pepper, a loose ground spice or dried herb (oregano, cumin, chilli "
    "flakes, etc.) — cannot sensibly have a bare count of '1' or '2': there is no "
    "such thing as '1 salt' or '2 oregano'. When one of these appears in the "
    "bare 'N - Item' format with no real unit, the N is not a usable quantity "
    "even though it isn't literally '0' — treat it the same as a genuine "
    "no-quantity line: measurement '' (empty), note '' unless the source explicitly "
    "provides a note. '1 - Salt' -> name 'salt', measurement '', note ''. "
    "Do NOT infer 'to taste'. Do NOT apply this to an "
    "ingredient that genuinely comes in discrete pieces, even if it's a "
    "seasoning — 'bay leaf', 'garlic clove', a whole chilli, a cinnamon stick, "
    "'sprigs' of an herb — those keep their real bare count as normal ('1 - Bay "
    "Leaf' -> measurement '1.0').\n"
    "- Never return the full ingredient line, or any other prose, as the measurement. "
    "Only a number, optionally with a unit or size word, or an empty string. If no "
    "quantity can be extracted, return '', not the source text."
)


class IngredientEntry(BaseModel):
    # Empty names (section headings) are dropped by _is_valid_name, not rejected for the whole recipe.
    name: str
    # No default on these three: a field with a Python-side default is not
    # marked "required" in the JSON schema handed to the model for function
    # calling, so a model/backend under load can omit the key entirely
    # instead of emitting an explicit empty string — Pydantic then silently
    # fills the default, masking a real extraction failure as "correctly
    # blank." Seen in practice: ~40% of a large batch came back with
    # measurement/display omitted outright once request volume ramped up
    # (a different backend route via OpenRouter, most likely), and retrying
    # didn't help since every attempt from that backend did the same thing.
    # Requiring the key (still allowing an empty string as its value) forces
    # the model to explicitly commit to "blank" rather than silently skip it.
    measurement: str
    display: str
    note: str


class ParsedIngredientLines(BaseModel):
    ingredients: List[IngredientEntry] = Field(min_length=1)


class IngredientChoiceExtraction(BaseModel):
    is_choice: bool
    alternatives: List[str]


_CHOICE_SYSTEM_PROMPT = (
    "Identify mutually exclusive ingredient choices joined by or. Return only complete "
    "alternative text spans, not parsed fields. Every alternative must be an independently "
    "intelligible full ingredient. Repeat any shared quantity and head food noun into every "
    "alternative. If a noun appears after the final option and applies to earlier options, "
    "copy it to them. Preserve different quantities. Instructions like 'or until tender' "
    "are not choices."
)


def _is_instructional_or(text: str) -> bool:
    return bool(re.search(r"\bor\s+(?:until|when|as)\b", text, re.IGNORECASE))


_BARE_MEASUREMENT_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")
_SIZE_MEASUREMENT_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s+(?:(?:extra|very)[ -]+)?(?:small|medium|large)(?:-sized)?\s*$",
    re.IGNORECASE,
)
_DISPLAY_COUNT_UNIT_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s+"
    r"(?:[a-z-]+\s+){0,4}"
    r"(cloves?|stalks?|sticks?|slices?|sprigs?|leaves?|fillets?|heads?|"
    r"bulbs?|bunch(?:es)?|cans?|jars?|packets?|packages?|sheets?|shells?|"
    r"pieces?|pods?|rashers?|cobs?|cubes?|breasts?|thighs?|steaks?|wrappers?|scoops?|"
    r"bags?|tubs?|tins?|packs?|boxes|box|blocks?|(?:(?:extra|very)[ -]+)?(?:small|medium|large))\b",
    re.IGNORECASE,
)
_DISPLAY_SPRAY_COUNT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(sprays?)\b", re.IGNORECASE)
_SOURCE_PORTION_UNIT_RE = re.compile(
    r"\b(pinch(?:es)?|dash(?:es)?|handfuls?|splash(?:es)?)\b",
    re.IGNORECASE,
)
_SOURCE_PORTION_UNIT_ALIASES = {
    "pinch": "pinch", "pinches": "pinch",
    "dash": "dash", "dashes": "dash",
    "handful": "handful", "handfuls": "handful",
    "splash": "splash", "splashes": "splash",
}
_SOURCE_VOLUME_WORD_RE = re.compile(
    r"\b(?:cups?|tablespoons?|tbsp|teaspoons?|tsp|ml|millilit(?:er|re)s?|"
    r"lit(?:er|re)s?)\b",
    re.IGNORECASE,
)
_SOURCE_DIMENSION_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*[- ]?\s*(cm|inch(?:es)?|in)\b",
    re.IGNORECASE,
)
_SOURCE_COUNT_UNIT_RE = re.compile(
    r"^\s*(?:(?:small|medium|large|baby|fresh)\s+)?"
    r"(bulbs?|leaves?|pieces?|pods?|rashers?|cobs?|heads?|spears?|wedges?|"
    r"sprigs?|stalks?|slices?|strips?|cubes?|bunch(?:es)?|whole|"
    r"tubs?|pots?|jars?|tins?|bags?|packets?|packs?|boxes|box|blocks?)\b",
    re.IGNORECASE,
)
_SOURCE_COUNT_UNIT_ALIASES = {
    "bulb": "bulb", "bulbs": "bulb",
    "leaf": "leaf", "leaves": "leaf",
    "piece": "piece", "pieces": "piece",
    "pod": "pod", "pods": "pod",
    "rasher": "rasher", "rashers": "rasher",
    "cob": "cob", "cobs": "cob",
    "head": "head", "heads": "head",
    "spear": "spear", "spears": "spear",
    "wedge": "wedge", "wedges": "wedge",
    "sprig": "sprig", "sprigs": "sprig",
    "stalk": "stalk", "stalks": "stalk",
    "slice": "slice", "slices": "slice",
    "strip": "strip", "strips": "strip",
    "cube": "cube", "cubes": "cube",
    "bunch": "bunch", "bunches": "bunch",
    "whole": "whole",
    "tub": "tub", "tubs": "tub", "pot": "pot", "pots": "pot", "jar": "jar", "jars": "jar",
    "tin": "tin", "tins": "tin", "bag": "bag", "bags": "bag", "packet": "packet", "packets": "packet",
    "pack": "pack", "packs": "pack", "box": "box", "boxes": "box", "block": "block", "blocks": "block",
}
_SOURCE_EXPLICIT_MEASUREMENT_UNIT_RE = re.compile(
    r"^\s*(fl\.?\s*oz|fluid ounces?|cups?|tablespoons?|tbsp|teaspoons?|tsp|"
    r"dessertspoons?|ml|millilit(?:er|re)s?|dl|decilit(?:er|re)s?|"
    r"lit(?:er|re)s?|quarts?|qts?|pints?|pts?|grams?|g|kg|kilograms?|"
    r"ounces?|oz|pounds?|lbs?)\b",
    re.IGNORECASE,
)
_SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES = {
    "cup": "cup", "cups": "cup",
    "tablespoon": "tablespoon", "tablespoons": "tablespoon", "tbsp": "tablespoon",
    "teaspoon": "teaspoon", "teaspoons": "teaspoon", "tsp": "teaspoon",
    "dessertspoon": "dessertspoon", "dessertspoons": "dessertspoon",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml",
    "millilitre": "ml", "millilitres": "ml",
    "dl": "dl", "deciliter": "dl", "deciliters": "dl",
    "decilitre": "dl", "decilitres": "dl",
    "l": "litre", "liter": "litre", "liters": "litre",
    "litre": "litre", "litres": "litre",
    "quart": "quart", "quarts": "quart", "qt": "quart", "qts": "quart",
    "pint": "pint", "pints": "pint", "pt": "pint", "pts": "pint",
    "fl oz": "fluid ounce", "fl. oz": "fluid ounce",
    "fluid ounce": "fluid ounce", "fluid ounces": "fluid ounce",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
}

_DISPLAY_VOLUME_TERM_RE = re.compile(
    r"(?P<qty>(?:\d+\s*)?[½⅓⅔¼¾⅛⅜⅝⅞]|(?:\d+\s+)?\d+(?:\.\d+)?(?:/\d+)?)\s*"
    r"(?:(?:heaped|extra)\s+)?"
    r"(?P<unit>cups?|tablespoons?|tbsp|teaspoons?|tsp|dessertspoons?|"
    r"ml|millilit(?:er|re)s?|dl|decilit(?:er|re)s?|lit(?:er|re)s?|l)\b",
    re.IGNORECASE,
)
_DISPLAY_VOLUME_ML = {
    "cup": 240.0, "cups": 240.0,
    "tablespoon": 15.0, "tablespoons": 15.0, "tbsp": 15.0,
    "teaspoon": 5.0, "teaspoons": 5.0, "tsp": 5.0,
    "dessertspoon": 10.0, "dessertspoons": 10.0,
    "ml": 1.0, "milliliter": 1.0, "milliliters": 1.0,
    "millilitre": 1.0, "millilitres": 1.0,
    "dl": 100.0, "deciliter": 100.0, "deciliters": 100.0,
    "decilitre": 100.0, "decilitres": 100.0,
    "l": 1000.0, "liter": 1000.0, "liters": 1000.0,
    "litre": 1000.0, "litres": 1000.0,
}
_MEASUREMENT_ML_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:ml|millilit(?:er|re)s?)\s*$",
    re.IGNORECASE,
)
_SOURCE_EACH_MASS_RE = re.compile(
    r"\b(?:about|approx(?:imately)?\.?|roughly)?\s*"
    r"(\d+(?:\.\d+)?)\s*(g|grams?|kg|kilograms?|oz|ounces?)\s+each\b",
    re.IGNORECASE,
)
_SOURCE_EACH_MASS_FACTORS = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "oz": 28.349523125, "ounce": 28.349523125, "ounces": 28.349523125,
}
_FEW_COUNT_UNIT_RE = re.compile(
    r"^\s*(?:a\s+)?few\s+(sprigs?|leaves|stalks?|slices?|pieces?)\b",
    re.IGNORECASE,
)
_SOURCE_NOTE_MASS_RE = re.compile(
    r"^\s*\(?\s*(?:about\s+)?(\d+(?:\.\d+)?)\s*(g|grams?|kg|kilograms?|oz|ounces?)\b",
    re.IGNORECASE,
)
_SOURCE_TOTAL_MASS_RE = re.compile(
    r"\b(?:around|about|approx(?:\.|imately)?|roughly|in total|total)\s+(\d+(?:\.\d+)?)\s*(g|grams?|kg|kilograms?)\b(?!\s*(?:each|per|apiece))",
    re.IGNORECASE,
)
_COUNT_MEASUREMENT_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:fillets?|breasts?|pieces?|thighs?|steaks?|chops?|small|medium|large)?\s*$", re.IGNORECASE)
_ALTERNATIVE_SPLIT_RE = re.compile(r"(?:^|\s)or\s+|;")
_IMPLIED_ONE_NOTE_UNIT_RE = re.compile(
    r"^\s*(?:a\s+|one\s+)?(?:small\s+|large\s+|fresh\s+)?"
    r"(bunch|handful|pinch|dash|splash|sprinkling)\b",
    re.IGNORECASE,
)


def _display_volume_ml(display: str) -> float | None:
    """Return explicit source volume, including simple added quantities."""
    # "1 tablespoon cornflour combined with 2 tablespoons water": only the first term is this row.
    display = re.split(r"\b(?:combined|mixed|dissolved|made|blended|whisked|stirred)\s+(?:with|in|into)\b", display)[0]
    matches = list(_DISPLAY_VOLUME_TERM_RE.finditer(display))
    if not matches or display[:matches[0].start()].strip():
        return None
    for previous, current in zip(matches, matches[1:]):
        if not re.fullmatch(
            r"\s*,?\s*(?:\+|and|plus)\s*",
            display[previous.end():current.start()],
        ):
            return None
    total = 0.0
    for match in matches:
        quantity = _deterministic_quantity(match.group("qty"))
        if quantity is None:
            return None
        total += quantity * _DISPLAY_VOLUME_ML[match.group("unit").lower()]
    return total


_STOCK_CUBE_LINE_RE = re.compile(
    r"^\s*(?P<qty>\d+(?:\.\d+)?|[½¼¾⅓⅔])\s+(?:a\s+)?"
    r"(?:(?!ml\b|litres?\b|l\b|pints?\b|cups?\b|g\b|kg\b|oz\b|tbsp\b|tsp\b|tablespoons?\b|teaspoons?\b)[a-z-]+\s+){0,4}?(?:stock\s+|bouillon\s+)?cubes?\b",
    re.IGNORECASE,
)


def restore_stock_cube_measurement(name: str, source_text: str) -> str | None:
    """"1 stock cube dissolved in 800 ml water": the primary item is the cube.

    The parser sometimes reads the cube count as ml ("1.0 ml") or drops the
    unit. The dissolving water is nutritionally nil; the cube carries the
    sodium. A cube that only appears in an "or" alternative never matches, as
    the pattern is anchored at the start of the line.
    """
    if not re.search(r"stock|bouillon|broth", name, re.IGNORECASE):
        return None
    match = _STOCK_CUBE_LINE_RE.match(source_text or "")
    if not match:
        return None
    quantity = _deterministic_quantity(match.group("qty"))
    return None if quantity is None else f"{_format_quantity(quantity)} cube"


_PLAIN_BIRD_NAME_RE = re.compile(
    r"^(?:(?:fresh|organic|free[- ]range)\s+)*(chicken|duck)$", re.IGNORECASE
)
_WHOLE_BIRD_EVIDENCE_RE = re.compile(
    r"\bwhole\b|\bfryer\b|\bbroiler\b|\bsized\s+chicken\b|\b(?:medium|large|small)[- ]sized\b", re.IGNORECASE
)
_BIRD_PART_EVIDENCE_RE = re.compile(
    r"drumstick|thigh|breast|\blegs?\b|\bwings?\b|boneless|bone-in|ground|minced|\bcups?\b",
    re.IGNORECASE,
)
_SKINLESS_RE = re.compile(r"skinless|skinned|skin (?:removed|off)|skin and fat removed", re.IGNORECASE)


def restore_whole_bird_name(entry: dict) -> str | None:
    """Rename a plain "chicken"/"duck" row to "whole [skinless] chicken" when the
    source text says it is a whole bird, so the weight tool applies the edible
    yield to its bone-in purchase weight. Bird parts and cooked meat keep their name.
    """
    match = _PLAIN_BIRD_NAME_RE.match(str(entry.get("name") or "").strip())
    if not match:
        return None
    text = " ".join(str(entry.get(k) or "") for k in ("measurement", "display", "note"))
    if not _WHOLE_BIRD_EVIDENCE_RE.search(text) or _BIRD_PART_EVIDENCE_RE.search(text):
        return None
    skinless = "skinless " if _SKINLESS_RE.search(text) else ""
    return f"whole {skinless}{match.group(1).lower()}"


_MEASUREMENT_QTY_UNIT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*$")
_RANGE_OR_COMPOUND_RE = re.compile(r"\bto\b|\bor\b|\bplus\b|\+|\bx\b|\d\s*[-–]", re.IGNORECASE)


def restore_leading_quantity_from_source(measurement: str, source_line: str, display: str) -> str | None:
    """Correct a parsed quantity that disagrees with the raw source line's leading
    "<qty> <unit>" when both use the same standard unit ("1 ½ cups" parsed as
    2.5 cup). Ranges, alternatives and compound quantities are left alone. The
    parser's own ``display`` must lead with the same quantity as the source line,
    which confirms the fuzzy line assignment picked this row and not a sibling.
    """
    parsed = _MEASUREMENT_QTY_UNIT_RE.match(str(measurement or ""))
    if not parsed or _RANGE_OR_COMPOUND_RE.search(source_line or ""):
        return None
    quantity, end = _leading_quantity_value_and_end(str(source_line or "").casefold())
    unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_RE.match(str(source_line or "").casefold()[end:])
    display_quantity, _ = _leading_quantity_value_and_end(str(display or "").casefold())
    if (
        quantity is None
        or not unit
        or display_quantity is None
        or abs(display_quantity - quantity) > 0.001
    ):
        return None
    source_unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES.get(re.sub(r"\s+", " ", unit.group(1)).strip())
    parsed_unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES.get(parsed.group(2).lower())
    parsed_quantity = float(parsed.group(1))
    if (
        source_unit is None
        or source_unit != parsed_unit
        or abs(parsed_quantity - quantity) <= max(0.01, quantity * 0.03)
        or not 0.2 <= parsed_quantity / quantity <= 5
    ):
        return None
    return f"{_format_quantity(quantity)} {source_unit}"


_BOUILLON_NAME_RE = re.compile(r"^(?:low[- ]sodium\s+)?(?:(?:chicken|beef|vegetable|fish)\s+)?bouillon$", re.IGNORECASE)
_LOW_SODIUM_NOTE_RE = re.compile(r"low[- ]sodium|low[- ]salt|reduced[- ]salt", re.IGNORECASE)


def restore_bouillon_form(entry: dict) -> tuple[str, str] | None:
    """Name the physical form of a bare "bouillon" row from its unit.

    "1 cube" and "2 teaspoons" of bouillon are dry concentrate (cube / granules); only
    a volume of liquid is the prepared broth. An explicit low-sodium note is carried into
    the name so the matcher abstains instead of assigning regular-sodium values.
    """
    name = str(entry.get("name") or "").strip()
    if not _BOUILLON_NAME_RE.match(name):
        return None
    measurement = str(entry.get("measurement") or "").strip()
    note = _ALTERNATIVE_SPLIT_RE.split(str(entry.get("note") or "").casefold())[0]
    new_name, new_measurement = name, measurement
    quantity = _BARE_MEASUREMENT_NUMBER_RE.match(measurement)
    if quantity and re.search(r"\bcubes?\b", note):
        new_name, new_measurement = f"{name} cube", f"{quantity.group(1)} cube"
    elif re.search(r"\b(?:teaspoons?|tsp|tablespoons?|tbsp)\s*$", measurement, re.IGNORECASE):
        new_name = f"{name} granules"
    if _LOW_SODIUM_NOTE_RE.search(note) and not _LOW_SODIUM_NOTE_RE.search(new_name):
        new_name = f"low sodium {new_name}"
    return None if (new_name, new_measurement) == (name, measurement) else (new_name, new_measurement)


_GREASING_RE = re.compile(r"\bgreas(?:e|ing)\b|\bnon-?stick\b|\bbrush(?:ing)?\b|\boil (?:the|a) (?:tin|tray|pan|dish|baking)", re.IGNORECASE)


def restore_greasing_measurement(entry: dict) -> str | None:
    """A blank-quantity oil whose note/display says it greases a tin or pan is "1 greasing"."""
    if str(entry.get("measurement") or "").strip():
        return None
    name = str(entry.get("name") or "")
    if not re.search(r"\boil\b", name, re.IGNORECASE) or re.search(r"spray", name, re.IGNORECASE):
        return None
    text = " ".join(str(entry.get(k) or "") for k in ("display", "note"))
    return "1 greasing" if _GREASING_RE.search(text) else None


def restore_explicit_source_measurement(
    entry: dict, allow_quantity_override: bool = True
) -> tuple[str | None, str | None]:
    """Restore a unit visibly present in parser display/note output.

    ``allow_quantity_override`` lets the parser's own ``display`` correct a wrong
    bare quantity ("1/2 teaspoon" parsed as 1.0); pass False when ``display`` is
    a raw source line matched by fuzzy assignment, where a different quantity
    means a different line.

    Returns ``(measurement, reason)`` or ``(None, None)``. This intentionally
    does not infer a unit from the ingredient noun; bare counts such as
    ``3 eggs`` remain measurement ``3.0`` and are interpreted by weighting.
    """
    measurement = str(entry.get("measurement") or "").strip()
    display = str(entry.get("display") or "").strip().casefold().replace("⁄", "/")

    # Correct impossible parser arithmetic such as "¾ cup" -> "0.75 ml"
    # and compound quantities such as "¼ cup + 3 tbsp" -> "130 ml".
    current_ml = _MEASUREMENT_ML_RE.match(measurement)
    source_ml = _display_volume_ml(display)
    if current_ml and source_ml is not None:
        parsed_ml = float(current_ml.group(1))
        if abs(parsed_ml - source_ml) > max(1.0, source_ml * 0.03):
            return f"{_format_quantity(source_ml)} ml", "source_volume"

    # "4 fillets, around 520g": an approximate total mass outranks a count unit the weight tool may not know.
    total = _SOURCE_TOTAL_MASS_RE.search(str(entry.get("note") or ""))
    if total and _COUNT_MEASUREMENT_RE.match(measurement):
        grams = float(total.group(1)) * _SOURCE_EACH_MASS_FACTORS[total.group(2).lower()]
        return f"{_format_quantity(grams)} g", "source_total_mass"

    bare = _BARE_MEASUREMENT_NUMBER_RE.match(measurement)
    sized = _SIZE_MEASUREMENT_RE.match(measurement)
    if measurement and not bare and not sized:
        return None, None

    quantity = (bare or sized).group(1) if (bare or sized) else "1"
    name = str(entry.get("name") or "").casefold()
    # Only the primary item counts: an "or <alternative>" clause carries the
    # alternative's units and masses, never this ingredient's.
    note = _ALTERNATIVE_SPLIT_RE.split(str(entry.get("note") or "").strip().casefold())[0].strip()

    each_mass = _SOURCE_EACH_MASS_RE.search(str(entry.get("note") or "").casefold()) if (bare or sized) else None
    if each_mass:
        total_grams = (
            float(quantity)
            * float(each_mass.group(1))
            * _SOURCE_EACH_MASS_FACTORS[each_mass.group(2).lower()]
        )
        return f"{_format_quantity(total_grams)} g", "source_each_mass"

    # A source note that explicitly names the counted object is more useful
    # than a size-only display such as "8 large".
    count_unit = _SOURCE_COUNT_UNIT_RE.match(note) if (bare or sized) else None
    if count_unit:
        unit = _SOURCE_COUNT_UNIT_ALIASES[count_unit.group(1).lower()]
        return f"{quantity} {unit}", "source_unit"

    few_unit = _FEW_COUNT_UNIT_RE.match(display) if (bare or not measurement) else None
    if few_unit:
        # "a few" is conventionally three.
        return f"3 {_SOURCE_COUNT_UNIT_ALIASES[few_unit.group(1).lower()]}", "source_unit"

    if bare:
        note_mass = _SOURCE_NOTE_MASS_RE.match(note) or re.search(
            r"\(\s*(\d+(?:\.\d+)?)\s*(g|grams?|kg|oz|ounces?)\b", note
        )
        if note_mass and (
            float(quantity) == 1 or re.match(r"\s*\(?\d+(?:\.\d+)?\s*g\s*/", note)
        ):
            grams = float(note_mass.group(1)) * _SOURCE_EACH_MASS_FACTORS[note_mass.group(2).lower()]
            return f"{_format_quantity(grams)} g", "source_note_mass"

    implied_note_unit = _IMPLIED_ONE_NOTE_UNIT_RE.match(note) if not measurement else None
    if implied_note_unit:
        return f"1.0 {implied_note_unit.group(1).lower()}", "source_unit"

    # Normalize only the leading numeric spelling; source fractions still carry size/count units.
    count_quantity, count_end = _leading_quantity_value_and_end(display)
    count_display = (
        f"{_format_quantity(count_quantity)}{display[count_end:]}"
        if count_quantity is not None else display
    )
    display_count = _DISPLAY_SPRAY_COUNT_RE.match(count_display) or _DISPLAY_COUNT_UNIT_RE.match(count_display)
    if display_count and display_count.group(2).lower() in {"cube", "cubes"} and re.search(
        r"[,;(]|\b(?:or|with|serve|without|not)\b", count_display[:display_count.start(2)]
    ):
        display_count = None
    if (
        (bare or sized or not measurement)
        and display_count
        and (
            not measurement
            or abs(float(quantity) - float(display_count.group(1))) < 0.001
        )
    ):
        unit = re.sub(r"[-\s]+", " ", display_count.group(2).lower())
        # The greedy descriptor prefix can consume "extra" before "large".
        if unit in {"small", "medium", "large"}:
            modifier = re.search(r"\b(extra|very)[ -]+$", count_display[:display_count.start(2)])
            if modifier:
                unit = f"{modifier.group(1)} {unit}"
        return (
            f"{_format_quantity(float(display_count.group(1)))} {unit}",
            "display_count",
        )

    # Recover a standard unit visibly attached to the same quantity in the
    # display text. The parser occasionally kept the number but dropped an
    # explicit "teaspoon", "cup", or metric unit.
    # Restore only the unit of a simple range already averaged by the model.
    # A container size or an alternative is not a range for this purpose.
    if bare:
        first, end = _match_quantity_token(display, 0)
        separator = _RANGE_SEP_RE.match(display, end) if first is not None else None
        if separator:
            second, end = _match_quantity_token(display, separator.end())
            range_unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_RE.match(display[end:])
            range_display = f"{quantity} {display[end:].lstrip()}"
            range_count = _DISPLAY_SPRAY_COUNT_RE.match(range_display) or _DISPLAY_COUNT_UNIT_RE.match(range_display)
            count_unit = range_count.group(2).lower() if range_count else None
            # Size words are not count nouns; compound-size ranges need separate handling.
            if count_unit and re.search(r"\b(?:small|medium|large)\b", count_unit):
                count_unit = None
            if (
                second is not None and (range_unit or count_unit)
                and abs(float(quantity) - (first + second) / 2) < 0.001
                and not re.search(r"\b(?:or|plus|can|cans|jar|jars|tin|tins|package|packages|bottle|bottles)\b", display)
                and "+" not in display
            ):
                if range_unit:
                    unit = re.sub(r"\s+", " ", range_unit.group(1).lower()).strip()
                    unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES[unit]
                else:
                    unit = _SOURCE_COUNT_UNIT_ALIASES.get(count_unit, count_unit)
                return f"{quantity} {unit}", "source_range_unit"

    display_quantity, display_quantity_end = _leading_quantity_value_and_end(display)
    explicit_unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_RE.match(
        display[display_quantity_end:]
    )
    if (
        (bare or sized or not measurement)
        and display_quantity is not None
        and (
            not measurement
            or abs(float(quantity) - display_quantity) < 0.001
            or (bare and allow_quantity_override)
        )
        and explicit_unit
        and not re.search(r"\bor\s+(?:cooking\s+)?spray\b", display)
    ):
        raw_unit = re.sub(r"\s+", " ", explicit_unit.group(1).lower()).strip()
        unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES[raw_unit]
        return f"{_format_quantity(display_quantity)} {unit}", "source_unit"

    dimension = _SOURCE_DIMENSION_RE.match(display)
    if dimension:
        unit = "inch" if dimension.group(2).lower() in {"in", "inches"} else dimension.group(2).lower()
        return f"{dimension.group(1)} {unit}", "source_unit"

    if bare and ("thumb-sized" in display or "thumb sized" in display):
        return f"{quantity} piece", "source_unit"

    if bare or sized:
        if "lettuce" in name and re.search(r"\bleaves?\b", note):
            return f"{quantity} leaf", "source_unit"
        if "ginger" in name and ("thumb-sized" in note or "thumb sized" in note):
            return f"{quantity} piece", "source_unit"

    # Oil spray is often represented by the source as just "spray". Do not
    # use alternatives such as "1/4 teaspoon oil or cooking spray" as proof.
    if (
        ("oil" in name or "cooking spray" in name)
        and "spray" in display
        and " or " not in display
        and not _SOURCE_VOLUME_WORD_RE.search(display)
    ):
        return f"{quantity} spray", "source_unit"

    match = _SOURCE_PORTION_UNIT_RE.search(display)
    if match:
        unit = _SOURCE_PORTION_UNIT_ALIASES[match.group(1).lower()]
        return f"{quantity} {unit}", "source_unit"

    # Notes are less reliable than display text. Accept only a leading unit
    # attached to an otherwise empty or numeric display ("1" + "handful").
    if not display or _BARE_MEASUREMENT_NUMBER_RE.match(display):
        explicit_unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_RE.match(note)
        if explicit_unit:
            raw_unit = re.sub(r"\s+", " ", explicit_unit.group(1).lower()).strip()
            unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES[raw_unit]
            return f"{quantity} {unit}", "source_unit"
        match = _SOURCE_PORTION_UNIT_RE.match(note)
        if match:
            unit = _SOURCE_PORTION_UNIT_ALIASES[match.group(1).lower()]
            return f"{quantity} {unit}", "source_unit"
        if ("oil" in name or "cooking spray" in name) and re.match(
            r"^(?:\w+\s+){0,2}spray\b", note
        ):
            return f"{quantity} spray", "source_unit"

    return None, None


# A measurement is either empty, or a number optionally followed by one unit/size
# word. Anything else (echoed prose, a whole ingredient line) is a prompt-rule
# violation we catch in code rather than trust the model to have followed.
# The space before the unit is OPTIONAL ("300g", not just "300 g") — some
# models pass the source's no-space form straight through instead of
# normalizing it, and that's still a valid measurement, not prose to reject.
# The unit/size word can contain a hyphen or apostrophe ("free-range",
# "6-inch") — a prior version of this regex didn't allow that and silently
# blanked correct values like "1.0 free-range" (see git history / session
# notes: this cost hours of misdiagnosis as a model "attention" problem
# before someone checked the raw model output and found it here).
_VALID_MEASUREMENT_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?(?:\s*[a-zA-Z0-9][a-zA-Z0-9 .'-]{0,20})?\s*$"
)


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_SUSPECT_DIGIT_IN_WORD_RE = re.compile(r"[a-zA-Z]\d[a-zA-Z]|[a-zA-Z]{2,}\d\b")

# Characters the pipeline gives real meaning to and must never fold away —
# unicode fractions (the deterministic quantity parser and the prompt's own
# fraction rule both depend on these) and the en/em dash (recognized as a
# range separator). Everything else non-ASCII gets accent-folded below.
_PRESERVE_UNICODE = set("½⅓⅔¼¾⅛⅜⅝⅞–—")


_TYPOGRAPHIC_TO_ASCII = str.maketrans({
    "’": "'", "‘": "'", "“": '"', "”": '"',
})

# Some sources (seen on healthyfood.com) render a mixed fraction as a whole
# number glued to a "stacked" fraction built from superscript digits on both
# sides of a slash, e.g. "1¹⁄³" (meant as "1 1/3") or "1¹/³". Superscript
# digits have a compatibility decomposition straight to the plain ASCII
# digit (NFKD('¹') == '1'), so the generic accent-fold below would silently
# glue them onto the preceding whole number instead of separating them —
# "1¹⁄³" -> "11⁄3", which a model then reads as "eleven thirds" or just "11"
# rather than "1 1/3". Rewrite this shape into a normal spaced ASCII mixed
# number ("1 1/3") before the generic fold runs, since that's the format
# already proven reliable everywhere else in this prompt.
_SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUPERSCRIPT_TO_ASCII = str.maketrans(_SUPERSCRIPT_DIGITS, "0123456789")
_STACKED_FRACTION_RE = re.compile(
    rf"(\d+)([{_SUPERSCRIPT_DIGITS}]+)[/⁄]([{_SUPERSCRIPT_DIGITS}]+)"
)
# Same shape with no leading whole number, e.g. "¹⁄³ cup" (meant as "1/3
# cup") -- also seen on healthyfood.com, and NOT covered by the pattern
# above (which requires a preceding \d+). Left unnormalized, this is a
# confirmed, high-frequency, ~3x-overstatement model failure found via a
# full production audit 2026-09-22: "¹⁄³ cup X" -> measurement "1.0 cup"
# (dropping the denominator entirely, reading only the numerator) in ~19%
# of all flagged audit disagreements on the HealthyFoods dataset. Must run
# AFTER _STACKED_FRACTION_RE so the whole-number case is consumed first and
# this bare pattern only catches what's left.
_BARE_STACKED_FRACTION_RE = re.compile(
    rf"(?<!\d)([{_SUPERSCRIPT_DIGITS}]+)[/⁄]([{_SUPERSCRIPT_DIGITS}]+)"
)


def _normalize_stacked_fractions(text: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        whole, num, den = m.groups()
        return f"{whole} {num.translate(_SUPERSCRIPT_TO_ASCII)}/{den.translate(_SUPERSCRIPT_TO_ASCII)}"

    def bare_repl(m: "re.Match[str]") -> str:
        num, den = m.groups()
        return f"{num.translate(_SUPERSCRIPT_TO_ASCII)}/{den.translate(_SUPERSCRIPT_TO_ASCII)}"

    text = _STACKED_FRACTION_RE.sub(repl, text)
    text = _BARE_STACKED_FRACTION_RE.sub(bare_repl, text)
    # The Unicode FRACTION SLASH (U+2044, "⁄") also appears between plain
    # ASCII digits, not just superscript ones -- e.g. "41⁄2 oz" ("4 1/2 oz"),
    # "1⁄2 teaspoon" (seen in Irish Heart / FoodHero source data). The two
    # patterns above only cover the superscript-digit form; this is the
    # general fallback for any remaining "⁄" once those are handled, since
    # the character is unambiguous (it exists only to mean fraction
    # division) and safe to normalize to a plain "/" everywhere else.
    text = text.replace("⁄", "/")
    return text


def _fold_accents(text: str) -> str:
    text = _normalize_stacked_fractions(text)
    text = text.translate(_TYPOGRAPHIC_TO_ASCII)
    # Seen in practice: accented Latin letters (é, č, š, ž, ...) in an
    # ingredient line reliably trigger a text-corruption glitch in this
    # model's structured-output encoder on longer multi-line recipes — not
    # occasional, reproducible on the same recipe across 5+ retries ("purée"
    # -> "pur3", "domači" -> "doma4i"). Retrying doesn't help a deterministic
    # failure, so the fix is upstream: strip the accent before the model ever
    # sees the character, e.g. "purée" -> "puree", "domači" -> "domaci". This
    # loses the diacritic in the output name, which is an acceptable trade
    # for not corrupting the data outright.
    out = []
    for ch in text:
        if ord(ch) < 128 or ch in _PRESERVE_UNICODE:
            out.append(ch)
            continue
        base = "".join(
            c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
        )
        out.append(base if base.isascii() and base else ch)
    return "".join(out)


def _strip_control_chars(text: str) -> str:
    # Seen in practice on long multi-line calls: the model's own structured-
    # output encoder occasionally mangles a unicode en-dash or curly quote
    # into a stray ASCII control byte (e.g. "500\x1500 g" instead of
    # "500–600 g") — a provider/model encoding glitch, not something a
    # prompt instruction can fix. These fields are short single-line text,
    # so any control character in them is always corruption, never intended.
    return _CONTROL_CHAR_RE.sub("", text)


def _sanitize_measurement(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _VALID_MEASUREMENT_RE.match(text):
        # Loud on purpose: a silent blank here has twice cost hours of
        # misdiagnosis chasing a "model reliability" problem that was
        # actually this regex rejecting a correct value. Anyone re-running
        # a batch sees this in the log instead of an unexplained empty
        # string three fields away.
        print(f"[_sanitize_measurement] rejected non-empty value: {text!r}", flush=True)
        return ""
    return text


# Names that are prep-instruction fragments, not real ingredients, if they
# show up alone as a 'name' — the scraper source sometimes splits a single
# ingredient's clause across array entries, and a stray fragment like this
# should never survive as its own ingredient row.
_BARE_PREP_FRAGMENT_NAMES = {
    "rinsed", "drained", "chopped", "sliced", "diced", "minced", "grated",
    "peeled", "crushed", "shredded", "cubed", "halved", "quartered",
    "trimmed", "deveined", "seeded", "pitted", "cored", "zested", "torn",
    "shaved", "to", "and", "or", "of", "notes", "see notes", "instructions",
    "information", "garnish", "optional",
}

_NO_QUANTITY_NOTE_MARKERS = (
    "to taste", "as needed", "optional", "garnish", "for serving",
    "to serve", "to flavour", "for topping", "for dusting", "for drizzling",
    "for frying",
)


def _is_valid_name(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if text.casefold() in _BARE_PREP_FRAGMENT_NAMES:
        return False
    return True


# The LLM occasionally miscalculates a plain mixed-number quantity (verified
# deterministic per-context, not stochastic: "1 teaspoon ground cinnamon" ->
# "0.5", "1 ½ cups milk" -> "2.5", "2 ½ cups flour" -> "5.0", "1 1/2 lb pork"
# -> "0.75", confirmed correct when the same line is parsed alone or in a
# short excerpt). A small, self-contained quantity parser (`_parse_simple_
# quantity`, below — no dependency on any other module's parsing code)
# cannot make this mistake, so for LINES SIMPLE ENOUGH FOR IT TO HANDLE
# RELIABLY, its number overrides the LLM's. Deliberately narrow: a real
# "or"-alternative with its OWN quantity ("1 clove garlic or ¼ tsp garlic
# powder"), a container-with-per-unit-size ("2 cans (15.5 oz each)"), and
# compound "+" quantities are excluded here because the LLM's richer
# handling of those is what's actually correct — only plain qty+unit lines
# get the deterministic override. "margarine or butter" (two acceptable
# names, one shared quantity, no second number) is NOT excluded — only an
# "or" followed by its own digit/fraction is a real alternative branch.
_HAS_ALTERNATIVE_WITH_QTY_RE = re.compile(
    r"\bor\b\s*[\d½⅓⅔¼¾⅛⅜⅝⅞]", re.IGNORECASE
)
_HAS_PAREN_WITH_DIGIT_RE = re.compile(r"\([^)]*\d[^)]*\)")
# "1-28 oz jar" means one 28 oz jar, not a range from 1 to 28 — this is
# genuinely ambiguous as written text (identical shape to a real range) and
# only the container word disambiguates it, so no quantity parser can get
# this right from the numbers alone; always defer to the LLM (which has an
# explicit prompt rule for it).
_PACKAGE_DASH_RANGE_RE = re.compile(
    r"\d+\s*-\s*\d+[\"']?\s*\w*\s*(cans?|jars?|tins?|bags?|boxes?|packages?|packets?)\b",
    re.IGNORECASE,
)
# "N x Mg" / "N x M oz" (a count of containers/portions times a per-item size,
# e.g. "1 x 400g tin", "4 x 175g (6oz) chicken breasts") is a multiplication
# the simple quantity parser has no concept of — it would just grab the
# leading "N" and silently discard "x M", corrupting the LLM's (correct)
# multiplied total back down to the container count. Always defer to the LLM
# for these, same reasoning as the cans/jars-with-parens exclusion above.
_MULTIPLY_COUNT_RE = re.compile(r"\d+\s*x\s*\d+", re.IGNORECASE)
# "N - Item Name Wg" (a dash-count format with a per-item weight trailing AFTER
# the name, no parens) is the same "count x per-item weight" multiplication as
# above, just shaped differently — the simple parser would grab the leading N
# and ignore the trailing weight entirely, corrupting an LLM answer that
# correctly multiplied them.
_TRAILING_WEIGHT_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*-\s*\D*\d+\s*(g|kg|ml|l|oz|lb)\b", re.IGNORECASE
)


_HAS_PLUS_WORD_RE = re.compile(r"\bplus\b", re.IGNORECASE)


def _is_simple_for_deterministic(line: str) -> bool:
    if "+" in line:
        return False
    if _HAS_PLUS_WORD_RE.search(line):
        return False
    if _HAS_ALTERNATIVE_WITH_QTY_RE.search(line):
        return False
    if _HAS_PAREN_WITH_DIGIT_RE.search(line):
        return False
    if _PACKAGE_DASH_RANGE_RE.search(line):
        return False
    if _MULTIPLY_COUNT_RE.search(line):
        return False
    if _TRAILING_WEIGHT_RE.search(line):
        return False
    return True


def _deterministic_quantity(line: str) -> Optional[float]:
    if not _is_simple_for_deterministic(line):
        return None
    try:
        return _parse_simple_quantity(line)
    except Exception:
        return None


# Common unit-conversion multipliers the LLM may legitimately have applied
# (e.g. "1kg" source -> "1000.0 g" measurement, factor 1000). Deliberately
# excludes small integer factors (2, 3, 4, ...) even though some real
# conversions use them (tbsp-per-cup is 16) — the observed miscalculation
# bugs are themselves small-integer multiples (doubled, halved), so a small
# factor here can't distinguish "legitimate conversion" from "the exact bug
# we're trying to catch." Only factors implausible as a miscalculation are
# included.
_PLAUSIBLE_CONVERSION_FACTORS = (1000, 453.59, 236.588, 240, 28.35, 15, 5)


def _explained_by_unit_conversion(det_value: float, llm_value: float, tol: float = 0.03) -> bool:
    if det_value <= 0:
        return False
    ratio = llm_value / det_value
    for factor in _PLAUSIBLE_CONVERSION_FACTORS:
        if abs(ratio - factor) <= factor * tol or abs(ratio - 1 / factor) <= (1 / factor) * tol:
            return True
    return False


def _format_quantity(value: float) -> str:
    if value == int(value):
        return f"{int(value)}.0"
    text = f"{value:.2f}".rstrip("0")
    return text if not text.endswith(".") else text + "0"


_UNICODE_FRACTION_VALUES = {
    "½": Fraction(1, 2), "⅓": Fraction(1, 3), "⅔": Fraction(2, 3),
    "¼": Fraction(1, 4), "¾": Fraction(3, 4), "⅛": Fraction(1, 8),
    "⅜": Fraction(3, 8), "⅝": Fraction(5, 8), "⅞": Fraction(7, 8),
}


# Self-contained quantity parser — does NOT call into the legacy
# `_split_measurement` (ingredient_weight_tool.py). That function is tuned
# for a different pipeline (portion/weight lookup) and turned out to have
# two real bugs for our purposes: it mis-averages a range when a unicode
# fraction is glued to a digit ("5½-6 lbs" -> wrongly 15.75, should be
# 5.75), and it deliberately returns "1" (meaning "1 container", a
# placeholder for its own downstream size lookup) for bare package-size
# lines like "14 oz can beans" instead of the real 14. Rather than keep
# excluding shapes to work around a function we don't fully control, this
# parses quantities itself — small enough to test exhaustively.
_QUANTITY_TOKEN_RE = re.compile(
    r"(?P<mixed_whole>\d+)\s+(?P<mixed_num>\d+)/(?P<mixed_den>\d+)"
    r"|(?P<spaced_whole>\d+)\s+(?P<spaced_uni>[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"|(?P<glued_whole>\d+)(?P<glued_uni>[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"|(?P<bare_uni>[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"|(?P<frac_num>\d+)/(?P<frac_den>\d+)"
    r"|(?P<decimal>\d+\.\d+)"
    r"|(?P<whole>\d+)"
)


def _match_quantity_token(text: str, pos: int = 0) -> tuple:
    """Match one quantity token at ``pos``. Returns (value, end_pos), or
    (None, pos) if nothing matches there."""
    m = _QUANTITY_TOKEN_RE.match(text, pos)
    if not m:
        return None, pos
    if m.group("mixed_whole"):
        val = float(m.group("mixed_whole")) + float(m.group("mixed_num")) / float(m.group("mixed_den"))
    elif m.group("spaced_whole"):
        val = float(m.group("spaced_whole")) + float(_UNICODE_FRACTION_VALUES[m.group("spaced_uni")])
    elif m.group("glued_whole"):
        val = float(m.group("glued_whole")) + float(_UNICODE_FRACTION_VALUES[m.group("glued_uni")])
    elif m.group("bare_uni"):
        val = float(_UNICODE_FRACTION_VALUES[m.group("bare_uni")])
    elif m.group("frac_num"):
        # A multi-digit numerator with no space before "/" is a glued mixed
        # number written without a space ("11/2" meaning "1 1/2" = 1.5), not
        # a genuine improper fraction (11/2 = 5.5) — recipes don't write
        # improper fractions like that. Single-digit numerator is a plain
        # fraction ("1/2" = 0.5).
        numerator, den = m.group("frac_num"), float(m.group("frac_den"))
        if len(numerator) >= 2:
            val = float(numerator[:-1]) + float(numerator[-1]) / den
        else:
            val = float(numerator) / den
    elif m.group("decimal"):
        val = float(m.group("decimal"))
    elif m.group("whole"):
        val = float(m.group("whole"))
    else:
        return None, pos
    return val, m.end()


_RANGE_SEP_RE = re.compile(r"\s*(-|–|—|\bto\b)\s*", re.IGNORECASE)


def _parse_simple_quantity(line: str) -> Optional[float]:
    """Parse the leading quantity of ``line``: a single number/fraction, or
    an A-B / A to B range (averaged). Returns None if nothing parseable."""
    text = line.strip()
    val1, end1 = _match_quantity_token(text, 0)
    if val1 is None:
        return None
    sep = _RANGE_SEP_RE.match(text, end1)
    if sep:
        val2, end2 = _match_quantity_token(text, sep.end())
        if val2 is not None:
            return (val1 + val2) / 2.0
    return val1
_LEADING_QTY_RE = re.compile(
    r"\s*(?P<whole>\d+)?\s*(?P<uni>[½⅓⅔¼¾⅛⅜⅝⅞])"
    r"|\s*(?P<int>\d+)\s+(?P<num>\d)/(?P<den>\d)"
    r"|\s*(?P<num2>\d+)/(?P<den2>\d+)"
    r"|\s*(?P<dec>\d+\.\d+)"
    r"|\s*(?P<int2>\d+)"
)


def _leading_quantity_value_and_end(text: str) -> tuple:
    """Return (float value, end index of the matched span) for the leading
    quantity of ``text`` — unlike a plain ASCII \\d+ regex, this also
    recognizes unicode fractions ('½ cup', '1 ½ cup') so `display` strings
    (which often use them) can be corrected, not just `measurement`."""
    m = _LEADING_QTY_RE.match(text)
    if not m:
        return None, 0
    if m.group("uni"):
        whole = float(m.group("whole")) if m.group("whole") else 0.0
        return whole + float(_UNICODE_FRACTION_VALUES[m.group("uni")]), m.end()
    if m.group("int"):
        return float(m.group("int")) + float(m.group("num")) / float(m.group("den")), m.end()
    if m.group("num2"):
        return float(m.group("num2")) / float(m.group("den2")), m.end()
    if m.group("dec"):
        return float(m.group("dec")), m.end()
    if m.group("int2"):
        return float(m.group("int2")), m.end()
    return None, 0


_ALIGNMENT_MODIFIERS = frozenset({"whole", "fresh", "dried", "raw", "large", "small", "medium", "ground", "chopped",
                                  "sliced", "low", "fat", "free", "reduced", "light", "extra"})


def _alignment_tokens(text: str, *, entry: bool = False) -> set:
    folded = unicodedata.normalize("NFKD", text.lower())
    words = re.findall(r"[a-z]+", "".join(c for c in folded if not unicodedata.combining(c)))

    def singular(word: str) -> str:
        if len(word) > 4 and word.endswith("oes"):
            return word[:-2]
        if len(word) > 4 and word.endswith("ies"):
            return word[:-3] + "y"
        if len(word) > 5 and word.endswith(("ches", "shes", "sses", "xes")):
            return word[:-2]
        return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word

    tokens = {singular(w) for w in words}
    if entry and tokens - _ALIGNMENT_MODIFIERS:
        tokens -= _ALIGNMENT_MODIFIERS
    return tokens


def align_entries_to_lines(entries: List[dict], lines: List[str]) -> List[Optional[int]]:
    """Source-line index per entry, in order. Parsed entries follow source order, so each entry
    binds to the first unused line at or after the previous binding that contains all its name
    words. Repeated foods therefore bind to their own line; only entries with disjoint names
    (salt, pepper) may share one line. No containing line means None, never a guess."""
    from recipe_wrangler.utils.seasoning_quantities import salt_and_pepper_parts, seasoning_source_name
    # "low salt stock" qualifies the stock; it never binds a salt entry.
    line_tokens = [_alignment_tokens(_QUALIFIED_SALT_RE.sub(" ", line)) for line in lines]
    seasonings = [[seasoning_source_name(part) for part in parts]
                  if (parts := salt_and_pepper_parts(re.sub(r"^\s*(?:a\s+)?pinch\s+of\s+", "", line, flags=re.IGNORECASE))) else []
                  for line in lines]

    def typo_holds(entry_tokens: set, index: int) -> bool:
        # One source word misspelt ("parlsey"): every other word present, the odd one at least 85% similar.
        from difflib import SequenceMatcher
        missing = entry_tokens - line_tokens[index]
        return len(missing) == 1 and len(next(iter(missing))) >= 5 and any(
            SequenceMatcher(None, next(iter(missing)), word).ratio() >= .85 for word in line_tokens[index])

    def holds(entry_tokens: set, kind: Optional[str], index: int) -> bool:
        return entry_tokens <= line_tokens[index] or (kind is not None and kind in seasonings[index])

    bound: List[Optional[int]] = []
    pointer = 0
    previous: Optional[tuple] = None
    for entry in entries:
        tokens = _alignment_tokens(_QUALIFIED_SALT_RE.sub(" ", entry["name"]), entry=True)
        kind = seasoning_source_name(entry["name"])
        found = None
        if tokens:
            if previous is not None and not (tokens & previous[1]) and holds(tokens, kind, previous[0]):
                found = previous[0]
            else:
                for index in range(pointer, len(lines)):
                    if holds(tokens, kind, index):
                        found = index
                        break
                if found is None:
                    found = next((index for index in range(pointer, len(lines)) if typo_holds(tokens, index)), None)
        bound.append(found)
        if found is not None:
            pointer = found + 1
            previous = (found, tokens | (previous[1] if previous and previous[0] == found else set()))
    return bound


_DUAL_UNIT_RE = re.compile(
    r"(?P<metric>\d+(?:\.\d+)?)\s*(?P<munit>kg|g)\s*/\s*(?P<imperial>\d+(?:\s+\d/\d|\.\d+|\s*[½¼¾])?)\s*"
    r"(?P<iunit>oz|ozs|ounces?|lbs?|pounds?)\b", re.IGNORECASE)


def metric_imperial_conflict(line: str) -> Optional[dict]:
    """Flag a g/oz display whose two amounts disagree. 'rounded_display' is within half an ounce
    (the line's metric amount stays authoritative); 'contradiction' needs a human decision."""
    # "60z" is "6oz" typed with a zero for the letter o (a spaced "60 oz" is left to the contradiction check).
    match = _DUAL_UNIT_RE.search(re.sub(r"(?<=\d)0z\b", " oz", _normalize_stacked_fractions(line)))
    if not match:
        return None
    fractions = {"½": .5, "¼": .25, "¾": .75}
    raw = match["imperial"]
    imperial = float(re.match(r"\d+(?:\.\d+)?", raw)[0]) + sum(fractions.get(c, 0) for c in raw)
    mixed = re.search(r"\s(\d)/(\d)$", raw)
    if mixed:
        imperial += int(mixed[1]) / int(mixed[2])
    metric_g = float(match["metric"]) * (1000 if match["munit"].lower() == "kg" else 1)
    unit_g = 453.59237 if match["iunit"].lower().startswith(("lb", "pound")) else 28.349523125
    expected = metric_g / unit_g
    if abs(imperial - expected) <= max(.05 * expected, .02):
        return None
    kind = "rounded_display" if abs(imperial - expected) <= .5 * (16 if unit_g > 400 else 1) else "contradiction"
    return {"kind": kind, "metric": f"{match['metric']} {match['munit'].lower()}",
            "imperial": f"{raw.strip()} {match['iunit'].lower().rstrip('.')}", "expected_imperial": round(expected, 2)}


def attach_source_links(entries: List[dict], lines: List[str]) -> None:
    """Record each entry's source line index; add source_conflict only when the line contradicts itself."""
    for entry, index in zip(entries, align_entries_to_lines(entries, lines)):
        entry["source_line_index"] = index
        conflict = metric_imperial_conflict(lines[index]) if index is not None else None
        if conflict:
            entry["source_conflict"] = conflict


def _assign_lines_to_entries(entries: List[dict], lines: List[str]) -> List[Optional[str]]:
    """One-to-one, greedy-best-match assignment of a source line to each
    entry, so two entries with the same name (e.g. two "brown sugar" lines
    in one recipe) don't both latch onto the same line — each line is
    consumed at most once, best-overlap-first."""
    aligned = align_entries_to_lines(entries, lines)
    if all(index is not None for index in aligned):
        return [lines[index] for index in aligned]
    name_token_sets = [set(re.findall(r"[a-z]+", e["name"].lower())) for e in entries]
    line_token_sets = [set(re.findall(r"[a-z]+", l.lower())) for l in lines]
    assigned: List[Optional[str]] = [None] * len(entries)
    used_lines: set = set()

    pairs = []
    for ei, ntoks in enumerate(name_token_sets):
        for li, ltoks in enumerate(line_token_sets):
            overlap = len(ntoks & ltoks)
            if overlap > 0:
                pairs.append((overlap, ei, li))
    pairs.sort(key=lambda p: -p[0])

    assigned_entries: set = set()
    for overlap, ei, li in pairs:
        if ei in assigned_entries or li in used_lines:
            continue
        assigned[ei] = lines[li]
        assigned_entries.add(ei)
        used_lines.add(li)
    return assigned


_DISCRETIONARY_NOTE_RE = re.compile(
    r"\b(?:to taste|to season|optional|garnish|to garnish|for garnishing|to serve|for serving|"
    r"as needed|for drizzling|for topping|for dusting|to dust)\b",
    re.IGNORECASE,
)


# Grams yielded by one fruit: juice from USDA SR portions, zest by convention (about 1 tbsp lemon, 2 tbsp orange).
_CITRUS_PART_GRAMS = {
    ("lemon", "juice"): 48.0, ("lime", "juice"): 44.0, ("orange", "juice"): 86.0,
    ("lemon", "zest"): 6.0, ("lime", "zest"): 2.0, ("orange", "zest"): 12.0,
}
_CITRUS_FRUIT_RE = re.compile(r"\b(lemon|lime|orange)s?\b", re.IGNORECASE)


def restore_citrus_part(entry: dict, line: str) -> tuple[str, str] | None:
    """'Zest 1 lemon' / 'juice of half a lime' weighed as the zest or juice, not the whole fruit."""
    name = str(entry.get("name") or "").casefold()
    fruit = _CITRUS_FRUIT_RE.fullmatch(name.replace(" zest", "").replace(" rind", "").replace(" peel", "").strip())
    measurement = str(entry.get("measurement") or "").strip()
    if not fruit or (measurement and not _BARE_MEASUREMENT_NUMBER_RE.match(measurement)):
        return None
    cue = f"{name} {entry.get('note') or ''} {line or ''}".casefold()
    part = "zest" if re.search(r"\b(?:zest|rind|peel)\b", name) else "juice" if re.search(r"\bjuice[d]?\b", cue) \
        else "zest" if re.search(r"\b(?:zest|rind)\b", cue) else None
    if part is None:
        return None
    qty = float(measurement) if measurement else 0.5 if re.search(r"\bhalf\b", cue) else 1.0
    spoon = re.search(r"(\d+(?:\.\d+)?)\s*(?:teaspoons?|tsp)\b[^,;]*\bzest\b", cue)
    if spoon and part == "zest":  # "you need 1 teaspoon zest": about 2 g per level teaspoon
        return f"{fruit.group(1).lower()} zest", f"{_format_quantity(float(spoon[1]) * 2.0)} g"
    return f"{fruit.group(1).lower()} {part}", f"{_format_quantity(qty * _CITRUS_PART_GRAMS[fruit.group(1).lower(), part])} g"


_QUALIFIED_SALT_RE = re.compile(r"\b(?:low|reduced|no[- ]added)[- ]salt\b|\bsalt[- ]free\b|\bunsalted\b", re.IGNORECASE)


def drop_phantom_seasoning(entries: List[dict], lines: List[str]) -> List[dict]:
    """Drop a salt entry when no source line asks for salt ("low salt stock" qualifies the stock, it is not salt)."""
    asks_for_salt = any(re.search(r"\bsalt\b", _QUALIFIED_SALT_RE.sub("", line), re.IGNORECASE) for line in lines)
    return [e for e in entries if asks_for_salt or str(e.get("name") or "").strip().casefold() not in {"salt", "sea salt", "table salt"}]


def prepare_entries_for_weights(
    entries: List[dict], source_lines: List[str]
) -> tuple[List[str], List[str], List[str]]:
    """Apply the post-parse repairs the batch path uses; returns names, measurements, originals."""
    lines = [_fold_accents(str(line).strip()) for line in source_lines if str(line).strip()]
    names, measurements, originals = [], [], []
    for entry, line in zip(entries, _assign_lines_to_entries(entries, lines)):
        current = dict(entry)
        source_measurement, _ = restore_explicit_source_measurement(current)
        if source_measurement is not None:
            current["measurement"] = source_measurement
        cube = restore_stock_cube_measurement(current["name"], line or current.get("display") or "")
        if cube is not None:
            current["measurement"] = cube
            if "cube" not in current["name"].casefold():
                current["name"] += " cube"
        grease = restore_greasing_measurement(current)
        if grease is not None:
            current["measurement"] = grease
        bouillon = restore_bouillon_form(current)
        if bouillon is not None:
            current["name"], current["measurement"] = bouillon
        bird = restore_whole_bird_name(current)
        if bird is not None:
            current["name"] = bird
        if line is not None:
            corrected = restore_leading_quantity_from_source(
                current["measurement"], line, current.get("display") or ""
            )
            if corrected is not None:
                current["measurement"] = corrected
            if not _DISCRETIONARY_NOTE_RE.search(current.get("note") or ""):
                recovered, _ = restore_explicit_source_measurement(
                    {**current, "display": line}, allow_quantity_override=False
                )
                if recovered is not None:
                    current["measurement"] = recovered
        # A metric amount cannot come from a line with no digits ("Salt and pepper to taste"): it leaked from another line.
        if line is not None and not re.search(r"\d", line) and re.search(r"^\s*[\d.]+\s*(?:g|kg|ml|l)\b", current["measurement"], re.IGNORECASE):
            current["measurement"] = ""
        # European decimal comma ("0,2 g"); three digits after the comma is a thousands separator, not a decimal.
        decimal = re.match(r"\s*(\d+),(\d{1,2})\s*(g|kg|ml|l)\b", line or "", re.IGNORECASE)
        if decimal:
            current["measurement"] = f"{_format_quantity(float(decimal[1] + '.' + decimal[2]))} {decimal[3].lower()}"
        # "1 red and 1 green pepper": the parser keeps only the first count.
        paired = re.match(r"\s*(\d+)\s+[a-z]+\s+and\s+(\d+)\s+[a-z]+\s+([a-z]+)", line or "", re.IGNORECASE)
        bare = _BARE_MEASUREMENT_NUMBER_RE.match(current["measurement"])
        if (paired and bare and float(bare[1]) == float(paired[1])
                and paired[3].casefold().rstrip("s") == current["name"].casefold().split()[-1].rstrip("s")):
            current["measurement"] = _format_quantity(float(paired[1]) + float(paired[2]))
        # "around 3-4lb / 1.5kg": a single stated metric mass outranks an imperial range.
        metric = re.search(r"/\s*(\d+(?:\.\d+)?)\s*(kg|g)\b", line or "", re.IGNORECASE)
        if metric and re.match(r"^\s*[\d.]+\s*(?:lbs?|pounds?|oz)\b", current["measurement"], re.IGNORECASE) \
                and re.search(r"\d\s*-\s*\d", current.get("display") or ""):
            current["measurement"] = f"{_format_quantity(float(metric[1]) * (1000 if metric[2].lower() == 'kg' else 1))} g"
        # "24 baby potatoes": baby potatoes are small ones, not the medium default.
        if re.search(r"\bbaby\b", f"{current.get('display') or ''} {current.get('note') or ''}", re.IGNORECASE) \
                and re.search(r"\bpotato(?:es)?\b", current["name"], re.IGNORECASE) and _BARE_MEASUREMENT_NUMBER_RE.match(current["measurement"]):
            current["measurement"] = f"{current['measurement'].strip()} small"
        citrus = restore_citrus_part(current, line or "")
        if citrus is not None:
            current["name"], current["measurement"] = citrus
        if not current["measurement"].strip() and _DISCRETIONARY_NOTE_RE.search(current.get("note") or ""):
            current["measurement"] = "optional"
        names.append(current["name"])
        measurements.append(current["measurement"])
        originals.append(line or " ".join(filter(None, [entry.get("display"), entry["name"], entry.get("note")])))
    return names, measurements, originals


_SOURCE_AMOUNT_CUE_RE = re.compile(
    r"\d|[½⅓⅔¼¾⅛⅜⅝⅞]|\b(?:a|an|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|half|quarter|couple|few|several|some|dozen|single|pair|double|triple|"
    r"sprays?|scoops?|ladles?|mugs?|bowls?|cans?|packages?|"
    + "|".join(re.escape(unit) for unit in (
        *_SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES,
        *_SOURCE_COUNT_UNIT_ALIASES,
        *_SOURCE_PORTION_UNIT_ALIASES,
    )) + r")\b",
    re.IGNORECASE,
)


def _clearly_quantity_free(entry: dict, line: str, lines: List[str]) -> bool:
    """Require an unambiguous exact food-name prefix, not fuzzy line assignment."""
    name = str(entry.get("name") or "").strip()
    prefix = re.compile(r"^\s*" + re.escape(name) + r"(?=\s*[,;(]|\s*$)", re.IGNORECASE)
    mention = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
    matches = [source for source in lines if mention.search(source)]
    product_spray = bool(re.search(r"\b(?:cooking|oil)\s+spray\b", line, re.IGNORECASE))
    amount_text = re.sub(r"\b(?:cooking|oil)\s+spray\b", "", line, flags=re.IGNORECASE)
    # “Ice cube” names a food form, not an explicit count of one.
    ice_form = bool(re.fullmatch(r"\s*ice\s+cubes?\s*(?:\(optional\))?\s*", line, re.IGNORECASE)) and name.casefold() in {"ice", "ice cube", "ice cubes"}
    if ice_form:
        amount_text = ""
    return (
        bool(name) and (bool(prefix.match(line)) or ice_form or (product_spray and (name.casefold().endswith("spray") or re.search(r"\boil\b", name, re.IGNORECASE))))
        and len(matches) == 1 and matches[0] == line
        and not _SOURCE_AMOUNT_CUE_RE.search(amount_text)
    )


def _preserve_merged_source_purposes(entries: List[dict], lines: List[str]) -> None:
    """Keep reserved-portion purposes when simple same-unit source rows were summed."""
    for entry in entries:
        name = str(entry.get("name") or "").strip().casefold()
        if not name or sum(e["name"].casefold() == name for e in entries) != 1:
            continue
        food = re.escape(name)
        if name == "flour":
            food = r"(?:all[- ]purpose\s+)?" + food
        portions = []
        for index, line in enumerate(lines):
            quantity, end = _leading_quantity_value_and_end(line)
            source_unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_RE.match(line[end:])
            if quantity is None or not source_unit:
                continue
            food_start = end + source_unit.end()
            tail = re.fullmatch(
                rf"\s*{food}\s*,?\s*(?P<purpose>(?:for|to)\s+[^,;()]+)?\s*(?P<plus>plus)?\s*",
                line[food_start:], re.IGNORECASE,
            )
            if tail and not re.search(r"\b(?:or|not|without|except)\b", line, re.IGNORECASE):
                raw_unit = source_unit.group(1).casefold()
                portions.append((index, quantity, _SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES[raw_unit],
                                 line[:food_start].strip(), tail.group('purpose'), bool(tail.group('plus'))))
        # shortcut: restore only adjacent explicit plus rows with one common unit;
        # compound conversions and ambiguous merges need a richer occurrence representation.
        if len(portions) != 2 or portions[1][0] != portions[0][0] + 1 or not portions[0][-1]:
            continue
        if portions[0][2] != portions[1][2]:
            continue
        current = str(entry.get("measurement") or "")
        quantity, end = _leading_quantity_value_and_end(current)
        current_unit = _SOURCE_EXPLICIT_MEASUREMENT_UNIT_ALIASES.get(current[end:].strip().casefold())
        if quantity is None or current_unit != portions[0][2] or abs(quantity - sum(p[1] for p in portions)) > .005:
            continue
        for _, _, _, amount, purpose, _ in portions:
            if purpose and purpose.casefold() not in entry.get("note", "").casefold():
                entry["note"] = "; ".join(filter(None, [entry.get("note", ""), f"{amount} {purpose}"]))


_CONTAINER_SIZE_RE = re.compile(
    r"\s*(?:(?P<open>\()|(?P<times>x)\s*|(?=\d))\s*(?P<size>[\d\s./⁄½⅓⅔¼¾⅛]+?)[\s-]*"
    r"(?P<unit>fl\.?\s*oz|fluid\s+ounces?|ounces?|oz|lbs?|pounds?|grams?|g|kg|ml|l)\b\.?"
    r"(?(open)\s*\))",
    re.IGNORECASE,
)
_CONTAINER_WORD_RE = re.compile(
    r"\s*(?:\([^)]*\)\s*)?(?:cans?|tins?|jars?|bags?|boxe?s?|packages?|packets?|pkgs?|"
    r"containers?|cartons?|bottles?|tubs?)\b",
    re.IGNORECASE,
)
_CONTAINER_UNITS = {"ounce": "oz", "ounces": "oz", "pound": "lb", "pounds": "lb", "lbs": "lb",
                    "gram": "g", "grams": "g"}


def _container_total(line: str) -> Optional[str]:
    """Total of '3 (14 1/2 ounce) cans' or '2 x 400g tins'; None for anything else."""
    if re.search(r"\bdrained\s+weight\b", line, re.IGNORECASE):
        return None
    text = line.replace("⁄", "/")
    count, end = _leading_quantity_value_and_end(text)
    size_match = _CONTAINER_SIZE_RE.match(text, end) if count else None
    if not size_match:
        return None
    size, size_end = _leading_quantity_value_and_end(size_match.group("size").strip())
    if size is None or size_end != len(size_match.group("size").strip()):
        return None
    if not size_match.group("times") and not _CONTAINER_WORD_RE.match(text, size_match.end()):
        return None
    if re.search(r"\d\s*(?:g|grams?|oz|ounces?)\b[^,]*\bdrained\b", text[size_match.end():], re.IGNORECASE):
        return None
    unit = re.sub(r"\s+", " ", size_match.group("unit").casefold())
    unit = "fl oz" if unit.startswith(("fl", "fluid")) else _CONTAINER_UNITS.get(unit, unit)
    return f"{_format_quantity(count * size)} {unit}"


def _unsum_alternatives(measurement: str, line: str) -> Optional[str]:
    """'5 lbs A or 5 lbs B' is one choice; the model sometimes adds both amounts."""
    sides = re.split(r"\bor\b", line.replace("⁄", "/"), maxsplit=1, flags=re.IGNORECASE)
    current = re.match(r"\s*(\d+(?:\.\d+)?)(.*)", measurement)
    if len(sides) != 2 or not current:
        return None
    first, _ = _leading_quantity_value_and_end(sides[0].strip())
    second, _ = _leading_quantity_value_and_end(sides[1].strip())
    if first is None or second is None or abs(float(current.group(1)) - (first + second)) > 0.01:
        return None
    return _format_quantity(first) + current.group(2)


def _apply_deterministic_overrides(entries: List[dict], lines: List[str]) -> None:
    """Ground quantities/units in assigned source lines; preserve ambiguous cases."""
    _preserve_merged_source_purposes(entries, lines)
    assigned_lines = _assign_lines_to_entries(entries, lines)
    for entry, line in zip(entries, assigned_lines):
        if line is None:
            continue
        # Dry garlic/onion forms are food identities, not merely preparation instructions.
        name = str(entry.get("name") or "").casefold()
        if name in {"garlic", "onion"} and sum(
            bool(re.search(rf"\b{name}\b", source, re.IGNORECASE)) for source in lines
        ) == 1:
            primary = re.split(r"[,;(]|\b(?:or|not|no|without|instead|except|excluding)\b", line, maxsplit=1, flags=re.IGNORECASE)[0]
            form = re.search(
                rf"\b(?:{name}\s+(?:powder|granules)|(?:powdered|granulated)\s+{name})\b",
                primary, re.IGNORECASE,
            )
            if form:
                entry["name"] = re.sub(r"\s+", " ", form.group(0).lower())
        container_total = _container_total(line)
        if container_total is not None:
            entry["measurement"] = container_total
            continue
        unsummed = _unsum_alternatives(entry.get("measurement") or "", line)
        if unsummed is not None:
            entry["measurement"] = unsummed
            continue
        measurement = entry.get("measurement") or ""
        if _clearly_quantity_free(entry, line, lines):
            entry["measurement"] = ""
            entry["display"] = ""
            continue
        source_entry = dict(entry)
        source_entry["display"] = line
        restored, _ = restore_explicit_source_measurement(source_entry)
        if restored is not None and (
            not measurement
            or _BARE_MEASUREMENT_NUMBER_RE.match(measurement)
            or _SIZE_MEASUREMENT_RE.match(measurement)
        ):
            entry["measurement"] = restored
            continue
        m = re.match(r"\s*(\d+(?:\.\d+)?)", measurement)
        if not m:
            continue
        llm_value = float(m.group(1))
        det_value = _deterministic_quantity(line)
        if det_value is None:
            continue
        if abs(det_value - llm_value) <= 0.05:
            continue
        if _explained_by_unit_conversion(det_value, llm_value):
            # The deterministic parser doesn't reliably extract the unit for
            # glued no-space lines ("800g" -> unit comes back wrong), so we
            # can't compare units directly. Instead: if the LLM's number is
            # explained by a real conversion factor from the deterministic
            # number (e.g. "1kg" -> det=1, llm=1000, factor=1000 for kg->g),
            # the LLM did a legitimate unit conversion — trust it, don't
            # overwrite with the un-converted deterministic value.
            continue
        new_num = _format_quantity(det_value)
        entry["measurement"] = new_num + measurement[m.end():]
        if entry.get("display"):
            disp_value, disp_end = _leading_quantity_value_and_end(entry["display"])
            if disp_value is not None and abs(disp_value - llm_value) <= 0.05:
                entry["display"] = new_num + entry["display"][disp_end:]


def _split_salt_and_pepper_entries(entries: List[dict]) -> List[dict]:
    """Apply the same deterministic split to standalone reparser entries."""
    split_entries: List[dict] = []
    for entry in entries:
        parts = _salt_and_pepper_parts(entry.get("name", ""))
        if parts is None:
            split_entries.append(entry)
            continue
        measurements = _split_shared_measurement(entry.get("measurement", ""))
        for name, measurement in zip(parts, measurements):
            split_entry = dict(entry)
            split_entry["name"] = name
            split_entry["measurement"] = measurement
            split_entry["display"] = measurement
            split_entries.append(split_entry)
    return split_entries


def _attach_postparse_choices(
    entries: List[dict],
    lines: List[str],
    llm: Any,
    structured_method: str,
) -> None:
    """Attach validated choices when parser input/output remains row-aligned."""
    if len(entries) != len(lines) and len(lines) != 1:
        return
    prompt = ChatPromptTemplate.from_messages(
        [("system", _CHOICE_SYSTEM_PROMPT), ("human", "{line}")]
    )
    chain = prompt | llm.with_structured_output(
        IngredientChoiceExtraction, method=structured_method
    )
    pairs = [(entries[0], lines[0])] if len(lines) == 1 else list(zip(entries, lines, strict=True))
    for entry, line in pairs:
        if not re.search(r"\bor\b", line, re.IGNORECASE):
            continue
        if _is_instructional_or(line):
            continue
        extracted = chain.invoke({"line": line})
        if not extracted.is_choice or len(extracted.alternatives) < 2:
            continue
        alternatives: list[dict] = []
        for text in extracted.alternatives:
            parsed = parse_ingredient_lines([text], max_attempts=2, _choices=False)
            if len(parsed) != 1:
                alternatives = []
                break
            alternative = parsed[0]
            alternative["original_text"] = text
            restored, _ = restore_explicit_source_measurement(
                {**alternative, "display": text}
            )
            if restored is not None:
                alternative["measurement"] = restored
            alternatives.append(alternative)
        if len(alternatives) < 2:
            continue
        entry["choice"] = {
            "connector": "or",
            "alternatives": alternatives,
            "selected_index": 0,
            "selection_policy": "first_listed_default",
        }
        if len(lines) == 1:
            entries[:] = [entry]


def parse_ingredient_lines(
    ingredient_lines: List[str],
    max_attempts: int = 2,
    *,
    _choices: bool = False,
) -> List[dict]:
    """Parse raw lines into ingredient dictionaries with optional choices.

    One call per recipe. The input lines are scraper array entries, which do
    NOT reliably correspond 1:1 to real ingredients (fragments split across
    entries, section headings glued onto the first ingredient of a section,
    several ingredients listed on one line) — the model is told to
    reconstruct real ingredients rather than preserve the input's line
    boundaries. Output count is therefore not checked against input count;
    instead each returned entry is validated on its own: a name that's just
    a bare prep-instruction fragment ("rinsed", "chopped") is dropped rather
    than kept as a fake ingredient, and a measurement that isn't a real
    number+unit (echoed prose, a whole line) is blanked rather than trusted.

    This does not touch any existing database record; it produces fresh,
    standalone data meant to be reviewed/saved locally first.

    Raises after ``max_attempts`` failed attempts (network error, schema
    rejection, or empty result) rather than returning nothing.
    """
    lines = [_fold_accents(str(l).strip()) for l in ingredient_lines if str(l).strip()]
    if not lines:
        raise ValueError("ingredient_lines must contain at least one non-empty line")

    model_name = (os.getenv("PARSE_LLM") or "llama-3.3-70b-versatile").strip()
    llm, structured_method = _parser_llm(model_name)

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", _MEASUREMENT_ONLY_SYSTEM_PROMPT),
            ("human", "Ingredient lines:\n{lines}{schema_feedback}"),
        ]
    )
    lines_text = "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines))
    chain = prompt | llm.with_structured_output(ParsedIngredientLines, method=structured_method)
    fallback_chain = prompt | llm.with_structured_output(ParsedIngredientLines, method="function_calling")

    last_exc: Optional[Exception] = None
    schema_feedback = ""
    for attempt in range(max_attempts):
        try:
            active_chain = fallback_chain if structured_method != "function_calling" and last_exc is not None else chain
            result = active_chain.invoke({"lines": lines_text, "schema_feedback": schema_feedback})
            if result is None:
                raise ValueError("structured output call returned no result")
        except Exception as exc:
            last_exc = exc
            if isinstance(exc, ValidationError):
                schema_feedback = (
                    "\n\nThe previous response failed schema validation. Return 'ingredients' "
                    "as an array of objects, each with string fields 'name', 'measurement', "
                    "'display', and 'note'. Never return ingredient lines as strings. "
                    "Use empty strings for unspecified fields; do not invent amounts or notes."
                )
            continue

        entries = []
        for entry in result.ingredients:
            if not _is_valid_name(entry.name):
                continue
            measurement = _sanitize_measurement(entry.measurement)
            display = _strip_control_chars(entry.display.strip())
            parsed_entry = {
                "name": _strip_control_chars(entry.name.strip()),
                "measurement": measurement,
                "display": display,
                "note": _strip_control_chars(entry.note.strip()),
            }
            restored, _ = restore_explicit_source_measurement(parsed_entry)
            if restored is not None:
                parsed_entry["measurement"] = restored
            elif not measurement:
                # A display value must not outlive an invalid/empty
                # measurement unless it supplied the unit used above.
                parsed_entry["display"] = ""
            entries.append(parsed_entry)
        if not entries:
            last_exc = ValueError("no valid ingredient entries after filtering")
            continue
        # A call can return a structurally valid result where every single
        # measurement is empty with no "to taste"/"optional" reason for it —
        # seen in practice on calls that clearly had real quantities in the
        # source (retrying the identical input succeeded cleanly). That's
        # not "no valid entries" (name/note filtering above wouldn't catch
        # it) but it's still a bad result worth one more attempt rather than
        # accepting silently.
        unexplained_empty = sum(
            1 for e in entries
            if not e["measurement"] and not any(m in e["note"].lower() for m in _NO_QUANTITY_NOTE_MARKERS)
        )
        # Only worth retrying if the source actually had a digit somewhere to
        # get right — a recipe whose lines are genuinely all bare names with
        # no quantity at all (e.g. "Carniolan sausages" / "horseradish" /
        # "mustard" / "fresh bread") legitimately produces all-empty
        # measurements every time, and forcing a retry there just burns
        # max_attempts until it raises for no reason.
        any_line_has_digit = any(c.isdigit() for line in lines for c in line)
        if unexplained_empty == len(entries) and len(entries) > 1 and any_line_has_digit:
            last_exc = ValueError("every measurement came back empty with no to-taste/optional reason")
            continue
        # Another encoder glitch seen on longer multi-line calls: instead of a
        # stray control byte (handled above), an accented letter sometimes
        # comes back as a plain digit substituted into the middle of the word
        # ("purée" -> "pur3", "domači" -> "doma4i", "crème fraîche" ->
        # "cr3me fra1che"). No real English/food word has a digit fused
        # directly onto letters like that, so it's a reliable corruption
        # signal — worth a retry rather than shipping a mangled name.
        if any(_SUSPECT_DIGIT_IN_WORD_RE.search(e["name"]) or _SUSPECT_DIGIT_IN_WORD_RE.search(e["note"]) for e in entries):
            last_exc = ValueError("digit fused into a word, likely corrupted accented character")
            continue
        _apply_deterministic_overrides(entries, lines)
        if _choices:
            _attach_postparse_choices(entries, lines, llm, structured_method)
        return _split_salt_and_pepper_entries(entries)

    raise RuntimeError(f"parse_ingredient_lines failed after {max_attempts} attempts") from last_exc


@tool
def parse_ingredient_lines_tool(ingredient_lines: List[str]) -> dict:
    """Parse lines into ingredients.

    Produces fresh standalone data (name/measurement/note per line) — does
    not read or write any database record.
    """
    return {"ingredients": parse_ingredient_lines(ingredient_lines)}


def Recipe_Parser_Node(state: RecipeState) -> RecipeState:
    """
    Node that converts raw recipe text in state into structured fields 
    (title, ingredients, measurements, directions, total_time, serves).
    """
    debug = bool(state.debug)

    result = parse_recipe_tool.invoke({"recipe": state.raw_recipe})

    state.title = result["title"]
    state.ingredient_names = result["ingredient_names"]
    state.ingredient_match_names = _aligned_match_names(
        state.ingredient_names,
        result.get("ingredient_match_names"),
    )
    state.measurements = result["measurements"] or []
    (
        state.ingredient_names,
        state.measurements,
        state.ingredient_match_names,
        _,
    ) = split_salt_and_pepper_rows(
        state.ingredient_names,
        state.measurements,
        state.ingredient_match_names,
    )
    state.directions = result["directions"]
    from recipe_wrangler.matching.source_evidence import unique_source_lines
    source_lines=[line.strip() for line in (state.raw_recipe or '').splitlines() if line.strip()]
    state.ingredient_original_texts=result.get("ingredient_original_texts") or unique_source_lines(state.ingredient_names,source_lines)
    state.total_time = result["total_time"]
    trusted_serves = getattr(state, "trusted_serves", None)
    state.serves = trusted_serves or result["serves"]

    
    if debug:
        print("[Recipe_Parser_Node] Updated State Keys:", list(state.model_dump().keys()))
        
    return state
