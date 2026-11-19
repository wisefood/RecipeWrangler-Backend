"""File-only ingredient and nutrition comparison; no production or network writes."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

from scripts.composition.evaluate_irish_inhouse import ROOT, METRICS, dump, sha

REGIONAL = dict(zip(METRICS, ['Energy (kcal) (kcal)', 'Protein (g)', 'Carbohydrate (g)', 'Fat (g)',
    'Total sugars (g)', 'Satd FA /100g fd (g)', 'Sodium (mg)', 'Fiber (g)']))
COFID = REGIONAL | {'fibre_g': 'AOAC fibre (g)'}
EU = dict(zip(METRICS, ['Energy, kcal', 'Protein', 'Carbohydrate, by difference', 'Total lipid (fat)',
    'Sugars, total', 'Fatty acids, total saturated', 'Sodium, Na', 'Fiber, total dietary']))
EFSA_COMPONENTS = {'energy, total metabolisable': 'energy_kcal', 'protein, total': 'protein_g',
    'carbohydrate': 'carbs_g', 'fat, total': 'fat_g', 'sugars, total': 'sugar_g',
    'fatty acids, total saturated': 'saturated_fat_g', 'sodium': 'sodium_mg', 'fibre, total dietary': 'fibre_g'}


def numeric(value) -> float | None:
    """Unknown, trace, inequality and non-finite source values remain unknown."""
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace(',', '.'))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def scale(nutrients: dict, weight: float | None) -> dict:
    if weight is not None and (not math.isfinite(weight) or weight < 0):
        raise ValueError('Weight must be finite and nonnegative')
    return {k: 0.0 if weight == 0 else None if weight is None or nutrients.get(k) is None
            else nutrients[k] * weight / 100 for k in METRICS}


def aggregate(contributions: list[dict], serves: float | None) -> dict:
    """Partial sums are always labelled; one missing contribution prevents a full total."""
    if serves is not None and (not math.isfinite(serves) or serves <= 0):
        raise ValueError('Serves must be positive and finite')
    missing = {k: sum(c.get(k) is None for c in contributions) for k in METRICS}
    partial = {k: sum(c[k] for c in contributions if c.get(k) is not None) for k in METRICS}
    return {'total': {k: None if missing[k] else partial[k] for k in METRICS},
        'supported_partial_sum': partial, 'missing_contributions': missing,
        'per_serving': {k: None if missing[k] or serves is None else partial[k]/serves for k in METRICS},
        'supported_partial_sum_per_serving': {k: partial[k]/serves if serves is not None else None for k in METRICS},
        'serving_count':serves,
        'basis': 'input ingredient sum; finished cooked weight/yield/retention not established'}


def regional_nutrients(row: dict, fields: dict = REGIONAL) -> dict:
    return {k: numeric(row.get(field)) for k, field in fields.items()}


def live_nutrients(row: dict | None) -> dict:
    if not row:
        return dict.fromkeys(METRICS)
    if 'nutrients' not in row:
        return regional_nutrients(row)
    result = {}
    for metric, name in EU.items():
        item = row['nutrients'].get(name)
        if metric == 'sugar_g' and item is None:
            item = row['nutrients'].get('Sugars, total including NLEA')
        expected = 'kcal' if metric == 'energy_kcal' else 'mg' if metric == 'sodium_mg' else 'g'
        value = numeric(item.get('value')) if isinstance(item, dict) and item.get('unit') == expected else None
        if metric == 'energy_kcal' and value is None:
            energy = row['nutrients'].get('Energy')
            if isinstance(energy, dict) and energy.get('unit') in {'kJ', 'kcal'}:
                amount = numeric(energy.get('value'))
                value = amount/(4.184 if energy['unit']=='kJ' else 1) if amount is not None else None
        result[metric] = value
    return result


def load_source_nutrients(records: dict[str, dict], output: Path) -> tuple[dict, list[Path]]:
    sources = {}; paths = []
    cofid_path = ROOT/'data/nutrition/composition_tables/eu_national/CoFID-UK/McCance_Widdowsons_Composition_of_Foods_Integrated_Dataset_2021..xlsx'
    paths.append(cofid_path)
    workbook = load_workbook(cofid_path, read_only=True, data_only=True)
    by_id = defaultdict(dict)
    for sheet_name in ['1.3 Proximates', '1.4 Inorganics']:
        sheet = workbook[sheet_name]
        headers = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))
        for values in sheet.iter_rows(min_row=4, values_only=True):
            by_id[str(values[0])].update({k:v for k,v in zip(headers, values) if k})
    workbook.close()
    for key, record in records.items():
        if record['source']['dataset'] == 'CoFID':
            raw = by_id.get(record['source']['record_id'], {})
            sources[key] = {'source': record['source'], 'label': record['label'],
                'values_per_100g': regional_nutrients(raw, COFID),
                'raw_values': {v:raw.get(v) for v in COFID.values()}, 'basis': 'per 100 g edible food in selected state',
                'file': str(cofid_path.relative_to(ROOT)), 'notes': ['Trace/N/blank are unknown, not zero.']}
    for relative, dataset in [('data/nutrition/composition_tables/irish/Irish_Comp_Table.csv', 'Irish composition table'),
                              ('data/nutrition/composition_tables/hungarian/hungarian_comp_table.csv','Hungarian composition table')]:
        path = ROOT/relative; paths.append(path)
        with path.open() as f:
            rows = {r['canonical_food_id']:r for r in csv.DictReader(f)}
        for key, record in records.items():
            if record['source']['dataset'] == dataset:
                raw = rows.get(record['source']['record_id'], {})
                fields = REGIONAL if dataset == 'Irish composition table' else REGIONAL | {
                    'sugar_g':'Sugar (g)', 'saturated_fat_g':'Saturated Fat (g)'}
                values = regional_nutrients(raw, fields)
                sources[key] = {'source': record['source'], 'label':record['label'], 'values_per_100g':values,
                    'file':relative,'raw_values':{v:raw.get(v) for v in fields.values()},
                    'notes': [] if dataset == 'Irish composition table' else ['Some Hungarian fields are enriched; retained source provenance must be reviewed.']}
    efsa_wanted = {(r['source']['dataset'],r['source']['record_id']):key for key,r in records.items()
                   if r['source']['release']=='EFSA EU FCDB 2026'}
    # Variants share native values but retain independent matching metadata.
    efsa_variants = defaultdict(list)
    for key,r in records.items():
        if r['source']['release']=='EFSA EU FCDB 2026':
            efsa_variants[(r['source']['dataset'],r['source']['record_id'])].append(key)
    component_path = ROOT/'data/nutrition/composition_tables/efsa_eu/component_dictionary.csv'
    values_path = ROOT/'data/nutrition/composition_tables/efsa_eu/composition_values.csv'
    paths.extend([component_path, values_path])
    with component_path.open(encoding='utf-16', newline='') as f:
        components = {(r['Dataset name'],r['Original component code']): EFSA_COMPONENTS.get(r['English component name'].strip().lower()) for r in csv.DictReader(f)}
    values = defaultdict(lambda:defaultdict(list))
    with values_path.open(encoding='utf-16', newline='') as f:
        for number,r in enumerate(csv.DictReader(f),2):
            native = (r['Dataset name'],r['Original food code'])
            if native not in efsa_wanted or r['Matrix Unit']!='Edible portion':
                continue
            metric = components.get((r['Dataset name'],r['Original component code']))
            value = numeric(r['Value']);unit = r['Value Unit']
            if metric is None or value is None:
                continue
            expected = {'energy_kcal':'Kilocalories/100 gram','sodium_mg':'Milligram/100 gram'}.get(metric,'Gram/100 gram')
            if metric == 'energy_kcal' and unit=='Kilojoule/100 gram':
                value /= 4.184
            elif metric == 'sodium_mg' and unit=='Gram/100 gram':
                value *= 1000
            elif metric == 'sodium_mg' and unit=='Microgram/100 gram':
                value /= 1000
            elif metric != 'energy_kcal' and metric != 'sodium_mg' and unit=='Milligram/100 gram':
                value /= 1000
            elif unit != expected:
                continue
            values[native][metric].append({'value':value,'source_csv_row':number,'raw_value':r['Value'],'raw_unit':unit})
    for native, keys in efsa_variants.items():
        for key in keys:
            r = records[key];known = values[native]; normalized = {}; issues=[]
            for metric in METRICS:
                observations = known[metric]
                distinct = {round(v['value'],6) for v in observations}
                # Prefer original kcal over converted kJ to avoid treating energy rounding as conflict.
                if metric == 'energy_kcal':
                    kcal = [v for v in observations if v['raw_unit']=='Kilocalories/100 gram']
                    if kcal: observations=kcal;distinct={round(v['value'],6) for v in observations}
                normalized[metric] = observations[0]['value'] if len(distinct)==1 else None
                if len(distinct)>1:issues.append('conflicting_native_values:'+metric)
            sources[key] = {'source':r['source'],'label':r['label'],'values_per_100g':normalized,
                'native_values':dict(known),'notes':issues,'file':str(values_path.relative_to(ROOT))}
    for key,r in records.items():
        sources.setdefault(key, {'source':r['source'],'label':r['label'],'values_per_100g':dict.fromkeys(METRICS),
            'notes':['No enabled native nutrient adapter; not treated as zero.']})
    dump(output/'source_nutrients.json', sources)
    return sources, paths


def build(output: Path) -> None:
    output = output.resolve()
    if (output/'comparison.xlsx').exists():
        raise FileExistsError(output/'comparison.xlsx')
    pack = json.loads((output/'pack.json').read_text())
    expected = json.loads((output/'expectations.json').read_text())
    expectations = {e['occurrence_id']:e for e in expected['expectations']}
    baseline = json.loads((output/'production_capture.json').read_text())
    legacy = {r['evaluation_id']:r for r in baseline['recipes']}
    controls = {r['occurrence_id']:r for r in baseline['identical_name_input_control']}
    hybrid = {}
    for line in (output/'hybrid_frozen.jsonl').open():
        r=json.loads(line);hybrid[(r['input_text'],r['input_origin'])]=r
    needed={e['reference_record_key'] for e in expectations.values() if e['reference_record_key']}
    for r in hybrid.values():needed.update(r['proposed_record_keys'] if r['status']=='proposal' else [])
    catalogue_path=ROOT/'data/nutrition/matching/catalogue/composition_catalogue_2026-10-22/records.jsonl'
    records={r['catalogue_key']:r for line in catalogue_path.open() if (r:=json.loads(line))['catalogue_key'] in needed}
    if needed != set(records):raise ValueError('Source record missing from retained catalogue')
    nutrient_path=output/'source_nutrients.json'
    if nutrient_path.exists():
        nutrients=json.loads(nutrient_path.read_text()); source_paths=[ROOT/'data/nutrition/composition_tables/efsa_eu/composition_values.csv',ROOT/'data/nutrition/composition_tables/efsa_eu/component_dictionary.csv',ROOT/'data/nutrition/composition_tables/irish/Irish_Comp_Table.csv', ROOT/'data/nutrition/composition_tables/hungarian/hungarian_comp_table.csv', ROOT/'data/nutrition/composition_tables/eu_national/CoFID-UK/McCance_Widdowsons_Composition_of_Foods_Integrated_Dataset_2021..xlsx']
    else:
        nutrients,source_paths=load_source_nutrients(records, output)
    dependencies=json.loads((output/'production_dependencies.json').read_text())
    live={}
    for k,v in dependencies.items():
        name,args,_=json.loads(k)
        if name.startswith('calculator.get_'):live[(name,args[0])]=v
    ingredients=[];recipe_results=[];outcomes=Counter();old_outcomes=Counter();comparison_counts=Counter()
    for recipe in pack['recipes']:
        old=legacy[recipe['evaluation_id']];arms=defaultdict(list)
        for o,m,d in zip(recipe['occurrences'],old['matches'],old['calculation']['details'],strict=True):
            e=expectations[o['occurrence_id']];text=o['parsed_name'].strip()
            h=hybrid[(text,'ingredient_name')]
            if m['query'] != text or controls[o['occurrence_id']]['query'] != text:
                raise ValueError('Old and new matcher inputs must equal the saved ingredient name')
            shown=next((c for c in h['candidates'] if c['key']==(h['proposed_candidate_key'] or h['review_candidate_key'])),None)
            keys=h['proposed_record_keys'] if h['status']=='proposal' else []
            ordered=sorted(keys,key=lambda k:(records[k]['source']['dataset']!='CoFID', records[k]['source']['dataset']!='Irish composition table',k))
            # This is a disclosed simulation source policy, not a new matcher decision.
            selected=ordered[0] if ordered else None
            new_values=nutrients[selected]['values_per_100g'] if selected else {}
            source=m['decision'].get('source_key'); match=m['decision'].get('match') or {}; meta=match.get('metadata') or {}
            native_id=meta.get('canonical_food_id') if source=='irish' else meta.get('eu_id')
            oldrow=live.get(('calculator.get_'+str(source)+'_ingredient_nutrition',native_id))
            used=m['decision'].get('confidence') in {'strong','curated'} and bool(oldrow)
            old_values=live_nutrients(oldrow) if used else {}
            reference=nutrients[e['reference_record_key']]['values_per_100g'] if e['reference_record_key'] else {}
            if e['reference_status']=='nonconsumed_cooking_medium':reference={k:0.0 for k in METRICS}
            contributions={
                'legacy_at_saved_weight':scale(old_values,o['saved_weight_g']),
                'hybrid_proposals_at_saved_weight':scale(new_values,o['saved_weight_g']),
                'reference_at_saved_weight':scale(reference,o['saved_weight_g']),
                'reference_at_reviewed_weight':scale(reference,e['reference_weight_g'])}
            for arm,c in contributions.items():arms[arm].append(c)
            ranges={k: [min(v),max(v)] if (v:=[nutrients[key]['values_per_100g'][k] for key in ordered if nutrients[key]['values_per_100g'][k] is not None]) else None for k in METRICS}
            outcome='proposal' if h['status']=='proposal' else 'abstention_with_unverified_search' if h['status']=='fallback_proposal' else h['status']
            old_outcomes[m['decision']['confidence']]+=1;outcomes[outcome]+=1
            transition=('legacy_used_to_new_abstention' if used and not keys else 'both_select_provisionally' if used and keys
                        else 'legacy_abstention_to_new_proposal' if keys else 'both_abstain')
            comparison_counts[transition]+=1
            issues=[]
            if e['reference_weight_g'] is None:issues.append('reference_quantity_unresolved')
            elif not math.isclose(e['reference_weight_g'],o['saved_weight_g']):issues.append('saved_quantity_disagrees_with_inhouse_reference')
            if d.get('weight_capped'):issues.append('production_quantity_cap_applied')
            if selected and any(v is None for v in new_values.values()):issues.append('proposed_source_nutrients_incomplete')
            if len(keys)>1:issues.append('multiple_proposed_source_variants; simulation_source_policy_required')
            row={'occurrence_id':o['occurrence_id'],'recipe_id':recipe['recipe_id'],'recipe_title':recipe['title'],
                'input_ingredient':text,'original_ingredient_text':o['original_text'],'parsed_name':o['parsed_name'],'old_candidate':m['decision'].get('matched_name'),
                'new_display_candidate':shown['label'] if shown else 'multiple foods / no review candidate',
                'old_confidence':m['decision']['confidence'],'new_outcome':outcome,'transition':transition,
                'old_source_id':native_id,'old_source':source,'old_query':m['query'],
                'identical_input_old_confidence':controls[o['occurrence_id']]['decision']['confidence'],
                'identical_input_old_candidate':controls[o['occurrence_id']]['decision'].get('matched_name'),
                'new_simulated_record_key':selected,'new_proposed_record_keys':keys,
                'new_candidate_facets':shown['annotation']['facets'] if shown else {},
                'new_input_facets':h['annotation']['facets'],'new_source_nutrient_ranges':ranges,
                'saved_weight_g':o['saved_weight_g'],'production_effective_weight_g':d['weight_g'],
                'reference_weight_g':e['reference_weight_g'],'expectation':e,
                'nutrients_per_100g':{'legacy':old_values,'hybrid':new_values,'reference':reference},
                'contributions':contributions,'issues':issues,'new_method':h.get('match_method'),
                'new_reasons':shown['compatibility']['rules'] if shown else [],
                'alternatives':[{'label':c['label'],'relation':c['compatibility']['relation']} for c in h['candidates'][:5]]}
            ingredients.append(row)
        result={'evaluation_id':recipe['evaluation_id'],'recipe_title':recipe['title'],'serves':recipe['serves'],
            'ingredient_count':len(recipe['occurrences']),'calculations':{a:aggregate(c,recipe['serves']) for a,c in arms.items()},
            'legacy_actual_calculator':old['calculation']['clean_totals'],
            'legacy_actual_per_serving':old['calculation']['clean_totals_per_serving'],
            'published_nutrition_diagnostic_only':recipe['published_nutrition_diagnostic_only'],
            'cooked_per_100g':None,'cooked_per_100g_reason':'No validated final cooked/drained weight or yield/retention factors.',
            'missing_original_lines':sum(not o['original_text'] for o in recipe['occurrences'])}
        recipe_results.append(result)
    dump(output/'ingredient_comparison.json',ingredients);dump(output/'recipe_comparison.json',recipe_results)
    deviations = {}
    for metric in METRICS:
        pairs = []
        for r in ingredients:
            c = r['contributions']
            ref = c['reference_at_saved_weight'][metric]
            a, b = c['legacy_at_saved_weight'][metric], c['hybrid_proposals_at_saved_weight'][metric]
            if ref is not None and a is not None and b is not None:
                pairs.append((abs(a-ref), abs(b-ref)))
        deviations[metric] = {'paired_occurrences':len(pairs),
            'legacy_mean_absolute_deviation':sum(a for a,b in pairs)/len(pairs) if pairs else None,
            'hybrid_mean_absolute_deviation':sum(b for a,b in pairs)/len(pairs) if pairs else None,
            'meaning':'Same saved grams, working reference records; conditional on both selections and known nutrient values. Not overall accuracy.'}
    summary={'recipes':len(recipe_results),'occurrences':len(ingredients),'old_confidence_counts':dict(old_outcomes),
        'new_outcomes':dict(outcomes),'operational_transitions_not_correctness':dict(comparison_counts),
        'provisional_old_description_assessments':dict(Counter(r['expectation']['old_description_assessment'] for r in ingredients)),
        'explicit_weight_corrections':sum('saved_quantity_disagrees_with_inhouse_reference' in r['issues'] for r in ingredients),
        'unresolved_reference_quantities':sum(r['reference_weight_g'] is None for r in ingredients),
        'production_caps':sum('production_quantity_cap_applied' in r['issues'] for r in ingredients),
        'reference_records_assigned':sum(bool(r['expectation']['reference_record_key']) for r in ingredients),
        'new_provisional_description_assessments':dict(Counter(r['expectation']['new_decision_assessment'] for r in ingredients)),
        'faulty_new_display_candidates':sum(r['expectation']['new_display_assessment']=='wrong_or_unsupported_candidate' for r in ingredients)
            if any(r['expectation']['new_display_assessment']!='not_reassessed_name_only' for r in ingredients) else None,
        'hybrid_proposals_with_all_eight_numeric_nutrients':sum(r['new_outcome']=='proposal' and all(v is not None for v in r['nutrients_per_100g']['hybrid'].values()) for r in ingredients),
        'paired_working_reference_deviations':deviations,
        'new_source_policy':'Among proposed variants: CoFID then Irish then lexical catalogue key. No retrieval fallback used. This is a simulation, not production selection.',
        'metric_complete_recipe_counts':{a:{k:sum(r['calculations'][a]['total'][k] is not None for r in recipe_results) for k in METRICS} for a in recipe_results[0]['calculations']},
        'limitations':['Purposive assistant-authored development pack, not independent accuracy.',
            'Current production replay on saved inputs, not a new parse/weight run.',
            'Both matchers receive identical saved ingredient names; hybrid has a wider catalogue, not a same-universe accuracy comparison.',
            'Reference values are working source choices with explicit assumptions, not measured recipe nutrition.',
            'Partial nutrient sums never imply full nutrition or validated finished cooked weight.']}
    dump(output/'summary.json',summary)
    headers=['Input ingredient name','Old candidate','New candidate / search suggestion','Old confidence','New outcome','Recipe',
        'Occurrence ID','Transition (not correctness)','Saved grams','Production effective grams','Reference grams',
        'Expected identity','Reference food','Reference status','In-house old description assessment','Interpretation / uncertainty',
        'Quantity rationale','Old source ID','New simulated source key','All proposed source keys JSON',
        'Nutrient contributions JSON','New reasons JSON','Leading alternatives JSON','New input facets JSON','Issues',
        'New in-house decision assessment','New displayed candidate assessment','New inspection reason','Original ingredient text (audit only)']
    table=[]
    for r in ingredients:
        e=r['expectation'];table.append([r['input_ingredient'],r['old_candidate'],r['new_display_candidate'],r['old_confidence'],r['new_outcome'],r['recipe_title'],r['occurrence_id'],r['transition'],r['saved_weight_g'],r['production_effective_weight_g'],r['reference_weight_g'],e['expected_identity'],e['reference_label'],e['reference_status'],e['old_description_assessment'],e['interpretation_rationale'],e['quantity_rationale'],r['old_source_id'],r['new_simulated_record_key'],json.dumps(r['new_proposed_record_keys']),json.dumps(r['contributions']),json.dumps(r['new_reasons']),json.dumps(r['alternatives']),json.dumps(r['new_input_facets']),'; '.join(r['issues']),e['new_decision_assessment'],e['new_display_assessment'],e['new_inspection_reason']])
        table[-1].append(r['original_ingredient_text'])
    with (output/'ingredient_comparison.csv').open('x',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(headers);w.writerows(table)
    book=Workbook();sheet=book.active;sheet.title='Ingredient comparison';sheet.append(headers)
    for row in table:sheet.append(row)
    totals=book.create_sheet('Recipe nutrients');totals.append(['Recipe','Arm','Nutrient','Full total (null if incomplete)','Supported partial sum','Missing contributions','Full per serving','Partial per serving','Finished cooked per 100g'])
    for r in recipe_results:
        for arm,c in r['calculations'].items():
            for k in METRICS:totals.append([r['recipe_title'],arm,k,c['total'][k],c['supported_partial_sum'][k],c['missing_contributions'][k],c['per_serving'][k],c['supported_partial_sum_per_serving'][k],None])
    contexts = json.loads((output/'recipe_context_review.json').read_text())
    context_by_id = {r['evaluation_id']:r for r in contexts}
    context_sheet = book.create_sheet('Recipe context')
    context_sheet.append(['Recipe','Evaluation ID','Source URL','Serves','Original ingredient lines',
        'Original instructions','In-house quantity and process notes','Stock dilution water ml missing from saved rows',
        'Published nutrition (diagnostic only)'])
    for recipe in pack['recipes']:
        context = context_by_id[recipe['evaluation_id']]
        context_sheet.append([recipe['title'],recipe['evaluation_id'],recipe['url'],recipe['serves'],
            '\n'.join(recipe['original_ingredients']),'\n'.join(recipe['instructions']),
            context['inhouse_quantity_and_process_notes'],context['stock_dilution_water_ml_not_in_saved_rows'],
            json.dumps(recipe['published_nutrition_diagnostic_only'])])
    source_sheet = book.create_sheet('Source nutrients')
    source_sheet.append(['Catalogue key','Food description',*METRICS,'Source file','Notes / missing values'])
    for key,source in sorted(nutrients.items()):
        source_sheet.append([key,source['label'],*[source['values_per_100g'][k] for k in METRICS],
            source.get('file'),'; '.join(source.get('notes',[]))])
    info=book.create_sheet('Read me')
    for label,value in [('Scope','20 development recipes; in-house provisional expectations, not independent accuracy.'),
        ('Candidate columns','Old and new shown adjacent. Search suggestions and review candidates are NOT selected food records.'),
        ('Missing values','Blank full totals mean incomplete. Supported partial sums omit unknown contributions; they are not complete recipe nutrition.'),
        ('Reference','Source-record input estimates with explicit state/quantity assumptions; trace/N/blank nutrient fields remain unknown.'),
        ('New source choice',summary['new_source_policy']),('Production actual','Actual production sums may use zeros for unmatched fields and protective quantity caps; JSON retains them separately.'),
        ('Comparison','Saved quantities held constant to isolate matching; corrected quantity scenario is separate. No new end-to-end parse.'),
        ('Final cooked weight','Not established. No cooked per-100g values, invented yield/retention factors or ranking by Nutri-Score.'),
        ('Published values','Source website nutrition is diagnostic only; not measured or guaranteed equivalent.'),
        ('Production','Unchanged. No training, data migration or reference-pack labelling.')]:info.append([label,value])
    for s in book:
        s.freeze_panes='D2';s.auto_filter.ref=s.dimensions
        for c in s[1]:c.font=Font(bold=True)
        for col in s.columns:
            s.column_dimensions[col[0].column_letter].width=45
        for row in s:
            for c in row:
                c.alignment=Alignment(vertical='top',wrap_text=True)
                if isinstance(c.value,str):c.data_type='s'
    book.save(output/'comparison.xlsx')
    paths=[output/'pack.json',output/'expectations.json',output/'recipe_context_review.json',output/'production_capture.json',output/'hybrid_frozen.jsonl',nutrient_path,
           Path(__file__),ROOT/'scripts/composition/evaluate_irish_inhouse.py',catalogue_path,*source_paths]
    dump(output/'comparison_manifest.json',{'sha256':{str(p.relative_to(ROOT)):sha(p) for p in paths},
        'production_writes':False,'no_training':True,'reference_label_status':'provisional_inhouse',
        'expected_before_fixes':True,'expectations_blind_to_previous_predictions':False})
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__);cli.add_argument('--output-dir',type=Path,required=True)
    build(cli.parse_args().output_dir)
