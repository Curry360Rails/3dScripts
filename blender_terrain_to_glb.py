"""Headless textured GLB -> optimized GLB with optional image resizing and single-atlas bake.
Blender 4.x/5.x. Run via blender --background --python SCRIPT -- --input ... --output ...
"""
import argparse
import sys
import time
from pathlib import Path
import bpy


def parse_args():
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--ratio', type=float, default=0.01)
    p.add_argument('--merge-distance', type=float, default=0.001)
    p.add_argument('--no-merge', action='store_true')
    p.add_argument('--no-join', action='store_true')
    p.add_argument('--texture-scale', type=float, default=1.0,
                   help='Linear dimension multiplier, e.g. 0.5 halves width and height')
    p.add_argument('--bake', action='store_true', help='Bake all tile materials into one atlas per mesh')
    p.add_argument('--atlas-size', type=int, default=8192, help='Square baked atlas pixels')
    p.add_argument('--bake-margin', type=int, default=4)
    p.add_argument('--bake-samples', type=int, default=1)
    p.add_argument('--texture-output', help='Directory for downsampled textures and baked atlas')
    return p.parse_args(argv)


def select_only(obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def downsample_images(objects, scale, folder):
    if scale == 1.0:
        return
    folder.mkdir(parents=True, exist_ok=True)
    cache = {}
    for obj in objects:
        for mat in obj.data.materials:
            if not mat or not mat.use_nodes:
                continue
            for node in mat.node_tree.nodes:
                if node.type != 'TEX_IMAGE' or not node.image:
                    continue
                original = node.image
                if original.source != 'FILE':
                    continue
                key = original.as_pointer()
                if key not in cache:
                    w, h = original.size
                    if w < 1 or h < 1:
                        raise RuntimeError(f'Cannot load image: {original.name}')
                    new = original.copy()
                    new.name = f'Downsampled_{len(cache):04d}_{Path(original.name).stem}'
                    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
                    new.scale(nw, nh)
                    target = folder / f'texture_{len(cache):04d}.png'
                    new.filepath_raw = str(target)
                    new.file_format = 'PNG'
                    new.save()
                    cache[key] = new
                    print(f'Texture: {original.name} {w}x{h} -> {nw}x{nh}', flush=True)
                node.image = cache[key]
    print(f'Downsampled {len(cache)} unique textures', flush=True)


def bake_atlas(obj, folder, size, margin, samples):
    """Bake existing diffuse colors to a fresh UV atlas on the already decimated mesh."""
    if not obj.data.uv_layers:
        raise RuntimeError('Cannot bake: source mesh has no UV map')
    folder.mkdir(parents=True, exist_ok=True)
    select_only(obj)
    source_uv = obj.data.uv_layers.active.name
    # Explicitly bind source texture sampling to original UV map before switching active UV.
    materials = [m for m in obj.data.materials if m]
    for mat in materials:
        if not mat.use_nodes:
            raise RuntimeError(f'Material {mat.name} has no shader nodes')
        nt = mat.node_tree
        for node in list(nt.nodes):
            if node.type == 'TEX_IMAGE' and node.image and not node.inputs['Vector'].is_linked:
                uv = nt.nodes.new('ShaderNodeUVMap')
                uv.uv_map = source_uv
                nt.links.new(uv.outputs['UV'], node.inputs['Vector'])
    # Create an independent UV layout for the atlas.
    atlas_uv = obj.data.uv_layers.new(name='BakedAtlasUV', do_init=True)
    obj.data.uv_layers.active = atlas_uv
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.smart_project(island_margin=margin / size)
    bpy.ops.object.mode_set(mode='OBJECT')
    atlas = bpy.data.images.new(f'BakedAtlas_{obj.name}', width=size, height=size, alpha=True)
    atlas.generated_color = (0, 0, 0, 0)
    for mat in materials:
        nt = mat.node_tree
        target = nt.nodes.new('ShaderNodeTexImage')
        target.name = 'BakeTargetImage'
        target.image = atlas
        for node in nt.nodes:
            node.select = False
        target.select = True
        nt.nodes.active = target
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = samples
    scene.render.bake.use_selected_to_active = False
    scene.render.bake.margin = margin
    scene.render.bake.use_pass_color = True
    scene.render.bake.use_pass_direct = False
    scene.render.bake.use_pass_indirect = False
    print(f'Baking {obj.name} to {size}x{size} atlas...', flush=True)
    bpy.ops.object.bake(type='DIFFUSE')
    atlas_path = folder / f'{obj.name}_baked.png'
    atlas.filepath_raw = str(atlas_path)
    atlas.file_format = 'PNG'
    atlas.save()
    # Replace many materials with one glTF-compatible material.
    baked_mat = bpy.data.materials.new(f'{obj.name}_BakedMaterial')
    baked_mat.use_nodes = True
    nodes = baked_mat.node_tree.nodes
    bsdf = next(n for n in nodes if n.type == 'BSDF_PRINCIPLED')
    image_node = nodes.new('ShaderNodeTexImage')
    image_node.image = atlas
    baked_mat.node_tree.links.new(image_node.outputs['Color'], bsdf.inputs['Base Color'])
    obj.data.materials.clear()
    obj.data.materials.append(baked_mat)
    obj.data.uv_layers.active = atlas_uv
    atlas_uv.active_render = True
    print(f'Baked atlas saved: {atlas_path}', flush=True)


def main():
    a = parse_args()
    if not 0 < a.ratio <= 1 or not 0 < a.texture_scale <= 1:
        raise ValueError('--ratio and --texture-scale must be in (0, 1]')
    if a.merge_distance < 0 or a.atlas_size < 64 or a.bake_margin < 0 or a.bake_samples < 1:
        raise ValueError('Invalid merge, atlas or bake setting')
    if a.bake and a.no_join:
        print('Note: --no-join produces one atlas per mesh object.', flush=True)
    source = Path(a.input).resolve()
    dest = Path(a.output).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    texture_dir = Path(a.texture_output).resolve() if a.texture_output else dest.parent / 'optimized_textures'
    started = time.perf_counter()
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    if source.suffix.lower() != '.glb':
        raise ValueError(f'Expected a .glb input, got: {source}')
    bpy.ops.import_scene.gltf(filepath=str(source))
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if not meshes:
        raise RuntimeError('GLB contains no mesh objects')
    print(f'Imported {len(meshes)} mesh objects', flush=True)
    if not a.no_join:
        bpy.ops.object.select_all(action='DESELECT')
        for obj in meshes:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        bpy.ops.object.join()
        meshes = [bpy.context.view_layer.objects.active]
    downsample_images(meshes, a.texture_scale, texture_dir)
    for obj in meshes:
        select_only(obj)
        if not a.no_merge and a.merge_distance:
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold=a.merge_distance)
            bpy.ops.object.mode_set(mode='OBJECT')
        if a.ratio < 1:
            mod = obj.modifiers.new('TerrainDecimate', 'DECIMATE')
            mod.decimate_type = 'COLLAPSE'
            mod.ratio = a.ratio
            bpy.ops.object.modifier_apply(modifier=mod.name)
        print(f'{obj.name}: {len(obj.data.vertices):,} vertices, {len(obj.data.polygons):,} faces', flush=True)
        if a.bake:
            bake_atlas(obj, texture_dir, a.atlas_size, a.bake_margin, a.bake_samples)
    bpy.ops.object.select_all(action='DESELECT')
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.export_scene.gltf(filepath=str(dest), export_format='GLB', use_selection=True, export_apply=False)
    print(f'Wrote {dest} ({dest.stat().st_size / 1024**2:.1f} MiB) in {time.perf_counter()-started:.1f}s', flush=True)


if __name__ == '__main__':
    main()
