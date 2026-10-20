"""Headless Blender OBJ -> GLB conversion, preserving textured materials.
Run with: blender --background --factory-startup --python blender_terrain_to_glb.py -- --input terrain.obj --output terrain.glb
"""
import argparse
import sys
import time
from pathlib import Path

import bpy


def args_parse():
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True, help='Path to textured terrain OBJ')
    p.add_argument('--output', required=True, help='Destination GLB')
    p.add_argument('--merge-distance', type=float, default=0.0001)
    p.add_argument('--ratio', type=float, default=0.5, help='Collapse decimation ratio (1 = no decimation)')
    p.add_argument('--no-merge', action='store_true')
    p.add_argument('--no-join', action='store_true')
    return p.parse_args(argv)


def main():
    a = args_parse()
    source = Path(a.input).resolve()
    dest = Path(a.output).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not (0 < a.ratio <= 1):
        raise ValueError('--ratio must be > 0 and <= 1')
    if a.merge_distance < 0:
        raise ValueError('--merge-distance must be nonnegative')
    dest.parent.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    bpy.ops.wm.obj_import(filepath=str(source))
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if not meshes:
        raise RuntimeError('OBJ did not contain any mesh objects')
    print(f'Imported {len(meshes)} mesh objects', flush=True)
    if a.no_join:
        targets = meshes
    else:
        bpy.ops.object.select_all(action='DESELECT')
        for obj in meshes:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        bpy.ops.object.join()
        targets = [bpy.context.view_layer.objects.active]
    for obj in targets:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        if not a.no_merge and a.merge_distance > 0:
            # BMesh welds spatially coincident vertices, but UV seams can be
            # damaged when vertices with different UVs are welded.
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold=a.merge_distance)
            bpy.ops.object.mode_set(mode='OBJECT')
        if a.ratio < 1:
            modifier = obj.modifiers.new('TerrainDecimate', 'DECIMATE')
            modifier.decimate_type = 'COLLAPSE'
            modifier.ratio = a.ratio
            bpy.ops.object.modifier_apply(modifier=modifier.name)
        print(f'{obj.name}: {len(obj.data.vertices):,} vertices, {len(obj.data.polygons):,} faces', flush=True)
    bpy.ops.object.select_all(action='DESELECT')
    for obj in targets:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = targets[0]
    bpy.ops.export_scene.gltf(filepath=str(dest), export_format='GLB', use_selection=True, export_apply=False)
    print(f'Wrote {dest} ({dest.stat().st_size / 1024**2:.1f} MiB) in {time.perf_counter()-t:.1f}s', flush=True)


if __name__ == '__main__':
    main()
