import pandas as pd
import pytest

from scripts.prepare_essrg import extract_components


@pytest.mark.parametrize('second_name',['Watercress, raw','Aubergine, roasted'])
def test_duplicate_food_codes_cannot_silently_overwrite_source_nutrients(monkeypatch,second_name):
    from scripts import prepare_essrg as hu
    frame=pd.DataFrame({'Food Code':['13-669','13-669'],
                        'Food Name':['Aubergine, roasted',second_name]})
    monkeypatch.setattr(hu.pd,'read_excel',lambda *a,**k:frame)
    with pytest.raises(ValueError,match='Duplicate embedded CoFID food code 13-669'):
        hu.build_cofid_lookup()


def test_duplicate_mineral_rows_cannot_overwrite_a_unique_food(monkeypatch):
    from scripts import prepare_essrg as hu
    prox=pd.DataFrame({'Food Code':['1-001'],'Food Name':['Milk']})
    minerals=pd.DataFrame({'Code':['1-001','1-001'],'Sodium (mg)':[10,20]})
    monkeypatch.setattr(hu.pd,'read_excel',lambda *a,**k:prox if k['sheet_name']=='1.3 Proximates' else minerals)
    with pytest.raises(ValueError,match='Duplicate embedded CoFID food code 1-001 in 1.4 Inorganics'):
        hu.build_cofid_lookup()


def test_extract_components_recovers_known_blank_dish_ids() -> None:
    tofu = {"dish_id": 168, "name": "Fried tofu slices", "ingredients": []}
    row = pd.Series(
        {
            "ID.1": float("nan"),
            "Dish #1": "Tofu slices",
            "ID.2": float("nan"),
            "Dish #2": "Unknown component",
        }
    )

    components, missing = extract_components(row, {168.0: tofu})

    assert [component["dish_id"] for component in components] == [168]
    assert missing == [{"dish_id": None, "dish_name": "Unknown component"}]
