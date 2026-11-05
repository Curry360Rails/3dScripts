#!/usr/bin/env python3
"""Run the Victorville OSM -> 3DEP/NAIP -> terrain -> GLB -> AnyLogic workflow.

Run `python run_rail_pipeline.py --help`. Each stage is opt-in; use --all for
acquisition and model building, and --import-anylogic to explicitly allow edits
in the open AnyLogic model. Paths are relative to this script's directory.
"""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from pipeline_cache import Cache, fingerprint, inventory

ROOT = Path(__file__).resolve().parent

def run(label, command, cwd=ROOT):
    print(f'\n=== {label} ===', flush=True)
    print(' '.join(str(x) for x in command), flush=True)
    subprocess.run([str(x) for x in command], cwd=str(cwd), check=True)

def check_file(path, label):
    if not Path(path).is_file():
        raise FileNotFoundError(f'{label} missing: {path}')

def check_tifs(path, label):
    path = Path(path)
    if path.is_file() and path.suffix.lower() in ('.tif', '.tiff'):
        return
    if path.is_dir() and any(p.suffix.lower() in ('.tif', '.tiff') for p in path.rglob('*')):
        return
    raise FileNotFoundError(f'No {label} TIFFs found under {path}')

def polygon_bbox(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if data.get('type') == 'FeatureCollection':
        features = data.get('features', [])
        if len(features) != 1:
            raise ValueError('Boundary GeoJSON must contain exactly one polygon feature')
        data = features[0]['geometry']
    elif data.get('type') == 'Feature':
        data = data['geometry']
    if data.get('type') != 'Polygon':
        raise ValueError('Boundary must be a WGS84 GeoJSON Polygon')
    ring = data['coordinates'][0]
    if not ring:
        raise ValueError('Empty boundary polygon')
    return min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--project-name',help='Override boundary filename project name')
    p.add_argument('--fresh',action='store_true',help='Use isolated new project resources and bypass existing cache')
    p.add_argument('--force-rebuild',action='store_true',help='Regenerate terrain stage')
    p.add_argument('--refresh-searches',action='store_true',help='Refresh OSM/USGS API searches')
    p.add_argument('--format',choices=['glb','obj'],default='glb')
    p.add_argument('--mesh-mode',choices=['adaptive','flat','full'],default='adaptive')
    p.add_argument('--cache-ttl-days',type=int,default=7)
    p.add_argument('--all', action='store_true', help='Run OSM, DEM, NAIP, terrain and Blender; AnyLogic requires --import-anylogic')
    for stage in ('osm', 'dem', 'naip', 'terrain', 'blender', 'anylogic'):
        p.add_argument('--' + stage, action='store_true', help='Run only this stage (combine flags as desired)')
    p.add_argument('--import-anylogic', action='store_true', help='Explicitly authorize edits to AnyLogic model (requires --anylogic or --all)')
    p.add_argument('--boundary', default='boundary.geojson', help='Shared WGS84 polygon GeoJSON for OSM, DEM and NAIP')
    p.add_argument('--dem-folder', default='3dep_dem')
    p.add_argument('--naip-folder', default='naip_downloads')
    p.add_argument('--osm-json', default='osm_output/railway_raw_osm.json')
    p.add_argument('--output', default='terrain_output')
    p.add_argument('--blender-exe', default=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe')
    p.add_argument('--workers', type=int, default=8)
    p.add_argument('--naip-workers', type=int, default=6)
    p.add_argument('--start-date', default='2023-01-01')
    p.add_argument('--end-date', default='2026-10-21')
    p.add_argument('--ratio', type=float, default=0.25, help='Blender decimation ratio; no baking')
    p.add_argument('--texture-scale', type=float, default=0.5)
    p.add_argument('--corridor-width', type=float, default=200)
    p.add_argument('--texture-resolution', type=float, default=0.25)
    p.add_argument('--texture-tile-size', type=int, default=2048)
    p.add_argument('--model-name', default='test3D')
    p.add_argument('--agent-name', default='Main')
    p.add_argument('--model-units-per-meter', type=float, default=0.03)
    p.add_argument('--dry-run', action='store_true', help='Print commands, do not execute any stages')
    args = p.parse_args()
    selected = {name for name in ('osm', 'dem', 'naip', 'terrain', 'blender', 'anylogic') if getattr(args, name)}
    if args.all:
        selected.update(('osm', 'dem', 'naip', 'terrain'))
        if args.format=='obj':selected.add('blender')
        if args.import_anylogic:
            selected.add('anylogic')
    if not selected:
        p.error('Choose --all or one or more stage flags, e.g. --terrain --blender')
    if 'anylogic' in selected and not args.import_anylogic:
        p.error('AnyLogic modifies the open model. Add --import-anylogic to confirm.')
    if args.workers < 1 or args.naip_workers < 1:
        p.error('Worker counts must be positive')
    if not (0 < args.ratio <= 1 and 0 < args.texture_scale <= 1 and args.model_units_per_meter > 0):
        p.error('Invalid ratio, texture scale, or model scale')
    boundary = (ROOT / args.boundary).resolve()
    project = args.project_name or boundary.stem
    if not project or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in project):
        p.error('Invalid project name')
    if args.fresh:
        from datetime import datetime,timezone
        project += '_fresh_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    def scoped(name):
        path=(ROOT/name).resolve()
        return path/project
    out=scoped(args.output);dem_dir=scoped(args.dem_folder);naip_dir=scoped(args.naip_folder)
    osm_json=scoped('osm_output')/'railway_raw_osm.json'
    cache=Cache(scoped('pipeline_cache')/'pipeline_cache.sqlite') if not args.dry_run else None
    def stage(label,cmd,kind,inputs,outputs,force=False,ttl=None):
        signature=fingerprint([kind,[str(x) for x in cmd[2:]],[(str(x),inventory(x)) for x in inputs],
                               int(time.time()//(ttl*86400)) if ttl else None])
        if args.dry_run:
            print('[DRY RUN]',label, ' '.join(map(str,cmd)));return
        if not (args.fresh or force or args.refresh_searches and ttl) and cache.valid(kind,signature,outputs):
            print(f'[CACHE HIT] {label}');return
        print(f'[CACHE MISS] {label}')
        run(label,cmd)
        if not all(Path(x).exists() for x in outputs):
            raise RuntimeError(f'{label} completed without expected outputs: {outputs}')
        cache.record(kind,signature,outputs)
    if 'osm' in selected:
        stage('Download Overpass railway geometry', [sys.executable, ROOT/'overpassQuery.py','--boundary',boundary,'--output',osm_json.parent],
              'osm',[boundary],[osm_json],ttl=args.cache_ttl_days)
    if 'dem' in selected:
        stage('Download 3DEP DEMs', [sys.executable,ROOT/'download_3dep_1m.py','--geojson',boundary,'--download','--extract','--output',dem_dir],
              'dem',[boundary],[dem_dir/'products.json'],ttl=args.cache_ttl_days)
    if 'naip' in selected:
        bbox=polygon_bbox(boundary) if not args.dry_run else ('WEST','SOUTH','EAST','NORTH')
        stage('Download NAIP imagery',[sys.executable,ROOT/'runM2M_NAIP.py','--bbox',*bbox,'--start',args.start_date,'--end',args.end_date,
             '--workers',args.naip_workers,'--output',naip_dir], 'naip',[boundary],[naip_dir],ttl=args.cache_ttl_days)
    if 'terrain' in selected:
        if not args.dry_run:
            check_tifs(dem_dir,'DEM');check_tifs(naip_dir,'NAIP imagery');check_file(osm_json,'OSM')
        cmd=[sys.executable,ROOT/'build_terrain.py','--dem',dem_dir,'--imagery',naip_dir,'--network',osm_json,
             '--corridor-only','--corridor-width',args.corridor_width,'--near-spacing',3,'--transition-spacing',10,
             '--background-spacing',70,'--fine-buffer',50,'--transition-buffer',200,
             '--texture-resolution',args.texture_resolution,'--texture-tile-size',args.texture_tile_size,
             '--texture-scale',args.texture_scale,'--workers',args.workers,'--format',args.format,
             '--mesh-mode',args.mesh_mode,'--texture-cache',out/'texture_cache','--output',out]
        stage('Build terrain',cmd,'terrain',[dem_dir,naip_dir,osm_json],
              [out/('terrain.glb' if args.format=='glb' else 'terrain.obj'),out/'georeference.json'],force=args.force_rebuild)
    if 'blender' in selected:
        if not args.dry_run:check_file(out/'terrain.glb','Terrain GLB')
        stage('Blender optimization',[args.blender_exe,'--background','--factory-startup','--python',ROOT/'blender_terrain_to_glb.py','--',
              '--input',out/'terrain.glb','--output',out/'terrain_optimized.glb','--ratio',args.ratio,
              '--texture-scale',args.texture_scale,'--merge-distance','0.0001'],
              'blender',[out/'terrain.glb'],[out/'terrain_optimized.glb'])
    if 'anylogic' in selected:
        rail_script = ROOT / 'buildAnylogicRailNetwork.py'
        if not args.dry_run:
            check_file(rail_script, '3D AnyLogic importer')
            check_file(osm_json, 'Raw OSM JSON')
            check_file(out / 'georeference.json', 'Terrain georeference')
            if not os.getenv('ANYLOGIC_DESIGN_TIME_TOKEN'):
                raise RuntimeError('Set ANYLOGIC_DESIGN_TIME_TOKEN before importing into AnyLogic')
            print('\n=== Importing into AnyLogic (model will be modified) ===', flush=True)
            spec = importlib.util.spec_from_file_location('rail_pipeline_importer', rail_script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.DATA_DIR = out
            module.OSM_JSON = osm_json
            module.DEM_MOSAIC = out / '_raster_mosaics' / 'dem_mosaic.vrt'
            module.DEM_FOLDER = dem_dir
            module.TERRAIN_GEOREFERENCE = out / 'georeference.json'
            module.MODEL_NAME = args.model_name
            module.AGENT_NAME = args.agent_name
            module.MODEL_UNITS_PER_METER = args.model_units_per_meter
            module.DRY_RUN = False
            module.main()
        else:
            print('[DRY RUN] Import AnyLogic tracks from', osm_json, 'using', rail_script)
    print('\nPipeline finished.', flush=True)

if __name__ == '__main__':
    main()
