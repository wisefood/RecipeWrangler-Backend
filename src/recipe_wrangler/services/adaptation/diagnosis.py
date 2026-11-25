"""What a recipe needs reduced and what it lacks, read from its Nutri-Score breakdown. Report-only: no recipe is changed."""
import re

NEGATIVE = {'energy': 'energy_kcal', 'sugar': 'sugar_g', 'saturated_fats': 'saturated_fat_g', 'sodium': 'sodium_mg'}
POSITIVE = {'fiber': 'fibre', 'fruit_percentage': 'fruit_veg', 'protein': 'protein'}
LOW_POINTS = {'fibre': 2, 'protein': 2, 'fruit_veg': 1}  # at or below this many points earned the component is a gap
STRONG_COMPONENT, STRONG_TOTAL = 4, 6  # excess is strong when one component reaches 4 points or the negative total reaches 6


def diagnose(breakdown: dict, details: list[dict], lines: dict, title: str) -> dict:
    """excess: negative components with points and their main source; gaps: positive components that earn little
    (protein only where the Nutri-Score protein rule still counts it); strategy: reduce, enrich, both or none."""
    negative, positive = breakdown['negative_points'], breakdown['positive_points']
    excess = []
    for key, column in NEGATIVE.items():
        item = negative['items'][key]
        if item['points'] <= 0:
            continue
        total = sum(d.get(column) or 0 for d in details) or 1
        top = max(details, key=lambda d: d.get(column) or 0)
        word = re.findall(r'[a-z]{4,}', (top.get('name') or '').lower())
        excess.append({'nutrient': key, 'points': item['points'], 'of': item['max'],
                       'main_source': lines.get(top.get('position'), top.get('name') or ''), 'share': round((top.get(column) or 0) / total, 2),
                       'is_dish_identity': bool(word and re.search(r'\b' + word[0], title.lower()))})
    excess.sort(key=lambda e: -e['points'])
    gaps = []
    for key, name in POSITIVE.items():
        item = positive['items'][key]
        if name == 'protein' and positive.get('protein_excluded_by_rule'):
            continue
        if item['points'] <= LOW_POINTS[name]:
            gaps.append({'component': name, 'points': item['points'], 'of': item['max']})
    strong = bool(excess) and (excess[0]['points'] >= STRONG_COMPONENT or negative['total'] >= STRONG_TOTAL)
    strategy = 'both' if strong and gaps else 'reduce' if strong else 'enrich' if gaps else 'none'
    return {'excess': excess, 'gaps': gaps, 'strategy': strategy}
