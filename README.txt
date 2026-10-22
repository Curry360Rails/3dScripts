Rail pipeline updated for inferred crossings + direct GLB + concurrent 3DEP.

Extract all files into your scripts directory, preserving any existing data directories.
Run: python run_rail_pipeline.py --all --dry-run
Then: python run_rail_pipeline.py --all
Resume: python run_rail_pipeline.py --terrain
Existing DEM mosaic: python run_rail_pipeline.py --terrain --dem-input terrain_output/_raster_mosaics/dem_mosaic.tif
AnyLogic import only after review: python run_rail_pipeline.py --anylogic --import-anylogic

3DEP --workers 0 (default) runs all matching product downloads concurrently; --dem-workers 8 caps it at 8. Beware of excessive connections on large result sets.
--all no longer invokes Blender; terrain.glb is exported directly.
No new bridge-grade/endpoint validation has been added.
