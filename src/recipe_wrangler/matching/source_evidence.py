"""Preserve uniquely anchored source descriptions and explicit quantity evidence."""
from __future__ import annotations

from fractions import Fraction
from collections import Counter
import re

from recipe_wrangler.matching.evaluation.source_quantity import name_tokens

_NUMBER=r'(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)'
_MEASURE=re.compile(r'(?<!\w)(?:'+_NUMBER+r'|[¼½¾⅓⅔⅛⅜⅝⅞])\s*(?:kg|g|grams?|ml|litres?|liters?|cups?|tablespoons?|tbsp|teaspoons?|tsp)\b',re.I)


def quantity_source_fragment(name: str, line: str) -> str:
    """Separate concatenated scrape ingredients only with one complete identity hit."""
    from recipe_wrangler.matching.evaluation.annotation_experiment import prepare_phrase
    from recipe_wrangler.utils.food_identity import compare_identities
    notes = [match.span() for match in _MASS_NOTE.finditer(line)]
    measures=[match for match in _MEASURE.finditer(line)
              if not any(start <= match.start() < end for start, end in notes)]
    if len(measures)<2 or re.search(r'\b(?:each|per|or|dissolved)\b',line,re.I):return line
    fragments=[]
    for i,measure in enumerate(measures):
        fragment=line[measure.start():measures[i+1].start() if i+1<len(measures) else len(line)].strip()
        identity=prepare_phrase(fragment)
        identity=re.sub(r'\boats\s+oatmeal\b','oats',identity,flags=re.I)
        identity=re.sub(r'\buncooked\b','raw',identity,flags=re.I)
        if compare_identities(name,identity).relation in {'exact','synonym','likely_equivalent'}:
            fragments.append(fragment)
    return fragments[0] if len(fragments)==1 else line


_METRIC=r'(\d+(?:\.\d+)?)\s*(kg|g|grams?)\b'
_PACK=r'(?:cans?|tins?|jars?|packs?|packets?|pouch(?:es)?|sachets?|bottles?|tubs?)\b'

_MASS_NOTE = re.compile(
    r'\(\s*(?:(?:about|around|approximately)\s+)?' + _NUMBER + r'\s*(?:kg|g|grams?)\b'
    r'(?:\s*/\s*' + _NUMBER + r'\s*(?:ozs?|ounces?|lbs?|pounds?)\.?)?\s*\)', re.I
)
_PREPARATION_TAIL = re.compile(
    r'(?:,\s*|\s+)(?:(?:and\s+)?(?:(?:finely|thinly|well)\s+)?'
    r'(?:chopped|diced|sliced|quartered|deseeded|washed|peeled|trimmed|scrubbed|rinsed|drained|grated)'
    r'\s*[,;.]?\s*)+$', re.I
)


def _quantity_identity_name(name: str) -> str:
    """Compare food identities without pure mass annotations or preparation tails."""
    name = _MASS_NOTE.sub(' ', name)
    name = re.sub(r'^\s*(?:mini|small|medium|large|individual)\s+', '', name, flags=re.I)
    name = _PREPARATION_TAIL.sub('', name).strip(' ,.;')
    # A potato's variety changes composition selection, not ownership of its mass.
    name = re.sub(r'\bnew\s+(?=potato(?:es)?\b)', '', name, flags=re.I)
    return ' '.join(name.split())


