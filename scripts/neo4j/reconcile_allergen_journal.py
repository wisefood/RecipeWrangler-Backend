"""Guarded selected-case graph repair/rollback; execution needs a controlled write window.

This module does not deploy code, alter mappings, install constraints or run a
corpus repair on import. The graph receipt is an outbox: repair and queue-pending
state commit together; PostgreSQL relay can retry after a process crash.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta, timezone
import hashlib
import json
import time
import pytz
from typing import Any

from neo4j.time import Date, DateTime, Time, Duration
from neo4j.spatial import CartesianPoint, WGS84Point

from scripts.neo4j.preview_allergen_reconciliation import QUERIES, ancestry, canonical, digest, encoded
from scripts.neo4j.preflight_allergen_reconciliation import compare_journal
from scripts.neo4j.rehearse_allergen_reconciliation import rehearse_journal

RECEIPT_CONSTRAINT = '''CREATE CONSTRAINT allergen_reconciliation_operation IF NOT EXISTS
FOR (n:AllergenReconciliation) REQUIRE n.operation_id IS UNIQUE'''
LOCK_KEY = '_rw_reconciliation_lock'
MAX_CASES = 4
MAX_RECIPES = 250
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
DERIVED_RECIPE_PROPERTIES = {
    'content_digest', 'projected_at', 'matching_projection_release',
    'matching_projection_status', 'matching_projected_revision',
    'matching_claims_source', 'matching_claims_version',
}


def typed_pack(value: Any) -> Any:
    """Keep temporal/spatial types and array ordering in durable rollback JSON."""
    if isinstance(value, (DateTime, Time)):
        zone = getattr(value, 'tzinfo', None)
        zone = getattr(zone, 'key', None) or getattr(zone, 'zone', None)
        native = value.to_native()
        offset = native.utcoffset()
        if value.tzinfo is not None and offset is None:
            raise ValueError('Timezone without a resolved offset cannot be journaled')
        fields = [value.hour,value.minute,value.second,value.nanosecond]
        if isinstance(value,DateTime):
            fields = [value.year,value.month,value.day]+fields
        # Driver ISO formatting can hand its custom DateTime to zoneinfo's C
        # implementation. Use native offset calculation and explicit fields.
        return {'__neo4j_type__':type(value).__name__, 'value':fields,
                'zone':zone,'offset_seconds':int(offset.total_seconds()) if offset is not None else None}
    if isinstance(value, (Date, Duration)):
        return {'__neo4j_type__':type(value).__name__, 'value':value.iso_format()}
    if isinstance(value, (CartesianPoint, WGS84Point)):
        return {'__neo4j_type__':type(value).__name__, 'value':list(value)}
    if isinstance(value, dict):
        if '__neo4j_type__' in value:
            raise ValueError('Reserved temporal codec key in source data')
        return {key:typed_pack(item) for key,item in value.items()}
    if isinstance(value, (list, tuple)):
        return [typed_pack(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError('Unsupported rollback property type: '+type(value).__name__)


def typed_unpack(value: Any) -> Any:
    if isinstance(value, dict) and '__neo4j_type__' in value:
        classes = {cls.__name__:cls for cls in (DateTime, Date, Time, Duration, CartesianPoint, WGS84Point)}
        cls = classes[value['__neo4j_type__']]
        if cls in (DateTime,Time):
            fields=value['value']
            offset=value['offset_seconds']
            tz=timezone(timedelta(seconds=offset)) if offset is not None else None
            result=cls(*fields[:-1],nanosecond=fields[-1],tzinfo=tz)
        else:
            result = cls(value['value']) if cls in (CartesianPoint, WGS84Point) else cls.from_iso_format(value['value'])
        if value.get('zone'):
            result = result.as_timezone(pytz.timezone(value['zone']))
        return result
    if isinstance(value, dict):
        return {key:typed_unpack(item) for key,item in value.items()}
    if isinstance(value, list):
        return [typed_unpack(item) for item in value]
    return value


def encode_receipt(value: dict) -> str:
    result = json.dumps(typed_pack(value), sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(result.encode()) > MAX_RECEIPT_BYTES:
        raise ValueError('Reconciliation receipt exceeds the reviewed size budget')
    return result


def decode_receipt(row: dict) -> dict:
    raw = row['payload_json']
    if hashlib.sha256(raw.encode()).hexdigest() != row['payload_sha256']:
        raise ValueError('Reconciliation receipt fingerprint changed')
    return typed_unpack(json.loads(raw))


def validate_scope(journal: dict) -> None:
    cases, recipes = journal['cases'], journal['affected_recipes']
    if not 1 <= len(cases) <= MAX_CASES or not 1 <= len(recipes) <= MAX_RECIPES:
        raise ValueError('Journal exceeds the reviewed case/recipe budget')
    for rows,key in [(cases,'ingredient_id'),(cases,'element_id'),(recipes,'recipe_id'),(recipes,'element_id')]:
        values = [row[key] for row in rows]
        if any(not isinstance(value,str) or not value for value in values) or len(set(values)) != len(values):
            raise ValueError('Journal identity is missing or duplicated')


def _rows(tx, query: str, **params) -> list[dict]:
    return [canonical(row.data()) for row in tx.run(query, **params)]


def _one(tx, query: str, **params) -> dict:
    rows = _rows(tx, query, **params)
    if len(rows) != 1:
        raise ValueError('Expected exactly one guarded graph operation result')
    return rows[0]


def _mutate(tx, query: str, **params) -> None:
    if _one(tx, query, **params).get('count') != 1:
        raise ValueError('Selected relationship disappeared or became ambiguous')


def capture_current(tx, journal: dict) -> dict:
    """Read bounded pages in this transaction, including complete inverse membership."""
    ids = [case['ingredient_id'] for case in journal['cases']]
    queries = dict(QUERIES)
    queries['ingredients'] = queries['ingredients'].replace(
        'WHERE elementId(i) > $after', 'WHERE elementId(i) > $after AND i.canonical_id IN $ids')
    queries['recipes'] = queries['recipes'].replace(
        'WHERE elementId(r) > $after', '''WHERE elementId(r) > $after AND EXISTS {
        MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient) WHERE i.canonical_id IN $ids }''')
    captured = {}
    deadline = time.monotonic()+55
    for kind,query in queries.items():
        rows,after = [],''
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('Guarded transaction capture budget exceeded')
            page = _rows(tx, query, after=after, limit=100, ids=ids)
            if not page:
                break
            if len(page) > 100 or page[-1]['element_id'] <= after:
                raise ValueError('Guarded capture pagination failed')
            rows.extend(page);after = page[-1]['element_id']
        captured[kind] = rows
    return captured


def recipe_inputs(tx, recipe_element_ids: list[str]) -> dict:
    """Protect full source properties/relationships while allowing our derived refresh."""
    from recipe_wrangler.utils.nutrition_claims import NUTRITION_CLAIM_TAG_NAMES
    query = QUERIES['recipes'].replace('WHERE elementId(r) > $after', 'WHERE elementId(r) IN $ids')
    query = query.replace('{recipe_id:r.recipe_id, id:r.id, title:r.title, source:r.source,\n  status:r.status, declared_allergens:r.declared_allergens,\n  declared_allergens_source:r.declared_allergens_source,\n  declared_allergens_updated_at:r.declared_allergens_updated_at}', 'properties(r)')
    query = query.replace('ORDER BY element_id\n', '' )
    # Recipe-level evidence must also remain untouched by selected ingredient repair.
    query = query.replace(' AS tags\n', ''' AS tags,
 [(r)-[l:HAS_ALLERGEN]->(a) | {element_id:elementId(l), properties:properties(l),
   target:elementId(a), allergen:a.name}] AS recipe_edges,
 [(r)-[l:HAS_DECLARATION]->(d) | {element_id:elementId(d), properties:properties(d),
   edge_id:elementId(l), edge_properties:properties(l), concerns:[(d)-[c:CONCERNS]->(a) |
   {element_id:elementId(c), properties:properties(c), target:elementId(a), allergen:a.name}]}] AS recipe_declarations
''')
    result = {}
    for start in range(0,len(recipe_element_ids),100):
        rows = _rows(tx, query, ids=recipe_element_ids[start:start+100], limit=100)
        for row in rows:
            row.pop('suitability')
            row['properties'] = {k:v for k,v in row['properties'].items() if k not in DERIVED_RECIPE_PROPERTIES}
            row['tags'] = [tag for tag in row['tags'] if tag['name'] not in NUTRITION_CLAIM_TAG_NAMES]
            result[row['element_id']] = row
    if set(result) != set(recipe_element_ids):
        raise ValueError('Recipe source guards are missing or duplicated')
    return result


def recipe_input_hashes(inputs: dict) -> dict[str,str]:
    """Untouched recipe sources need type-sensitive guards, not duplicate copies."""
    return {key:digest(typed_pack(value)) for key,value in inputs.items()}


def _lock_selected(tx, journal: dict, classes: list[dict], *, detached_declarations: list[dict] = (), additional_roots: list[str] = ()) -> None:
    """Lock existing entities; the controlled window additionally prevents phantoms."""
    by_class = {row['element_id']:row for row in classes}
    nodes = {row['element_id'] for row in journal['affected_recipes']}
    relationships = set()
    for declaration in detached_declarations:
        nodes.add(declaration['element_id'])
        relationships.update(concern['element_id'] for concern in declaration['concerns'])
    for root in additional_roots:
        nodes.update(ancestry(root,by_class))
    for case in journal['cases']:
        nodes.add(case['element_id'])
        link = case['link_operation']
        for root in [link['before']['target'],link['target_element_id']]:
            nodes.update(ancestry(root,by_class))
        relationships.add(link['before']['element_id'])
        for operation in case['evidence_operations']:
            for edge in operation.get('exact_edges', operation.get('exact_before_edges', [])):
                nodes.add(edge['target']);relationships.add(edge['element_id'])
            for declaration in operation.get('exact_declarations', operation.get('exact_before_declarations', [])):
                nodes.add(declaration['element_id']);relationships.add(declaration['edge_id'])
                relationships.update(c['element_id'] for c in declaration['concerns'])
    if len(nodes) > 2000 or len(relationships) > 100:
        raise ValueError('Lock scope exceeds the reviewed selected-case budget')
    for relationship,ids in [(False,sorted(nodes)),(True,sorted(relationships))]:
        match = 'MATCH ()-[n]->()' if relationship else 'MATCH (n)'
        for start in range(0,len(ids),100):
            batch = ids[start:start+100]
            rows = _rows(tx, match+' WHERE elementId(n) IN $ids RETURN elementId(n) AS id, n.'+LOCK_KEY+' AS existing', ids=batch)
            if len(rows) != len(batch) or any(row['existing'] is not None for row in rows):
                raise ValueError('Lock target missing or reserved lock property already exists')
            query = match+' WHERE elementId(n) IN $ids WITH n ORDER BY elementId(n) SET n.'+LOCK_KEY+'=true REMOVE n.'+LOCK_KEY+' RETURN count(n) AS count'
            if _one(tx,query,ids=batch)['count'] != len(batch):
                raise ValueError('Could not lock the complete selected scope')


def _save_receipt(tx, operation_id: str, receipt: dict, state: str) -> None:
    raw = encode_receipt(receipt)
    _mutate(tx, '''MATCH (n:AllergenReconciliation {operation_id:$operation})
        SET n.payload_json=$payload, n.payload_sha256=$fingerprint, n.state=$state,
            n.updated_at=datetime() RETURN count(n) AS count''',
        operation=operation_id,payload=raw,fingerprint=hashlib.sha256(raw.encode()).hexdigest(),state=state)


def _forward(tx, journal: dict, before: dict, classes: list[dict]) -> None:
    by_node = {row['element_id']:row for row in before['ingredients']}
    labels = {row['properties'].get('foodon_id'):row['properties'].get('label') for row in classes}
    for case in journal['cases']:
        node = case['element_id'];link = case['link_operation'];old = link['before']
        _mutate(tx, '''MATCH (i:Ingredient)-[old:HAS_CLASS]->(previous:FoodOnClass)
            WHERE elementId(i)=$node AND elementId(old)=$edge AND elementId(previous)=$previous
            MATCH (target:FoodOnClass) WHERE elementId(target)=$target
            DELETE old CREATE (i)-[new:HAS_CLASS]->(target)
            SET new=$properties, new.ancestor_level=false, new.linked_at=datetime()
            RETURN count(new) AS count''', node=node,edge=old['element_id'],previous=old['target'],
            target=link['target_element_id'],properties=link['properties'])
        for operation in case['evidence_operations']:
            edges = operation.get('exact_edges',operation.get('exact_before_edges'))
            declarations = operation.get('exact_declarations',operation.get('exact_before_declarations'))
            for declaration in declarations:
                _mutate(tx, '''MATCH (i:Ingredient)-[edge:HAS_DECLARATION]->(d:AllergenDeclaration)
                    WHERE elementId(i)=$node AND elementId(edge)=$edge AND elementId(d)=$target
                    DELETE edge RETURN count(*) AS count''',node=node,edge=declaration['edge_id'],target=declaration['element_id'])
            if operation['action'] == 'retire_selected_derived_pair':
                for edge in edges:
                    _mutate(tx, '''MATCH (i:Ingredient)-[edge:HAS_ALLERGEN]->(a:Allergen)
                        WHERE elementId(i)=$node AND elementId(edge)=$edge AND elementId(a)=$target
                        DELETE edge RETURN count(*) AS count''',node=node,edge=edge['element_id'],target=edge['target'])
                continue
            evidence = deepcopy(operation['proposed_evidence'])
            evidence['foodon_labels'] = [labels[root] for root in evidence.get('foodon_ids', [])]
            edge = edges[0]
            _mutate(tx, '''MATCH (i:Ingredient)-[edge:HAS_ALLERGEN]->(a:Allergen)
                WHERE elementId(i)=$node AND elementId(edge)=$edge AND elementId(a)=$target
                SET edge += $properties RETURN count(edge) AS count''',node=node,edge=edge['element_id'],target=edge['target'],properties=evidence)
            old_declaration = next(row for row in by_node[node]['declarations'] if row['element_id'] == declarations[0]['element_id'])
            properties = {**old_declaration['properties'],**evidence}
            declaration_id = 'ingredient:'+case['ingredient_id']+':allergen:'+operation['allergen']+':version:'+evidence['classification_version']
            properties['declaration_id'] = declaration_id
            if _one(tx,'MATCH (d:AllergenDeclaration {declaration_id:$id}) RETURN count(d) AS count',id=declaration_id)['count']:
                raise ValueError('New declaration identity already exists; refusing to overwrite it')
            _mutate(tx, '''MATCH (i:Ingredient),(a:Allergen) WHERE elementId(i)=$node AND elementId(a)=$target
                CREATE (i)-[attachment:HAS_DECLARATION]->(d:AllergenDeclaration)-[concern:CONCERNS]->(a)
                SET d=$properties, d.created_at=datetime(),d.updated_at=datetime(),
                    attachment=$attachment_properties,concern=$concern_properties
                RETURN count(d) AS count''',node=node,target=edge['target'],properties=properties,
                attachment_properties=old_declaration['edge_properties'],concern_properties=old_declaration['concerns'][0]['properties'])


def verify_after(journal: dict, before: dict, actual: dict, expected: dict) -> None:
    """Verify all fields, substituting only newly allocated IDs and timestamps."""
    by_before = {row['element_id']:row for row in before['ingredients']}
    normalized = deepcopy(actual['ingredients'])
    for row in normalized:
        case = next(case for case in journal['cases'] if case['element_id'] == row['element_id'])
        old = by_before[row['element_id']]
        new_links = [link for link in row['links'] if link['element_id'] not in {link['element_id'] for link in old['links']}]
        if len(new_links) != 1 or not isinstance(new_links[0]['properties'].get('linked_at'),DateTime):
            raise ValueError('Forward link allocation differs from rehearsal')
        new_links[0]['element_id'] = 'rehearsal:link:'+case['ingredient_id']
        new_links[0]['properties']['linked_at'] = 'rehearsal_timestamp_not_a_live_value'
        for declaration in row['declarations']:
            if declaration['element_id'] in {value['element_id'] for value in old['declarations']}:
                continue
            allergen = declaration['concerns'][0]['allergen'] if len(declaration['concerns']) == 1 else ''
            for key in ['created_at','updated_at']:
                if not isinstance(declaration['properties'].get(key),DateTime):
                    raise ValueError('New declaration lost timestamp types')
                declaration['properties'][key] = 'rehearsal_timestamp_not_a_live_value'
            declaration['element_id'] = 'rehearsal:declaration:'+case['ingredient_id']+':'+allergen
            declaration['edge_id'] = 'rehearsal:has_declaration:'+declaration['element_id']
            for concern in declaration['concerns']:
                concern['element_id'] = 'rehearsal:concerns:'+declaration['element_id']
    if digest(canonical(normalized)) != digest(canonical(expected['after'])):
        raise ValueError('Forward graph state differs from the reviewed rehearsal')
    if digest(actual['recipes']) != digest(before['recipes']) or digest(actual['classes']) != digest(before['classes']):
        raise ValueError('Recipe or ontology changed during reconciliation')


def apply_transaction(tx, journal: dict, *, operation_id: str, profile_guard: dict,
                      expected_classes: list[dict], controlled_window: bool = False) -> dict:
    """Atomically repair exact relationships and persist the complete durable outbox."""
    if not controlled_window:
        raise ValueError('A controlled input/ontology write window is required for dense-node membership guards')
    if not isinstance(operation_id,str) or not operation_id or len(operation_id)>110:
        raise ValueError('A bounded reviewed operation identity is required')
    validate_scope(journal)
    existing = _rows(tx,'MATCH (n:AllergenReconciliation {operation_id:$operation}) RETURN properties(n) AS receipt',operation=operation_id)
    if existing:
        if len(existing) != 1:
            raise ValueError('Reconciliation receipt identity is duplicated')
        receipt = decode_receipt(existing[0]['receipt'])
        if receipt['journal_sha256'] != digest(journal) or existing[0]['receipt']['state'] not in {'applied_queue_pending','applied_queued'}:
            raise ValueError('Operation was reused or has already been rolled back')
        return receipt
    _lock_selected(tx,journal,expected_classes)
    current = capture_current(tx,journal)
    if not compare_journal(journal,**current)['passed'] or digest(current['classes']) != digest(expected_classes):
        raise ValueError('Fresh journal/ontology preconditions changed')
    expected = rehearse_journal(journal,**json.loads(encoded(current)))
    ids = [row['element_id'] for row in journal['affected_recipes']]
    inputs = recipe_input_hashes(recipe_inputs(tx,ids))
    _mutate(tx,'''CREATE (n:AllergenReconciliation {operation_id:$operation, created_at:datetime()})
        RETURN count(n) AS count''',operation=operation_id)
    _forward(tx,journal,current,current['classes'])
    after = capture_current(tx,journal)
    verify_after(journal,current,after,expected)
    if recipe_input_hashes(recipe_inputs(tx,ids)) != inputs:
        raise ValueError('Recipe source inputs changed during reconciliation')
    receipt = {'version':'allergen-reconciliation-v1','operation_id':operation_id,
               'journal_sha256':digest(journal),'journal':journal,'profile_guard':profile_guard,
               'before_ingredients':current['ingredients'],'after_ingredients':after['ingredients'],
               'recipe_input_sha256':inputs,'ontology_sha256':digest(current['classes']),
               'recipe_ids':sorted(row['recipe_id'] for row in journal['affected_recipes']),
               'memberships':{case['element_id']:case['guarded_recipe_membership'] for case in journal['cases']}}
    _save_receipt(tx,operation_id,receipt,'applied_queue_pending')
    return receipt


def semantic_ingredient(row: dict) -> dict:
    """Rollback restores values/endpoints; deleted Neo4j relationship IDs change."""
    row = deepcopy(row)
    for key in ['links','edges']:
        for edge in row[key]:
            edge.pop('element_id')
    for declaration in row['declarations']:
        declaration.pop('edge_id')
    return canonical(row)


def _guard_detached_declarations(tx, receipt: dict) -> None:
    by_after = {row['element_id']:row for row in receipt['after_ingredients']}
    for before in receipt['before_ingredients']:
        after = by_after[before['element_id']]
        attached_after = {row['element_id'] for row in after['declarations']}
        for old in before['declarations']:
            if old['element_id'] in attached_after:
                continue
            row = _one(tx,'''MATCH (d:AllergenDeclaration) WHERE elementId(d)=$id
                RETURN elementId(d) AS element_id, properties(d) AS properties,
                [(d)-[c:CONCERNS]->(a) | {element_id:elementId(c),properties:properties(c),
                  target:elementId(a),allergen:a.name}] AS concerns''',id=old['element_id'])
            expected = {key:value for key,value in old.items() if key not in {'edge_id','edge_properties'}}
            if digest(row) != digest(expected):
                raise ValueError('Detached original declaration changed; rollback would reattach edited evidence')


def _reverse(tx, receipt: dict) -> None:
    by_after = {row['element_id']:row for row in receipt['after_ingredients']}
    for before in receipt['before_ingredients']:
        node = before['element_id'];after = by_after[node]
        for kind,rel_type in [('links','HAS_CLASS'),('edges','HAS_ALLERGEN')]:
            originals = {row['element_id']:row for row in before[kind]}
            current = {row['element_id']:row for row in after[kind]}
            for key,row in current.items():
                if key not in originals:
                    _mutate(tx,'''MATCH (i)-[edge:'''+rel_type+''']->(target)
                        WHERE elementId(i)=$node AND elementId(edge)=$edge AND elementId(target)=$target
                        DELETE edge RETURN count(*) AS count''',node=node,edge=key,target=row['target'])
                elif digest(row['properties']) != digest(originals[key]['properties']):
                    _mutate(tx,'''MATCH (i)-[edge:'''+rel_type+''']->(target)
                        WHERE elementId(i)=$node AND elementId(edge)=$edge AND elementId(target)=$target
                        SET edge=$properties RETURN count(edge) AS count''',
                        node=node,edge=key,target=row['target'],properties=originals[key]['properties'])
            for key,row in originals.items():
                if key not in current:
                    _mutate(tx,'''MATCH (i),(target) WHERE elementId(i)=$node AND elementId(target)=$target
                        CREATE (i)-[edge:'''+rel_type+''']->(target) SET edge=$properties
                        RETURN count(edge) AS count''',node=node,target=row['target'],properties=row['properties'])
        originals = {row['edge_id']:row for row in before['declarations']}
        current = {row['edge_id']:row for row in after['declarations']}
        for key,row in current.items():
            if key not in originals:
                _mutate(tx,'''MATCH (i)-[edge:HAS_DECLARATION]->(d)
                    WHERE elementId(i)=$node AND elementId(edge)=$edge AND elementId(d)=$target
                    DELETE edge RETURN count(*) AS count''',node=node,edge=key,target=row['element_id'])
        for key,row in originals.items():
            if key not in current:
                _mutate(tx,'''MATCH (i),(d) WHERE elementId(i)=$node AND elementId(d)=$target
                    CREATE (i)-[edge:HAS_DECLARATION]->(d) SET edge=$properties
                    RETURN count(edge) AS count''',node=node,target=row['element_id'],properties=row['edge_properties'])


def rollback_transaction(tx, operation_id: str, *, profile_guard: dict,
                         expected_classes: list[dict], controlled_window: bool = False) -> dict:
    """Restore selected evidence only if the exact after/source/membership guards hold."""
    if not controlled_window:
        raise ValueError('A controlled input/ontology write window is required for rollback')
    rows = _rows(tx,'MATCH (n:AllergenReconciliation {operation_id:$operation}) RETURN properties(n) AS receipt',operation=operation_id)
    if len(rows) != 1:
        raise ValueError('Reconciliation receipt is missing or duplicated')
    stored = rows[0]['receipt'];receipt = decode_receipt(stored)
    if stored['state'] in {'rolled_back_queue_pending','rolled_back_queued'}:
        return receipt
    if stored['state'] not in {'applied_queue_pending','applied_queued'}:
        raise ValueError('Reconciliation is not in an applied state')
    if digest(profile_guard) != digest(receipt['profile_guard']):
        raise ValueError('Profile/deployment owner versions changed before rollback')
    journal = receipt['journal'];validate_scope(journal)
    # The original selected relationships may have been deleted. Lock the
    # surviving current entities and all original declaration/ontology nodes.
    rollback_locks = deepcopy(journal)
    by_after = {row['element_id']:row for row in receipt['after_ingredients']}
    for case in rollback_locks['cases']:
        after = by_after[case['element_id']]
        before = next(row for row in receipt['before_ingredients'] if row['element_id'] == case['element_id'])
        original_links = {row['element_id'] for row in before['links']}
        new_link = next(row for row in after['links'] if row['element_id'] not in original_links)
        case['link_operation']['before'] = new_link
        case['evidence_operations'] = []
        # All still-attached declarations/edges are guarded by exact after hashes.
        for edge in after['edges']:
            case['evidence_operations'].append({'exact_edges':[edge], 'exact_declarations':[]})
        for declaration in after['declarations']:
            case['evidence_operations'].append({'exact_edges':[], 'exact_declarations':[declaration]})
    detached = [declaration for before in receipt['before_ingredients'] for declaration in before['declarations']
                if declaration['element_id'] not in {row['element_id'] for row in by_after[before['element_id']]['declarations']}]
    _lock_selected(tx,rollback_locks,expected_classes,detached_declarations=detached,
                   additional_roots=[case['link_operation']['before']['target'] for case in journal['cases']])
    current = capture_current(tx,journal)
    if digest(current['ingredients']) != digest(receipt['after_ingredients']):
        raise ValueError('Intervening ingredient changes prevent rollback')
    if digest(current['classes']) != receipt['ontology_sha256'] or digest(expected_classes) != receipt['ontology_sha256']:
        raise ValueError('Ontology changed before rollback')
    for node,members in receipt['memberships'].items():
        actual = sorted(row['element_id'] for row in current['recipes'] if node in {i['target'] for i in row['ingredients']})
        if actual != members:
            raise ValueError('Inverse recipe membership changed before rollback')
    ids = list(receipt['recipe_input_sha256'])
    if recipe_input_hashes(recipe_inputs(tx,ids)) != receipt['recipe_input_sha256']:
        raise ValueError('Recipe source inputs changed before rollback')
    _guard_detached_declarations(tx,receipt)
    _reverse(tx,receipt)
    restored = capture_current(tx,journal)
    before = {row['element_id']:semantic_ingredient(row) for row in receipt['before_ingredients']}
    actual = {row['element_id']:semantic_ingredient(row) for row in restored['ingredients']}
    if digest(typed_pack(actual)) != digest(typed_pack(before)):
        raise ValueError('Rollback failed to restore exact typed values and endpoints')
    if recipe_input_hashes(recipe_inputs(tx,ids)) != receipt['recipe_input_sha256']:
        raise ValueError('Recipe source inputs changed during rollback')
    receipt['restored_ingredients'] = restored['ingredients']
    _save_receipt(tx,operation_id,receipt,'rolled_back_queue_pending')
    return receipt


def read_receipt(tx, operation_id: str) -> tuple[str,dict]:
    row = _one(tx,'MATCH (n:AllergenReconciliation {operation_id:$operation}) RETURN properties(n) AS receipt',operation=operation_id)['receipt']
    return row['state'],decode_receipt(row)


def acknowledge_queue(tx, operation_id: str, direction: str, generations: dict[str,int]) -> dict:
    """Acknowledge only this phase; a delayed forward relay cannot undo rollback."""
    if direction not in {'forward','rollback'}:
        raise ValueError('Unknown reconciliation direction')
    _mutate(tx,'''MATCH (n:AllergenReconciliation {operation_id:$operation})
        SET n._relay_lock=true REMOVE n._relay_lock RETURN count(n) AS count''',operation=operation_id)
    state,receipt = read_receipt(tx,operation_id)
    prefix = 'applied' if direction == 'forward' else 'rolled_back'
    if state not in {prefix+'_queue_pending',prefix+'_queued'}:
        raise ValueError('Queue acknowledgement arrived for an obsolete reconciliation phase')
    if set(generations) != set(receipt['recipe_ids']) or any(type(value) is not int or value < 1 for value in generations.values()):
        raise ValueError('Incomplete sibling queue acknowledgement')
    key = direction+'_generations'
    if key in receipt and receipt[key] != generations:
        raise ValueError('Reconciliation queue generation receipt changed')
    receipt[key] = generations
    _save_receipt(tx,operation_id,receipt,prefix+'_queued')
    return receipt


def _graph_driver():
    import os
    from neo4j import GraphDatabase
    from recipe_wrangler.utils.env_loader import load_runtime_env
    load_runtime_env()
    auth=os.getenv('NEO4J_AUTH')
    pair=tuple(auth.split('/',1)) if auth else (os.getenv('NEO4J_USERNAME') or os.getenv('NEO4J_USER'),os.getenv('NEO4J_PASSWORD'))
    return GraphDatabase.driver(os.getenv('NEO4J_URI','bolt://localhost:7687'),auth=pair,
        max_connection_pool_size=1,max_transaction_retry_time=0,
        connection_timeout=5,connection_acquisition_timeout=5)


def _postgres_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from recipe_wrangler.utils.nutrition_postgres import _get_config
    cfg=_get_config()
    return create_engine(URL.create('postgresql+psycopg2',username=cfg['db_user'],password=cfg['db_password'],
        host=cfg['db_host'],port=int(cfg['db_port']),database=cfg['db_name']),
        pool_size=1,max_overflow=0,connect_args={'connect_timeout':5})


def owner_guard(state: dict) -> dict:
    """Queue generations legitimately change; nutritional owner versions must not."""
    return {key:state[key] for key in ['profiles','deployment']}


def check_prerequisites(expected_index: str | None = None, *, api_base: str = 'http://127.0.0.1:8001') -> dict:
    """GET/read-only schema checks. Never install a constraint or patch a mapping."""
    from urllib.parse import quote
    from sqlalchemy import text
    from neo4j import READ_ACCESS, unit_of_work
    from recipe_wrangler.api.config import get_settings
    from recipe_wrangler.catalog.elastic import get_catalog_client
    from recipe_wrangler.utils.consumer_suitability import composition_policy_versions
    from recipe_wrangler.matching.projection_worker import PROJECTION_PIPELINE_VERSION
    from recipe_wrangler.utils.http_pool import get_http_session
    import requests
    client=get_catalog_client()
    settings=get_settings()
    mapping={}
    alias_targets={}
    for alias in dict.fromkeys([settings.catalog_recipes_alias,settings.elastic_index]):
        current=client._request('GET',quote(alias,safe='')+'/_mapping',timeout=60)
        alias_targets[alias]=sorted(current)
        mapping.update(current)
    targets=sorted(mapping)
    properties=next(iter(mapping.values())).get('mappings',{}).get('properties',{}) if len(mapping)==1 else {}
    mapping_ready=(len(targets)==1 and (expected_index is None or targets==[expected_index])
        and properties.get('allergen_uncertainty',{}).get('type')=='boolean'
        and properties.get('allergen_assessment',{}).get('type')=='object'
        and properties.get('allergen_assessment',{}).get('enabled') is False
        and properties.get('allergen_evidence',{}).get('type')=='nested'
        and properties.get('consumer_suitability',{}).get('type')=='nested')
    engine=_postgres_engine()
    try:
        with engine.connect() as connection,connection.begin():
            connection.execute(text('SET TRANSACTION READ ONLY'))
            connection.execute(text("SET LOCAL statement_timeout='60s'"))
            columns={row[0]:row[1] for row in connection.execute(text('''SELECT column_name,data_type
                FROM information_schema.columns WHERE table_schema='public'
                AND table_name='matching_reconciliation_receipts' ORDER BY column_name LIMIT 100'''))}
            table_ready=all(columns.get(key)==value for key,value in
                {'operation_id':'text','recipe_ids':'jsonb','generations':'jsonb'}.items())
            unique=connection.execute(text('''SELECT EXISTS(SELECT 1 FROM information_schema.table_constraints t
                JOIN information_schema.key_column_usage k USING(constraint_catalog,constraint_schema,constraint_name)
                WHERE t.table_schema='public' AND t.table_name='matching_reconciliation_receipts'
                  AND t.constraint_type='PRIMARY KEY'
                GROUP BY t.constraint_name HAVING count(*)=1 AND min(k.column_name::text)='operation_id')''')).scalar()
            table_ready=table_ready and bool(unique)
    finally:
        engine.dispose()
    @unit_of_work(timeout=60)
    def schema(tx):
        return _rows(tx,'''SHOW CONSTRAINTS YIELD labelsOrTypes,properties,type
            WHERE 'AllergenReconciliation' IN labelsOrTypes
              AND properties=['operation_id'] AND type='UNIQUENESS'
            RETURN labelsOrTypes,properties,type LIMIT 100''')
    with _graph_driver() as driver,driver.session(default_access_mode=READ_ACCESS,fetch_size=100) as session:
        graph_ready=bool(session.execute_read(schema))
    api_ready=False
    api_error=None
    try:
        response=get_http_session().get(api_base.rstrip('/')+'/health/matching',timeout=(5,60))
        response.raise_for_status()
        health=response.json()
        api_ready=(health.get('composition_policy_versions')==composition_policy_versions()
                   and health.get('projection_pipeline_version')==PROJECTION_PIPELINE_VERSION
                   and health.get('mode')=='new' and health.get('runtime_mode')=='new')
    except (requests.RequestException,ValueError,AttributeError) as exc:
        api_error=type(exc).__name__
    return {'ready':bool(mapping_ready and table_ready and graph_ready and api_ready),'mapping_ready':mapping_ready,
            'mapping_targets':targets,'mapping_aliases':alias_targets,'queue_receipt_schema_ready':table_ready,
            'graph_receipt_constraint_ready':graph_ready,'api_policy_ready':api_ready,
            'api_check_error_type':api_error,'writes_executed':False}


def relay_queue(operation_id: str, *, expected_journal_sha256: str | None = None) -> dict:
    """Resume the durable outbox using serial graph/PG/graph connections."""
    from neo4j import READ_ACCESS, unit_of_work
    from sqlalchemy import text
    from recipe_wrangler.matching.reconciliation_queue import enqueue_reconciliation
    @unit_of_work(timeout=60)
    def read(tx):return read_receipt(tx,operation_id)
    with _graph_driver() as driver,driver.session(default_access_mode=READ_ACCESS,fetch_size=100) as session:
        state,receipt=session.execute_read(read)
    if expected_journal_sha256 is not None and receipt['journal_sha256'] != expected_journal_sha256:
        raise ValueError('Queue relay journal does not match the reviewed operation')
    if state in {'applied_queued','rolled_back_queued'}:
        return receipt
    if state not in {'applied_queue_pending','rolled_back_queue_pending'}:
        raise ValueError('Reconciliation is not awaiting a queue handoff')
    direction='forward' if state=='applied_queue_pending' else 'rollback'
    engine=_postgres_engine()
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout='60s'"))
            generations=enqueue_reconciliation(connection,operation_id+':'+direction,receipt['recipe_ids'])
    finally:
        engine.dispose()
    @unit_of_work(timeout=60)
    def acknowledge(tx):return acknowledge_queue(tx,operation_id,direction,generations)
    with _graph_driver() as driver,driver.session(fetch_size=100) as session:
        return session.execute_write(acknowledge)


def main() -> None:
    import argparse
    from pathlib import Path
    from neo4j import unit_of_work
    from scripts.neo4j.preflight_allergen_reconciliation import preflight,profile_state
    from scripts.neo4j.preview_allergen_reconciliation import read_rows
    from recipe_wrangler.utils.env_loader import load_runtime_env
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['check','apply','rollback','relay'],default='check')
    parser.add_argument('--journal',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--operation-id')
    parser.add_argument('--expected-journal-sha256')
    parser.add_argument('--expected-index')
    parser.add_argument('--api-base',default='http://127.0.0.1:8001')
    parser.add_argument('--controlled-window',action='store_true',
        help='Operator confirms recipe/profile/ontology writers and the projection worker are quiesced for the selected repair window.')
    args=parser.parse_args()
    raw=args.journal.read_bytes();file_hash=hashlib.sha256(raw).hexdigest()
    journal=json.loads(raw);validate_scope(journal)
    if args.mode in {'apply','rollback'}:
        if not args.controlled_window or args.expected_journal_sha256!=file_hash:
            parser.error('apply/rollback require the reviewed file SHA-256 and --controlled-window')
    if args.mode=='apply' and not args.expected_index:
        parser.error('apply requires the reviewed --expected-index')
    if args.mode in {'rollback','relay'} and not args.operation_id:
        parser.error('rollback/relay require --operation-id from the durable graph receipt')
    args.out.mkdir(parents=True,exist_ok=False)
    load_runtime_env()
    operation_id=args.operation_id or 'allergen:'+file_hash
    result={'mode':args.mode,'operation_id':operation_id,'journal_file_sha256':file_hash,
            'stored_writes':False,'deployment':False,'phase':'starting'}
    try:
        if args.mode=='apply':
            @unit_of_work(timeout=60)
            def find_existing(tx):
                return _rows(tx,'MATCH (n:AllergenReconciliation {operation_id:$operation}) RETURN properties(n) AS receipt',operation=operation_id)
            with _graph_driver() as driver,driver.session(fetch_size=100) as session:
                existing=session.execute_read(find_existing)
            if existing:
                if len(existing)!=1:
                    raise ValueError('Reconciliation receipt identity is duplicated')
                receipt=decode_receipt(existing[0]['receipt'])
                if receipt['journal_sha256']!=digest(journal) or existing[0]['receipt']['state'] not in {'applied_queue_pending','applied_queued'}:
                    raise ValueError('Existing operation was reused or already rolled back')
                result.update(phase=existing[0]['receipt']['state'],existing_repair=True)
                (args.out/'receipt.json').write_text(encode_receipt(receipt)+'\n')
                relay_queue(operation_id,expected_journal_sha256=digest(journal))
                result.update(phase='applied_queued',recipe_count=len(receipt['recipe_ids']),
                              stored_writes=existing[0]['receipt']['state']=='applied_queue_pending')
                (args.out/'run.json').write_text(json.dumps(result,indent=2)+'\n')
                print(json.dumps(result,indent=2))
                return
        if args.mode=='relay':
            result['phase']='queue_relay'
            receipt=relay_queue(operation_id,expected_journal_sha256=digest(journal))
            result.update(stored_writes=True,phase='queued',recipe_count=len(receipt['recipe_ids']))
        elif args.mode=='rollback':
            @unit_of_work(timeout=60)
            def read_rollback_receipt(tx):return read_receipt(tx,operation_id)[1]
            with _graph_driver() as driver,driver.session(fetch_size=100) as session:
                original=session.execute_read(read_rollback_receipt)
            if original['journal_sha256'] != digest(journal):
                raise ValueError('Rollback journal does not match the reviewed operation')
            classes=read_rows(Path(journal['rollback_inputs']['classes']))
            state=profile_state(original['recipe_ids'],time.monotonic()+600)
            @unit_of_work(timeout=60)
            def rollback(tx):return rollback_transaction(tx,operation_id,profile_guard=owner_guard(state),
                expected_classes=classes,controlled_window=True)
            result['phase']='rollback_transaction'
            with _graph_driver() as driver,driver.session(fetch_size=100) as session:
                receipt=session.execute_write(rollback)
            result.update(stored_writes=True,phase='rolled_back_queue_pending')
            # Persist a local receipt before any cross-database handoff.
            (args.out/'receipt.json').write_text(encode_receipt(receipt)+'\n')
            relay_queue(operation_id,expected_journal_sha256=digest(journal));result['phase']='rolled_back_queued'
        else:
            result['phase']='fresh_preflight'
            fresh=preflight(args.journal,args.out/'fresh')
            if not fresh['passed'] or fresh['pending_jobs']:
                raise ValueError('Fresh stable guards and an idle affected projection queue are required')
            result['prerequisites']=check_prerequisites(args.expected_index,api_base=args.api_base)
            if args.mode=='check':
                result.update(phase='checked',ready=result['prerequisites']['ready'])
            else:
                if not result['prerequisites']['ready']:
                    raise ValueError('Reviewed mapping/receipt schema prerequisites are not installed')
                classes=read_rows(args.out/'fresh/classes.1.jsonl.gz')
                state=json.loads((args.out/'fresh/postgres_state.1.json').read_text())
                @unit_of_work(timeout=60)
                def apply(tx):return apply_transaction(tx,journal,operation_id=operation_id,
                    profile_guard=owner_guard(state),expected_classes=classes,controlled_window=True)
                result['phase']='apply_transaction'
                with _graph_driver() as driver,driver.session(fetch_size=100) as session:
                    receipt=session.execute_write(apply)
                result.update(stored_writes=True,phase='applied_queue_pending')
                (args.out/'receipt.json').write_text(encode_receipt(receipt)+'\n')
                after=profile_state(receipt['recipe_ids'],time.monotonic()+600)
                if digest(owner_guard(after))!=digest(receipt['profile_guard']):
                    raise ValueError('Owner versions changed during controlled window; durable queue relay awaits review')
                relay_queue(operation_id,expected_journal_sha256=digest(journal));result['phase']='applied_queued'
    except Exception as exc:
        if result['phase'] in {'apply_transaction','rollback_transaction'}:
            result['stored_writes']=None
            result['commit_state']='unknown_until_durable_receipt_is_checked'
        elif result['phase'] in {'queue_relay','applied_queue_pending','rolled_back_queue_pending'}:
            if result['stored_writes'] is False:
                result['stored_writes']=None
            result['queue_handoff_state']='inspect_durable_graph_and_postgres_receipts_before_retry'
        result['error_type']=type(exc).__name__
        result['error']=str(exc) if isinstance(exc,(ValueError,TimeoutError)) else 'Operation failed; inspect durable receipt before retrying.'
        (args.out/'run.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
        raise SystemExit(1)
    (args.out/'run.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
