"""User-facing sentence per reason code when a recipe gets no cost suggestion."""

WHY = {
    'no_prices': 'None of the ingredients has a reference price, so the recipe cost cannot be computed.',
    'no_substitute_found': 'None of the priced ingredients has a usable substitute.',
    'no_cheaper_option': 'No same-category substitute is cheaper by enough to be worth changing the recipe.',
    'alternatives_add_allergen': 'Cheaper options exist, but they would add an allergen or break a diet you set.',
    'recipe_breaks_user_restriction': 'This recipe already contains an ingredient that breaks your allergens or diet, so no swap can make it suitable.',
    'no_active_profile': 'This recipe has no stored profile for this region.',
}
