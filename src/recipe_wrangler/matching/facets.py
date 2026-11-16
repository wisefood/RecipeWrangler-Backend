"""Deterministic baseline facet extraction for experiments only."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from recipe_wrangler.matching.models import FoodFacets
from recipe_wrangler.utils.food_identity import compare_identities


EXTRACTOR_ID = "wisefood_rule_facets_v3_shared_anatomy_experimental"

_COOKING = {
    "scrambled": r"\bscrambl(?:ed|ing)\b",
    "baked": r"\bbak(?:e|ed|ing)\b",
    "blanched": r"\bblanch(?:ed|ing)?\b",
    "boiled": r"\bboil(?:ed|ing)?\b",
    "braised": r"\bbrais(?:e|ed|ing)\b",
    "fried": r"\b(?:fried|frying)\b",
    "grilled": r"\bgrill(?:ed|ing)?\b",
    "poached": r"\bpoach(?:ed|ing)?\b",
    "roasted": r"\broast(?:ed|ing)?\b",
    "sauteed": r"\bsaut(?:e|é)(?:d|ed|ing)?\b",
    "steamed": r"\bsteam(?:ed|ing)?\b",
    "toasted": r"\btoast(?:ed|ing)?\b",
}
_PROCESSES = {
    **_COOKING,
    "chopped": r"\bchopp?ed\b",
    "crushed": r"\bcrush(?:ed)?\b",
    "drained": r"\bdrain(?:ed)?\b",
    "diced": r"\bdic(?:e|ed|ing)\b",
    "fermented": r"\bferment(?:ed|ing)?\b",
    "grated": r"\bgrat(?:ed|ing)\b",
    "ground": r"\bground\b",
    "minced": r"\bminc(?:ed)?\b",
    "peeled": r"\bpeel(?:ed)?\b",
    "prepared": r"\bprepared\b",
    "concentrated": r"\bconcentrat(?:e|ed|ing)\b",
    "refined": r"\brefin(?:e|ed|ing)\b",
    "rinsed": r"\brins(?:ed|ing)\b",
    "sliced": r"\bslic(?:ed)?\b",
}
_PRESERVATION = {
    "canned": r"\b(?:canned|tinned)\b",
    "cured": r"\bcured\b",
    "dried": r"\b(?:dried|dry|dehydrated)\b",
    "fresh": r"\bfresh\b",
    "frozen": r"\b(?:frozen|defrosted|thawed)\b",
    "pickled": r"\bpickled\b",
    "salted": r"\bsalted\b",
    "smoked": r"\bsmoked\b",
}
_FORMS = {
    "cube": r"\bcubes?\b",
    "flakes": r"\bflakes?\b",
    "granules": r"\bgranules?\b",
    "juice": r"\bjuice\b",
    "paste": r"\bpaste\b",
    "powder": r"\bpowder(?:ed)?\b",
    "puree": r"\bpur(?:ee|ée)\b",
    "sauce": r"\bsauce\b",
    "syrup": r"\bsyrup\b",
    "whole": r"\bwhole\b",
}
_COLOURS = ("black", "blue", "brown", "green", "orange", "purple", "red", "white", "yellow")
_SOURCES = {
    "cattle": r"\b(?:beef|cattle|cow)\b",
    "chicken": r"\bchicken\b",
    "cod": r"\bcod\b",
    "duck": r"\bduck\b",
    "goat": r"\bgoat\b",
    "lamb": r"\b(?:lamb|mutton|sheep)\b",
    "pig": r"\b(?:pork|pig)\b",
    "salmon": r"\bsalmon\b",
    "shrimp": r"\b(?:prawn|prawns|shrimp)\b",
    "tuna": r"\btuna\b",
    "turkey": r"\bturkey\b",
}
_PARTS = {
    "breast": r"\bbreasts?\b",
    "egg_white": r"\begg\s+whites?\b|\bwhites?\s+of\s+eggs?\b",
    "egg_yolk": r"\begg\s+yolks?\b|\byolks?\s+of\s+eggs?\b",
    "fillet": r"\bfillets?\b",
    "heart": r"\bhearts?\b",
    "kidney": r"\bkidneys?\b",
    "leg": r"\blegs?\b",
    "liver": r"\blivers?\b",
    "seed": r"\bseeds?\b",
    "skin": r"\bskin\b",
    "thigh": r"\b(?:thighs?|(?:high|upper)\s+legs?)\b",
    "wing": r"\bwings?\b",
    "drumstick": r"\bdrumsticks?\b",
    "shoulder": r"\bshoulders?\b",
    "loin": r"\bloins?\b",
    "shank": r"\bshanks?\b",
    "neck": r"\bnecks?\b",
    "rib": r"\bribs?\b",
    "rump": r"\brumps?\b",
    "sirloin": r"\bsirloins?\b",
}


def part_matches(text: str) -> list[tuple[str, re.Match]]:
    """Share cut terminology and prefer a specific phrase over its component words."""
    matches = [(value, match) for value, pattern in _PARTS.items()
               for match in re.finditer(pattern, text, re.I)
               if not (value == 'skin' and re.search(r'\b(?:with|without|and)\s*$', text[:match.start()], re.I))]
    return [(value, match) for value, match in matches if not any(
        other.start() <= match.start() and match.end() <= other.end()
        and other.end() - other.start() > match.end() - match.start()
        or value == 'fillet' and part not in {'fillet', 'skin', 'seed'}
        and other.end() <= match.start()
        and re.fullmatch(r'[\s,]+', text[other.end():match.start()])
        for part, other in matches)]


@dataclass(frozen=True)
class FacetExtraction:
    facets: FoodFacets
    evidence: tuple[str, ...]
    extractor_id: str = EXTRACTOR_ID


def extract_mention_facets(
    identity: str,
    text: str,
    note: str | None = None,
) -> FacetExtraction:
    """Extract only explicitly stated facets, leaving everything else unknown."""
    haystack = " ".join(part for part in (text, identity, note or "") if part).casefold()
    evidence: list[str] = []

    processes = _matches(haystack, _PROCESSES, "process", evidence)
    preservation = _matches(haystack, _PRESERVATION, "preservation", evidence)
    sources = _matches(haystack, _SOURCES, "biological_source", evidence)
    # Skin describes inclusion separately from the anatomical cut.
    part_text = re.sub(r"\b(?:without|with|and)\s+skin\b|\bskinless\b", " ", haystack)
    parts = _matches(part_text, _PARTS, "food_part", evidence)
    if re.search(r"\beggs?\b", haystack):
        if re.search(r"\byolks?\b", haystack):
            parts = {"egg_yolk"}
        elif re.search(r"\bwhites?\b", haystack):
            parts = {"egg_white"}
    skin_status = _inclusion(haystack, "skin", "skinless")
    bone_status = _inclusion(haystack, "bone", "boneless")
    packing = re.search(r"\bin\s+(?:spring\s+)?(oil|brine|water)\b", haystack)
    packing_medium = packing.group(1) if packing else None
    for facet, value in (("skin_status", skin_status), ("bone_status", bone_status), ("packing_medium", packing_medium)):
        if value:
            evidence.append(f"{facet}:{value}")
    forms = _matches(haystack, _FORMS, "physical_form", evidence)

    raw = bool(re.search(r"\b(?:raw|uncooked)\b", haystack))
    cooked = bool(re.search(r"\b(?:cooked|leftovers?)\b", haystack)) or bool(processes & _COOKING.keys())
    preparation_state = "unknown" if raw and cooked else "raw" if raw else "cooked" if cooked else None
    if preparation_state is not None:
        evidence.append(f"preparation_state:{preparation_state}")

    colour_matches = [colour for colour in _COLOURS if re.search(rf"\b{colour}\b", haystack)]
    colour = colour_matches[0] if len(colour_matches) == 1 else None
    if colour:
        evidence.append(f"colour:{colour}")

    sodium_level = _sodium_level(haystack)
    if sodium_level:
        evidence.append(f"sodium_level:{sodium_level}")

    fat_level = _fat_level(haystack)
    sugar_level = _sugar_level(haystack)
    grain_refinement = _grain_refinement(haystack)
    if grain_refinement == "wholegrain" and forms == {"whole"}:
        forms.clear()
    gluten_free = _gluten_free(haystack)
    for field, value in (
        ("fat_level", fat_level),
        ("sugar_level", sugar_level),
        ("grain_refinement", grain_refinement),
        ("gluten_free", gluten_free),
    ):
        if value is not None:
            evidence.append(f"{field}:{str(value).lower()}")

    core_text = f"{text} {identity}".casefold()
    core_text = re.sub(
        r"\b(?:no[- ]added|low[- ])sugar\s+and\s+salt\b",
        "",
        core_text,
    )
    compound_pattern = r"\b(?:and|or|with)\b|&|(?<!\d)\b[a-z]+\s*/\s*[a-z]+\b"
    compound = True if re.search(compound_pattern, core_text) else None
    if compound:
        evidence.append("compound:true")

    result = FacetExtraction(
        facets=FoodFacets(
            identity=recover_source_identity(identity, haystack) or None,
            biological_source=_single(sources),
            food_part=_single(parts),
            preparation_state=preparation_state,  # type: ignore[arg-type]
            processes=frozenset(processes),
            preservation_methods=frozenset(preservation),
            physical_form=_single(forms),
            colour=colour,
            sodium_level=sodium_level,  # type: ignore[arg-type]
            fat_level=fat_level,  # type: ignore[arg-type]
            sugar_level=sugar_level,  # type: ignore[arg-type]
            grain_refinement=grain_refinement,  # type: ignore[arg-type]
            skin_status=skin_status,
            bone_status=bone_status,
            packing_medium=packing_medium,
            gluten_free=gluten_free,
            compound=compound,
        ),
        evidence=tuple(evidence),
    )

    base_identity = _base_food_identity(result.facets.identity or identity, result.facets)
    return replace(result, facets=replace(result.facets, base_identity=base_identity),
                   evidence=(*result.evidence, f"base_identity:{base_identity}"))


def extract_candidate_facets(label: str) -> FacetExtraction:
    """Extract explicit facets from a composition-record label."""
    normalized_label = " ".join(label.casefold().split())
    facet_text = re.sub(
        r"\b(?:canned,?\s+)?in\s+(?:(?:own|[a-z]+)\s+)?juice\b",
        "canned",
        label,
        flags=re.IGNORECASE,
    )
    extraction = extract_mention_facets(
        identity=facet_text.casefold(),
        text=facet_text,
    )
    if re.search(r"\b(?:chicken|turkey)\s+(?:cold cuts|slices)\b", label, re.IGNORECASE):
        extraction = replace(
            extraction,
            facets=replace(extraction.facets, preparation_state="cooked"),
            evidence=(*extraction.evidence, "preparation_state:cooked"),
        )
    return replace(
        extraction,
        facets=replace(extraction.facets, identity=normalized_label),
    )


def _matches(
    text: str,
    patterns: dict[str, str],
    field: str,
    evidence: list[str],
) -> set[str]:
    values = ({value for value, _ in part_matches(text)} if field == 'food_part'
              else {value for value, pattern in patterns.items() if re.search(pattern, text)})
    evidence.extend(f"{field}:{value}" for value in sorted(values))
    return values


def _single(values: set[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None


def _sodium_level(text: str) -> str | None:
    if re.search(
        r"\b(?:no[- ]added[- ]salt|no[- ]salt[- ]added|without[- ]added[- ]salt|salt[- ]free)\b|"
        r"\bwo\s+salt\b|"
        r"\bno[- ]added[- ]sugar\s+and\s+salt\b",
        text,
    ):
        return "no_added_salt"
    if re.search(r"\bunsalted\b", text):
        return "unsalted"
    if re.search(r"\b(?:reduced[- ](?:salt|sodium)|salt[- ]reduced)\b", text):
        return "reduced"
    if re.search(r"\blow[- ](?:salt|sodium)\b", text):
        return "low"
    if re.search(r"\blow[- ]sugar\s+and\s+salt\b", text):
        return "low"
    return None


def _fat_level(text: str) -> str | None:
    if re.search(r"\bsemi[- ]skimmed\b", text):
        return "semi_skimmed"
    if re.search(r"\bskimmed\b", text):
        return "skimmed"
    if re.search(r"\b(?:low[- ]fat|half[- ]fat)\b", text):
        return "low"
    if re.search(r"\b(?:reduced[- ]fat|\d+%\s+less\s+fat)\b", text):
        return "reduced"
    if re.search(r"\bfull[- ]fat\b", text):
        return "full_fat"
    if re.search(r"\b(?:whole\s+milk|milk[^,;]{0,30}\bwhole)\b", text):
        return "whole"
    return None


def _sugar_level(text: str) -> str | None:
    if re.search(r"\b(?:sugar[- ]free|no[- ]sugar)\b", text):
        return "sugar_free"
    if re.search(r"\b(?:no[- ]added[- ]sugar|no[- ]sugar[- ]added|100%\s+peanuts?)\b", text):
        return "no_added_sugar"
    if re.search(r"\blow[- ]sugar\b", text):
        return "low"
    if re.search(r"\breduced[- ]sugar\b", text):
        return "reduced"
    return None


def _grain_refinement(text: str) -> str | None:
    if re.search(r'\bbrown\s+rice\b|\brice\s*,?\s*brown\b',text):
        return 'wholegrain'
    if re.search(r"\b(?:wholegrain|wholemeal|whole[- ]wheat|whole[- ]oats?)\b", text):
        return "wholegrain"
    if re.search(r"\brefined\b", text):
        return "refined"
    return None


def _gluten_free(text: str) -> bool | None:
    if not re.search(r"\b(?:gluten[- ]free|no[- ]gluten)\b", text):
        return None
    if re.search(r"\bif\s+(?:necessary|needed|required)\b|\boptional(?:ly)?\b", text):
        return None
    return True


def _inclusion(text: str, noun: str, absent_word: str) -> str | None:
    absent = bool(re.search(rf"\b{absent_word}\b|\bwithout\s+{noun}s?\b", text))
    present = bool(re.search(rf"\b(?:with|and)\s+{noun}s?\b|\b{noun}[- ]on\b|\b{noun}[- ]in\b", text))
    return "unknown" if absent and present else "absent" if absent else "present" if present else None


def recover_source_identity(identity: str, source: str) -> str:
    """Restore explicit identity distinctions only when the core food agrees."""
    if re.search(r"\beggs?\b", identity, re.I):
        if re.search(r"\beggs?\b", source, re.I) and re.search(r"\byolks?\b", source, re.I):
            return "egg yolk"
        if re.search(r"\beggs?\b", source, re.I) and re.search(r"\bwhites?\b", source, re.I):
            return "egg white"
    if re.search(r"\bartichokes?\b", identity, re.I) and re.search(r"\bartichokes?\b", source, re.I) and re.search(r"\bjerusalem\b", source, re.I):
        return "Jerusalem artichoke"
    return identity.strip()


def _base_food_identity(identity: str, facets: FoodFacets) -> str:
    """Remove recognized property expressions, retaining unknown food words.

    No food-name allowlist is required. Product names such as milk, stock,
    paste and oil remain part of identity. Removed facts are compared as facets.
    """
    text = identity.casefold()
    # Strip only properties successfully represented by the shared extractor.
    for value, pattern in {**_PROCESSES, **_PRESERVATION}.items():
        if value in facets.processes or value in facets.preservation_methods:
            text = re.sub(pattern, " ", text)
    if facets.preparation_state not in (None, "unknown"):
        text = re.sub(r"\b(?:raw|uncooked|cooked)\b", " ", text)
    text = re.sub(r"\b(?:with|without|and)\s+skin\b|\b(?:skinless|boneless|whole)\b|\bbone[- ]in\b", " ", text)
    if facets.packing_medium:
        text = re.sub(r"\bin\s+(?:spring\s+)?" + re.escape(facets.packing_medium) + r"\b", " ", text)
    if facets.food_part in {"egg_yolk", "egg_white"}:
        text = re.sub(r"\b(?:yolks?|whites?)\b", " ", text)
    elif facets.food_part:
        text = re.sub(_PARTS.get(facets.food_part, r"(?!)"), " ", text)
    without_source = text
    if facets.biological_source:
        without_source = re.sub(_SOURCES[facets.biological_source], " ", text)
    core = compare_identities(without_source, without_source).mention_key
    if not core:
        core = compare_identities(text, text).mention_key
    return " ".join(core) or identity.strip().casefold()
