"""Evidence-based replacement policy; never uses recipe reference nutrients."""
from __future__ import annotations
import copy
import re

from recipe_wrangler.utils.food_identity import compare_identities
from recipe_wrangler.matching.normalization import label_states_reduced_fat, normalized_value

SPECIAL_PARTS = re.compile(r'\b(head|brain|brains|liver|kidney|kidneys|heart|tongue|lung|lungs|offal|feet|trotters)\b', re.I)
STATE_SENSITIVE = re.compile(r'\b(beef|pork|lamb|veal|mutton|chicken|turkey|duck|goose|fish|cod|salmon|tuna|haddock|prawn|shrimp|egg|eggs|milk|yoghurt|yogurt|cheese|cream|rice|pasta|lentil|lentils|bean|beans|chickpeas|flour|oats)\b', re.I)
MEAT = re.compile(r'\b(pork|beef|lamb|veal|chicken|turkey|duck|goose|mutton)\b', re.I)


def material_scope_conflicts(query: dict, candidate: dict) -> list[str]:
    """Reject unsupported material refinements and explicit state conflicts.

    These checks apply to aliases, retrieved candidates and final calculations.
    Unknown source properties alone do not establish a contradiction.
    """
    q,c=query['facets'],candidate['facets']
    conflicts=[]
    qtext=query.get('input_text','');ctext=candidate.get('input_text','')
    if re.search(r'\byogh?urt\b',qtext,re.I) and re.search(r'\byogh?urt\b',ctext,re.I):
        flavours=r'\b(?:fruit|toffee|hazelnut|vanilla|chocolate|strawberry|raspberry|peach|apricot|cherry|honey)\b'
        if set(re.findall(flavours,ctext.casefold()))-set(re.findall(flavours,qtext.casefold())):
            conflicts.append('product.unsupported_yogurt_flavour')
    # Matching a shared product word does not preserve what is being substituted.
    if re.search(r'\begg\s+(?:substitute|replacement)\b',qtext,re.I) and not re.search(r'\beggs?\b',ctext,re.I):
        conflicts.append('identity.egg_substitute_target_conflict')
    if re.fullmatch(r'\s*(?:cooking\s+)?liquid\s*',qtext,re.I):
        conflicts.append('identity.unspecified_liquid')
    if re.search(r'\b(?:beef|veal|pork|lamb|chicken|turkey|fish)\s+bones?\b',qtext,re.I):
        conflicts.append('basis.bones_require_edible_yield')
    grain_berries=re.search(r'\b(kamut|wheat|spelt|rye)\s+berries\b',qtext,re.I)
    if grain_berries:
        grain=grain_berries[1].casefold()
        equivalents='kamut|khorasan|wheat' if grain=='kamut' else re.escape(grain)
        if not re.search(r'\b(?:'+equivalents+r')\b',ctext,re.I):
            conflicts.append('identity.grain_berries_not_fruit')
    dried=r'\b(?:dried|dehydrated|lyophili[sz]ed)\b'
    dry_input=re.search(r'\b(?:dried|dry|dehydrated|lyophili[sz]ed|powder(?:ed)?|granules?)\b',qtext,re.I)
    conventional_dried=(re.search(r'\b(?:dates?|raisins?|sultanas?|currants?|prunes)\b',qtext,re.I)
        and not re.search(r'\bfresh\b',qtext,re.I) and not re.search(r'\blyophili[sz]ed\b',ctext,re.I)
        and not re.search(r'\bprune\s+plums\b',qtext,re.I))
    if re.search(dried,ctext,re.I) and not dry_input and not conventional_dried and 'dried' not in q.get('preservation_methods',[]):
        from recipe_wrangler.tools.nutrition_match import food_class
        if food_class(qtext) in {'fruit','vegetable','leafy_green'}:
            conflicts.append('product.unsupported_produce_drying')
    if re.search(r'\bmilk\b',qtext,re.I) and re.search(r'\bmilk\b',ctext,re.I):
        for species in ['goat','sheep','ewe','buffalo','donkey','human','breast']:
            if re.search(r'\b'+species+r"(?:'s|s)?\b",ctext,re.I) and not re.search(r'\b'+species+r"(?:'s|s)?\b",qtext,re.I):
                conflicts.append('product.unsupported_milk_source')
    for subtype in ['brown','black','wild']:
        pattern=r'\b'+subtype+r'\s+rice\b|\brice\s*,?\s*'+subtype+r'\b'
        if re.search(pattern,qtext,re.I) and not re.search(pattern,ctext,re.I):
            conflicts.append('product.rice_subtype_lost')
    # Explicit product words remain material even without an ontology head hit.
    def surface_style(annotation):
        label=annotation.get('input_text','')
        if re.search(r'\bsurimi\b',label,re.I):return 'surimi'
        if re.search(r'\bspoon dumplings?\b',label,re.I):return 'spoon_dumpling'
        return annotation.get('qualifiers',{}).get('product_style')
    style=surface_style(candidate)
    if style and style!=surface_style(query):
        conflicts.append('product.unsupported_subtype')
    def variety(annotation):
        label=annotation.get('input_text','')
        if re.search(r'\bbananas?\b',label,re.I) and re.search(r'\bred\b',label,re.I):return 'red_banana'
        return annotation['facets'].get('variety')
    if variety(candidate) and variety(candidate)!=variety(query):
        conflicts.append('product.unsupported_variety')
    def packing(annotation):
        value=annotation['facets'].get('packing_medium')
        if value:return value
        match=re.search(r'\bin (?:light |heavy )?(syrup|oil)\b',annotation.get('input_text',''),re.I)
        return match[1].casefold() if match else None
    if packing(candidate) in {'oil','syrup'} and packing(candidate)!=packing(query):
        conflicts.append('product.unsupported_packing_medium')
    if q.get('grain_refinement')=='wholegrain' and (c.get('grain_refinement')=='refined' or (c.get('grain_refinement') is None and c.get('colour')=='white')):
        conflicts.append('product.wholegrain_refined_conflict')
    # Exact source wording can cover extraction omissions. Raw cereal/pasta
    # describes uncooked dry input; it does not assert fresh, hydrated produce.
    candidate_dried=('dried' in c.get('preservation_methods',[]) or
        bool(re.search(r'\b(?:dried|dehydrated)\b',candidate.get('input_text',''),re.I)))
    # A low-fat claim does not make milk powder compatible with liquid milk.
    # Reconstitution must be explicit; nutrient density is never evidence for it.
    if (re.search(r'\bmilk\b',query.get('input_text',''),re.I) and
            re.search(r'\b(?:dried|dehydrated|powder(?:ed)?)\b',candidate.get('input_text',''),re.I) and
            not re.search(r'\b(?:dried|dehydrated|powder(?:ed)?|reconstituted)\b',query.get('input_text',''),re.I) and
            'dried' not in q.get('preservation_methods',[])):
        conflicts.append('product.unsupported_milk_powder')
    dry_staple=(any(food.get('family') in {'cereal','pasta'} for food in query.get('foods',[])) or
        bool(re.search(r'\b(?:rice|wheat|bulgur|bulgar|oats|pasta|noodles|flour|couscous|polenta)\b',
                       query.get('input_text',''),re.I)))
    if ('dried' in q.get('preservation_methods',[]) and not candidate_dried and not dry_staple
            and c.get('preparation_state')=='raw'):
        conflicts.append('product.dried_raw_conflict')
    return conflicts


