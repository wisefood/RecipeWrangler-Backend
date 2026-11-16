"""Recipe revision and exact canonical occurrence binding shared by consumers."""

from fractions import Fraction
import math
import re
import unicodedata

from recipe_wrangler.repositories.matching_store import digest
from recipe_wrangler.utils.neo4j_utils import run_query


def source_line_key(line: str) -> str:
    """Normalize display fractions/filler and a verified redundant unit amount."""
    def fraction(match):
        value = unicodedata.normalize('NFKC', match[2]).replace('⁄', '/')
        return (match[1] + ' ' if match[1] else '') + value
    text = re.sub(r'(?:(\d)\s*)?([½⅓⅔¼¾⅛⅜⅝⅞])', fraction, line).casefold().strip()
    # Display damage such as '1 0.5 teaspoon' for '1 ½ teaspoon' (spoon units only).
    text = re.sub(r'(?<![\w.])(\d+)\s+(0\.(?:25|5|75))(?=\s*(?:tsp|tbsp|teaspoons?|tablespoons?)\b)',
        lambda m: format(int(m[1]) + float(m[2]), '.12g'), text)
    # Normalize written metric volume before checking redundant imperial units.
    text = re.sub(r'\bmillilit(?:re|er)s?\b', 'ml', text)
    text = re.sub(r'(?<=\d)mls\b', 'ml', text)
    text = re.sub(r'\blit(?:re|er)s?\b', 'l', text)
    text = re.sub(r'\blitress\b', 'l', text)
    number = r'(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)'
    # Unit alternatives can occur inside a parenthetical item weight too.
    dual = re.compile(r'(?P<primary>(?P<value>' + number + r')\s*(?P<unit>kg|g|ml|l))\s*/\s*'
        r'(?P<other>' + number + r')\s*(?P<other_unit>fl\s*oz|pints?|ozs?|ounces?|lbs?|pounds?)\b\.?')
    def verified_primary(match):
        amount = lambda value: sum(float(Fraction(part)) for part in value.split())
        primary = amount(match['value']) * (1000 if match['unit'] in {'kg', 'l'} else 1)
        unit = match['other_unit']
        if match['unit'] in {'kg', 'g'}:
            factors = [28.349523125] if unit.startswith(('oz', 'ounce')) else [453.59237] if unit.startswith(('lb', 'pound')) else []
        else:
            # Living Lab dual-unit displays use the imperial pint; allowing a
            # US-pint rescue can hide a conflicting source amount.
            factors = [568.26125] if unit.startswith('pint') else [28.4130625, 29.5735295625] if unit.startswith('fl') else []
        return match['primary'] if any(
            math.isclose(primary, amount(match['other']) * factor, rel_tol=.02)
            or abs(primary - amount(match['other']) * factor)
                <= max(primary * .08, 4 if primary <= 50 and match['unit'] in {'kg', 'g'} else 0,
                       10 if primary <= 100 and match['unit'] in {'ml', 'l'} else 0)
            for factor in factors) else match[0]
    text = dual.sub(verified_primary,text)
    # Equivalent fractions/decimals and unit spellings are display differences;
    # the amount, ingredient identity, negation and cooking state stay in the key.
    text = re.sub(r'(?<![\w.])' + number,
        lambda m: format(sum(float(Fraction(part)) for part in m[0].split()),'.12g'), text)
    for pattern,unit in [(r'grams?','g'),(r'teaspoons?|tsp','tsp'),
                         (r'tablespoons?|tbsp','tbsp'),(r'litres?|liters?','l')]:
        text = re.sub(r'(?<=\d)\s*(?:'+pattern+r')\b\.?',unit,text)
        text = re.sub(r'\b(?:'+pattern+r')\b\.?',unit,text)
    text = re.sub(r'('+number+r')\s*(kg|g|ml|l|tsp|tbsp)\b',r'\1\2',text)
    units = r'kg|g|ml|l|tsp|tbsp|sticks?|sprigs?|bunch(?:es)?|dessertspoons?|cups?|cloves?|slices?|leaves|fillets?|tins?|cans?|pots?|pinches?|handfuls?'
    text = re.sub(r'(' + number + r'\s*(?:'+units+r'))\s+of\s+', r'\1 ', text)
    text = re.sub(r'\b(pinch|dash|juice|zest|small amount)\s+of\s+', r'\1 ', text)
    text = re.sub(r'\b(pot|tin|can|jar)\s+of\s+', r'\1 ', text)
    text = re.sub(r'\b(\d+(?:\.\d+)?)-inch\b', r'\1 inch', text)
    text = re.sub(r'\b(cans?|tins?|bottles?|pots?|jars?|inch(?:es)?)\s+of\s+', r'\1 ', text)
    text = re.sub(r'\b(tsp|tbsp)\s+of\s+', r'\1 ', text)
    text = re.sub(r'\b(\d+(?:\.\d+)?)\s+of\s+a\b', r'\1 a', text)
    text = re.sub(r'\b(ml|litres?)s\b', r'\1', text)
    text = re.sub(r'\b(handful|bunch|slices?|serving|head|chunks?|piece)\s+of\s+', r'\1 ', text)
    text = re.sub(r'(?<=[a-z])-(?=[a-z])',' ',text)
    text = text.replace('&',' and ')
    return ' '.join(text.split())



