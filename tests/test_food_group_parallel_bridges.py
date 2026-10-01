from recipe_wrangler.catalog.vocabularies import food_groups_from_foodon


def test_mixed_vegetables_parallel_branch_maps_to_vegetables() -> None:
    assert "vegetables" in food_groups_from_foodon(["FOODON_00002683"])


def test_pasta_parallel_branch_maps_to_grains() -> None:
    assert "grains" in food_groups_from_foodon(["FOODON_00001211"])