def _stated_pack_or_total_mass(name: str, text: str) -> float | None:
    """Mass stated for a whole pack or the whole line: "2 x 400g cans", "½ 400g can", "4 breasts, 520g", "2oz/50g sultanas"."""
    if len(re.findall(r'(?<![\w.])'+_METRIC,text))!=1:return None
    if re.search(r'\b(?:each|per|or)\b|\d\s*[-–]\s*\d|\band\b(?!\s+(?:rinsed|drained|chopped|sliced|diced)\b)',text):return None
    if not any(word[:4] in text for word in re.findall(r'[a-z]+',name.casefold())):return None
    grams=lambda m:float(m[-2])*(1000 if m[-1]=='kg' else 1)
    if m:=re.match(r'^('+_NUMBER+r')\s*[x×]\s*'+_METRIC,text):
        return float(sum(Fraction(part) for part in m[1].split()))*grams(m.groups())
    if m:=re.match(r'^(\d+/\d+)\s+(?:an?\s+)?'+_METRIC+r'\s+'+_PACK,text):
        return float(Fraction(m[1]))*grams(m.groups())
    if m:=re.match(r'^'+_METRIC+r'\s+'+_PACK,text):
        return grams(m.groups())
    if m:=re.match(r'^\d+(?:\.\d+)?\s+[^,/()]+[,/]\s*'+_METRIC,text):
        return grams(m.groups())
    # A count followed by an explicitly stated approximate total is still
    # source-backed mass, not the count interpreted as grams.
    if m:=re.match(r'^\d+(?:\.\d+)?\s+[^()]+?(?:,\s*|[-–]\s*)'
                   r'(?:approx(?:imately)?\.?|about|around)\s*'+_METRIC,text):
        return grams(m.groups())
    if m:=re.match(r'^[\d.]+\s*(?:ozs?|lbs?)\.?\s*/\s*'+_METRIC,text):
        return grams(m.groups())
    return None


def stated_weight(name: str, line: str, *, source_name: str | None = None) -> dict | None:
    """Read unambiguous source mass, or liquid volume using existing conventions.

    Package, per-item, range and drained-weight interpretations remain unknown.
    Volume conversion is an estimate, not a measured density.
    """
    from recipe_wrangler.tools.ingredient_weight_tool import (
        _normalize_fraction_text, _liquid_density_for_name, _VOLUME_UNIT_ML,_REVIEWED_CUP_GRAMS,
    )
    if source_name is None:line=quantity_source_fragment(name,line)
    spaced=re.sub(r'(?<=\d)(?=[¼½¾⅓⅔⅛⅜⅝⅞])',' ',line)
    text=_normalize_fraction_text(spaced).strip().casefold()
    if text.count('(')!=text.count(')'):return None
    text=re.sub(r'\(\s*or\b[^)]*\)','',text).strip()  # "(or 3x 400g tins)" offers an alternative, not a second amount
    pack=_stated_pack_or_total_mass(name,text)
    if pack and 0<pack<=10000:
        return {'weight_g':pack,'kind':'explicit_source_mass','source_line':line}
    # "8 small (600g) chicken thighs": the bracket is the line's mass in grams; the shared
    # parser rule only reads it per piece when a total would be far too light ("4 small (50g) eggs").
    # "3 (75g) pita breads" too, not only size words, so graph weights and calories read it the same way.
    if re.match(r'^\d+\s*(?:[x×]\s*)?(?:(?:mini|small|medium|large|individual)\s*)?\(',text) and any(word[:4] in text for word in re.findall(r'[a-z]+',name.casefold())):
        from recipe_wrangler.tools.parse_recipe_tool import _bracket_total_mass
        mass=_bracket_total_mass(text,'1',name)
        return {'weight_g':mass,'kind':'explicit_source_mass','source_line':line} if mass and 0<mass<=10000 else None
    # Structured source identities must denote the same complete food. A dish
    # mass cannot be borrowed for one component merely sharing a name token.
    if source_name is not None:
        from recipe_wrangler.utils.food_identity import compare_identities
        source_name=re.sub(r'\boats\s+oatmeal\b','oats',source_name,flags=re.I)
        source_name=re.sub(r'\buncooked\b','raw',source_name,flags=re.I)
        identity_name = _quantity_identity_name(_normalize_fraction_text(name))
        source_identity = _quantity_identity_name(_normalize_fraction_text(source_name))
        if compare_identities(identity_name,source_identity).relation not in {'exact','synonym','likely_equivalent'}:return None
    # This compound catalogue label denotes one reconstituted liquid, not a
    # choice between separately quantified ingredients. Preserve all other ORs.
    reconstituted=bool(re.fullmatch(r'broth or stock,\s*(?:beef|chicken|vegetable),\s*dehydrated and reconstituted',name.strip(),re.I))
    volume_identity=re.sub(r'^'+_NUMBER+r'\s*(?:ml|millilitres?|milliliters?|litres?|liters?|l|cups?|tablespoons?|tbsp|teaspoons?|tsp)\s+', '',text)
    literal_stock=reconstituted and volume_identity==' '.join(name.casefold().split())
    guarded=text.replace('broth or stock','stock',1) if literal_stock else text
    if re.search(r'\b(?:each|per|drained|or|dissolved)\b|\d\s*[-–]\s*\d',guarded):return None
    quantities=re.findall(r'(?<!\w)'+_NUMBER+r'\s*(?:kg|g|grams?|ml|litres?|liters?|cups?|tablespoons?|tbsp|teaspoons?|tsp)\b', _MASS_NOTE.sub('', text))
    if len(quantities)>1:return None
    metrics=list(re.finditer(r'(?<!\w)(\d+(?:\.\d+)?)\s*(kg|g|grams?)\b',text))
    mass=None
    if len(metrics)==1:
        token=metrics[0]
        leading=token.start()==0
        parenthesized=bool(re.search(r'\(\s*(?:(?:about|around|approximately)\s*)?$',text[:token.start()]))
        count=bool(re.match(r'^\d+(?:\.\d+)?\b',text))
        if leading or (parenthesized and count and not re.search(r'\b(?:packs?|packets?|cans?|tins?|jars?|bottles?)\b',text)):
            mass=float(token[1])*(1000 if token[2]=='kg' else 1)
    if mass is not None:
        return {'weight_g':mass,'kind':'explicit_source_mass','source_line':line} if 0<mass<=10000 else None
    # Multiple gram amounts cannot silently turn into a volume interpretation.
    if metrics:return None
    match=re.match(r'^('+_NUMBER+r')\s*(ml|millilitres?|milliliters?|litres?|liters?|l|cups?|tablespoons?|tbsp|teaspoons?|tsp)\b',text)
    if not match:return None
    density=_liquid_density_for_name(name)
    if re.search(r'\b(?:powder(?:ed)?|granules?|cubes?)\b',text) and not re.search(r'\b(?:powder(?:ed)?|granules?|cubes?)\b',name,re.I):
        return None
    # shortcut: solid-volume recovery covers dry rolled oats; expand only with
    # explicit food/state contracts rather than a generic vegetable cup density.
    if density is None and re.fullmatch(r'(?:rolled\s+)?oats?',name.strip(),re.I):
        density=next(((grams/240,'reviewed grams per cup: '+basis)
            for pattern,grams,basis in _REVIEWED_CUP_GRAMS if pattern.search(name.casefold())),None)
    if density is None:return None
    value=sum(float(Fraction(part)) for part in match[1].split())
    volume=value*_VOLUME_UNIT_ML[match[2]]
    mass=volume*density[0]
    if not 0<mass<=10000:return None
    return {'weight_g':mass,'kind':'source_volume_estimate','volume_ml':volume,
        'density_g_per_ml':density[0],'density_basis':density[1],
        'volume_convention':'existing weight tool: cup 240 ml, tablespoon 15 ml, teaspoon 5 ml',
        'source_line':line}


