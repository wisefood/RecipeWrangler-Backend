from scripts.eval.evaluate_parsed_diets import metrics,native_allergen_queries


def test_accuracy_counts_abstentions_and_failures_in_denominator():
    cells=[{'expected':'suitable','predicted':s} for s in ['suitable','not_suitable','unknown','error']]
    cells += [{'expected':'not_suitable','predicted':s} for s in ['suitable','not_suitable','unknown']]
    cells += [{'expected':'unknown','predicted':'suitable'}]
    result=metrics(cells)
    assert result['assessable_cells']==7
    assert result['accuracy']==2/7
    assert result['positive_precision']==1/2
    assert result['positive_recall']==1/4
    assert result['unsafe_admission_rate']==1/3
    assert result['unknown_rate']==2/7
    assert result['accuracy_when_decided']==1/2
    assert result['failures']==1
    assert result['uncertain_references']==1


def test_empty_positive_precision_is_not_fabricated():
    result=metrics([{'expected':'suitable','predicted':'unknown'}])
    assert result['positive_precision'] is None
    assert result['positive_recall']==0
    assert result['accuracy']==0
    assert result['accuracy_when_decided'] is None


def test_full_cohort_keeps_uncertain_truth_visible_without_inventing_errors():
    result=metrics([
        {'expected':'suitable','predicted':'suitable'},
        {'expected':'suitable','predicted':'unknown'},
        {'expected':'not_suitable','predicted':'not_suitable'},
        {'expected':'unknown','predicted':'suitable'},
        {'expected':'unknown','predicted':'unknown'},
        {'expected':'unknown','predicted':'error'},
    ])
    assert result['positive_precision']==1
    assert result['positive_recall']==1/2
    assert result['coverage']==2/3
    assert result['correct_decision_yield']==result['accuracy']==2/3
    assert result['full_cohort_cells']==6
    assert result['full_cohort_unknowns']==2
    assert result['full_cohort_unknown_rate']==2/6
    assert result['full_cohort_decision_coverage']==3/6
    assert result['full_cohort_failures']==1
    assert result['unverified_suitable']==1
    assert result['false_positive']==result['incorrect_rejections']==0


def test_empty_full_cohort_rates_remain_unknown():
    result=metrics([])
    assert result['full_cohort_cells']==0
    assert result['full_cohort_unknown_rate'] is None
    assert result['full_cohort_decision_coverage'] is None


def test_allergen_queries_are_production_queries_scoped_to_recipe():
    keyword,ancestry=native_allergen_queries()
    assert 'UNWIND $ingredient_hits AS candidate' in keyword
    assert '$supported_links' in ancestry
    assert all('(r:Recipe {recipe_id: $recipe_id})' in q for q in [keyword,ancestry])
