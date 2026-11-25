"""CLI for the shared matching graph projector."""
import argparse
import json
from pathlib import Path
from recipe_wrangler.matching.graph_sync import bridge


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-id',required=True)
    p.add_argument('--apply',action='store_true');p.add_argument('--recipe-id',action='append')
    p.add_argument('--recipe-ids-file',type=Path)
    p.add_argument('--active-only',action='store_true')
    p.add_argument('--skip-frames',action='store_true',help='Retain existing frames during a profile-only refresh')
    a=p.parse_args();ids=json.loads(a.recipe_ids_file.read_text()) if a.recipe_ids_file else a.recipe_id
    bridge(a.release_id,a.apply,ids,active_only=a.active_only,project_frames=not a.skip_frames)
