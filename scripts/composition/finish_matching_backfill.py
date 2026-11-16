"""Wait for a staged backfill, then reconcile all graph links and verify storage."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
from sqlalchemy import text
from recipe_wrangler.utils.nutrition_postgres import get_engine


def finish(release_id):
    status=Path('data/analysis')/('matching_backfill_'+release_id)/'status.json'
    while True:
        if status.exists():
            current=json.loads(status.read_text())
            if current.get('status')=='failed':raise RuntimeError('Backfill failed; graph finalization not claimed complete')
        with get_engine().connect() as c:
            missing=c.execute(text('''SELECT count(*) FROM "nutrients-recipe-profiles" p
                WHERE p.nutrition_source IN ('eu','irish','hungarian','slovenian')
                AND NOT EXISTS (SELECT 1 FROM matching_profile_runs r
                    WHERE r.recipe_id=p.recipe_id AND r.nutrition_source=p.nutrition_source
                    AND r.release_id=:release)'''),{'release':release_id}).scalar()
        print('Profiles awaiting calculation',missing,flush=True)
        if missing==0:break
        time.sleep(30)
    from scripts.composition.bridge_matching_graph import bridge
    from scripts.composition.audit_matching_rollout import audit
    bridge(release_id,apply=True)
    result=audit(release_id)
    failures=[key for key in ['backfill_pending','graph_runs_pending','legacy_total_mismatches',
        'selected_occurrences_without_variant','graph_concepts_missing_registry','quantity_invariant_failures',
        'graph_quantity_mismatches'] if result.get(key)]
    if failures:raise RuntimeError('Final verification failed: '+','.join(failures))
    from scripts.composition.report_live_matching_reference import report
    if report(release_id):raise RuntimeError('Reference comparison is incomplete')
    print('Staged backfill and graph reconciliation verified. Production activation unchanged.',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-id',required=True)
    finish(p.parse_args().release_id)
