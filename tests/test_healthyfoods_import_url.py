from scripts.postgres.import_healthyfoods_to_neo4j import _extract_recipe_fields


def test_healthyfoods_import_preserves_source_url():
    fields = _extract_recipe_fields(
        "recipe-1",
        {
            "healthyfoods_recipe": {
                "title": "Test recipe",
                "url": "https://www.healthyfood.com/healthy-recipes/test-recipe/",
                "ingredients": ["1 cup beans"],
                "instructions": ["Cook."],
                "serves": 2,
                "duration": 10,
            },
            "profile_result": {
                "ingredient_names": ["beans"],
                "measurements": ["1 cup"],
            },
        },
    )

    assert fields is not None
    assert fields["url"] == "https://www.healthyfood.com/healthy-recipes/test-recipe/"
