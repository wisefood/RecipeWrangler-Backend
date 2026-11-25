from copy import deepcopy
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from recipe_wrangler.matching.composition_catalogue import (
    CompositionCatalogue, annotate_record, consolidate_sources,
    exact_description_key, validate_mappings,
)
from scripts.composition.build_composition_catalogue import read_efsa, read_slovenian


class Catalogue:
    version = 'test'
    sha256 = 'test-hash'
    terms = {
        code: SimpleNamespace(code=code, label=label, status='APPROVED', implicit_facets=())
        for code, label in [('A031G', 'Hen eggs'), ('A032B', 'Boiled eggs'),
                            ('A032C', 'Fried eggs'), ('A032D', 'Poached eggs')]
    }

    def resolve(self, code):
        return SimpleNamespace(issues=() if code in self.terms else ('foodex2:unknown_code:' + code,),
                               terms=(self.terms[code],) if code in self.terms else ())


def source(label='Egg, raw', code='A031G', dataset='test', row=1, record_id='001'):
    return {'source': {'dataset': dataset, 'release': 'v1', 'record_id': record_id},
            'label': label, 'metadata': {'FoodEx2 code': code}, 'source_row': row}


def annotated(**kwargs):
    return annotate_record(consolidate_sources([source(**kwargs)])[0], Catalogue())


def test_source_egg_annotation_and_evidence_are_reused():
    record = annotated()
    assert record['facets']['base_identity'] == 'egg'
    assert record['facets']['biological_source'] == 'chicken'
    assert record['facets']['preparation_state'] == 'raw'
    assert 'preparation_state' not in record['source_facets']
    assert any(item['method'] == 'source_code_mapping' and item['facet'] == 'biological_source'
               for item in record['annotation_evidence'])
    assert record['nutrition_eligibility'] == 'not_assessed'


def test_boiled_code_does_not_invent_species_or_extent():
    record = annotated(label='Egg, boiled', code='A032B')
    assert record['source_facets']['processes'] == ['boiled']
    assert record['facets']['biological_source'] is None
    assert record['facets']['cooking_extent'] is None
    soft = annotated(label='Egg, soft-boiled', code='A032B')
    assert soft['facets']['cooking_extent'] == 'soft_boiled'
    assert 'cooking_extent' not in soft['source_facets']


def test_broader_source_process_disagreement_is_not_silently_merged():
    record = annotated(label='Egg, scrambled, with added fat', code='A032C')
    assert record['source_facets']['processes'] == ['fried']
    assert record['facets']['processes'] == ['scrambled']
    assert 'source_label_disagreement:processes' in record['issues']
    assert record['review_required'] is True


def test_physical_state_disagreement_is_flagged():
    record = source(label='Milk powder', code=None)
    record['metadata']['Physical state FoodEx2'] = 'Liquid'
    result = annotate_record(consolidate_sources([record])[0], Catalogue())
    assert result['source_facets']['physical_form'] == 'liquid'
    assert result['facets']['physical_form'] == 'powder'
    assert 'source_label_disagreement:physical_form' in result['issues']


def test_source_duplicates_and_variants_preserve_native_ids():
    a = source(row=1)
    b = source(row=2)
    assert consolidate_sources([a, b])[0]['source_rows'] == [1, 2]
    c = source(label='Egg, boiled', row=3)
    records = consolidate_sources([a, b, c])
    assert len(records) == 2
    assert len({record['catalogue_key'] for record in records}) == 2
    assert len({record['source_record_key'] for record in records}) == 1
    assert all(record['source']['record_id'] == '001' for record in records)
    assert all('source_id_multiple_metadata_variants' in record['issues'] for record in records)


def test_identical_names_from_different_sources_remain_ambiguous():
    records = [annotated(dataset='A'), annotated(dataset='B')]
    catalogue = CompositionCatalogue(records)
    assert catalogue.lookup('  EGG, RAW ')['status'] == 'ambiguous_exact_description'
    assert catalogue.lookup('Egg raw', 'A')['status'] == 'unique_exact_description'
    assert catalogue.lookup('Egg, boiled')['status'] == 'no_exact_description'
    assert exact_description_key('egg, not boiled') != exact_description_key('egg boiled')
    assert exact_description_key('duck egg') != exact_description_key('chicken egg')
    assert exact_description_key('Egg, raw') != exact_description_key('Eggs, raw')


