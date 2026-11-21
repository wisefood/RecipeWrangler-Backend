"""Read-only repair proposals for canonical original ingredient context.

No database writes, activation or graph mutation. Optional rematching uses the
deployed deterministic matcher and exports recalculated profiles for review.
"""

import argparse
import json
from pathlib import Path

from recipe_wrangler.matching.quantities import recipe_inputs, repair_profile
from recipe_wrangler.matching.source_evidence import unique_source_line,unique_source_lines
from recipe_wrangler.repositories import matching_store as store


def recover_missing_context(names: list[str], originals: list[str], source_lines: list[str]) -> tuple[list[str], list[dict]]:
    """Fill only unique one-to-one source bindings; never replace existing text."""
    if len(names)!=len(originals):raise ValueError('Ingredient/context lengths differ')
    if not all(isinstance(line,str) and line.strip() for line in source_lines):
        raise ValueError('Raw source must contain complete ingredient strings')
    candidates=[unique_source_line(name,source_lines) for name in names]
    anchored=unique_source_lines(names,source_lines)
    revised=[];bindings=[]
    for position,(name,old,line) in enumerate(zip(names,originals,candidates,strict=True)):
        allowed=bool(anchored[position])
        chosen=old or anchored[position]
        revised.append(chosen)
        bindings.append({'position':position,'ingredient':name,'source_line':chosen,
            'source_line_hash':store.digest(chosen) if chosen else None,
            'status':'preserved_canonical' if old else 'unique_archived_source_line' if allowed else 'unresolved',
            'reason':None if old or allowed else 'ambiguous_or_repeated_source_line' if line else 'no_unique_source_line'})
    return revised,bindings


def propose(active: dict, canonical: dict) -> dict:
    profile=active['profile']
    if profile.get('quantity_input_hash')!=canonical['quantity_input_hash']:
        raise ValueError('Active profile and canonical source context hash differ')
    details=profile['details']
    if len(details)!=len(canonical['names']):raise ValueError('Canonical occurrence count differs')
    for pos,detail in enumerate(details):
        if (detail.get('position')!=pos or (detail.get('ingredient') or detail.get('name'))!=canonical['names'][pos]
            or detail['weight_g']!=canonical['quantities'][pos]['weight_g']):
            raise ValueError('Canonical source occurrence does not match active profile')
    if profile.get('serves')!=canonical.get('serves'):
        raise ValueError('Canonical servings differ')
    repaired=repair_profile(profile,canonical,active['nutrition_source'],'original_context_repair_v1')
    changed=[{'position':d['position'],'ingredient':d['ingredient'],
        'before':old.get('original_ingredient_text') or '', 'after':d['original_ingredient_text'],
        'requires_rematching':d.get('source_context_status')=='requires_rematching'}
        for old,d in zip(details,repaired['details'],strict=True)
        if (old.get('original_ingredient_text') or '')!=d['original_ingredient_text']]
    return {'recipe_id':active['recipe_id'],'nutrition_source':active['nutrition_source'],
        'baseline_run_id':active['run_id'],'quantity_input_hash':canonical['quantity_input_hash'],
        'status':'requires_rematching' if repaired['source_context_rematch_positions'] else
            'missing_source_context' if any(not d['original_ingredient_text'] for d in repaired['details']) else 'already_complete',
        'total_occurrences':len(details),'before_with_text':sum(bool(d.get('original_ingredient_text')) for d in details),
        'after_with_text':sum(bool(d['original_ingredient_text']) for d in repaired['details']),
        'missing_positions':[d['position'] for d in repaired['details'] if not d['original_ingredient_text']],
        'changes':changed,'proposed_profile':repaired}


def recover_raw_context(active: dict, canonical: dict, raw_recipes: object) -> tuple[dict, dict]:
    """Bind full archived lines uniquely; never use the fragmented graph order."""
    propose(active,canonical)  # Verify the predecessor canonical vector first.
    rows=[dict(value,title=value.get('title') or key) for key,value in raw_recipes.items()] if isinstance(raw_recipes,dict) else raw_recipes
    matches=[row for row in rows if row.get('title')==active['profile']['title']]
    if len(matches)!=1:raise ValueError('Raw source recipe title must resolve uniquely')
    source_lines=matches[0].get('ingredients')
    if not isinstance(source_lines,list) or not all(isinstance(line,str) and line.strip() for line in source_lines):
        raise ValueError('Raw source must contain complete ingredient strings')
    for name,old in zip(canonical['names'],canonical['originals'],strict=True):
        line=unique_source_line(name,source_lines)
        if old and line and old!=line:raise ValueError('Archived source conflicts with canonical original text')
    originals,bindings=recover_missing_context(canonical['names'],canonical['originals'],source_lines)
    revised=recipe_inputs(canonical['names'],canonical['weights'],originals)
    revised['serves']=canonical['serves']
    return revised,{'previous_quantity_input_hash':canonical['quantity_input_hash'],
        'revised_quantity_input_hash':revised['quantity_input_hash'],
        'raw_source_digest':store.digest(raw_recipes),'bindings':bindings,
        'weight_changes':[{'position':i,'before':old['weight_g'],'after':new['weight_g']}
            for i,(old,new) in enumerate(zip(canonical['quantities'],revised['quantities'],strict=True)) if old['weight_g']!=new['weight_g']]}


