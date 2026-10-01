import re

from recipe_wrangler.utils.consumer_suitability import PLANT_STAPLE_PATTERNS

_RX = [re.compile(p, re.IGNORECASE) for p in PLANT_STAPLE_PATTERNS]


def _staple(name: str) -> bool:
    return any(rx.match(name.lower()) for rx in _RX)


def test_plain_plant_foods_with_form_words_are_staples():
    for name in ("sugar", "cherry tomatoes", "spring onions", "baking powder", "fresh flat-leaf parsley",
                 "self-raising white flour", "pure maple syrup", "canned crushed tomatoes", "red chilli flakes"):
        assert _staple(name), name


def test_mixed_or_animal_foods_are_not_staples():
    for name in ("tomato and beef pie", "chicken stock", "cheese sauce", "butter", "egg noodles", "honey",
                 "milk chocolate", "tomato soup with cream", "prawns", "fresh pasta"):
        assert not _staple(name), name
