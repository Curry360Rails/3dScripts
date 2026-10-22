#!/usr/bin/env python3
"""OSM -> 3DEP / NAIP -> direct GLB terrain + inferred crossings -> AnyLogic.

All paths relative to this script. --all runs acquisition and terrain only;
AnyLogic model edits require --import-anylogic. --dry-run prints commands.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def resolve_path(value):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def check_file(path, label):
    if not Path(path).is_file():
        raise FileNotFoundError(f'{label} missing: {path}')


def check_tifs(path, label):
    path = Path(path)
    if path.is_file() and path.suffix.lower() in ('.tif', '.tiff'):
        return
    if path.is_dir() and any(p.suffix.lower() in ('.tif', '.tiff') for p in path.rglob('*')):
        return
    raise FileNotFoundError(f'No {label} TIFFs found: {path}')


def polygon_bbox(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if data.get('type') == 'FeatureCollection':
        features = data.get('features', [])
        if len(features) != 1:
            raise ValueError('Boundary must contain one polygon feature')
        data = features[0]['geometry']
    elif data.get('type') == 'Feature':
        data = data['geometry']
    if data.get('type') != 'Polygon':
        raise ValueError('Boundary must be a WGS84 GeoJSON Polygon')
    ring = data['coordinates'][0]
    return min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--all', action='store_true', help='Run OSM, DEM, NAIP and direct-GLB terrain stages')
    for name in ('osm', 'dem', 'naip', 'terrain', 'blender', 'anylogic'):
        p.add_argument('--' + name, action='store_true', help=f'Run {name} stage')
    p.add_argument('--import-anylogic', action='store_true', help='Confirm modifying open AnyLogic model')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--boundary', default='boundary.geojson')
    p.add_argument('--dem-folder', default='3dep_dem')
    p.add_argument('--dem-input', help='Optional existing DEM mosaic TIFF (avoids rebuilding DEM mosaic)')
    p.add_argument('--naip-folder', default='naip_downloads')
    p.add_argument('--osm-json', default='osm_output/railway_raw_osm.json')
    p.add_argument('--infrastructure-json', default='osm_output/infrastructure_raw_osm.json')
    p.add_argument('--output', default='terrain_output')
    p.add_argument('--workers', type=int, default=8, help='Clipping workers')
    p.add_argument('--refine-workers', type=int, default=12)
    p.add_argument('--dem-workers', type=int, default=0, help='3DEP concurrent downloads: 0 = all products (default)')
    p.add_argument('--naip-workers', type=int, default=6)
    p.add_argument('--start-date', default='2023-01-01')
    p.add_argument('--end-date', default='2026-10-21')
    p.add_argument('--corridor-width', type=float, default=200)
    p.add_argument('--near-spacing', type=float, default=3)
    p.add_argument('--transition-spacing', type=float, default=10)
    p.add_argument('--background-spacing', type=float, default=70)
    p.add_argument('--fine-buffer', type=float, default=50)
    p.add_argument('--transition-buffer', type=float, default=200)
    p.add_argument('--max-cells', type=int, default=5000000)
    p.add_argument('--max-elevation-error', type=float, default=0.5)
    p.add_argument('--flat-spacing', type=float, default=20)
    p.add_argument('--texture-resolution', type=float, default=0.25)
    p.add_argument('--texture-scale', type=float, default=0.5)
    p.add_argument('--texture-tile-size', type=int, default=2048)
    p.add_argument('--no-bridges', action='store_true', help='Skip OSM-tagged bridge decks')
    p.add_argument('--no-inferred-crossings', action='store_true', help='Skip DEM-inferred rail crossings and shared Z profiles')
    p.add_argument('--no-ground-mask', action='store_true', help='Skip dark ground mask beneath inferred crossings')
    p.add_argument('--rail-sample-spacing', type=float, default=5)
    p.add_argument('--rail-max-grade', type=float, default=0.03)
    p.add_argument('--rail-min-span', type=float, default=10)
    p.add_argument('--rail-max-span', type=float, default=150)
    p.add_argument('--rail-min-dem-drop', type=float, default=1.5)
    p.add_argument('--blender-exe', default=r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe')
    p.add_argument('--ratio', type=float, default=0.25, help='Legacy optional Blender stage decimation ratio')
    p.add_argument('--model-name', default='test3D')
    p.add_argument('--agent-name', default='Main')
    p.add_argument('--model-units-per-meter', type=float, default=0.03)
    args = p.parse_args()
    names = ('osm', 'dem', 'naip', 'terrain', 'blender', 'anylogic')
    selected = {name for name in names if getattr(args, name)}
    if args.all:
        selected.update(('osm', 'dem', 'naip', 'terrain'))
        if args.import_anylogic:
            selected.add('anylogic')
    if not selected:
        p.error('Specify --all or at least one stage')
    if 'anylogic' in selected and not args.import_anylogic:
        p.error('AnyLogic changes the open model. Add --import-anylogic')
    if min(args.workers, args.refine_workers, args.naip_workers) < 1 or args.dem_workers < 0:
        p.error('Invalid worker count')
    if not 0 < args.texture_scale <= 1:
        p.error('Texture scale must be in (0, 1]')
    out = resolve_path(args.output)
    dem_dir = resolve_path(args.dem_folder)
    dem_input = resolve_path(args.dem_input) if args.dem_input else dem_dir
    naip_dir = resolve_path(args.naip_folder)
    osm_json = resolve_path(args.osm_json)
    infrastructure = resolve_path(args.infrastructure_json)
    boundary = resolve_path(args.boundary)

    def stage(label, cmd):
        cmd = [str(x) for x in cmd]
        print(f'\n=== {label} ===\n' + ' '.join(cmd), flush=True)
        if not args.dry_run:
            subprocess.run(cmd, cwd=str(ROOT), check=True)

    if 'osm' in selected:
        if not args.dry_run: check_file(boundary, 'Boundary GeoJSON')
        stage('Download railway and infrastructure OSM', [sys.executable, ROOT / 'overpassQuery.py', '--boundary', boundary, '--output', ROOT / 'osm_output'])
    if 'dem' in selected:
        if not args.dry_run: check_file(boundary, 'Boundary GeoJSON')
        stage('Download 3DEP DEM products concurrently', [sys.executable, ROOT / 'download_3dep_1m.py', '--geojson', boundary, '--download', '--extract', '--workers', args.dem_workers, '--output', dem_dir])
    if 'naip' in selected:
        if not args.dry_run: check_file(boundary, 'Boundary GeoJSON')
        bbox = polygon_bbox(boundary) if not args.dry_run else ('WEST', 'SOUTH', 'EAST', 'NORTH')
        stage('Download latest NAIP scenes', [sys.executable, ROOT / 'runM2M_NAIP.py', '--bbox', *bbox, '--start', args.start_date, '--end', args.end_date, '--workers', args.naip_workers, '--output', naip_dir])
    if 'terrain' in selected:
        if not args.dry_run:
            check_tifs(dem_input, 'DEM')
            check_tifs(naip_dir, 'NAIP')
            check_file(osm_json, 'Railway OSM JSON')
            if not args.no_bridges: check_file(infrastructure, 'Infrastructure OSM JSON')
            check_file(ROOT / 'rail_elevation_inference.py', 'Railway inference helper')
        cmd = [sys.executable, ROOT / 'build_terrain.py', '--dem', dem_input,
               '--imagery', naip_dir, '--network', osm_json, '--corridor-only',
               '--corridor-width', args.corridor_width, '--near-spacing', args.near_spacing,
               '--transition-spacing', args.transition_spacing, '--background-spacing', args.background_spacing,
               '--fine-buffer', args.fine_buffer, '--transition-buffer', args.transition_buffer,
               '--max-cells', args.max_cells, '--max-elevation-error', args.max_elevation_error,
               '--flat-spacing', args.flat_spacing, '--refine-workers', args.refine_workers,
               '--workers', args.workers, '--texture-resolution', args.texture_resolution,
               '--texture-scale', args.texture_scale, '--texture-tile-size', args.texture_tile_size,
               '--format', 'glb', '--output', out]
        if not args.no_bridges:
            cmd += ['--bridges', '--infrastructure', infrastructure]
        if not args.no_inferred_crossings:
            cmd += ['--infer-rail-crossings', '--rail-sample-spacing', args.rail_sample_spacing,
                    '--rail-max-grade', args.rail_max_grade, '--rail-min-span', args.rail_min_span,
                    '--rail-max-span', args.rail_max_span, '--rail-min-dem-drop', args.rail_min_dem_drop]
            if not args.no_ground_mask:
                cmd += ['--mask-inferred-ground']
        stage('Build bridge-aware direct GLB terrain', cmd)
    if 'blender' in selected:
        # Legacy optional OBJ conversion, not part of --all; GLB is produced directly.
        if not args.dry_run:
            check_file(out / 'terrain.obj', 'OBJ for optional Blender stage')
            check_file(args.blender_exe, 'Blender executable')
        stage('Optional legacy Blender OBJ conversion', [args.blender_exe, '--background', '--factory-startup',
              '--python', ROOT / 'blender_terrain_to_glb.py', '--', '--input', out / 'terrain.obj',
              '--output', out / 'terrain_optimized.glb', '--ratio', args.ratio,
              '--texture-scale', args.texture_scale, '--merge-distance', '0.0001'])
    if 'anylogic' in selected:
        rail_script = ROOT / 'buildAnylogicRailNetwork.py'
        if not args.dry_run:
            check_file(rail_script, 'AnyLogic importer')
            check_file(osm_json, 'Railway OSM JSON')
            check_file(out / 'georeference.json', 'Terrain georeference')
            if not args.no_inferred_crossings:
                check_file(out / 'railway_elevation_profiles.json', 'Corrected rail elevation profiles')
            if not os.getenv('ANYLOGIC_DESIGN_TIME_TOKEN'):
                raise RuntimeError('Set ANYLOGIC_DESIGN_TIME_TOKEN before import')
            spec = importlib.util.spec_from_file_location('rail_pipeline_importer', rail_script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.DATA_DIR = out
            module.OSM_JSON = osm_json
            module.DEM_MOSAIC = (dem_input if dem_input.is_file() else out / '_raster_mosaics' / 'dem_mosaic.tif')
            module.DEM_FOLDER = dem_dir
            module.TERRAIN_GEOREFERENCE = out / 'georeference.json'
            module.RAIL_PROFILES = out / 'railway_elevation_profiles.json'
            module.MODEL_NAME = args.model_name
            module.AGENT_NAME = args.agent_name
            module.MODEL_UNITS_PER_METER = args.model_units_per_meter
            module.DRY_RUN = False
            print('\n=== Modifying open AnyLogic model ===', flush=True)
            module.main()
        else:
            print('[DRY RUN] Would import AnyLogic tracks from', osm_json, 'using', rail_script)
    print('\nPipeline finished.', flush=True)

if __name__ == '__main__':
    main()
