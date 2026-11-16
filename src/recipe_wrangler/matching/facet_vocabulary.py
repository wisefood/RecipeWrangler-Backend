"""FoodEx2-backed descriptor recognition; a lexical hit is not an assertion."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path


WORDS = re.compile(r'[^\W_]+', re.UNICODE)
FACETS = {'source': 'biological_source', 'part': 'food_part', 'process': 'processes'}

# Lexical normalization, not independently approved semantic annotations.
PROCESS_CODES = {
    'blanched': ('A07GF', 'Blanching'), 'poached': ('A07GH', 'Poaching'),
    'simmered': ('A07GJ', 'Simmering'), 'boiled': ('A07GL', 'Boiling'),
    'stewed': ('A07GM', 'Stewing'),
    'fried': ('A07GR', 'Frying'), 'baked': ('A07GX', 'Baking'),
    'roasted': ('A07GY', 'Roasting'), 'grilled': ('A07GZ', 'Broiling/grilling'),
    'toasted': ('A07HC', 'Toasting'),
}


def tokens(text: str) -> tuple[str, ...]:
    return tuple(m.group().casefold() for m in WORDS.finditer(text))


class FacetVocabulary:
    """Index approved labels and explicit parenthetical names, preserving ambiguity."""

    def __init__(self, payload: dict, sha256: str = ''):
        self.version = payload['catalogue_version']
        self.sha256 = sha256
        self.index = defaultdict(list)
        self.terms = {}
        self.counts = {}
        for facet in payload['facets']:
            if facet['code'] not in FACETS:
                continue
            approved = [t for t in facet['terms'] if t['status'] == 'APPROVED']
            self.counts[facet['code']] = len(approved)
            for term in approved:
                kind = facet['code']
                code = term['code']
                clean = re.sub(r'\s*\(as (?:part-nature|animal|plant|organism)\)\s*$', '', term['label'], flags=re.I)
                names = [clean]
                # Label-supplied parentheses are explicit names, not synonyms mined
                # from free definitions (which often mention other organisms).
                parenthetical = [name for name in re.findall(r'\(([^()]+)\)', clean)
                                 if name.casefold() not in {'generic','unspecified','other','spp.','spp'}]
                names.extend(parenthetical)
                bare = re.sub(r'\([^()]*\)', '', clean).strip()
                if bare:
                    names.append(bare)
                canonical = parenthetical[0] if kind == 'source' and parenthetical else bare or clean
                self.terms[(kind, code)] = {**term, 'canonical': '_'.join(tokens(canonical)),
                    'animal': term['label'].casefold().endswith('(as animal)')}
                for name in set(names):
                    key = tokens(name)
                    if not key:
                        continue
                    self.index[key[0]].append((key, kind, code))

    @classmethod
    def load(cls, path: Path):
        raw = path.read_bytes()
        return cls(json.loads(raw), hashlib.sha256(raw).hexdigest())

    def under(self, code: str, roots: set[str]) -> bool:
        pending, seen = [code], set()
        while pending:
            item = pending.pop()
            if item in roots:
                return True
            if item not in seen:
                seen.add(item)
                pending.extend(self.terms.get(('part',item),{}).get('parent_codes',[]))
        return False

    def recognize(self, text: str) -> list[dict]:
        spans = list(WORDS.finditer(text))
        words = [m.group().casefold() for m in spans]
        hits = []
        for i, word in enumerate(words):
            for phrase, kind, code in self.index.get(word,[]):
                if tuple(words[i:i+len(phrase)]) != phrase:
                    continue
                end = i+len(phrase)-1
                # A descriptor phrase cannot jump over a food connector or comma.
                segment = text[spans[i].start():spans[end].end()]
                if ',' in segment or ';' in segment:
                    continue
                term = self.terms[(kind,code)]
                hits.append({'facet':kind,'field':FACETS[kind], 'code':code,
                    'label':term['label'],'value':term['canonical'],'start':spans[i].start(),
                    'end':spans[end].end(),'quote':segment,'catalogue_version':self.version,
                    'catalogue_sha256':self.sha256,'status':'lexical_candidate_not_assertion'})
        # Prefer longest overlapping names within a facet; equal-span ambiguous
        # concepts survive. Bombay duck must not silently become domestic duck.
        kept = []
        for hit in sorted(hits,key=lambda h:(-(h['end']-h['start']),h['start'],h['code'])):
            if any(h['facet']==hit['facet'] and max(h['start'],hit['start'])<min(h['end'],hit['end'])
                   and (h['start'],h['end'])!=(hit['start'],hit['end']) for h in kept):
                continue
            if hit not in kept:
                kept.append(hit)
        return sorted(kept,key=lambda h:(h['start'],h['facet'],h['code']))

    def attach_source_part(self, annotation: dict) -> None:
        """Resolve a directly adjacent animal-source/anatomical-part phrase."""
        text = annotation['original_text']
        hits = self.recognize(text)
        annotation['vocabulary_candidates'] = hits
        annotation['vocabulary_provenance'] = {'version':self.version,'sha256':self.sha256,
                                               'approved_terms':self.counts}
        if len(annotation['foods'])!=1 or not annotation['main_ingredient']:
            return
        head = annotation['foods'][0]
        if head['family'] not in {'generic','meat','egg'}:
            return
        sources = [h for h in hits if h['facet']=='source' and self.terms[('source',h['code'])]['animal']]
        # Taxonomy branches: offal, meat, egg-related. These are applicability
        # roots, not a new per-part list. Product-nature descriptors stay candidates.
        parts = [h for h in hits if h['facet']=='part' and self.under(h['code'],{'A069L','A069H','A06BQ'})]
        pairs = []
        for source in sources:
            for part in parts:
                if source['end']<=part['start']:
                    between=text[source['end']:part['start']]
                elif part['end']<=source['start']:
                    between=text[part['end']:source['start']]
                else:
                    continue
                if not re.fullmatch(r'[\s,]*(?:of[\s,]+)?',between,re.I):
                    continue
                part_head = max(head['start'],part['start'])<min(head['end'],part['end'])
                source_head = (head['family'] == 'generic'
                    and source['start'] <= head['start'] and head['end'] <= source['end'])
                if not (part_head or source_head):
                    continue
                prefix=text[max(0,min(source['start'],part['start'])-25):min(source['start'],part['start'])]
                if re.search(r'\b(?:not|no|without|for|with|and|or)\s*$',prefix,re.I):
                    continue
                pairs.append((source,part))
        if len(pairs)!=1:
            if pairs:
                annotation['review_reasons'].append('ambiguous_vocabulary_source_part')
            return
        source,part=pairs[0]
        for field,hit in [('biological_source',source),('food_part',part)]:
            value=hit['value']
            previous=annotation['facets'][field]
            if previous is not None and previous!=value:
                annotation['review_reasons'].append('vocabulary_conflict:'+field)
                return
        for field,hit in [('biological_source',source),('food_part',part)]:
            annotation['facets'][field]=hit['value']
            annotation['modifiers'].append({**hit,'field':field,'value':hit['value'],
                'polarity':'asserted','target_food_id':head['food_id'],
                'status':'unreviewed_relation_proposal','rule':'foodex2_adjacent_source_anatomical_part'})
            annotation['evidence'].append({'field':'facets.'+field,'quote':hit['quote'],
                'polarity':'asserted','foodex2_code':hit['code'],'catalogue_version':self.version,
                'rule':'foodex2_adjacent_source_anatomical_part'})
            annotation['unresolved']=[u for u in annotation['unresolved']
                                      if not (hit['start']<=u['start'] and u['end']<=hit['end'])]
        if head['family']=='generic':
            head['family']='egg' if self.under(part['code'],{'A06BQ'}) else 'meat'
            head['identity']='egg' if head['family']=='egg' else source['value']
            annotation['main_ingredient']=head['identity']
            annotation['facets']['base_identity']=head['identity']
            annotation['evidence'].append({'field':'main_ingredient',
                'quote':text[min(source['start'],part['start']):max(source['end'],part['end'])],
                'polarity':'asserted','rule':'foodex2_joint_source_part_identity',
                'premise_codes':[source['code'],part['code']]})

    def link_processes(self, annotation: dict) -> None:
        """Link scoped process events, retaining their polarity and food target."""
        for modifier in annotation['modifiers']:
            if modifier['field'] != 'processes' or modifier['value'] not in PROCESS_CODES:
                continue
            code, label = PROCESS_CODES[modifier['value']]
            term = self.terms.get(('process', code))
            if not term:
                continue
            if term['label'] != label:
                raise ValueError('Process mapping disagrees with exported FoodEx2: ' + code)
            modifier['foodex2_code'] = code
            modifier['catalogue_version'] = self.version
            annotation['evidence'].append({
                'field': 'facets.processes', 'quote': modifier['quote'],
                'start': modifier['start'], 'end': modifier['end'],
                'target_food_id': modifier['target_food_id'], 'polarity': modifier['polarity'],
                'foodex2_code': code, 'catalogue_version': self.version,
                'rule': 'local_process_lexical_normalization',
                'review_status': 'unreviewed_proposal'})
