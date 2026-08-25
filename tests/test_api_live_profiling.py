from recipe_wrangler.api.routers import recipes as R


def test_live_profile_job_profiles_only_selected_region_and_reprojects(monkeypatch):
    invoked = []
    persisted = []
    projected = []

    def invoke(payload):
        invoked.append(payload)
        return {
            "title": payload["title"],
            "serves": payload["serves"],
            "nutrition_source": "slovenian",
            "nutrition_source_key": "slovenian",
            "profiling_totals": {},
            "ingredients": [],
        }

    class FakeChain:
        @staticmethod
        def invoke(payload):
            return invoke(payload)

    monkeypatch.setattr(R, "Recipe_Profiling_Chain_Structured", FakeChain)
    monkeypatch.setattr(
        R,
        "_persist_profile_trace_best_effort",
        lambda payload, result: (persisted.append((payload, result)) or True, None),
    )

    from recipe_wrangler.catalog import projection

    monkeypatch.setattr(projection, "project", lambda recipe_id: projected.append(recipe_id))

    key = "r1:SI"
    R._LIVE_PROFILE_JOBS.clear()
    R._LIVE_PROFILE_JOBS.add(key)
    R._run_live_profile_job(
        "r1",
        {
            "title": "Soup",
            "ingredients": [{"name": "potato", "measurement": "200 g"}],
            "serves": 2,
            "duration": 30,
            "instructions": ["Boil."],
        },
        "SI",
        key,
    )

    assert [payload["region"] for payload in invoked] == ["SI"]
    assert len(persisted) == 1
    assert projected == ["r1"]
    assert key not in R._LIVE_PROFILE_JOBS


def test_live_profile_guard_is_per_recipe_and_region(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, *, target, args, name, daemon):
            started.append((target, args, name, daemon))

        def start(self):
            return None

    monkeypatch.setattr(R.threading, "Thread", FakeThread)
    R._LIVE_PROFILE_JOBS.clear()
    recipe = {"ingredients": [{"name": "apple"}]}

    assert R._schedule_live_profile_job("r1", recipe, "IE")
    assert R._schedule_live_profile_job("r1", recipe, "IE")
    assert R._schedule_live_profile_job("r1", recipe, "SI")

    assert len(started) == 2
    assert set(R._LIVE_PROFILE_JOBS) == {"r1:IE", "r1:SI"}
    R._LIVE_PROFILE_JOBS.clear()


def test_live_profile_job_requires_ingredients():
    R._LIVE_PROFILE_JOBS.clear()
    assert not R._schedule_live_profile_job("r1", {"ingredients": []}, "IE")
    assert R._LIVE_PROFILE_JOBS == set()
