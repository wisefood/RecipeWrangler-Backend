from scripts.elasticsearch.sync_nutri_scores_from_postgres import (
    obsolete_us_cleanup_request,
    score_document,
)


def test_score_document_maps_all_three_regions():
    profile = {
        "eu": {"nutri_score": "Nutriscore_A", "nutri_color": "dark green"},
        "ie": {"nutri_score": "Nutriscore_B", "nutri_color": "green"},
        "hu": {"nutri_score": "Nutriscore_C", "nutri_color": "yellow"},
    }

    assert score_document(profile) == {
        "nutri_score_eu": "Nutriscore_A",
        "nutri_color_eu": "dark green",
        "nutri_score_ie": "Nutriscore_B",
        "nutri_color_ie": "green",
        "nutri_score_hu": "Nutriscore_C",
        "nutri_color_hu": "yellow",
    }


def test_score_document_rejects_incomplete_profiles():
    profile = {
        "eu": {"nutri_score": "Nutriscore_A", "nutri_color": "dark green"},
        "ie": {"nutri_score": "Nutriscore_B", "nutri_color": "green"},
    }

    assert score_document(profile) is None


def test_us_cleanup_scans_source_nulls_and_noops_other_documents():
    request = obsolete_us_cleanup_request()

    assert request["query"] == {"match_all": {}}
    script = request["script"]["source"]
    assert "containsKey('nutri_score_us')" in script
    assert "containsKey('nutri_color_us')" in script
    assert "ctx.op = 'noop'" in script