def recovered_property_support(query: dict, facts: dict | None) -> tuple[set[str], set[str]]:
    """Return requested material fields and those positively supported by a record."""
    handling={'chopped','sliced','diced','grated','peeled','drained','rinsed'}
    requested={r['field'] for r in query.get('occurrence_recovery',{}).get('recovered',[])}
    # Name assertions matter too: aliases must not suppress an explicitly named
    # cut/state merely because it did not come from the longer recipe line.
    material={'biological_source','food_part','preparation_state','skin_status','bone_status',
              'packing_medium','drainage_status','fat_level','fat_content_description',
              'meat_fat_description','sodium_level','sugar_level','grain_refinement',
              'reconstitution_state','fortification','physical_form','colour','variety',
              'processes','preservation_methods'}
    requested.update(item['field'].removeprefix('facets.') for item in query.get('evidence', [])
        if item.get('polarity') == 'asserted' and item.get('quote')
        and item['quote'] in (query.get('parsed_text') or query.get('input_text') or '')
        and item.get('field', '').removeprefix('facets.') in material
        and query['facets'].get(item['field'].removeprefix('facets.')) not in (None, [], '', 'unknown'))
    if requested:
        requested.update(field for field in ('biological_source','food_part','preparation_state',
            'skin_status','bone_status','packing_medium','drainage_status','fat_level',
            'fat_content_description','meat_fat_description','sodium_level','sugar_level',
            'grain_refinement','reconstitution_state','fortification','physical_form','colour','variety')
            if query['facets'].get(field) not in (None,[], 'unknown'))
    if 'processes' in requested and not set(query['facets']['processes'])-handling:
        requested.remove('processes')
    if normalized_value('fat_level',query['facets'].get('fat_level')) in {'low','reduced'}:
        requested.add('fat_level')
    if facts is None:return requested,set()
    native=facts.get('native_annotation') or facts['text_annotation']
    actual=native['facets'];supported=set()
    for field in requested:
        left=normalized_value(field,query['facets'].get(field))
        right=normalized_value(field,actual.get(field))
        if field=='fat_level' and left in {'low','reduced'}:
            numeric=normalized_value('fat_content_description',actual.get('fat_content_description'))
            if right in {'low','reduced','semi_skimmed'} or numeric in {'1% fat','1.5% fat','1.8% fat','2% fat'} or label_states_reduced_fat(facts.get('label') or ''):
                supported.add(field)
        elif isinstance(left,list):
            wanted=set(left)-(handling if field=='processes' else set())
            if wanted and wanted<=set(right or []):supported.add(field)
        elif left is not None and left==right:
            supported.add(field)
    return requested,supported