def unique_source_line(name: str, lines: list[str]) -> str | None:
    """Require a unique ingredient-token join; repeated identical scrape lines collapse."""
    tokens=name_tokens(name)
    matches=list(dict.fromkeys(line for line in lines if tokens and tokens<=name_tokens(line)))
    # Scrapers can emit both a whole section and its individual ingredient lines.
    matches=[line for line in matches if not any(other!=line and other in line for other in matches)]
    return matches[0] if len(matches)==1 else None


def unique_source_lines(names: list[str], lines: list[str]) -> list[str]:
    """Bind each occurrence once; shared or repeated source lines stay unknown.

    Lines bound uniquely are removed and the rest retried, so "Cabbage" binds the remaining cabbage line once
    "Cabbage, red, raw" has taken its own. True ties never bind (nothing is eliminated first).
    """
    repeats=Counter(lines);bound=['']*len(names);taken=set()
    while True:
        remaining=[line for line in lines if line not in taken]
        candidates={i:unique_source_line(name,remaining) for i,name in enumerate(names) if not bound[i]}
        uses=Counter(line for line in candidates.values() if line)
        new={i:line for i,line in candidates.items() if line and uses[line]==1 and repeats[line]==1}
        if not new:return bound
        for i,line in new.items():
            bound[i]=line;taken.add(line)
