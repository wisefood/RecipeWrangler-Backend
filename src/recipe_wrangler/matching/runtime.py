"""Occurrence-aware nutrition matching using a published PostgreSQL/search release."""
from __future__ import annotations

import copy
import os
import hashlib
import re
from functools import lru_cache
from pathlib import Path

from recipe_wrangler.repositories import matching_store as store
from recipe_wrangler.repositories.elasticsearch_vectors import query_elasticsearch_hybrid_collection
from recipe_wrangler.tools import nutrition_match as nm
from recipe_wrangler.tools.nutrition_completeness import native_values,contribution,completeness,METRICS
from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS,native_candidate_id
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.facet_vocabulary import FacetVocabulary
from recipe_wrangler.matching.langual import LangualVocabulary
from recipe_wrangler.matching.foodex2 import FoodEx2Catalogue
from recipe_wrangler.matching.candidate_gate import CompatibilityGatedCatalogue
from recipe_wrangler.matching.occurrence_facts import recover_occurrence_facts,occurrence_key,source_description_name,stated_oil_example
from recipe_wrangler.matching.nutrition_policy import SourceNutrientPolicy,calculation_decision
from recipe_wrangler.matching.source_suitability import replacement_reason,choose_supported_occurrence_candidate,recovered_property_support,material_scope_conflicts
from recipe_wrangler.matching.quantities import recipe_inputs,resolve_quantity,QUANTITY_VERSION,per_person_weight,adjusted_weight,effective_quantity
from recipe_wrangler.matching.reviewed_aliases import BLOCKED_RECORDS,REVIEWED_ALIASES,APPROXIMATE_NAMES,RECORD_FILLS,REVIEWED_SELECTIONS,low_salt_name,preferred_name,stated_state_name,reviewed_record_compatible
from recipe_wrangler.matching.completeness_repair import eligible_record, choose_complete_record, label_key

VERSION='occurrence_matching_v51_sibling_superscript_quantity_repairs'
CURRENT_NATIONAL_TABLE=nm.CURRENT_NATIONAL_TABLE
ROOT=Path(__file__).resolve().parents[3]
ASSETS={
    'dictionary':'data/nutrition/matching/dictionary/ingredient_dictionary_2026-10-22/foodon_dictionary.json',
    'parents':'data/nutrition/matching/vocabularies/derived/matching_vocabularies_2026-10-21/foodon_named_parents.csv',
    'foodex_facets':'data/nutrition/matching/vocabularies/derived/foodex2/17.2/facets.json',
    'langual':'data/nutrition/matching/vocabularies/derived/langual/2017/terms.json',
    'foodex':'data/nutrition/matching/vocabularies/raw/matching_vocabularies_2026-10-21/MTX.ecf',
}


def asset(name):
    root=Path(os.getenv('NUTRITION_MATCHING_ASSET_ROOT',str(ROOT)))
    path=root/ASSETS[name]
    if not path.is_file():raise RuntimeError(f'Missing matching release asset: {name}; configure NUTRITION_MATCHING_ASSET_ROOT')
    return path


def build_annotator():
    return HybridAnnotator(ContextualParser.from_snapshots(asset('dictionary'),asset('parents')),
        FacetVocabulary.load(asset('foodex_facets')),langual=LangualVocabulary.load(asset('langual')))


def missing_metrics(source_facts):
    values=(source_facts or {}).get('values_per_100g') or {}
    return {metric for metric in METRICS if values.get(metric) is None}


def alias_index(rows):
    return {nm._norm(r['alias']):{'eu_food_id':r['eu_food_id'],**{
        region:r.get(region+'_food_id') for region in ['irish','hungarian','slovenian']}}
        for r in rows if r.get('alias') and r.get('eu_food_id')}


