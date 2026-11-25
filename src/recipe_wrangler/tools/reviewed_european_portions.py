"""Reviewed European size defaults; source values are retained unchanged."""
import re


_PORTIONS = {('apple', 'with_peel', 'large'): ('875',
                                   'Appel m schil gem',
                                   'stuks groot >7,5 cm (zonder klokhuis)',
                                   162.0),
 ('apple', 'with_peel', 'medium'): ('875',
                                    'Appel m schil gem',
                                    'stuks middel 6,5-7,5 cm (zonder klokhuis)',
                                    135.0),
 ('apple', 'with_peel', 'small'): ('875', 'Appel m schil gem', 'stuks klein <6,5 cm (zonder klokhuis)', 76.0),
 ('apple', 'without_peel', 'large'): ('147',
                                      'Appel z schil gem',
                                      'stuks groot >7,5 cm (zonder klokhuis)',
                                      144.0),
 ('apple', 'without_peel', 'medium'): ('147',
                                       'Appel z schil gem',
                                       'stuks middel 6,5-7,5 cm (zonder klokhuis)',
                                       120.0),
 ('apple', 'without_peel', 'small'): ('147',
                                      'Appel z schil gem',
                                      'stuks klein <6,5 cm (zonder klokhuis)',
                                      68.0),
 ('avocado', 'without_peel', 'large'): ('689', 'Avocado', 'stuks groot >11 cm (zonder schil en pit)', 300.0),
 ('avocado', 'without_peel', 'medium'): ('689',
                                         'Avocado',
                                         'stuks middel 10-11 cm (zonder schil en pit)',
                                         180.0),
 ('avocado', 'without_peel', 'small'): ('689', 'Avocado', 'stuks klein <10 cm (zonder schil en pit)', 80.0),
 ('banana', 'with_peel', 'large'): ('151', 'Banaan', 'stuks groot (met schil)', 236.0),
 ('banana', 'with_peel', 'medium'): ('151', 'Banaan', 'stuks middel (met schil)', 186.0),
 ('banana', 'with_peel', 'small'): ('151', 'Banaan', 'stuks klein (met schil)', 143.0),
 ('banana', 'without_peel', 'large'): ('151', 'Banaan', 'stuks groot (zonder schil)', 165.0),
 ('banana', 'without_peel', 'medium'): ('151', 'Banaan', 'stuks middel (zonder schil)', 130.0),
 ('banana', 'without_peel', 'small'): ('151', 'Banaan', 'stuks klein (zonder schil)', 100.0),
 ('cucumber', 'with_peel', 'large'): ('2739',
                                      'Komkommer m schil rauw',
                                      'stuks groot lengte >34 cm zonder afval',
                                      522.0),
 ('cucumber', 'with_peel', 'medium'): ('2739',
                                       'Komkommer m schil rauw',
                                       'stuks middel lengte 31-34 cm zonder afval',
                                       399.0),
 ('cucumber', 'with_peel', 'small'): ('2739',
                                      'Komkommer m schil rauw',
                                      'stuks klein lengte <31 cm zonder afval',
                                      304.0),
 ('cucumber', 'without_peel', 'large'): ('27', 'Komkommer z schil rauw', 'stuks groot lengte >34 cm', 495.0),
 ('cucumber', 'without_peel', 'medium'): ('27',
                                          'Komkommer z schil rauw',
                                          'stuks middel lengte 31-34 cm',
                                          378.0),
 ('cucumber', 'without_peel', 'small'): ('27', 'Komkommer z schil rauw', 'stuks klein lengte <31 cm', 288.0),
 ('mandarin', 'with_peel', 'large'): ('165', 'Mandarijn', 'stuks groot (met schil)', 107.0),
 ('mandarin', 'with_peel', 'medium'): ('165', 'Mandarijn', 'stuks middel (met schil)', 80.0),
 ('mandarin', 'with_peel', 'small'): ('165', 'Mandarijn', 'stuks klein (met schil)', 40.0),
 ('mandarin', 'without_peel', 'large'): ('165', 'Mandarijn', 'stuks groot (zonder schil)', 80.0),
 ('mandarin', 'without_peel', 'medium'): ('165', 'Mandarijn', 'stuks middel (zonder schil)', 60.0),
 ('mandarin', 'without_peel', 'small'): ('165', 'Mandarijn', 'stuks klein (zonder schil)', 30.0),
 ('orange', 'with_peel', 'large'): ('171', 'Sinaasappel', 'stuks groot (met schil)', 243.0),
 ('orange', 'with_peel', 'medium'): ('171', 'Sinaasappel', 'stuks middel (met schil)', 186.0),
 ('orange', 'with_peel', 'small'): ('171', 'Sinaasappel', 'stuks klein (met schil)', 100.0),
 ('orange', 'without_peel', 'large'): ('171', 'Sinaasappel', 'stuks groot (zonder schil)', 170.0),
 ('orange', 'without_peel', 'medium'): ('171', 'Sinaasappel', 'stuks middel (zonder schil)', 130.0),
 ('orange', 'without_peel', 'small'): ('171', 'Sinaasappel', 'stuks klein (zonder schil)', 70.0),
 ('pear', 'with_peel', 'medium'): ('2748', 'Peer m schil', 'stuks middel (zonder klokhuis)', 214.0),
 ('pear', 'without_peel', 'medium'): ('168', 'Peer z schil', 'stuks middel (zonder klokhuis)', 180.0),
 ('potato', 'with_peel', 'large'): ('1', 'Aardappelen rauw', 'grote (lengte >7,5 cm) met schil', 125.0),
 ('potato', 'with_peel', 'medium'): ('1',
                                     'Aardappelen rauw',
                                     'middelgrote (lengte 6,5-7,5 cm) met schil',
                                     88.0),
 ('potato', 'with_peel', 'small'): ('1', 'Aardappelen rauw', 'kleine (lengte <6,5 cm) met schil', 63.0),
 ('potato', 'without_peel', 'large'): ('1', 'Aardappelen rauw', 'grote (lengte >7,5 cm) zonder schil', 100.0),
 ('potato', 'without_peel', 'medium'): ('1',
                                        'Aardappelen rauw',
                                        'middelgrote (lengte 6,5-7,5 cm) zonder schil',
                                        70.0),
 ('potato', 'without_peel', 'small'): ('1', 'Aardappelen rauw', 'kleine (lengte <6,5 cm) zonder schil', 50.0)}


