"""User-facing sentence per reason code when a recipe gets no sustainability suggestion (stored as reason_code in recipe_adaptations)."""

WHY = {  # reason code -> sentence for the UI when a recipe gets no sustainability suggestion
    'already_low_impact': 'The ingredients with the largest footprint are already low-impact plant foods.',
    'main_food_protected': 'The main contributor is a food we never swap for climate (eggs, fruit, oily fish, nuts, avocado).',
    'main_food_defines_the_dish': 'The main contributor is what the dish is (rice in a risotto, cream in a whipped dessert, a raw dish).',
    'prepared_product': 'The main contributor is a prepared product, not a single food that can be swapped.',
    'footprint_estimate_only': 'The main contributor has only an estimated footprint, too uncertain to suggest a swap.',
    'no_lower_impact_option': 'There is no clearly lower-impact version of the main contributor (for example chicken is already a low-impact meat).',
    'alternatives_add_allergen': 'Lower-impact options would add an allergen or break a diet the recipe respects.',
    'recipe_breaks_user_restriction': 'This recipe already contains an ingredient that breaks your allergens or diet, so no swap can make it suitable.',
    'amount_not_stated': 'The main contributor has no stated amount, so any saving would be a guess.',
    'saving_too_small': 'Lower-impact options exist but save too little to be worth changing the recipe.',
    'less_than_95pct_of_mass_has_a_co2_factor': 'Too few of the ingredients have a footprint value to score this recipe.',
}
