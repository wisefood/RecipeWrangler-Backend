"""Comparison normalization; original quotes remain untouched."""
import re
import unicodedata
from decimal import Decimal


# 'Light' states a fat level only on these products: light soy sauce and light brown sugar say nothing about fat.
LIGHT_MEANS_FAT = re.compile(r'\b(?:mayo(?:nnaise)?|cheese|cr[eè]me fra[iî]che|cream|milk|margarine|spread|butter|yog(?:h)?urt|dressing)\b', re.I)


def label_states_reduced_fat(label: str) -> bool:
    """Release annotations are frozen, so read reduced fat wording from the record label."""
    return bool(re.search(r'\b(?:reduced|half)[- ]fat\b', label, re.I)
                or (re.search(r'\b(?:light|lite)\b', label, re.I) and LIGHT_MEANS_FAT.search(label)))


def normalized_value(field: str, value):
    if not isinstance(value, str):
        return value
    text = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    if field == 'fat_level' and text == 'low_fat':
        return 'low'
    if field == 'fat_content_description':
        match = re.fullmatch(r'(?:fat\s*)?(\d+(?:[.,]\d+)?)\s*%(?:\s*fat)?', text)
        if match:
            amount = Decimal(match[1].replace(',', '.'))
            return format(amount.normalize(), 'f') + '% fat'
    return text
