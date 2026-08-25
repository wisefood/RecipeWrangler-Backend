"""Public recipe reads use the v4 catalog, not query-time Neo4j calls."""

from recipe_wrangler.api.routers import recipes as R


class _FakeRecipeEntity:
    def __init__(self, docs):
        self.docs = docs

    def get(self, recipe_id):
        return self.docs.get(recipe_id)

    def get_many(self, recipe_ids):
        return {recipe_id: self.docs[recipe_id] for recipe_id in recipe_ids if recipe_id in self.docs}


def _doc(status="active"):
    return {
        "recipe_id": "r1",
        "title": "Soup",
        "status": status,
        "instructions": "Chop.\nBoil.",
        "ingredients": ["onion", {"name": "water", "quantity": 1, "unit": "l"}],
        "course_types": ["main-dish"],
        "allergens": ["celery"],
        "tags": ["vegan"],
    }


def test_catalog_detail_normalizes_the_v4_document(monkeypatch):
    monkeypatch.setattr(R, "recipe_entity", lambda: _FakeRecipeEntity({"r1": _doc()}))
    recipe = R._catalog_recipe_by_id("r1")
    assert recipe["instructions"] == ["Chop.", "Boil."]
    assert recipe["ingredients"][0] == {"name": "onion", "position": 0}
    assert recipe["ingredients"][1]["quantity"] == 1
    assert recipe["dish_types"] == ["main-dish"]
    assert recipe["allergens"] == ["celery"]


def test_catalog_detail_hides_disabled_unless_admin_opted_in(monkeypatch):
    monkeypatch.setattr(
        R, "recipe_entity", lambda: _FakeRecipeEntity({"r1": _doc("disabled")})
    )
    assert R._catalog_recipe_by_id("r1") is None
    assert R._catalog_recipe_by_id("r1", include_disabled=True)["status"] == "disabled"


def test_catalog_batch_uses_one_mget_and_skips_disabled(monkeypatch):
    entity = _FakeRecipeEntity({"r1": _doc(), "r2": {**_doc("disabled"), "recipe_id": "r2"}})
    monkeypatch.setattr(R, "recipe_entity", lambda: entity)
    recipes = R._catalog_recipes_by_ids(["r1", "r2", "missing"])
    assert list(recipes) == ["r1"]
