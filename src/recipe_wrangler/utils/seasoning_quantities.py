"""Explicit prototype assumption for unquantified salt and pepper."""
import re


def seasoning_quantity_assumption(name: str, amount_text: str = '') -> dict | None:
    """Estimate each seasoning per recipe; never replace an explicit amount."""
    salt = r'(?:(?:fine|coarse|kosher|sea|table|rock|pink|himalayan|iodised|iodized)\s+)*salt'
    pepper = r'(?:(?:freshly\s+)?(?:ground|cracked)\s+)*(?:(?:black|white)\s+)?pepper'
    clean_name = re.sub(r'\s*[,;]?\s*(?:\(\s*)?(?:to taste|to season|for seasoning)\s*\)?\s*$', '', name.strip(), flags=re.I)
    if not re.fullmatch(f'(?:{salt}|{pepper})', clean_name, re.I):
        return None
    text = amount_text.strip()
    if re.search(r'\d|[¼½¾⅓⅔⅛⅜⅝⅞]|\b(?:one|two|three|half|quarter|pinch|dash|teaspoons?|tablespoons?|tsp|tbsp|grams?|each)\b', text, re.I):
        return None
    if text and not (re.search(r'\b(?:to taste|to season|for seasoning|as needed)\b', text, re.I)
                     or text.casefold() == clean_name.casefold()):
        return None
    return {'policy': 'salt_pepper_1g_per_recipe_v1', 'weight_g': 1.0,
            'basis': 'per_recipe', 'status': 'estimated',
            'reason': 'Unspecified seasoning amount estimated as 1 g per ingredient for the whole recipe.'}


_SALT_AND_PEPPER_RE = re.compile(
    r"(?P<salt>(?:(?:fine|coarse|kosher|sea|table|rock|pink|himalayan)\s+)*salt)",
    re.IGNORECASE,
)
_SEASONING_PEPPER_RE = re.compile(
    r"(?:(?:freshly\s+)?(?:ground|cracked)\s+)*(?:(?P<colour>black|white)\s+)?pepper(?:corns?)?",
    re.IGNORECASE,
)

def salt_and_pepper_parts(name: str) -> tuple[str, str] | None:
    """Return canonical parts for a standalone salt-and-pepper ingredient."""
    text = re.sub(r"\s*[,;]?\s*(?:\(\s*)?(?:to taste|to season|for seasoning|optional)\s*\)?\s*$", "", str(name or "").strip(), flags=re.I)
    text = re.sub(r"^season\s+with\s+", "", text, flags=re.I)
    parts = re.split(r"\s*(?:\band\b|&|/|,)\s*", text, flags=re.I)
    if len(parts) != 2:
        return None
    for salt, pepper in (parts, parts[::-1]):
        pepper_match = _SEASONING_PEPPER_RE.fullmatch(pepper)
        if _SALT_AND_PEPPER_RE.fullmatch(salt) and pepper_match:
            return re.sub(r"\s+", " ", salt.lower()), (pepper_match['colour'] or 'black').lower() + ' pepper'
    return None



def seasoning_source_name(name: str) -> str | None:
    """Canonical single seasoning identity for a proven split source line."""
    text = ' '.join(name.casefold().strip().split())
    if _SALT_AND_PEPPER_RE.fullmatch(text):
        return 'salt'
    text = re.sub(r'^pepper,\s*(black|white)$',r'\1 pepper',text)
    match = _SEASONING_PEPPER_RE.fullmatch(text)
    return (match['colour'] or 'black')+' pepper' if match else None
