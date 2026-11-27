import pytest

from recipe_wrangler.tools import parse_recipe_tool as P


TABLE = {"chicken thighs": 1004.0, "parsnips": 156.0, "wholemeal pita bread": 162.0, "eggs": 152.0, "butter beans": 454.0}


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setattr(P, "_table_weight", lambda name, measurement: TABLE.get(name))


def restore(name, measurement, display):
    return P.restore_explicit_source_measurement({"name": name, "measurement": measurement, "display": display})[0]


@pytest.mark.parametrize("name,measurement,display,expected", [
    ("chicken thighs", "4800.0 g", "8 small (600g)", "600.0 g"),  # parser multiplied count by bracket
    ("chicken thighs", "8 small", "8 small (600g)", "600.0 g"),
    ("parsnips", "400.0 g", "2 small (200g)", "200.0 g"),
    ("butter beans", "800.0 g", "2 cups (400g)", "400.0 g"),
    ("wholemeal pita bread", "2 large", "2 large (70g)", "140.0 g"),  # one piece of 70 g, total 35 g is implausible
    ("eggs", "4 small", "4 small (50g)", "200.0 g"),
])
def test_bracket_after_count(name, measurement, display, expected):
    assert restore(name, measurement, display) == expected


def test_single_piece_and_correct_mass_unchanged():
    assert restore("potato", "1 small", "1 small (60g)") == "60.0 g"
    assert restore("chicken thighs", "600.0 g", "8 small (600g)") is None


def test_no_bracket_unchanged():
    assert P._bracket_total_mass("2 large", "2 large", "parsnips") is None


def test_each_stays_per_piece_and_fractions_and_volume():
    assert P._bracket_total_mass("2 smoked chicken breasts (300g each)", "2", "chicken breast") == 600.0
    assert P._bracket_total_mass("2 (about 200g total)", "2", "chicken breasts") == 200.0
    assert restore("ricotta", "0.67 cup", "⅔ cup (150g)") == "150.0 g"
    assert restore("flour", "1.5 cup", "1 ½ cups (200g)") == "200.0 g"


@pytest.mark.parametrize("name,line,expected", [
    ("chicken thighs", "8 small (600g) chicken thighs, skin off", 600.0),
    ("wholemeal pita bread", "2 large (70g) wholemeal pita bread", 140.0),
    ("potatoes", "4 large (300g) potatoes, scrubbed, dried", 300.0),
    ("butter beans", "2 cups (400g) dried butter beans, soaked overnight (or 3× 400g tins butter beans)", 400.0),
])
def test_profile_evidence_reads_same_rule(name, line, expected):
    from recipe_wrangler.matching.source_evidence import stated_weight
    assert stated_weight(name, line)["weight_g"] == expected


# HealthyFoods lists every source line twice; the September parse summed the copies
# ("⅓ cup (60g)" stored as 120 g). The mass the source line states wins over such grams.
@pytest.mark.parametrize("name,measurement,line,expected", [
    ("sun-dried tomatoes", "120.0 g", "⅓ cup (60g) roughly chopped oil-free sun-dried tomatoes", "60.0 g"),
    ("homemade gluten-free flour mix", "1.0 g", "2 cups (270g) homemade gluten-free flour mix (see Tips)", "270.0 g"),
    ("tomatoes", "0.5 g", "½ 400g can chopped tomatoes", "200.0 g"),
    ("mushrooms", "1000.0 g", "4 cups (250g) mushrooms, quartered", "250.0 g"),
    ("new potatoes", "4.0", "4 new potatoes (200g), sliced or diced", "200.0 g"),  # a count gives way to the stated mass
    ("celery", "1.0 stalk", "1 stalk celery (150g), trimmed, chopped finely", "150.0 g"),
])
def test_source_stated_mass_beats_wrong_parsed_grams(name, measurement, line, expected):
    assert P.restore_source_stated_mass(measurement, line, name) == expected