_ALIASES = {
    'bananas': 'banana', 'apples': 'apple', 'pears': 'pear', 'oranges': 'orange',
    'mandarins': 'mandarin', 'potatoes': 'potato', 'cucumbers': 'cucumber',
    'avocados': 'avocado',
}
_DEFAULT_BASIS = {
    'banana': 'without_peel', 'orange': 'without_peel', 'mandarin': 'without_peel',
    'avocado': 'without_peel', 'apple': 'with_peel', 'pear': 'with_peel',
    'potato': 'with_peel', 'cucumber': 'with_peel',
}


def european_size_reference(name: str, unit: str | None) -> dict | None:
    """Select an exact reviewed raw-food size, retaining its source and edible basis."""
    if unit not in {'small', 'medium', 'large'}:
        return None
    name = name.lower()
    words = {_ALIASES.get(word, word) for word in re.findall(r'[a-z]+', name)}
    foods = words & _DEFAULT_BASIS.keys()
    if len(foods) != 1:
        return None
    food = next(iter(foods))
    allowed = {food, 'raw', 'fresh', 'ripe', 'small', 'medium', 'large',
               'peeled', 'unpeeled', 'with', 'without', 'peel', 'skin'}
    # Unsupported variety, preparation, core/pit or waste requests must not match.
    if words - allowed or words & {'small', 'medium', 'large'} - {unit}:
        return None
    with_peel = 'unpeeled' in words or bool(re.search(r'\bwith\s+(?:peel|skin)\b', name))
    without_peel = 'peeled' in words or bool(re.search(r'\bwithout\s+(?:peel|skin)\b', name))
    if with_peel and without_peel:
        return None
    if words & {'peel', 'skin', 'with', 'without'} and not (with_peel or without_peel):
        return None
    basis = ('with_peel' if with_peel else 'without_peel') if (with_peel or without_peel) else _DEFAULT_BASIS[food]
    row = _PORTIONS.get((food, basis, unit))
    if row is None:
        return None
    code, source_name, description, grams = row
    excluded_parts = {'apple': ['core'], 'pear': ['core'], 'avocado': ['pit'],
                      'cucumber': ['waste']}.get(food, [])
    return {
        'grams_per_unit': grams,
        'portion_desc': description,
        'source': 'Portie-online versie 2026/2.0, RIVM, Bilthoven.',
        'source_url': 'https://portie-online.rivm.nl/',
        'source_food_id': 'NEVO' + code,
        'source_food_name': source_name,
        'weight_basis': 'with_peel' if basis == 'with_peel' else 'edible_without_peel',
        'excluded_parts': excluded_parts,
        'basis_assumed': not (with_peel or without_peel),
    }