class Catalogue:
    def __init__(self, resources, *, legacy=False):
        self.resources=resources;self.legacy=legacy;self.errors=[]
        self.literal_scope=None

    def _in_scope(self,collection,candidate):
        row = getattr(self.resources, 'ids', {}).get((collection, str(candidate.get('id'))))
        if row is not None and not eligible_record(row['annotation']):
            return False
        if str(candidate.get('id')) in BLOCKED_RECORDS:
            return False
        if self.literal_scope is None:return True
        preferred,label=self.literal_scope
        metadata=candidate.get('metadata') or {}
        actual=metadata.get('food_name') or candidate.get('document') or ''
        return collection==preferred and ' '.join(actual.casefold().split())==label

    def query(self,collection,query):
        if self.literal_scope is not None and collection!=self.literal_scope[0]:return []
        try:
            return [copy.deepcopy(candidate) for candidate in self.resources.search(collection,query,self.legacy)
                    if self._in_scope(collection,candidate)]
        except Exception:
            self.errors.append({'collection':collection,'query':query,'reason':'retrieval_failure'})
            raise

    def by_id(self,collection,identifier):
        row=self.resources.ids.get((collection,str(identifier)))
        return copy.deepcopy(row['candidate']) if row and self._in_scope(collection,row['candidate']) else None

    def nutrient(self,source,identifier):
        row=self.resources.ids.get((COLLECTIONS[source],str(identifier)))
        return row['nutrient_row'] if row else None


_COUNTED_PEPPER = re.compile(r'^\s*(?:\d+(?:[./]\d+)?|[¼½¾])\s*(?:(?:small|medium|large)\s+)?(?:(?:red|green|yellow|orange)\s+and\s+\d+\s+(?:red|green|yellow|orange)\s+)?peppers?\b', re.I)


def counted_pepper_name(name, original):
    """A counted bare "1 pepper" is a capsicum; the alias for bare "pepper" means the spice."""
    return 'sweet pepper' if re.fullmatch(r'\s*peppers?\s*', name, re.I) and _COUNTED_PEPPER.match(original or '') else name


_CANNED=re.compile(r'\b(?:cans?|canned|tins?|tinned|jars?)\b',re.I)
_COOKED=re.compile(r'\b(?:cooked|boiled|steamed)\b',re.I)
_DRIED=re.compile(r'\b(?:dried|dry)\b',re.I)


def _reviewed_values(row):
    """A public-source food outside the release: synthetic source facts carrying the citation."""
    return {'source_key':'eu','native_id':row['source'],'catalogue_key':None,'source':{'dataset':row['source']},
        'label':row['food_name'],'values_per_100g':{m:row['values'].get(m) for m in METRICS},
        'known_nutrients':[m for m in METRICS if row['values'].get(m) is not None],'blockers':[],'warnings':[]}