def _source_name_words(value: str) -> list[str]:
    normalized = unicodedata.normalize('NFKD', value.casefold())
    normalized = ''.join(char for char in normalized if not unicodedata.combining(char))
    return re.findall(r'[a-z]+', normalized)


def _source_resembles_name(name: str, line: str) -> bool:
    """Single-word name that differs from a line word only by a typo (ratio >= .85)."""
    from difflib import SequenceMatcher
    words = _source_name_words(name)
    return len(words) == 1 and len(words[0]) >= 5 and any(
        SequenceMatcher(None, words[0], word).ratio() >= .85 for word in _source_name_words(line))


def _source_contains_name(name: str, line: str) -> bool:
    name_words, line_words = _source_name_words(name), _source_name_words(line)
    return bool(name_words) and any(line_words[index:index + len(name_words)] == name_words
        for index in range(len(line_words) - len(name_words) + 1))


def split_seasoning_bindings(ingredients, source_lines: list[str]) -> list[dict] | None:
    """Bind an unquantified salt/pepper split to its unique literal source line.

    Every source line must remain accounted for. This recovers source text only;
    it never changes a saved ingredient, amount, composition or observation.
    """
    from collections import defaultdict
    from recipe_wrangler.utils.seasoning_quantities import salt_and_pepper_parts,seasoning_source_name
    keys=[source_line_key(line) for line in source_lines]
    if len(set(keys))!=len(keys):return None
    positions={key:i for i,key in enumerate(keys)}
    groups=defaultdict(list);bindings=[]
    for ingredient in ingredients:
        line=ingredient.original_text or (ingredient.source_evidence.source_line if ingredient.source_evidence else '')
        recovered=not bool(line)
        if recovered:
            kind=seasoning_source_name(ingredient.name)
            if kind is None:
                # Ordinary missing lines and explicit seasoning splits can coexist.
                # Full coverage and group validation below still require a bijection
                # except for the two proven parts of one unmeasured seasoning line.
                candidates=[line for line in source_lines if _source_contains_name(ingredient.name, line)]
            else:
                candidates=[line for line in source_lines if (parts:=salt_and_pepper_parts(line))
                    and kind in [seasoning_source_name(part) for part in parts]]
            if len(candidates)!=1:return None
            line=candidates[0]
        key=source_line_key(line)
        if key not in positions:return None
        groups[key].append(ingredient)
        bindings.append({'profile_position':ingredient.position,'source_position':positions[key],
            'source_text':source_lines[positions[key]],'method':'unique_complete_source_line',
            'source_text_recovered':recovered})
    if set(groups)!=set(keys):return None
    split=False
    for key,items in groups.items():
        if len(items)==1:continue
        parts=salt_and_pepper_parts(source_lines[positions[key]])
        if (len(items)!=2 or parts is None or
                sorted(seasoning_source_name(item.name) or '' for item in items)!=
                sorted(seasoning_source_name(part) for part in parts)):return None
        split=True
        for binding in bindings:
            if binding['source_position']==positions[key]:binding['method']='explicit_split_seasoning_source_line'
    return bindings if split else None


