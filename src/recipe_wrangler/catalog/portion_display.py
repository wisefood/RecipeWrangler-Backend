"""Conservative human-facing portions derived from calculation measurements."""

from __future__ import annotations

import re
from dataclasses import dataclass


CALCULATION_NATIVE_SOURCES = frozenset({
    "Curated Hungarian Recipes",
    "Curated Slovenian Recipes",
})


@dataclass(frozen=True)
class PortionReference:
    ingredient_key: str
    grams_per_unit: float
    singular: str
    plural: str
    sources: frozenset[str]
    relative_tolerance: float = 0.1
    minimum_count: int = 1
    maximum_count: int = 20


# Multiple references are intentional: the Slovenian source uses two reviewed
# egg-size bases. The closest clean integer wins; otherwise conversion abstains.
PORTION_REFERENCES = (
    PortionReference(
        "egg", 50.0, "egg", "eggs",
        frozenset({"Curated Hungarian Recipes", "Curated Slovenian Recipes"}),
    ),
    PortionReference(
        "egg", 59.84, "egg", "eggs",
        frozenset({"Curated Slovenian Recipes"}),
    ),
    PortionReference(
        "egg_yolk", 17.0, "egg yolk", "egg yolks",
        frozenset({"Curated Slovenian Recipes"}),
    ),
    PortionReference(
        "egg_white", 34.0, "egg white", "egg whites",
        frozenset({"Curated Slovenian Recipes"}),
    ),
)

_MASS_RE = re.compile(
    r"^\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kg|g|grams?)\s*$",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z]+")


def _ingredient_key(name: str) -> str | None:
    tokens = set(_TOKEN_RE.findall(name.casefold()))
    if "egg" not in tokens and "eggs" not in tokens:
        return None
    if tokens & {"pasta", "noodle", "noodles", "macaroni", "spaghetti"}:
        return None
    if tokens & {"yolk", "yolks"}:
        return "egg_yolk"
    if tokens & {"white", "whites"}:
        return "egg_white"
    return "egg"


def _mass_grams(measurement: object) -> float | None:
    match = _MASS_RE.fullmatch(str(measurement or ""))
    if not match:
        return None
    amount = float(match.group("amount"))
    return amount * 1000 if match.group("unit").casefold() == "kg" else amount


def generated_display_text(
    *,
    ingredient_name: str,
    calculation_measurement: object,
    source: str,
) -> tuple[str, str] | None:
    """Return a reviewed count display and provenance, or safely abstain."""
    if source not in CALCULATION_NATIVE_SOURCES:
        return None
    ingredient_key = _ingredient_key(ingredient_name)
    grams = _mass_grams(calculation_measurement)
    if ingredient_key is None or grams is None:
        return None

    candidates: list[tuple[float, int, PortionReference]] = []
    for reference in PORTION_REFERENCES:
        if reference.ingredient_key != ingredient_key or source not in reference.sources:
            continue
        raw_count = grams / reference.grams_per_unit
        count = round(raw_count)
        if not reference.minimum_count <= count <= reference.maximum_count:
            continue
        relative_error = abs(raw_count - count) / raw_count
        if relative_error <= reference.relative_tolerance:
            candidates.append((relative_error, count, reference))
    if not candidates:
        return None

    _, count, reference = min(candidates, key=lambda candidate: candidate[0])
    unit = reference.singular if count == 1 else reference.plural
    return f"{count} {unit}", "reviewed_portion_reference_v1"


def _forms(token: str) -> set[str]:
    return {token, token.removesuffix("s"), token.removesuffix("es")}


def _covers(name: str, text: object) -> bool:
    text_forms = set().union(*(_forms(t) for t in _TOKEN_RE.findall(str(text or "").casefold())))
    tokens = _TOKEN_RE.findall(str(name or "").casefold())
    return all(_forms(t) & text_forms for t in tokens)


def usable_source_texts(names: list[str], texts: list[object]) -> list[object]:
    """Drop an original line that is another ingredient's.

    The original line is joined to a parsed row by position. If the stored originals
    do not match the scrape the parser read, every row after the first difference
    shows its neighbour's text ("onion" under "4 ounces or"). A line is dropped
    only when it does not name the ingredient and a line within two places does,
    so lines that legitimately omit the name ("2 eggs" for "chicken egg") stay.
    """
    usable: list[object] = []
    for k, text in enumerate(texts):
        shifted = (
            text and not _covers(names[k], text)
            and any(
                texts[j] and _covers(names[k], texts[j])
                for j in (k - 2, k - 1, k + 1, k + 2) if 0 <= j < len(texts)
            )
        )
        usable.append(None if shifted else text)
    return usable


def authored_display_text(display: object, ingredient_name: str) -> str | None:
    """Turn the parser's display fragment into one non-duplicated full line."""
    text = re.sub(r"(?<![\d.])(\d+)\.0(?!\d)", r"\1", " ".join(str(display or "").split()))
    name = " ".join(str(ingredient_name or "").split())
    if not text:
        return None
    name_tokens = _TOKEN_RE.findall(name.casefold())
    text_forms = set().union(*(_forms(t) for t in _TOKEN_RE.findall(text.casefold())))
    if name_tokens and all(_forms(t) & text_forms for t in name_tokens):
        single = re.fullmatch(r"1 (\w+?)s", text)
        return f"1 {single[1]}" if single and single[1] in name_tokens else text
    return f"{text} {name}".strip()


def ingredient_display_text(
    *,
    ingredient_name: str,
    calculation_measurement: object,
    stored_display: object,
    source_text: object = None,
    source: str,
) -> tuple[str, str]:
    authored_source = " ".join(str(source_text or "").split())
    if authored_source:
        return authored_source, "authored_source_text"
    authored = authored_display_text(stored_display, ingredient_name)
    if authored:
        return authored, "authored_display"
    generated = generated_display_text(
        ingredient_name=ingredient_name,
        calculation_measurement=calculation_measurement,
        source=source,
    )
    if generated:
        return generated
    fallback = " ".join(
        part for part in (str(calculation_measurement or "").strip(), ingredient_name.strip())
        if part
    )
    return fallback, "calculation_fallback"