def inspect(canonical_path: Path, recipe_ids: list[str], sources: list[str], output: Path, *, rematch: bool=False,
            raw_source: Path | None=None) -> dict:
    canonical=json.loads(canonical_path.read_text())
    before=store.deployment()
    report={'workflow':'original_context_repair_v1','database_writes':0,'llm_calls':0,'activated':False,
        'canonical_file':str(canonical_path),'canonical_digest':store.digest(canonical),'profiles':[]}
    matcher=None
    for recipe_id in recipe_ids:
        for source in sources:
            active=store.active_profile(recipe_id,source,include_provenance=True)
            if active is None:raise ValueError('Active profile missing: '+recipe_id+'/'+source)
            proposal=propose(active,canonical[recipe_id])
            inputs=canonical[recipe_id]
            if raw_source is not None:
                inputs,recovery=recover_raw_context(active,inputs,json.loads(raw_source.read_text()))
                revised=repair_profile(active['profile'],inputs,source,'original_context_repair_v1')
                proposal.update(status='requires_rematching',raw_source_path=str(raw_source),raw_source_recovery=recovery,
                    proposed_profile=revised,after_with_text=sum(bool(d['original_ingredient_text']) for d in revised['details']),
                    missing_positions=[d['position'] for d in revised['details'] if not d['original_ingredient_text']],
                    revised_canonical_inputs=inputs,
                    changes=[{'position':d['position'],'ingredient':d['ingredient'],'before':old.get('original_ingredient_text') or '',
                        'after':d['original_ingredient_text'],'requires_rematching':d.get('source_context_status')=='requires_rematching'}
                        for old,d in zip(active['profile']['details'],revised['details'],strict=True)
                        if (old.get('original_ingredient_text') or '')!=d['original_ingredient_text']])
            print(recipe_id,source,proposal['before_with_text'],'->',proposal['after_with_text'],flush=True)
            if rematch and proposal['proposed_profile']['source_context_rematch_positions']:
                from recipe_wrangler.matching.runtime import engine
                from recipe_wrangler.matching.quality import finalize_profile
                if matcher is None:matcher=engine(active['release_id'])
                if matcher.release_id!=active['release_id']:raise ValueError('Mixed catalogue releases')
                # Passing recipe_id persists a run; review rematches must stay local.
                recalculated=matcher.calculate(title=active['profile']['title'],ingredient_names=inputs['names'],
                    weights=inputs['weights'],source=source,serves=inputs['serves'],original_texts=inputs['originals'],
                    quantity_evidence=[q['quantity_evidence'] for q in inputs['quantities']])
                if recalculated['quantity_input_hash']!=inputs['quantity_input_hash']:
                    raise ValueError('Rematched source hash differs')
                recalculated['source_context_repair_of_run_id']=active['run_id']
                proposal['recalculated_profile']=finalize_profile(recalculated)
                proposal['status']='rematched_for_review' if not proposal['missing_positions'] else 'partially_recovered_rematched_for_review'
            current=store.active_profile(recipe_id,source,include_provenance=True)
            if current is None or current['run_id']!=active['run_id']:raise ValueError('Active profile changed during inspection')
            report['profiles'].append(proposal)
    if store.deployment()!=before:raise ValueError('Deployment changed during inspection')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--canonical',type=Path,default=Path('analysis/nutrition_finalization_2026-10-29/canonical_inputs_candidate.json'))
    parser.add_argument('--recipe-id',action='append',required=True)
    parser.add_argument('--source',action='append',choices=['eu','irish','hungarian','slovenian'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rematch',action='store_true')
    parser.add_argument('--raw-source',type=Path,help='Explicit archived raw source; recipe title must resolve uniquely')
    args=parser.parse_args()
    inspect(args.canonical,args.recipe_id,args.source or ['eu','irish','hungarian','slovenian'],args.output,
        rematch=args.rematch,raw_source=args.raw_source)