def test_missing_qualifier_is_not_a_wildcard_or_automatic_alias():
    record = annotated(label='Eggs, chicken, whole, boiled', code='A032B')
    catalogue = CompositionCatalogue([record])
    assert catalogue.lookup('eggs chicken boiled')['status'] == 'no_exact_description'
    assert catalogue.lookup('eggs chicken whole boiled')['status'] == 'unique_exact_description'


def test_reviewed_aliases_are_explicit_and_scoped():
    a = annotated(label='Egg, boiled', code='A032B', dataset='A')
    b = annotated(label='Boiled hen egg', code='A032B', dataset='B')
    alias = {'phrase': 'Egg, boiled', 'catalogue_key': b['catalogue_key'],
             'status': 'reviewed', 'rationale': 'Test fixture mapping'}
    catalogue = CompositionCatalogue([a, b], [alias])
    assert catalogue.lookup('Egg, boiled', 'B')['method'] == 'reviewed_alias'
    bad = deepcopy(alias)
    bad['status'] = 'proposed'
    with pytest.raises(ValueError, match='reviewed'):
        CompositionCatalogue([a, b], [bad])


def test_facets_require_explicit_values_and_exclude_review_cases():
    safe = annotated(label='Eggs, chicken, boiled', code='A032B')
    compound = annotated(label='Eggs with chicken', code=None, record_id='002')
    negated = annotated(label='Eggs, not boiled', code=None, record_id='003')
    catalogue = CompositionCatalogue([safe, compound, negated])
    assert len(catalogue.find_facets({'base_identity': 'egg', 'biological_source': 'chicken', 'processes': ['boiled']})) == 1
    assert catalogue.find_facets({'biological_source': 'duck'}) == ()
    assert negated['facets']['processes'] == []
    assert negated['rule_facets']['processes'] == ['boiled']
    with pytest.raises(ValueError, match='nonempty'):
        catalogue.find_facets({})


def test_unknown_code_and_changed_mapping_are_visible():
    record = annotated(code='XXXXX')
    assert record['review_required']
    assert record['foodex2_evidence']['issues']
    changed = Catalogue()
    changed.terms = dict(changed.terms)
    changed.terms['A031G'] = SimpleNamespace(label='Changed', status='APPROVED')
    with pytest.raises(ValueError, match='mapping target changed'):
        validate_mappings(changed)


def test_efsa_loader_retains_original_metadata_and_row(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['title'])
    sheet.append(['subtitle'])
    sheet.append(['Dataset name', 'Original food code', 'English food name', 'FoodEx2 code'])
    sheet.append(['National table 2025', '0001', 'Egg, raw', 'A031G'])
    path = tmp_path / 'foods.xlsx'
    workbook.save(path)
    rows = list(read_efsa(path))
    assert rows[0]['source']['record_id'] == '0001'
    assert rows[0]['source_row'] == 4
    assert rows[0]['metadata']['FoodEx2 code'] == 'A031G'


def test_slovenian_nutrient_rows_do_not_become_duplicate_foods(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    headers = ['book', 'food_id', 'food_name_en', 'food_name_slo', 'food_name_latin', 'group_en', 'category_en', 'category_slo', 'nutrient_en']
    sheet.append(headers)
    for nutrient in ['Protein', 'Fat']:
        sheet.append(['plant', '001', 'Buckwheat', 'Ajda', '', '', 'Cereals', '', nutrient])
    path = tmp_path / 'slovenian.xlsx'
    workbook.save(path)
    records = consolidate_sources(read_slovenian(path))
    assert len(records) == 1
    assert records[0]['source_rows'] == [2, 3]


def test_extent_is_separate_from_identity_and_numeric_comma_is_retained():
    record = annotated(label='Egg, soft-boiled', code='A032B')
    assert record['facets']['base_identity'] == 'egg'
    assert 'source_label_disagreement:base_identity' not in record['issues']
    assert exact_description_key('Milk, 1,5% fat') != exact_description_key('Milk, 1 5% fat')


def test_description_evidence_is_distinct_from_structured_source_evidence():
    row = source(label='Chicken breast', code=None)
    row['metadata']['Description'] = 'Without skin, boneless; 10 samples'
    record = annotate_record(consolidate_sources([row])[0], Catalogue())
    assert record['facets']['skin_status'] == 'absent'
    assert record['facets']['bone_status'] == 'absent'
    assert record['source_facets'] == {}
    assert record['description_rule_facets']['skin_status'] == 'absent'
    assert any(item['method'] == 'description_rule' for item in record['annotation_evidence'])
    assert record['facets']['base_identity'] == 'chicken'
