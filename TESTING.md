# Rail pipeline regression tests

These tests target the scripts in this archive: `3dScripts.zip` plus the newer GLB-input Blender and runner replacements from `glb_blender_update.zip`. They are **offline** and do not contact USGS, Overpass, Blender, or AnyLogic. Production source files are not modified by the test suite.

## Run in Git Bash (Windows)

From the directory containing the Python scripts and `tests/`:

```bash
python -m pip install pytest numpy rasterio shapely pillow tqdm requests
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

Tests use temporary directories for all synthetic TIFFs, ZIPs, GLBs, and SQLite databases. No real project output is overwritten. They cover cache records, CRS and grid checks, VRT reading, GLB container structure, downloader safety and reuse, railway topology/geometry helpers, the 4x AnyLogic coordinate conversion, master-runner dry-run/authorization, and Blender CLI parsing.

## Known defect detected by the tests

`pipeline_cache.Cache.valid()` currently compares `json.loads(row[1])` (nested **lists**) against `[inventory(p) for p in outputs]` (nested **tuples**). Consequently, recorded cache entries are not recognized as valid. Two regression tests document this with `pytest.mark.xfail(strict=True)`. The tests will become **XPASS failures** once the defect is corrected; remove the xfail markers when fixing it. This test bundle intentionally does not change production code.

## Not covered by the offline suite

- Live USGS/Overpass requests, authentication, rate limits, and multi-GB downloads.
- Full Victorville/Denver terrain runs and performance benchmarks.
- Blender's actual import, decimation, texture resampling, and export (requires Blender executable and test fixture GLB).
- Live AnyLogic model mutation and railway topology validation.

For a manual Blender smoke test after running terrain generation:

```bash
"/c/Program Files/Blender Foundation/Blender 5.2/blender.exe" --background --factory-startup --python blender_terrain_to_glb.py -- --input terrain_output/victorville/terrain.glb --output terrain_output/victorville/terrain_test_optimized.glb --ratio 0.5
```

Inspect the optimized GLB before importing into AnyLogic. Adjust the Blender executable path to your installed version. This command is **not run by pytest**.
