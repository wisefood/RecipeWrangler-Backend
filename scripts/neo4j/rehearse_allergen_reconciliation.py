"""Pure in-memory journal rehearsal and guarded rollback; no database writer."""
from __future__ import annotations

from copy import deepcopy

from scripts.neo4j.preflight_allergen_reconciliation import compare_journal
from scripts.neo4j.preview_allergen_reconciliation import canonical, digest, ontology_support
from recipe_wrangler.utils.food_ontology import CLASSIFICATION_VERSION


def rehearse_journal(journal: dict, ingredients: list[dict], recipes: list[dict], classes: list[dict]) -> dict:
    """Apply only selected relationship operations to copies of captured rows."""
    from recipe_wrangler.repositories.neo4j_recipes import _allergen_keyword_hits
    selected_ids = {case['element_id'] for case in journal['cases']}
    preflight = compare_journal(journal, [r for r in ingredients if r['element_id'] in selected_ids], recipes, classes)
    if not preflight['passed']:
        raise ValueError('Journal preconditions changed')
    after = deepcopy(ingredients)
    by_node = {r['element_id']:r for r in after}
    by_class = {r['element_id']:r for r in classes}
    labels = {r['properties'].get('foodon_id'):r['properties'].get('label') for r in classes}
    for case in journal['cases']:
        row = by_node[case['element_id']]
        link = case['link_operation']
        if row['links'].count(link['before']) != 1:
            raise ValueError('Selected link is missing or duplicated')
        row['links'].remove(link['before'])
        target = by_class[link['target_element_id']]
        row['links'].append({'element_id':'rehearsal:link:'+case['ingredient_id'],
                             'target':link['target_element_id'], 'foodon_id':link['target_foodon_id'],
                             'label':target['properties'].get('label'),
                             'properties':{**link['properties'], 'ancestor_level':False,
                                           'linked_at':'rehearsal_timestamp_not_a_live_value'}})
        for operation in case['evidence_operations']:
            action = operation['action']
            if action not in {'retire_selected_derived_pair', 'refresh_selected_supported_pair'}:
                raise ValueError('Unsupported journal action')
            old_edges = operation.get('exact_edges', operation.get('exact_before_edges'))
            old_declarations = operation.get('exact_declarations', operation.get('exact_before_declarations'))
            if old_edges is None or old_declarations is None:
                raise ValueError('Missing exact evidence preconditions')
            for value in old_edges + old_declarations:
                props = value['properties']
                if (props.get('classification_version') not in {'fato-foodon-v1', 'fato-foodon-v2', 'fato-foodon-v3', 'fato-foodon-v4', 'fato-foodon-v5', CLASSIFICATION_VERSION}
                    or props.get('evidence_status') != 'inferred' or props.get('presence') != 'contains'
                    or not props.get('sources') or not set(props['sources']) <= {'keyword', 'foodon'}
                    or ('concerns' in value and (len(value['concerns']) != 1
                        or value['concerns'][0]['allergen'] != operation['allergen']
                        or props.get('declaration_type') != 'inferred_ingredient_presence'))):
                    raise ValueError('Journal includes foreign or ambiguous declaration ownership')
            roots = ontology_support(row['properties']['name'], row['links'], by_class).get(operation['allergen'], [])
            hits = _allergen_keyword_hits(row['properties']['name'], operation['allergen'])
            if action == 'retire_selected_derived_pair' and (roots or hits):
                raise ValueError('Retirement would remove independently supported evidence')
            for edge in old_edges:
                if row['edges'].count(edge) != 1 or edge['allergen'] != operation['allergen']:
                    raise ValueError('Selected allergen edge changed')
                row['edges'].remove(edge)
            for declaration in old_declarations:
                if row['declarations'].count(declaration) != 1:
                    raise ValueError('Selected declaration changed')
                # A future writer must detach only the selected HAS_DECLARATION.
                # Shared/orphan declaration-node cleanup is outside this journal.
                row['declarations'].remove(declaration)
            if action == 'refresh_selected_supported_pair':
                if not roots and not hits:
                    raise ValueError('Supported refresh has no current root or keyword support')
                if len(old_edges) != 1 or len(old_declarations) != 1:
                    raise ValueError('Supported refresh requires one exact edge and declaration')
                evidence = deepcopy(operation['proposed_evidence'])
                if evidence.get('presence') != 'contains' or not evidence.get('sources'):
                    raise ValueError('Supported refresh lacks positive evidence')
                if set(evidence.get('foodon_ids', [])) != {root['root'] for root in roots} or evidence.get('keyword_matches', []) != hits:
                    raise ValueError('Supported refresh does not match current roots and keywords')
                evidence['foodon_labels'] = [labels[root] for root in evidence.get('foodon_ids', [])]
                edge = deepcopy(old_edges[0])
                edge['properties'].update(evidence)
                row['edges'].append(edge)
                declaration = deepcopy(old_declarations[0])
                declaration['element_id'] = 'rehearsal:declaration:'+case['ingredient_id']+':'+operation['allergen']
                declaration['edge_id'] = 'rehearsal:has_declaration:'+declaration['element_id']
                declaration['properties'].update(evidence)
                declaration['properties']['declaration_id'] = (
                    'ingredient:'+case['ingredient_id']+':allergen:'+operation['allergen']
                    +':version:'+evidence['classification_version'])
                declaration['properties']['created_at'] = 'rehearsal_timestamp_not_a_live_value'
                declaration['properties']['updated_at'] = 'rehearsal_timestamp_not_a_live_value'
                for concern in declaration['concerns']:
                    concern['element_id'] = 'rehearsal:concerns:'+declaration['element_id']
                row['declarations'].append(declaration)
        by_node[case['element_id']] = canonical(row)
    after = [by_node[r['element_id']] for r in after]
    before = {r['element_id']:deepcopy(r) for r in ingredients if r['element_id'] in selected_ids}
    return {'kind':'in_memory_only', 'stored_writes':False, 'after':after,
            'rollback':{'before_ingredients':before,
                        'expected_after_sha256':{key:digest(by_node[key]) for key in selected_ids},
                        'recipe_guards':deepcopy(journal['affected_recipes']),
                        'memberships':{c['element_id']:c['guarded_recipe_membership'] for c in journal['cases']}}}


def rollback_rehearsal(rehearsal: dict, current_ingredients: list[dict], current_recipes: list[dict]) -> list[dict]:
    """Restore selected copies only if after-state and recipe/membership guards agree."""
    rollback = rehearsal['rollback']
    by_node = {r['element_id']:r for r in current_ingredients}
    by_recipe = {r['element_id']:r for r in current_recipes}
    for key, expected in rollback['expected_after_sha256'].items():
        if key not in by_node or digest(by_node[key]) != expected:
            raise ValueError('Rollback would overwrite intervening ingredient changes')
        memberships = sorted(r['element_id'] for r in current_recipes if key in {i['target'] for i in r['ingredients']})
        if memberships != rollback['memberships'][key]:
            raise ValueError('Rollback inverse membership changed')
    for guard in rollback['recipe_guards']:
        if guard['element_id'] not in by_recipe or digest(by_recipe[guard['element_id']]) != guard['before_sha256']:
            raise ValueError('Rollback would overwrite intervening recipe changes')
    return [deepcopy(rollback['before_ingredients'].get(r['element_id'], r)) for r in current_ingredients]
