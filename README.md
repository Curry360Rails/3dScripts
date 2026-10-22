# Group-level railway bridge elevation reconciliation

Replace `build_terrain.py`, `crossing_deck_groups.py`, `rail_topology_inference.py`, and `rail_elevation_inference.py` in the current pipeline; add `group_elevation_solution.py` alongside them.

The new solver runs **after** inferred spans are grouped and **before** the final railway profile JSON, OBJ rail lines, and GLB are written. It selects DEM-derived approach anchors only at the outside of each group, fits one longitudinal grade, preserves supported lateral offsets, and replaces the elevations of all group member profiles and original OSM nodes inside the footprint (including switches). The deck uses that same solution. Nested span endpoints cannot become independent vertical anchors.

For safety, a group is left unchanged and flagged in `group_elevation_diagnostics` if either outer approach lacks DEM coverage or if the group-level grade exceeds `--rail-max-grade`. This is an estimated visualization workflow, not an engineering survey.

Run `python run_rail_pipeline.py --terrain --force-rebuild --no-ground-mask`. Existing download/cache/project folder logic is unchanged. Use `--rail-inference-mode topology` if it is not already the default.

Known limitation: the solver uses the high-side of outer DEM endpoint samples to reduce roadbed contamination; a long bridge whose *outermost* detected points still lie over the lower roadbed may need manual anchors. Lateral differences are interpolated from outer anchors, not independently surveyed.
