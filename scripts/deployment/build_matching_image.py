"""Build a small-context API image with its pinned offline matching assets."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

from huggingface_hub import snapshot_download

from recipe_wrangler.utils.env_loader import load_runtime_env
load_runtime_env()
from recipe_wrangler.repositories.matching_store import release
from recipe_wrangler.matching.runtime import ASSETS


def build(release_id: str, tag: str) -> None:
    root=Path(__file__).resolve().parents[2]
    manifest=release(release_id)['manifest']
    snapshot=Path(snapshot_download(manifest['embedding_model'],
        revision=manifest['embedding_revision'],local_files_only=True))
    with tempfile.TemporaryDirectory(prefix='recipe-wrangler-image-') as tmp:
        context=Path(tmp)
        for relative in ['Dockerfile','pyproject.toml','README.md',
                         'scripts/deployment/matching-runtime-constraints.txt',*ASSETS.values()]:
            target=context/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(root/relative,target)
        shutil.copytree(root/'src',context/'src',ignore=shutil.ignore_patterns('.env*','__pycache__','*.pyc'))
        target=context/'matching-model-cache'/'hub'/snapshot.parent.parent.name/'snapshots'/snapshot.name
        shutil.copytree(snapshot,target,symlinks=False)
        subprocess.run(['docker','build','-t',tag,str(context)],check=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-id',required=True)
    parser.add_argument('--tag',default='wisefood/recipe-wrangler:matching-2026-10-29')
    args=parser.parse_args();build(args.release_id,args.tag)
