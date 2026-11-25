"""User-facing sentence per reason code when a recipe gets no time suggestion."""

WHY = {
    'no_overlap_found': 'No later step can safely start while a passive step (simmer, boil, bake, rest) is running.',
    'existing_parallelism_requires_baseline_analysis': 'The recipe already tells you to do things at the same time.',
    'recipe_exceeds_planning_bounds': 'The recipe is too long or too complex for the time check.',
    'source_action_records_required': 'The steps could not be split into timed actions.',
    'no_instructions': 'This recipe has no instruction steps.',
    'recipe_breaks_user_restriction': 'This recipe already contains an ingredient that breaks your allergens or diet, so no change can make it suitable.',
}