@pytest.mark.parametrize("name,measurement,line", [
    ("four-bean mix", "850.0 g", "2 x 425g cans four-bean mix, drained, rinsed"),  # already right
    ("chives", "0.25 cup", "¼ cup finely chopped fresh chives"),  # not a gram amount
    ("beef schnitzel", "680.0 g", "8 thin slices (around 85g per slice, 680g in total) beef schnitzel"),  # no clear mass
    ("edam cheese", "30.0 g", "30g edam cheese, cut into 25 pea-sized cubes"),
    ("butter", "115.0 g", "1 tbsp + 100g butter"),  # two amounts add up
    ("flour", "1000.0 g", "1,000 g flour"),  # thousands separator
    ("sweet potato", "200.0 g", "200g baked sweet potato, mashed (orange flesh is best, 400g chopped raw sweet potato)"),
    ("borlotti beans", "400.0 g", "4 small (about 300g each) eggplant aubergines, halved olive spray oil 400g can borlotti beans"),
    ("chicken breasts", "500.0 g", "4 (about 500g) chicken breasts, each cut in 2 thin fillets"),  # "each" belongs to the cut
    ("wholemeal pita bread", "140.0 g", "2 large (70g) wholemeal pita breads"),  # one piece of 70 g
])
def test_source_stated_mass_leaves_correct_or_unclear_lines(name, measurement, line):
    assert P.restore_source_stated_mass(measurement, line, name) is None


def test_prepare_entries_for_weights_uses_source_mass():
    entries = [{"name": "sun-dried tomatoes", "measurement": "120.0 g", "display": "⅔ cup (120g)", "note": ""}]
    lines = ["⅓ cup (60g) roughly chopped oil-free sun-dried tomatoes"] * 2
    _, measurements, _ = P.prepare_entries_for_weights(entries, lines)
    assert measurements == ["60.0 g"]


@pytest.mark.parametrize("name,measurement,line", [
    ("water", "0.5 cups", "½ x 400g can chickpeas in spring water, drained, rinsed"),  # neighbour's mass
    ("brown sugar", "0.25 cup", "150g sugar snap peas, trimmed"),
    ("eggs", "3.0", "400g can light sweetened condensed milk 3 eggs zest of 1 lemon"),  # glued line
    ("strong tea", "1.0 cup", "1 cup of strong tea 6oz/180g brown sugar"),
    ("lasagne sheets", "6.0", "175g (6) instant lasagne sheets"),  # count does not lead the line
])
def test_count_or_volume_needs_its_own_stated_equivalent(name, measurement, line):
    assert P.restore_source_stated_mass(measurement, line, name) is None


@pytest.mark.parametrize("name,measurement,line,expected", [
    ("bacon", "2.0 rashers", "2 rashers (about 100g) lean bacon", "100.0 g"),
    ("ham", "1.0 kg", "1 ham, around 2kg / 4½ lb", "2000.0 g"),  # a wrong mass unit is a wrong mass
    ("lettuce", "15.0 oz", "15g / 1/2 oz. lettuce", "15.0 g"),
])
def test_stated_equivalent_or_wrong_mass_is_replaced(name, measurement, line, expected):
    assert P.restore_source_stated_mass(measurement, line, name) == expected


@pytest.mark.parametrize("name,line", [
    ("wholemeal pita bread", "3 (75g) wholemeal pita breads, toasted, torn"),
    ("cooked beetroot", "2 (80g) cooked beetroot"),
    ("chicken thighs", "8 small (600g) chicken thighs, skin off"),
    ("mushrooms", "4 cups (250g) mushrooms, quartered"),
])
def test_calorie_reader_and_graph_rule_agree(name, line):
    from recipe_wrangler.matching.source_evidence import stated_weight

    assert stated_weight(name, line)["weight_g"] == P._bracket_total_mass(line.casefold(), "1", name)


def test_light_total_check_uses_the_lines_own_words(monkeypatch):
    seen = []
    monkeypatch.setattr(P, "_table_weight", lambda name, measurement: seen.append(name) or {"new potatoes": 100.0}.get(name, 692.0))
    assert P._bracket_total_mass("4 new potatoes (160g)", "1", "potatoes") == 160.0  # 40 g each fits new potatoes
    assert seen == ["new potatoes"]