def unambiguous_missing_source_bindings(ingredients, source_lines: list[str]) -> list[dict] | None:
    """Recover blanks only when complete lines and remaining names form a bijection."""
    if len(ingredients) != len(source_lines):
        return None
    missing = [index for index, item in enumerate(ingredients)
               if not (item.original_text or (item.source_evidence.source_line if item.source_evidence else ''))]
    if not missing:
        return None
    keys = [source_line_key(line) for line in source_lines]
    if len(set(keys)) != len(keys):
        return None
    positions = {key: index for index, key in enumerate(keys)}
    bound = {}
    for index, item in enumerate(ingredients):
        if index in missing:
            continue
        saved = item.original_text or (item.source_evidence.source_line if item.source_evidence else '')
        position = positions.get(source_line_key(saved))
        if position is None or position in bound.values():
            return None
        bound[index] = position
    remaining = set(range(len(source_lines))) - set(bound.values())
    if len(remaining) != len(missing):
        return None
    for index in missing:
        matches = [position for position in remaining
                   if _source_contains_name(ingredients[index].name, source_lines[position])]
        if not matches and len(missing) == 1 and len(remaining) == 1:
            matches = [position for position in remaining
                       if _source_resembles_name(ingredients[index].name, source_lines[position])]
        if len(matches) != 1 or matches[0] in bound.values():
            return None
        bound[index] = matches[0]
    if len(set(bound.values())) != len(source_lines):
        return None
    return [{'profile_position': item.position, 'source_position': index,
             'source_text': source_lines[index],
             'method': 'unique_missing_source_line' if position in missing
                       else 'unique_complete_source_line',
             'source_text_recovered': position in missing}
            for position, item in enumerate(ingredients) for index in [bound[position]]]


def recipe_revision(recipe: dict) -> str:
    return digest({key: recipe.get(key) for key in (
        'recipe_id','title','instructions','original_ingredients','ingredients','serves','status')})


def projected_occurrences(recipe_id: str, run_id: str, source: str) -> list[dict]:
    """Read the current canonical occurrence projection, never raw fragment order."""
    return [dict(row) for row in run_query('''
        MATCH (r:Recipe)-[:HAS_MATCHING_OCCURRENCE]->(o:MatchingIngredientOccurrence)
        WHERE toString(r.recipe_id)=$recipe AND o.run_id=$run AND o.nutrition_source=$source AND o.current=true
        RETURN o.position AS position,o.ingredient AS ingredient,o.original_text AS original_text,
            o.weight_g AS weight_g,o.quantity_input_hash AS quantity_input_hash,
            o.source_recipe_revision AS source_recipe_revision
        ORDER BY position''',{'recipe':recipe_id,'run':run_id,'source':source})]


def graph_source_lines(recipe_ids: list[str]) -> dict[str, list[str]]:
    """Full archived ingredient lines per recipe; their order is not the parsed order."""
    return {row['rid']:[str(line) for line in row['lines']] for row in run_query('''
        UNWIND $ids AS rid MATCH (r:Recipe {recipe_id:rid})
        OPTIONAL MATCH (r)-[rel:HAS_INGREDIENT_ORIGINAL]->(o:Ingredients_original)
        WITH rid,rel,o ORDER BY coalesce(rel.position,2147483647)
        RETURN rid,[v IN collect(coalesce(o.original_text,o.name)) WHERE v IS NOT NULL AND trim(toString(v))<>''] AS lines''',
        {'ids':list(recipe_ids)})}
