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
        selected.update(('osm', 'dem', 'naip', 'terrain', 'blender'))
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
    out = (ROOT / args.output).resolve()
    dem_dir = (ROOT / args.dem_folder).resolve()
    naip_dir = (ROOT / args.naip_folder).resolve()
    osm_json = (ROOT / args.osm_json).resolve()
    boundary = (ROOT / args.boundary).resolve()
    if not args.dry_run and any(k in selected for k in ('osm', 'dem', 'naip')):
        check_file(boundary, 'Shared boundary GeoJSON')
    def stage(label, cmd):
        if args.dry_run:
            print(f'[DRY RUN] {label}:', ' '.join(map(str, cmd)), flush=True)
        else:
            run(label, cmd)
    if 'osm' in selected:
        stage('Download Overpass railway geometry', [sys.executable, ROOT / 'overpassQuery.py', '--boundary', boundary])
    if 'dem' in selected:
        if not args.dry_run: check_file(boundary, 'Boundary polygon')
        stage('Download 3DEP DEMs', [sys.executable, ROOT / 'download_3dep_1m.py',
              '--geojson', boundary, '--download', '--extract', '--output', dem_dir])
    if 'naip' in selected:
        if not args.dry_run:
            check_file(boundary, 'Boundary polygon')
            bbox = polygon_bbox(boundary)
        else:
            bbox = ('WEST', 'SOUTH', 'EAST', 'NORTH')
        stage('Download latest NAIP scenes', [sys.executable, ROOT / 'runM2M_NAIP.py',
              '--bbox', *bbox, '--start', args.start_date, '--end', args.end_date,
              '--workers', args.naip_workers, '--output', naip_dir])
    if 'terrain' in selected:
        if not args.dry_run:
            check_tifs(dem_dir, 'DEM')
            check_tifs(naip_dir, 'NAIP imagery')
            check_file(osm_json, 'Raw OSM JSON')
        stage('Build adaptive terrain', [sys.executable, ROOT / 'build_terrain.py',
              '--dem', dem_dir, '--imagery', naip_dir, '--network', osm_json,
              '--corridor-only', '--corridor-width', args.corridor_width,
              '--near-spacing', 3, '--transition-spacing', 10, '--background-spacing', 70,
              '--fine-buffer', 50, '--transition-buffer', 200,
              '--texture-resolution', args.texture_resolution,
              '--texture-tile-size', args.texture_tile_size,
              '--workers', args.workers, '--output', out])
    if 'blender' in selected:
        if not args.dry_run:
            check_file(out / 'terrain.obj', 'Terrain OBJ')
            check_file(args.blender_exe, 'Blender executable')
        stage('Optimize terrain and export GLB (no baking)', [args.blender_exe,
              '--background', '--factory-startup', '--python', ROOT / 'blender_terrain_to_glb.py', '--',
              '--input', out / 'terrain.obj', '--output', out / 'terrain_optimized.glb',
              '--ratio', args.ratio, '--texture-scale', args.texture_scale,
              '--merge-distance', '0.0001'])
    if 'anylogic' in selected:
        rail_script = ROOT / 'buildAnylogicRailNetwork_osm_dem_scaled_fixedz.py'
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
            module.DEM_MOSAIC = out / '_raster_mosaics' / 'dem_mosaic.tif'
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
