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
import re
from datetime import datetime, timezone
import pipeline_cache
from pipeline_cache import get as cache_get, put as cache_put, file_signature, fingerprint

ROOT = Path(__file__).resolve().parent


def resolve_path(value):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def check_file(path, label):
    if not Path(path).is_file():
        raise FileNotFoundError(f'{label} missing: {path}')


def check_tifs(path, label):
    path = Path(path)
    if path.is_file() and path.suffix.lower() in ('.tif', '.tiff', '.vrt'):
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


def polygon_project_name(boundary):
    """Use the GeoJSON filename stem as a safe project subdirectory name."""
    stem = Path(boundary).stem.strip()
    slug = re.sub(r'[^A-Za-z0-9_.-]+', '_', stem).strip('._-')
    if not slug or slug.lower() in ('con', 'prn', 'aux', 'nul'):
        raise ValueError(f'Invalid boundary file stem for project folder: {stem!r}')
    return slug


def project_path(value, project):
    """Keep a user-specified parent directory, append project subfolder once."""
    parent = resolve_path(value)
    return parent if parent.name == project else parent / project


def select_dem_input(value, dem_dir, project):
    if not value:
        return dem_dir
    path = resolve_path(value)
    if path.is_dir():
        return path if path.name == project else path / project
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--all', action='store_true', help='Run OSM, DEM, NAIP and direct-GLB terrain stages')
    for name in ('osm', 'dem', 'naip', 'terrain', 'blender', 'anylogic'):
        p.add_argument('--' + name, action='store_true', help=f'Run {name} stage')
    p.add_argument('--import-anylogic', action='store_true', help='Confirm modifying open AnyLogic model')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--fresh', action='store_true', help='Rerun ALL acquisition and terrain from scratch in an isolated new directory; never reuse existing downloads, caches, or mosaics')
    p.add_argument('--refresh-searches', action='store_true', help='Requery OSM/3DEP/NAIP APIs; reuse valid downloaded products')
    p.add_argument('--force-rebuild', action='store_true', help='Regenerate terrain even when a matching valid GLB is cached')
    p.add_argument('--verify-cache', choices=['fast','normal','strict'], default='normal')
    p.add_argument('--osm-ttl-days', type=int, default=7)
    p.add_argument('--naip-ttl-days', type=int, default=7)
    p.add_argument('--dem-ttl-days', type=int, default=30)

    p.add_argument('--boundary', default='boundary.geojson')
    p.add_argument('--project-name', help='Optional safe project folder name; defaults to boundary GeoJSON filename stem')
    p.add_argument('--dem-folder', default='3dep_dem')
    p.add_argument('--dem-input', help='Optional DEM TIFF or VRT (bypasses automatic DEM VRT generation)')
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
    p.add_argument('--rail-boundary-spacing', type=float, default=0.5, help='Local horizontal DEM anomaly boundary sampling (m)')
    p.add_argument('--rail-boundary-search', type=float, default=30)
    p.add_argument('--deck-group-gap', type=float, default=40)
    p.add_argument('--deck-group-z-tolerance', type=float, default=40)
    p.add_argument('--rail-max-grade', type=float, default=0.03)
    p.add_argument('--rail-min-span', type=float, default=10)
    p.add_argument('--rail-max-span', type=float, default=500)
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
    if args.fresh and (not args.all or args.anylogic or args.import_anylogic or args.blender):
        p.error('--fresh requires --all and excludes AnyLogic/Blender; it creates isolated new downloads and outputs')
    if args.fresh and args.dem_input:
        p.error('--fresh cannot reuse --dem-input; a new DEM VRT will be generated')
    if min(args.osm_ttl_days, args.naip_ttl_days, args.dem_ttl_days) < 0:
        p.error('TTL days cannot be negative')
    if 'anylogic' in selected and not args.import_anylogic:
        p.error('AnyLogic changes the open model. Add --import-anylogic')
    if min(args.workers, args.refine_workers, args.naip_workers) < 1 or args.dem_workers < 0:
        p.error('Invalid worker count')
    if not 0 < args.texture_scale <= 1:
        p.error('Texture scale must be in (0, 1]')
    boundary = resolve_path(args.boundary)
    project = args.project_name or polygon_project_name(boundary)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project) or project in ('.','..'):
        p.error('Invalid --project-name; use letters, digits, underscore, dot or hyphen')
    if args.fresh:
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        workspace = ROOT / 'fresh_runs' / project / run_id
        if not args.dry_run:
            workspace.mkdir(parents=True, exist_ok=False)
        out = workspace / 'terrain_output' / project
        dem_dir = workspace / '3dep_dem' / project
        naip_dir = workspace / 'naip_downloads' / project
        osm_json = workspace / 'osm_output' / project / 'railway_raw_osm.json'
        infrastructure = workspace / 'osm_output' / project / 'infrastructure_raw_osm.json'
        db_path = workspace / 'pipeline_cache' / project / 'pipeline_cache.sqlite'
        print(f'FRESH RUN: isolated workspace {workspace}', flush=True)
    else:
        out = project_path(args.output, project)
        dem_dir = project_path(args.dem_folder, project)
        naip_dir = project_path(args.naip_folder, project)
        osm_json = project_path(Path(args.osm_json).parent, project) / Path(args.osm_json).name
        infrastructure = project_path(Path(args.infrastructure_json).parent, project) / Path(args.infrastructure_json).name
        db_path = ROOT / 'pipeline_cache' / project / 'pipeline_cache.sqlite'
    dem_input = select_dem_input(args.dem_input, dem_dir, project)
    print(f'PROJECT: {project} | boundary: {boundary}', flush=True)
    print(f'OUTPUT: {out}', flush=True)
    env = os.environ.copy()
    env.update(RAIL_CACHE_DB=str(db_path.resolve()),
               RAIL_CACHE_VERIFY=args.verify_cache,
               RAIL_CACHE_BYPASS='1' if args.fresh else '0',
               RAIL_CACHE_REFRESH_SEARCHES='1' if args.refresh_searches else '0',
               RAIL_OSM_TTL_DAYS=str(args.osm_ttl_days),
               RAIL_NAIP_SEARCH_TTL_DAYS=str(args.naip_ttl_days),
               RAIL_DEM_SEARCH_TTL_DAYS=str(args.dem_ttl_days))
    os.environ.update({k:v for k,v in env.items() if k.startswith('RAIL_CACHE_') or k.startswith('RAIL_')})
    # The cache module was imported before the project was resolved; update its
    # module globals as well as the subprocess environment.
    pipeline_cache.DB = db_path.resolve()
    pipeline_cache.MODE = args.verify_cache
    pipeline_cache.BYPASS = args.fresh

    def stage(label, cmd, kind=None, inputs=(), outputs=None, ttl=None, always=False):
        cmd = [str(x) for x in cmd]
        print(f'\n=== {label} ===\n' + ' '.join(cmd), flush=True)
        if args.dry_run:
            return
        script_paths = [Path(x) for x in cmd if str(x).endswith('.py') and Path(x).is_file()]
        params = {'cmd': cmd, 'scripts': [file_signature(x) for x in script_paths],
                  'inputs': [file_signature(x) for x in inputs]}
        if kind and not args.fresh and not always and not (args.refresh_searches and kind in ('osm','dem','naip')):
            hit = cache_get('stage-' + kind, params, ttl=ttl, verify=args.verify_cache)
            if hit:
                print(f'CACHE HIT: {label}; skipping stage', flush=True)
                return
        subprocess.run(cmd, cwd=str(ROOT), check=True, env=env)
        if kind and not args.fresh and outputs is not None:
            paths = outputs() if callable(outputs) else outputs
            paths = [Path(x) for x in paths]
            if not paths:
                raise RuntimeError(f'{label} produced no outputs to register')
            cache_put('stage-' + kind, params, paths)

    def tifs_in(folder):
        return sorted(x for x in Path(folder).rglob('*') if x.is_file() and x.suffix.lower() in ('.tif','.tiff'))

    if 'osm' in selected:
        if not args.dry_run: check_file(boundary, 'Boundary GeoJSON')
        stage('Download railway and infrastructure OSM', [sys.executable, ROOT / 'overpassQuery.py', '--boundary', boundary, '--output', osm_json.parent], kind='osm', inputs=[boundary], outputs=lambda: sorted(osm_json.parent.glob('*.json')) + sorted(osm_json.parent.glob('*.geojson')) + sorted(osm_json.parent.glob('*.kml')), ttl=args.osm_ttl_days*86400)
    if 'dem' in selected:
        if not args.dry_run: check_file(boundary, 'Boundary GeoJSON')
        stage('Download 3DEP DEM products concurrently', [sys.executable, ROOT / 'download_3dep_1m.py', '--geojson', boundary, '--download', '--extract', '--workers', args.dem_workers, '--output', dem_dir] + (['--overwrite'] if args.fresh else []), kind='dem', inputs=[boundary], outputs=lambda: [dem_dir / 'products.json'] + tifs_in(dem_dir), ttl=args.dem_ttl_days*86400)
    if 'naip' in selected:
        if not args.dry_run: check_file(boundary, 'Boundary GeoJSON')
        bbox = polygon_bbox(boundary) if not args.dry_run else ('WEST', 'SOUTH', 'EAST', 'NORTH')
        stage('Download latest NAIP scenes', [sys.executable, ROOT / 'runM2M_NAIP.py', '--bbox', *bbox, '--start', args.start_date, '--end', args.end_date, '--workers', args.naip_workers, '--output', naip_dir] + (['--overwrite'] if args.fresh else []), kind='naip', inputs=[boundary], outputs=lambda: [naip_dir / 'selected_scenes.csv'] + tifs_in(naip_dir), ttl=args.naip_ttl_days*86400)
    if 'terrain' in selected:
        if not args.dry_run:
            check_tifs(dem_input, 'DEM')
            check_tifs(naip_dir, 'NAIP')
            check_file(osm_json, 'Railway OSM JSON')
            if not args.no_bridges: check_file(infrastructure, 'Infrastructure OSM JSON')
            check_file(ROOT / 'rail_elevation_inference.py', 'Railway inference helper')
            check_file(ROOT / 'crossing_deck_groups.py', 'Crossing deck grouping helper')
        cmd = [sys.executable, ROOT / 'build_terrain.py', '--dem', dem_input, '--boundary', boundary,
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
                    '--rail-max-span', args.rail_max_span, '--rail-min-dem-drop', args.rail_min_dem_drop,
                    '--rail-boundary-spacing', args.rail_boundary_spacing,
                    '--rail-boundary-search', args.rail_boundary_search,
                    '--deck-group-gap', args.deck_group_gap,
                    '--deck-group-z-tolerance', args.deck_group_z_tolerance]
            if not args.no_ground_mask:
                cmd += ['--mask-inferred-ground']
        if args.fresh:
            cmd += ['--rebuild-mosaics']
        terrain_outputs = ([] if dem_input.is_file() else [out/'_raster_mosaics'/'dem_mosaic.vrt']) + [out/'terrain.glb', out/'georeference.json', out/'network_lines.obj']
        if not args.no_inferred_crossings:
            terrain_outputs += [out/'railway_elevation_profiles.json', out/'inferred_crossings.csv',
                                out/'crossing_groups.geojson', out/'crossing_groups.csv']
        if not args.no_bridges:
            terrain_outputs += [out/'bridge_profiles.json', out/'bridge_review.csv']
        stage('Build bridge-aware direct GLB terrain', cmd, kind='terrain',
              inputs=[dem_input, naip_dir, osm_json] + ([] if args.no_bridges else [infrastructure]),
              outputs=terrain_outputs, always=args.force_rebuild)
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
            module.DEM_MOSAIC = (dem_input if dem_input.is_file() else out / '_raster_mosaics' / 'dem_mosaic.vrt')
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
