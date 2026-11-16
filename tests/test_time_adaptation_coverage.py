"""Coverage screening must retain uncertainty and exclude obvious non-candidates."""

from scripts.instructions.time_adaptation_coverage import classify, long_cooking


def test_dried_pulse_requires_preparation_opportunity():
    flags, _ = classify({"originals": ["200g dried kidney beans"],
                         "instructions": ["Soak the beans overnight."]})
    assert flags["pulses_explicit_dried_and_soak_or_long_cook"]
    assert flags["pulses_explicit_dried_with_named_pulse_step"]
    flags, _ = classify({"originals": ["200g dried kidney beans"], "instructions": ["Serve."]})
    assert not flags["pulses_explicit_dried_and_soak_or_long_cook"]


def test_canned_and_non_pulse_beans_do_not_enter_dried_count():
    for line in ["400g canned kidney beans", "50g Beans, butter, dried, boiled in unsalted water",
                 "100g dried green beans", "coffee beans", "vanilla bean"]:
        flags, _ = classify({"originals": [line], "instructions": ["Soak the beans overnight."]})
        assert not flags["pulses_explicit_dried_and_soak_or_long_cook"]


def test_missing_form_and_recipe_wide_cooccurrence_remain_separate():
    flags, _ = classify({"originals": ["lentils"], "instructions": ["Simmer for 45 minutes."]})
    assert flags["pulses_unprepared_or_unspecified_and_soak_or_long_cook"]
    assert not flags["pulses_explicit_dried_and_soak_or_long_cook"]
    assert not flags["pulses_explicit_dried_with_named_pulse_step"]


def test_grain_and_vegetable_opportunities_exclude_prepared_inputs():
    for lines in [["cooked rice", "frozen onions"], ["rice flour", "onion powder"]]:
        flags, _ = classify({"originals": lines, "instructions": ["Cook the rice. Chop the onions."]})
        assert not flags["grains_unprepared_or_unspecified_with_named_cook_step"]
        assert not flags["vegetables_unprepared_or_unspecified_with_named_cut_step"]


def test_long_cook_threshold_does_not_treat_storage_time_as_cooking():
    assert long_cooking("Simmer for 30 to 45 minutes.")
    assert long_cooking("Cook for 1 hour.")
    assert not long_cooking("Simmer for 15 minutes.")
    assert not long_cooking("Refrigerate for 3 days.")
