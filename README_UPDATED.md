# Rail terrain pipeline — reverted geometry baseline + infrastructure improvements

This package starts from `blender_terrain_to_glb.zip`. No topology-aware crossing inference or experimental bridge decks have been imported.

## Normal runs (Git Bash)

```bash
python run_rail_pipeline.py --all
python run_rail_pipeline.py --terrain --force-rebuild
python run_rail_pipeline.py --all --refresh-searches
python run_rail_pipeline.py --all --fresh
python run_rail_pipeline.py --all --dry-run
```

Direct GLB is the default for **corridor-only** terrain. OBJ and optional Blender are retained:

```bash
python run_rail_pipeline.py --terrain --format obj
python run_rail_pipeline.py --blender --format obj
```

The mesh mode can be `adaptive` (default), `flat` (ground flat, not rails), or `full` (native DEM spacing, potentially very large):

```bash
python run_rail_pipeline.py --terrain --mesh-mode full --force-rebuild
```

## Layout

`osm_output/<boundary-name>/`, `3dep_dem/<boundary-name>/`, `naip_downloads/<boundary-name>/`, `terrain_output/<boundary-name>/`, `pipeline_cache/<boundary-name>/`.

`--project-name NAME` overrides the boundary filename. `--fresh` creates an isolated timestamped project variant, retaining existing data.

## Caching

A SQLite stage registry records fingerprints and output inventories; reruns reuse verified stages. Source metadata changes invalidate terrain. Search stages expire after `--cache-ttl-days` (default 7); `--refresh-searches` forces a new search. `--force-rebuild` bypasses the terrain-stage registry but still reuses the per-tile PNG cache. Cached PNGs are keyed by source inventory, bounds, CRS and texture dimensions. Interrupted PNG writes use `.partial.png` files.

Downloaded files use the original downloader's reuse checks; the 3DEP downloader additionally recognizes previously extracted TIFF folders. This is **not** a content-addressed SHA-256 cache of each USGS download and is not a guarantee that remote products remain current during a cache TTL. `--fresh` bypasses prior downloads by using a new isolated output tree.

DEM virtual mosaic uses Rasterio and XML; no GDAL command-line tools required. It requires consistent CRS, resolution and pixel alignment and errors otherwise. It does not reproject DEM sources.

## Limitations

- Full-resolution mode can require huge memory; `--max-cells` in `build_terrain.py` remains a safeguard.
- GLB output is currently implemented for corridor-only terrain. Non-corridor full-area output still uses OBJ.
- A prior unscoped download folder is not migrated automatically.
- AnyLogic import still requires explicit `--import-anylogic`, credentials, and a compatible design-time API. No live model import has been tested.
- This is a targeted infrastructure update; the full Denver dataset has not been run end-to-end.