def plain_fat_variant(name: str, label: str) -> bool:
    """A stated fat level outweighs a variety: low fat Greek yogurt -> "Yogurt, low fat, plain", never toffee."""
    words=lambda text:re.findall(r'[a-z]+',text.casefold().replace('yoghurt','yogurt'))
    *variety,head=[w for w in words(name) if w not in {'low','reduced','fat'}] or ['']
    found=set(words(label))
    return bool(variety) and set(variety)<={'greek','french','italian','natural','style'} and head in found and found-{head}<={'low','reduced','fat','plain','natural','basic','retail','average'}


def fat_variant_keeps_food(name: str, label: str) -> bool:
    """Every food word of the name is in the label, and its head leads the label: not puff pastry -> Danish pastry."""
    stem=lambda word:word[:-2] if word.endswith('oes') else word[:-1] if word.endswith('s') else word
    words=lambda text:[stem(w) for w in re.findall(r'[a-z]+',text.casefold())]
    food=[w for w in words(name) if w not in {'low','reduced','light','lite','half','fat'}]
    return bool(food) and set(food)<=set(words(label)) and food[-1] in words(label.split(',')[0])


def choose_supported_occurrence_candidate(query: dict, current: dict | None, checked: list[dict], source_policy) -> dict | None:
    """Prefer complete support over an unknown-property proxy in the checked pool.

    This is an experimental estimate selector, not a calibrated acceptance rule.
    """
    from dataclasses import asdict
    from recipe_wrangler.matching.hybrid import assess_compatibility
    from recipe_wrangler.matching.reranking import ranking_details, ranking_key
    handling={'chopped','sliced','diced','grated','peeled','drained','rinsed'}
    if current is None and compare_identities(query.get('input_text'),query.get('main_ingredient')).relation not in {'exact','synonym','likely_equivalent'}:
        return None
    requested,supported=recovered_property_support(query,current)
    current_conflicts=current and material_scope_conflicts(query,current['text_annotation'])
    if not requested or (requested<=supported and not current_conflicts) or query.get('occurrence_recovery',{}).get('conflicts'):
        return None
    choices=[];seen=set()
    for rank,entry in enumerate(checked):
        key=(entry['collection'],entry['candidate_id'])
        if entry['blocked'] or key in seen:continue
        seen.add(key)
        candidate=source_policy.catalogue.by_id(*key)
        if candidate is None:continue
        facts=source_policy.source(entry['collection'],candidate)
        if facts['blockers'] or not facts['core_complete']:continue
        if material_scope_conflicts(query,facts['text_annotation']):continue
        annotation=facts['text_annotation']
        if facts.get('native_annotation'):
            annotation=copy.deepcopy(annotation)
            annotation['facets']=facts['native_annotation']['facets']
        previous=(current.get('native_annotation') or current['text_annotation'])['facets'] if current else {}
        if any(previous.get(field) and not annotation['facets'].get(field)
               for field in ('fat_level','meat_fat_description','skin_status','bone_status','drainage_status')):
            continue
        if (not query['facets'].get('preparation_state') and previous.get('preparation_state')
                and annotation['facets'].get('preparation_state')!=previous['preparation_state']):
            continue
        if not ('fat_level' in requested and plain_fat_variant(query['input_text'],facts['label'])):
            if compare_identities(query['main_ingredient'],annotation['main_ingredient']).relation not in {'exact','synonym','likely_equivalent'}:
                continue
            heads=compare_identities(query['foods'][0]['text'],annotation['foods'][0]['text'])
            same_anatomy=all(query['facets'].get(field) not in (None, '', 'unknown')
                and normalized_value(field,query['facets'][field])==normalized_value(field,annotation['facets'].get(field))
                for field in ('biological_source','food_part'))
            if not same_anatomy and (heads.relation not in {'exact','synonym','likely_equivalent'} or 'partial_identity_token_overlap' in heads.reasons):
                continue
        extra_processes=set(annotation['facets'].get('processes',[]))-set(query['facets'].get('processes',[]))-handling
        if extra_processes:continue
        assessed=asdict(assess_compatibility(query,annotation))
        if assessed['relation']=='reject':continue
        _,support=recovered_property_support(query,facts)
        if not requested<=support:continue
        if 'fat_level' in requested and not (plain_fat_variant(query['input_text'],facts['label']) or fat_variant_keeps_food(query['input_text'],facts['label'])):
            continue
        details=ranking_details(query,annotation,assessed)
        if details['additional_identity_properties'] or details['additional_product_properties']:
            continue
        choices.append((ranking_key(details,rank),{'candidate':candidate,'source_facts':facts,
            'supported_fields':sorted(support),'requested_fields':sorted(requested)}))
    return min(choices,key=lambda r:r[0])[1] if choices else None


