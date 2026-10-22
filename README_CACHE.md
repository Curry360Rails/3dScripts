# Resumable rail terrain pipeline

Run from Git Bash with Python 3.14 and the existing GIS dependencies. The existing Overpass, 3DEP, NAIP, DEM mosaicking, direct GLB, inferred railway crossing, and optional AnyLogic workflows are retained.

## Commands

```bash
python run_rail_pipeline.py --all
python run_rail_pipeline.py --all --dry-run
python run_rail_pipeline.py --all --refresh-searches
python run_rail_pipeline.py --terrain --force-rebuild
python run_rail_pipeline.py --all --verify-cache strict
python run_rail_pipeline.py --fresh --all
```

`--fresh --all` is a **completely new run**: creates `fresh_runs/<UTC timestamp>/` containing new OSM, 3DEP, NAIP, and terrain outputs, ignores cache records, reissues API requests, and re-downloads source files. It does not delete or read previous download/output directories. `boundary.geojson` and Python source code are reused as configuration, not data products. It excludes AnyLogic changes.

`--refresh-searches` ignores cached API search responses but retains successfully downloaded products when they can be matched and verified. The Overpass retry schedule is 5, 9, 21, 33 seconds, with jitter. `--force-rebuild` regenerates the terrain stage but can reuse a cached DEM mosaic unless `--fresh` is used. `--verify-cache` supports `fast`, `normal` (default), `strict`. Strict rereads every registered file and verifies its SHA-256; this can take a while with many GB of imagery.

The default 3DEP `--workers 0` downloads all returned products concurrently; `--dem-workers 8` limits this to eight. NAIP uses `--naip-workers 6` by default. Beware that `--dem-workers 0` can overwhelm the connection for large product sets.

## Cache internals

`pipeline_cache.sqlite` records successful operations and output metadata. `_api_cache/*.json` holds cached search responses. Requests are keyed by canonical JSON of parameters, and stage keys also include input metadata and script timestamps. A completed file is recorded with path, byte size, modification time, and SHA-256. Routine cache hits validate format and metadata; strict hits also recompute SHA-256. Concurrent threads open separate SQLite connections with WAL and short transactions.

- Overpass: caches the polygon query result with a default seven-day TTL and regenerates railway/infrastructure exports.
- 3DEP: caches each search page for 30 days, and each product's downloaded file or successfully extracted files. ZIP extraction is validated and performed through a temporary directory.
- NAIP: caches each scene-search page and download-options response for seven days. Completed selected-scene sets are cached and can skip download-request calls; individual download URLs are also registered. If the newest scene selection changes, download requests may be repeated for some unchanged products because USGS download URLs are not stable product identifiers.
- Terrain: cached only when GLB, georeference, railway lines, and enabled bridge/crossing review outputs are all present and valid. Input file changes and changed arguments invalidate this stage.
- AnyLogic: intentionally not auto-cached; modifying the live model always requires `--import-anylogic`. Review the model for duplicate tracks before importing again.

The cache is populated for new successful operations; it does **not** blindly trust old pre-registry files. This may require a first run to revalidate or redownload previously extracted data. Avoid deleting the SQLite file unless you intend to lose the completion registry. No credentials are stored in the cache. Set `USGS_M2M_USERNAME`, `USGS_M2M_TOKEN` and (when importing) `ANYLOGIC_DESIGN_TIME_TOKEN` in your shell.

## Verification

`python test_pipeline_cache.py` runs offline tests for cache hits, invalidation, missing files, strict hashes, TTL, refresh and bypass. Python syntax and CLI dry-run tests were performed; live API calls, full terrain generation, and AnyLogic model writes have not been tested here.
