"""CLI contract and Blender argument parsing without starting Blender or AnyLogic."""
import subprocess
import sys
import types
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]


def runner(*args):
    return subprocess.run([sys.executable,str(ROOT/'run_rail_pipeline.py'),*args],cwd=ROOT,capture_output=True,text=True)


def test_dry_run_terrain_uses_project_folder_and_glb():
    r=runner('--boundary','victorville.geojson','--terrain','--dry-run')
    assert r.returncode==0, r.stderr
    assert '3dep_dem/victorville' in r.stdout.replace('\\','/')
    assert '--format glb' in r.stdout
    assert 'terrain_output/victorville' in r.stdout.replace('\\','/')


def test_anylogic_requires_explicit_authorization():
    r=runner('--anylogic','--dry-run')
    assert r.returncode!=0
    assert '--import-anylogic' in r.stderr


def test_blender_cli_uses_glb_without_loading_bpy(monkeypatch):
    # bpy is only available inside Blender. A stub is enough to test argparse.
    monkeypatch.setitem(sys.modules,'bpy',types.ModuleType('bpy'))
    import importlib.util
    spec=importlib.util.spec_from_file_location('blender_under_test',ROOT/'blender_terrain_to_glb.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(sys,'argv',['blender','--','--input','terrain.glb','--output','optimized.glb','--ratio','0.25'])
    args=module.parse_args()
    assert args.input=='terrain.glb' and args.output=='optimized.glb' and args.ratio==.25