def unsupported_specialist_part(query: str, label: str) -> bool:
    """Generic meat must not silently become an organ or specialist part."""
    return bool(MEAT.search(query) and MEAT.search(label) and
                {m.group().lower() for m in SPECIAL_PARTS.finditer(label)} -
                {m.group().lower() for m in SPECIAL_PARTS.finditer(query)})


def replacement_reason(query: str, previous: dict, proposed: dict, *, observed_facets: dict | None = None) -> str | None:
    """Return why a compatible previous record has stronger positive evidence.

    Both records must pass the same identity/native checks before calling this.
    Missing source annotations are uncertainty, not evidence of equivalence.
    """
    if unsupported_specialist_part(query, proposed['label']):
        return 'unsupported_specialist_part'
    old = (previous.get('native_annotation') or previous['text_annotation'])['facets']
    new = (proposed.get('native_annotation') or proposed['text_annotation'])['facets']
    # Raw is often an input default, not an observed property. It must not
    # automatically outweigh strictly fuller profiles of low-risk equivalents.
    known_old=set(previous['known_nutrients']);known_new=set(proposed['known_nutrients'])
    same_identity=compare_identities(previous['text_annotation'].get('main_ingredient'),
        proposed['text_annotation'].get('main_ingredient')).relation in {'exact','synonym','likely_equivalent'}
    old_description=re.sub(r'\braw\b','',previous['label'],flags=re.I)
    new_description=re.sub(r'\braw\b','',proposed['label'],flags=re.I)
    same_description=compare_identities(old_description,new_description).relation in {'exact','synonym','likely_equivalent'}
    fuller_default=(same_identity and same_description and old.get('preparation_state')=='raw' and
        (observed_facets or {}).get('preparation_state') != 'raw' and
        not re.search(r'\b(raw|uncooked)\b',query,re.I) and not STATE_SENSITIVE.search(query) and
        known_old < known_new and not new.get('processes') and not new.get('preservation_methods'))
    if old.get('preparation_state') and not new.get('preparation_state') and not fuller_default:
        return 'lost_source_preparation_evidence'
    for field in ('fat_level', 'meat_fat_description', 'skin_status', 'bone_status', 'drainage_status'):
        if old.get(field) and not new.get(field):
            return 'lost_source_evidence:' + field
    # Completeness only breaks ties for identical supported food identities/states.
    if previous['label'].casefold() == proposed['label'].casefold():
        a = set(previous['known_nutrients']); b = set(proposed['known_nutrients'])
        if b < a:
            return 'less_complete_equivalent_record'
    return None
