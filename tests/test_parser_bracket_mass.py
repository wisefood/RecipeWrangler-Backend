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
