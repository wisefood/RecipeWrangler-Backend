import csv
import json

import pytest

from scripts.composition.export_langual_json import export_langual


def source_tables(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    def write(name, fields, rows):
        with (source / name).open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)
    write('terms.csv', ['code', 'facet', 'label', 'scope_note', 'additional_information', 'active', 'classification_only'], [
        ['00000', 'root', 'Root', '', '', '1', '1'],
        ['G0014', 'G', 'BOILED', 'Cooking in boiling water', 'Source note', '1', '0'],
        ['G9999', 'G', 'Old term', '', '', '0', '0'],
    ])
    write('synonyms.csv', ['code', 'language_id', 'original_language_code', 'label', 'relation'],
          [['G0014', '1', 'en UK', 'boiling', 'untyped_source_synonym']])
    write('parents.csv', ['parent_code', 'child_code', 'grandparent_context'], [['00000', 'G0014', '']])
    write('related_terms.csv', ['source_code', 'related_code', 'source_code_raw', 'related_code_raw'],
          [['G0014', 'G9999', 'G0014', 'G9999 ']])
    write('localized_terms.csv', ['code', 'language_id', 'original_language_code', 'label'], [['G0014', '1', 'en UK', 'BOILED']])
    write('languages.csv', ['ID', 'Language'], [['1', 'en UK']])
    (source / 'summary.json').write_text(json.dumps({
        'version': '2017', 'source_mdb_sha256': 'snapshot-hash',
        'term_records': 3, 'synonym_rows': 1, 'hierarchy_rows': 1,
        'related_term_rows': 1, 'localized_term_rows': 1,
        'facets': {'G': {'name': 'cooking_method', 'terms': 2}},
    }))
    return source


def test_preserves_source_semantics_and_prevents_overwrite(tmp_path):
    source = source_tables(tmp_path)
    output = tmp_path / 'langual'
    release = export_langual(source, output)
    assert release['counts']['term_records'] == 3
    assert len(release['input_sha256']['terms.csv']) == 64
    terms = json.loads((output / 'terms.json').read_text())['terms']
    assert terms[0]['classification_only'] is True
    assert terms[-1]['active'] is False
    synonyms = json.loads((output / 'synonyms.json').read_text())['synonyms']
    assert synonyms[0]['relation'] == 'untyped_source_synonym'
    assert synonyms[0]['original_language_code'] == 'en UK'
    related = json.loads((output / 'relationships.json').read_text())['related_terms']
    assert related[0]['related_code_raw'] == 'G9999 '
    assert json.loads((output / 'reviewed_mappings.json').read_text())['mappings'] == []
    with pytest.raises(FileExistsError):
        export_langual(source, output)


def test_unknown_reference_fails_before_export(tmp_path):
    source = source_tables(tmp_path)
    path = source / 'parents.csv'
    path.write_text(path.read_text().replace('G0014', 'MISSING'))
    output = tmp_path / 'langual'
    with pytest.raises(ValueError, match='Unknown parent or child'):
        export_langual(source, output)
    assert not output.exists()
