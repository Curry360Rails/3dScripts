# Documentation-only edition of 3dScripts

This package is based on the uploaded **3dScripts.zip** (the version before the most recent adaptive-texture optimizations). Only Python comments were added. No executable statements, constants, function signatures, or existing docstrings were changed. Python abstract syntax trees were compared before and after for every `.py` file.

## Workflow map

1. `run_rail_pipeline.py` — command-line orchestration, stage selection, project paths, and cache lookups.
2. `overpassQuery.py` — OSM rail and node acquisition.
3. `download_3dep_1m.py` — elevation product discovery and downloads.
4. `runM2M_NAIP.py` — aerial imagery discovery, selection, and downloads.
5. `build_terrain.py` — DEM preparation, virtual imagery, adaptive mesh, texture export, and direct GLB.
6. `glb_writer.py` — incremental GLB encoding.
7. `blender_terrain_to_glb.py` — optional Blender optimization.
8. `buildAnylogicRailNetwork.py` — AnyLogic railway network creation.
9. `pipeline_cache.py` — project-local stage cache.

## Important operational details

- Project directories use the boundary GeoJSON stem; `--fresh` intentionally uses a new timestamp-suffixed project name.
- The DEM CRS and grid compatibility checks remain as implemented in the source.
- Texture caching is distinct from caching a completed terrain stage.
- `--import-anylogic` authorizes modifying the open AnyLogic model.
- For a dry run, use `python run_rail_pipeline.py --all --dry-run`.
- The existing code's logic has not been modified or validated against live services in this documentation pass.

## Documentation approach

Each module now has a maintainer overview and most top-level functions/classes have purpose and input comments. The existing inline comments and docstrings remain intact. This intentionally does not reorganize source or rewrite compact one-line implementations.