class MatchingEngine:
    def __init__(self,release_id,*,rows=None,manifest=None,annotator=None,foodex=None):
        self.release_id=release_id
        self.manifest=manifest if manifest is not None else store.release(release_id)['manifest']
        if manifest is None:
            for name,relative in ASSETS.items():
                expected=self.manifest.get('input_sha256',{}).get(relative)
                if not expected or hashlib.sha256(asset(name).read_bytes()).hexdigest()!=expected:
                    raise RuntimeError('Matching asset does not match published release: '+name)
        self.rows=rows if rows is not None else store.compositions(release_id)
        self.annotator=annotator or build_annotator()
        self.foodex=foodex or FoodEx2Catalogue(asset('foodex'))
        self.aliases=[*self.manifest['aliases'],*REVIEWED_ALIASES];self.alias_index=alias_index(self.aliases)
        self.ids={};self.records={};self.profiles={};self.annotations={};self.source_cache={}
        self.source_descriptions=set()
        self.source_description_rows={}
        self.coverage_index={}
        for row in self.rows:
            collection=row['collection'];candidate=row['candidate'];metadata=candidate.get('metadata') or {}
            label=metadata.get('food_name') or candidate.get('document') or ''
            self.source_descriptions.add(' '.join(label.casefold().split()))
            self.source_description_rows.setdefault((collection,' '.join(label.casefold().split())),[]).append(row)
            self.ids[collection,row['candidate_id']]=row
            self.ids.setdefault((collection,row['native_id']),row)
            key=metadata.get('catalogue_key')
            if key:
                self.records[key]=row['source_record'];self.profiles[key]=row['source_profile']
            annotation=row['annotation']
            self.coverage_index.setdefault(('label',label_key(annotation['label'])),[]).append(row)
            if annotation.get('text_annotation'):
                self.annotations[candidate['document']]=annotation['text_annotation']
                self.source_cache[collection,row['candidate_id']]=annotation

    @lru_cache(maxsize=16000)
    def search(self,collection,query,legacy=False):
        index=self.manifest['legacy_index'] if legacy else self.manifest['search_index']
        hits=query_elasticsearch_hybrid_collection(collection,query,index=index,embedding=self.embed(query))
        # Search cannot silently introduce records from a different release.
        return [h for h in hits if (collection,str(h['id'])) in self.ids]

    @lru_cache(maxsize=8000)
    def embed(self,query):
        if not hasattr(self,'encoder'):
            from sentence_transformers import SentenceTransformer
            self.encoder=SentenceTransformer(self.manifest['embedding_model'],
                revision=self.manifest['embedding_revision'],device='cpu',local_files_only=True)
        return self.encoder.encode(query,show_progress_bar=False).tolist()

    def _exact_source_selection(self,name,region):
        """Respect regional precedence; duplicate literal labels require review."""
        key=' '.join(name.casefold().split())
        # The current national table is released only inside the EU collection; it outranks older regional copies.
        prefix=CURRENT_NATIONAL_TABLE.get(region)
        current=[row for row in self.source_description_rows.get((COLLECTIONS['eu'],key),[])
                 if prefix and row['candidate_id'].startswith(prefix) and eligible_record(row['annotation'])]
        if len(current)==1:return 'eu',current[0]
        for source in dict.fromkeys([region,'eu']):
            matches=[row for row in self.source_description_rows.get((COLLECTIONS[source],key),[])
                     if eligible_record(row['annotation'])]
            if matches:
                # Expanded catalogue copies must not displace a single native
                # record with the same literal label in the preferred region.
                native=[row for row in matches if not row['candidate_id'].startswith('catalogue:')]
                if native:matches=native
                return source,matches[0] if len(matches)==1 else None
        return None,None

    def _literal_name_selection(self,name,original,region,policy):
        """A source line stating exactly a unique released label selects that record, only if it carries energy.

        The line must state the label itself ("3 g Dill, fresh"); a generic name ("tomatoes") that merely equals
        some label keeps normal matching. The energy condition keeps the v16 regression out: an incomplete literal
        row (black pepper) must not displace a complete match.
        """
        normalize=lambda value:' '.join(value.casefold().split())
        stated=re.sub(r'^\s*\d+(?:[.,]\d+)?\s*(?:kg|ml|g|l)\s+','',original or '',flags=re.I)
        if normalize(stated)!=normalize(name) or normalize(name) not in self.source_descriptions:return None
        source,row=self._exact_source_selection(name,region)
        if row is None:return None
        candidate=copy.deepcopy(row['candidate'])
        source_facts=policy.source(COLLECTIONS[source],candidate)
        if (source_facts.get('values_per_100g') or {}).get('energy_kcal') is None:return None
        return source,candidate,source_facts

    def _stated_literal(self,name,original):
        """Preserve explicitly stated plant parts in complete released labels."""
        if ',' not in name:return False
        if not re.search(r'(?:^|,)\s*(?:bulbs?|roots?|leaves|leaf)\s*(?:,|$)',name.partition(',')[2],re.I):
            return False
        stated=source_description_name(name.split(',',1)[0],original,self.source_descriptions)
        return ' '.join(stated.casefold().split())==' '.join(name.casefold().split())

    def _reviewed_selection(self,name,original,region,facts,provider,policy):
        """Fallback for lines normal matching left unresolved; it never replaces a working match."""
        row=REVIEWED_SELECTIONS.get(' '.join(name.casefold().split()))
        if not row:return None
        base={'observed_facts':facts,'evaluation_key':region+':'+occurrence_key(facts),'retrieval_name':name,
            'source_description_recovered':False,'leading_alternatives':[]}
        if row.get('non_food'):
            calculation={'kind':'unresolved','assumptions':[],'blockers':[],'values_per_100g':dict.fromkeys(METRICS)}
            return {**base,'decision':{'match':None,'confidence':'none','reason':'non_food'},'calculation':calculation,'source_facts':None}
        state='canned' if _CANNED.search(original) else 'cooked' if _COOKED.search(original) else 'dried' if _DRIED.search(original) else None
        variant=row.get(state)
        if re.search(r'\b(?:low|reduced)[- ]fat\b',name+' '+original,re.I):
            variant=row.get('low_fat') or variant
        chosen=variant or row['eu_food_id']
        if chosen in row.get('public',{}):
            source_facts=_reviewed_values(row['public'][chosen]);candidate={'id':chosen}
        else:
            candidate=provider.by_id(COLLECTIONS['eu'],chosen)
            if candidate is None:return None
            source_facts=policy.source(COLLECTIONS['eu'],candidate)
        if not row.get('confirmed') and not reviewed_record_compatible(name,original,source_facts):return None
        # A dry-basis record must not price a tin or a cooked weight.
        if state in {'canned','cooked'} and not variant and (source_facts['values_per_100g'].get('energy_kcal') or 0)>=200:return None
        calculation={'kind':'automatic_estimate','policy_version':'reviewed_alias_v1','assumptions':[{'policy':'reviewed_alias','field':'identity',
                'value':source_facts['label'],'reason':row.get('approximate') or 'Reviewed same food.'}],'blockers':[],
            'values_per_100g':copy.deepcopy(source_facts['values_per_100g']),
            'missing_nutrients':[m for m in METRICS if source_facts['values_per_100g'].get(m) is None]}
        return {**base,'decision':{'match':candidate,'source_key':'eu','confidence':'curated','matched_name':source_facts['label'],
            'reason':'reviewed_alias'},'calculation':calculation,'source_facts':source_facts}

    @lru_cache(maxsize=16000)
    def resolve(self,name,region,original=''):
        result = self._resolve(name,region,original)
        current = result.get('source_facts')
        if not current or not missing_metrics(current) or not hasattr(self, 'rows'):
            return result
        key = label_key(current['label'])
        records = self.coverage_index.get(('label',key),[])
        unique = {(row['collection'],row['candidate_id']):row for row in records}
        rows = [row for row in unique.values() if row['collection'] in {COLLECTIONS[region],COLLECTIONS['eu']}
                and row['candidate_id'] not in BLOCKED_RECORDS]
        chosen = choose_complete_record(result['observed_facts'], current, rows)
        if chosen is None:
            return result
        row, calculation = chosen
        return {**result, 'source_facts':copy.deepcopy(row['annotation']), 'calculation':calculation,
            'decision':{'match':copy.deepcopy(row['candidate']), 'source_key':row['annotation']['source_key'],
                'confidence':'strong', 'matched_name':row['annotation']['label'],
                'reason':'compatible_more_complete_record'}}

    def _resolve(self,name,region,original=''):
        example=stated_oil_example(name,original)
        if example:
            result=copy.deepcopy(self.resolve(example['name'],region,original))
            if result['calculation']['kind']!='unresolved':
                observed=recover_occurrence_facts(name,original,self.annotator.annotate)
                result['observed_facts']=observed
                result['calculation']['observed_facts']=copy.deepcopy(observed)
                if result['calculation']['kind']=='direct_match':
                    result['calculation']['kind']='automatic_estimate'
                result['calculation']['assumptions'].append({'policy':'explicit_source_oil_example',
                    'field':'identity','value':example['name'],'evidence':example,
                    'reason':'Uses the first oil explicitly offered in the original ingredient line; other offered oils remain alternatives.'})
                result.update(retrieval_name=example['name'],source_description_recovered=True)
                return result
        result=self._match(name,region,original)
        decision=result['decision']
        exact=decision.get('reason') in {'exact_source_description','exact_literal_name'}
        facts=result['observed_facts']
        # Retrieval must see a fat qualifier recovered from the source line, too.
        if not exact and facts.get('facets') and not facts.get('occurrence_recovery',{}).get('conflicts'):
            requested,supported=recovered_property_support(facts,result.get('source_facts'))
            fat=facts['facets'].get('fat_level')
            if fat in {'low','reduced'} and 'fat_level' in requested-supported and not re.search(r'\b(?:low|reduced|light|lite|half)\b',name,re.I):
                retry=self._match(fat+' fat '+name,region,original)
                source=retry.get('source_facts')
                _,support=recovered_property_support(facts,source)
                if (source and requested<=support and retry['calculation']['kind']!='unresolved'
                        and not material_scope_conflicts(facts,source['text_annotation'])
                        and missing_metrics(source)<=missing_metrics(result.get('source_facts'))):
                    retry=copy.deepcopy(retry)
                    retry['observed_facts']=facts
                    retry['calculation']['observed_facts']=copy.deepcopy(facts)
                    retry['evaluation_key']=result['evaluation_key']
                    return retry
        reviewed=None if exact else low_salt_name(name,original) or preferred_name(name,region) or stated_state_name(name,original,decision.get('matched_name') or '')
        if not reviewed and (result['calculation']['kind']!='unresolved' or decision.get('reason')=='non_food'):
            return result
        provider=Catalogue(self)
        policy=SourceNutrientPolicy(provider,self.annotator,self.records,self.profiles,self.foodex,native_values)
        policy.annotations=self.annotations;policy.cache=self.source_cache
        return self._reviewed_selection(reviewed or name,original,region,result['observed_facts'],provider,policy) or result

    def _match(self,name,region,original=''):
        if region not in COLLECTIONS:raise ValueError('Unsupported nutrition region')
        name=counted_pepper_name(nm.same_food_name(name),original)  # the gate and the matcher must see the same food name
        parsed_name=name
        name=source_description_name(name,original,self.source_descriptions)
        # A structured source label ("Pork, leg") stated after an amount names the source's own record, whatever the parser called it.
        stated=' '.join(re.sub(r'^\s*\d+(?:[.,]\d+)?\s*(?:kg|ml|g|l)\s+','',original or '',flags=re.I).split())
        structured=',' in stated and stated!=' '.join((original or '').split()) and stated.casefold() in self.source_descriptions
        if structured:name=stated
        facts=recover_occurrence_facts(name,original,self.annotator.annotate)
        provider=Catalogue(self);legacy=Catalogue(self,legacy=True)
        policy=SourceNutrientPolicy(provider,self.annotator,self.records,self.profiles,self.foodex,native_values)
        policy.annotations=self.annotations;policy.cache=self.source_cache
        source_description_ambiguous=False;literal=None
        def exact(source,candidate,source_facts,reason):
            calculation=calculation_decision(facts,candidate,source_facts,selected=True)
            decision={'match':candidate,'source_key':source,'confidence':'strong',
                'matched_name':source_facts['label'],'reason':reason}
            return {'decision':decision,'calculation':calculation,'observed_facts':facts,
                'source_facts':source_facts,'evaluation_key':region+':'+occurrence_key(facts),
                'retrieval_name':name,'source_description_recovered':name!=parsed_name,'leading_alternatives':[]}
        if name!=parsed_name or structured or self._stated_literal(name,original):
            source,row=self._exact_source_selection(name,region)
            if row is not None:
                candidate=copy.deepcopy(row['candidate'])
                return exact(source,candidate,policy.source(COLLECTIONS[source],candidate),'exact_source_description')
            source_description_ambiguous=source is not None
            if source_description_ambiguous:
                # A duplicate food label does not justify replacing its named
                # subtype with a generic alias. Keep ranking within that food.
                provider.literal_scope=legacy.literal_scope=(COLLECTIONS[source],' '.join(name.casefold().split()))
            else:
                name=parsed_name
                facts=recover_occurrence_facts(name,original,self.annotator.annotate)
        else:
            literal=self._literal_name_selection(name,original,region,policy)
        gate=CompatibilityGatedCatalogue(provider,self.annotator,self.aliases,
            alias_normalizer=nm.clean_query,source_policy=policy,facet_retrieval=True)
        with nm.matching_backend(legacy,self.alias_index):previous=nm.best_nutrition_match(name,region,identity_name=name)
        with nm.matching_backend(provider,self.alias_index):expanded=nm.best_nutrition_match(name,region,identity_name=name)
        gate.begin_input(name,facts)
        with nm.matching_backend(gate,self.alias_index):decision=nm.best_nutrition_match(name,region,identity_name=name)
        if decision.get('reason') == 'alias' and decision.get('match'):
            alias_facts=policy.source(COLLECTIONS[decision['source_key']],decision['match'])
            requested,supported=recovered_property_support(gate.semantic_input(),alias_facts)
            if requested-supported:
                # A curated identity is a fallback, not proof of missing material
                # properties. Let normal gated retrieval expose supported choices.
                for search_source in dict.fromkeys([region,'eu']):
                    gate.query(COLLECTIONS[search_source],name)
        checked=list(gate.audit);match=decision.get('match');source=decision.get('source_key');retained=False
        prior=previous.get('match');expanded_prior=expanded.get('match')
        if prior and previous['confidence'] in {'strong','curated'}:
            before=previous['source_key'];old=gate.by_id(COLLECTIONS[before],native_candidate_id(prior,before))
            displaced=expanded_prior and any(r['candidate_id']==expanded_prior['id'] and r['blocked'] for r in checked)
            if old and (displaced or match):
                old_facts=policy.source(COLLECTIONS[before],prior)
                reason=displaced or replacement_reason(name,old_facts,policy.source(COLLECTIONS[source],match),observed_facets=gate.semantic_input()['facets'])
                if reason and not old_facts['blockers']:
                    decision=copy.deepcopy(previous);match=decision['match'];source=before;retained=True
        source_facts=policy.source(COLLECTIONS[source],match) if match else None
        choice=choose_supported_occurrence_candidate(gate.semantic_input(),source_facts,checked,policy)
        if choice:
            match=choice['candidate'];source_facts=choice['source_facts'];source=source_facts['source_key'];retained=False
            decision={**decision,'match':match,'source_key':source,'matched_name':source_facts['label'],
                'confidence':'strong','reason':'positive_occurrence_support'}
        calculation=calculation_decision(gate.input_facts(),match,source_facts,
            selected=bool(match and decision['confidence'] in {'strong','curated'}),retained=retained,
            alias_identity=any(r.get('curated_alias_identity_evidence') for r in checked),selected_input=gate.semantic_input())
        if provider.errors or legacy.errors:raise RuntimeError('Nutrition retrieval failed; no profile was accepted')
        # The abstention reads only the name; a state stated in the source line ("400g can chickpeas") resolves it.
        # The same holds when the gate admitted the stated-state record ("Thyme, dried") but the name-only
        # matcher rejected it as an unrequested form.
        if decision.get('reason') in {'ambiguous_preparation_state','top_candidate_incompatible'}:
            # Drained/rinsed pulses are canned or cooked; "drained" alone retrieves the dried record.
            facets=facts['facets'];kept=facets['preservation_methods']
            # A stated can/tin outranks "dried", which the parser also reads from "patted dry".
            container=re.search(r'\b(?:cans?|canned|tins?|tinned)\b',original or '',re.I)
            stated=('canned' if 'canned' in kept or container else 'dried' if 'dried' in kept else
                facets['preparation_state'] or ('canned' if facets['drainage_status'] else None))
            # A name with no food ('400g can no-added-salt') must not become any canned product.
            if stated and stated not in name.split() and facts.get('main_ingredient'):return self.resolve(stated+' '+name,region,original)
        # The stated literal record wins only if it is missing no nutrient the normal match supplies.
        if literal and missing_metrics(literal[2])<=missing_metrics(source_facts):
            return exact(*literal,'exact_literal_name')
        return {'decision':decision,'calculation':calculation,'observed_facts':facts,
            'retrieval_name':name,'source_description_recovered':name!=parsed_name,
            'source_description_ambiguous':source_description_ambiguous,
            'source_facts':source_facts,'evaluation_key':region+':'+occurrence_key(facts),
            'leading_alternatives':[{k:r[k] for k in ['label','candidate_id','blocked']} for r in checked[:8]]}

    def resolve_source_identity(self,name,region,original,binding):
        """Use a supplied, release-pinned source identity without lexical guessing."""
        if region not in COLLECTIONS:raise ValueError('Unsupported nutrition region')
        if binding['release_id']!=self.release_id:raise ValueError('Source identity release differs')
        collection=binding['collection']
        if collection not in {COLLECTIONS[region],COLLECTIONS['eu']}:
            raise ValueError('Source identity collection is outside requested region')
        provider=Catalogue(self)
        candidate=provider.by_id(collection,binding['candidate_id'])
        if candidate is None:raise ValueError('Supplied source identity is not in the release')
        row=self.ids[collection,str(candidate['id'])];native=row.get('nutrient_row') or {}
        if (native.get('source')!=binding['source_prefix'] or native.get('country')!=binding['country']
                or str(native.get('id'))!=binding['source_prefix']+':'+binding['food_code']):
            raise ValueError('Supplied source namespace or food code differs')
        country={'irish':'IE','hungarian':'HU','slovenian':'SI'}.get(region)
        if country and native['country']!=country:raise ValueError('Supplied source country differs from requested region')
        normalize=lambda value:' '.join(value.casefold().split())
        if normalize(original)!=normalize(binding['source_line']):raise ValueError('Supplied source occurrence differs')
        if normalize(name)!=normalize(native['food_name']) or normalize(name)!=normalize(binding['expected_label']):
            raise ValueError('Supplied source label differs from ingredient identity')
        if store.digest(native)!=binding['nutrient_row_digest']:raise ValueError('Supplied source row changed')
        facts=recover_occurrence_facts(name,original,self.annotator.annotate)
        policy=SourceNutrientPolicy(provider,self.annotator,self.records,self.profiles,self.foodex,native_values)
        policy.annotations=self.annotations;policy.cache=self.source_cache
        source_facts=policy.source(collection,candidate)
        if source_facts['values_per_100g']!=native_values(native):raise ValueError('Supplied source annotation is stale')
        calculation=calculation_decision(facts,candidate,source_facts,selected=True)
        return {'decision':{'match':candidate,'source_key':source_facts['source_key'],'confidence':'strong',
                    'matched_name':source_facts['label'],'reason':'explicit_source_identity'},
            'calculation':calculation,'observed_facts':facts,'source_facts':source_facts,
            'retrieval_name':name,'leading_alternatives':[],'source_identity_evidence':copy.deepcopy(binding)}

    def calculate(self,*,title,ingredient_names,weights,source,serves=None,
                  original_texts=None,measurements=None,recipe_id=None,quantity_evidence=None,source_bindings=None,**kwargs):
        if len(ingredient_names)!=len(weights):raise ValueError('Ingredient/quantity lengths differ')
        originals=original_texts or ['']*len(weights)
        if len(originals)!=len(weights):raise ValueError('Ingredient/context lengths differ')
        if quantity_evidence is not None and len(quantity_evidence)!=len(weights):
            raise ValueError('Ingredient/quantity evidence lengths differ')
        if source_bindings is not None and len(source_bindings)!=len(weights):
            raise ValueError('Ingredient/source identity lengths differ')
        # Resolve the entire quantity vector before retrieving any regional food.
        inputs=recipe_inputs(ingredient_names,weights,originals,serves=serves)
        if source_bindings is not None:inputs['source_bindings']=copy.deepcopy(source_bindings)
        if quantity_evidence is not None:
            inputs['quantities']=[resolve_quantity(n,w,o,e,serves=serves) for n,w,o,e in
                zip(ingredient_names,weights,originals,quantity_evidence,strict=True)]
            inputs['quantity_input_hash']=store.digest([QUANTITY_VERSION,ingredient_names,weights,
                originals,inputs['quantities']])
        details=[];contributions=[]
        for position,(name,weight,original) in enumerate(zip(ingredient_names,weights,originals,strict=True)):
            original=original or ''
            # Retrieval uses the ingredient name; source quantities are separate evidence.
            binding=source_bindings[position] if source_bindings is not None else None
            result=copy.deepcopy(self.resolve_source_identity(name,source,original,binding) if binding is not None
                                 else self.resolve(name,source,original))
            quantity=effective_quantity(inputs['quantities'][position],original)
            effective=quantity['weight_g']
            calculation=result['calculation'];sf=result['source_facts'];decision=result['decision']
            selected=sf and calculation['kind']!='unresolved'
            ignored=bool(decision.get('intentionally_ignored')) or decision.get('reason')=='non_food'
            values=calculation['values_per_100g']
            # Most blanks in a matched record are missing data, not zeros: fill reviewed ones, keep the rest unknown.
            fills={m:fill for m,fill in RECORD_FILLS.get(sf['native_id'],{}).items() if values.get(m) is None} if selected else {}
            values={**values,**{m:fill['value'] for m,fill in fills.items()}}
            blank=[m for m in METRICS if values.get(m) is None] if selected or ignored else []
            if ignored:values={**values,**dict.fromkeys(blank,0.0)}
            scaled=contribution(values,effective,ingredient=name);contributions.append(scaled)
            detail={'ingredient':name,'name':name,'position':position,
                'occurrence_id':f'{recipe_id}:{position}' if recipe_id else f'input:{position}',
                'original_ingredient_text':original,'source_context_status':'matched_with_original_text' if original else 'missing_original_text',
                'retrieval_name':result.get('retrieval_name',name),
                'source_description_recovered':result.get('source_description_recovered',False),
                'source_description_ambiguous':result.get('source_description_ambiguous',False),
                'original_weight_g':weight,'weight_g':effective,
                **quantity,'quantity_input_hash':inputs['quantity_input_hash'],'quantity_version':QUANTITY_VERSION,
                'source':source,'source_nutrition':sf['source_key'] if sf else source,
                'matched_nutritional_ingredient':sf['label'] if selected else None,
                'canonical_food_id':sf['native_id'] if selected else None,
                'selected_candidate_id':str((decision.get('match') or {}).get('id')) if selected else None,
                'match_confidence':decision.get('confidence'),'match_reason':decision.get('reason'),
                'distance':(decision.get('match') or {}).get('distance'),
                'similarity':decision.get('similarity'),
                'outcome':calculation['kind'],'observed_facts':result['observed_facts'],
                'blank_nutrients_as_zero':blank,'nutrition_intentionally_ignored':ignored,
                'nutrients_filled':{m:fill['source'] for m,fill in fills.items()},
                'approximate_match':(result['calculation']['assumptions'][0]['reason'] if decision.get('reason')=='reviewed_alias' and result['calculation']['assumptions'] and result['calculation']['assumptions'][0]['reason']!='Reviewed same food.' else APPROXIMATE_NAMES.get(' '.join(name.casefold().split()))) if selected else None,
                'selected_composition_facts':sf,'assumptions':calculation['assumptions'],
                'blockers':calculation['blockers'],'leading_alternatives':result['leading_alternatives'],
                'values_per_100g':values,'mechanism_version':VERSION,'catalogue_release':self.release_id,
                **scaled}
            if binding is not None:detail['source_identity_evidence']=copy.deepcopy(result['source_identity_evidence'])
            for field,metric in {'protein_per_100g':'protein_g','carbs_per_100g':'carbs_g',
                'fat_per_100g':'fat_g','sugars_per_100g':'sugar_g',
                'saturated_fat_per_100g':'saturated_fat_g','sodium_per_100g_mg':'sodium_mg',
                'fibre_per_100g':'fibre_g','energy_kcal_per_100g':'energy_kcal'}.items():
                detail[field]=values.get(metric)
            details.append(detail)
        complete=completeness(contributions,serves)
        rename=lambda m:'carbohydrate_g' if m=='carbs_g' else m
        result={'title':title,'details':details,'source':source,'source_key':source,'serves':serves,
            'quantity_input_hash':inputs['quantity_input_hash'],'quantity_version':QUANTITY_VERSION,
            'source_nutrition':source,'mechanism_version':VERSION,'catalogue_release':self.release_id,
            'totals_mode':'strict','calculation_assumptions':[],**complete,
            'clean_totals':{rename(m):v for m,v in complete['complete_totals'].items()},
            'clean_totals_per_serving':{rename(m):v for m,v in complete['complete_totals_per_serving'].items()}}
        for m in METRICS:
            result[f'total_{rename(m)}_{source}']=complete['complete_totals'][m]
            result[f'total_{rename(m)}_per_serving_{source}']=complete['complete_totals_per_serving'][m]
        from recipe_wrangler.matching.quality import finalize_profile
        finalize_profile(result)
        if recipe_id:
            result['matching_run_id']=store.save_run(recipe_id,source,self.release_id,VERSION,
                {**inputs,'serves':serves},result)
        return result


@lru_cache(maxsize=2)
def engine(release_id: str) -> MatchingEngine:
    return MatchingEngine(release_id)
