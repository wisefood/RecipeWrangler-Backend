from recipe_wrangler.services.food_safety import search_safefood_guidance


def test_chicken_retrieves_cooking_and_cross_contamination_guidance():
    results = search_safefood_guidance("How do I cook this safely?", ["raw chicken"])
    ids = {item["id"] for item in results}
    assert "safefood-meat-cooking" in ids
    assert "safefood-cross-contamination" in ids
    assert all(item["url"].startswith("https://www.safefood.net/") for item in results)


def test_general_query_always_returns_authoritative_baseline():
    results = search_safefood_guidance("", [])
    assert results[0]["id"] == "safefood-general-prevention"
    assert results[0]["advisory"] is True
