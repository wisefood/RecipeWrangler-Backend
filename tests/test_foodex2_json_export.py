import json
import zipfile

import pytest

from scripts.composition.export_foodex2_json import export_catalogue


def make_catalogue(tmp_path, parent='root'):
    path = tmp_path / 'MTX.ecf'
    terms = ''.join(
        f'<term><termDesc><termCode>{code}</termCode><termExtendedName>{label}</termExtendedName>'
        f'<termScopeNote>{label} definition</termScopeNote></termDesc>'
        f'<termVersion><status>{status}</status></termVersion><hierarchyAssignments>'
        f'<hierarchyAssignment><hierarchyCode>{hierarchy}</hierarchyCode><parentCode>{parent}</parentCode>'
        '</hierarchyAssignment></hierarchyAssignments></term>'
        for code, label, status, hierarchy in [
            ('A07GL', 'Boiling', 'APPROVED', 'process'),
            ('A07GR', 'Frying', 'APPROVED', 'process'),
            ('A000A', 'Example food category', 'DEPRECATED', 'report'),
        ]
    )
    hierarchies = ''.join(
        f'<hierarchy><hierarchyDesc><code>{code}</code><label>{code}</label>'
        f'<hierarchyApplicability>{applicability}</hierarchyApplicability></hierarchyDesc></hierarchy>'
        for code, applicability in [('process', 'attribute'), ('report', 'base')]
    )
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('MTX.xml', '<catalogue><catalogueVersion><version>test</version></catalogueVersion>' + hierarchies + terms + '</catalogue>')
    return path


def test_exports_source_terms_separately_from_local_mappings(tmp_path):
    path = make_catalogue(tmp_path)
    output = tmp_path / 'foodex2'
    release = export_catalogue(path, output)
    assert release['term_count'] == 3
    assert release['food_term_count'] == 1
    assert release['facet_counts'] == {'process': 2}
    assert len(release['archive_sha256']) == 64
    food = json.loads((output / 'food_terms.json').read_text())['terms']
    assert food[0]['status'] == 'DEPRECATED'
    assert food[0]['hierarchy_assignments'] == [{'hierarchy': 'report', 'parent_code': 'root'}]
    mappings = json.loads((output / 'reviewed_mappings.json').read_text())['mappings']
    assert mappings[0]['internal_assertions']['cooking_method'] == 'boiled'
    assert all(mapping['runtime_enabled'] is False for mapping in mappings)
    with pytest.raises(FileExistsError):
        export_catalogue(path, output)


def test_invalid_parent_fails_before_creating_exports(tmp_path):
    path = make_catalogue(tmp_path, parent='UNKNOWN')
    output = tmp_path / 'foodex2'
    with pytest.raises(ValueError, match='Unknown parent'):
        export_catalogue(path, output)
    assert not output.exists()
