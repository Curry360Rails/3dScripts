"""Adaptive, crack-free terrain mesh from a DEM and GeoPackage corridor lines."""
import argparse
import bisect
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from pathlib import Path

from tqdm.auto import tqdm

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform_geom

from shapely.geometry import LineString, MultiLineString, GeometryCollection, box, Polygon
from shapely.ops import unary_union
from shapely import constrained_delaunay_triangles
from shapely import from_wkb
from shapely.geometry import shape


def load_lines(path, layers, target_crs, preserve_original_crs=False):
    import pyogrio
    available = pyogrio.list_layers(path)[:, 0].tolist()
    selected = layers or available
    if set(selected) - set(available):
        raise ValueError(f"Unknown GPKG layers: {sorted(set(selected)-set(available))}")
    geometries = []
    for layer in tqdm(selected, desc="Reading GPKG layers", unit="layer"):
        info = pyogrio.read_info(path, layer=layer)
        source_crs = info.get('crs')

if not source_crs:
    if preserve_original_crs:
        print(
            f"WARNING: Layer {layer!r} has no stored CRS. "
            "Preserving original coordinates without reprojection."
        )
    else:
        raise ValueError(
            f"Layer {layer!r} has no stored CRS. "
            "Cannot safely reproject geometry."
        )
        from pyogrio.raw import read as read_raw
        _, _, wkbs, _ = read_raw(path, layer=layer, columns=[], read_geometry=True)
        for wkb in wkbs:
            if wkb is None:
                continue
            geom = from_wkb(wkb)
            if geom.is_empty:
                continue
            # Avoid unnecessary coordinate transformation for matching CRS.
            same_crs = rasterio.crs.CRS.from_user_input(source_crs) == rasterio.crs.CRS.from_user_input(target_crs)
            if preserve_original_crs and not same_crs:
                raise ValueError(f"Exact-vertex export requires matching CRS: {source_crs} != {target_crs}")
                if preserve_original_crs:
                    projected = geom
                else:
                    projected = geom if same_crs else shape(transform_geom(source_crs, target_crs, geom.__geo_interface__))
                    #projected = shape(
                    #    transform_geom(
                    #        source_crs,
                    #        target_crs,
                    #        geom.__geo_interface__
                    #    )
                    #)
            
            if projected.geom_type == 'LineString':
                geometries.append(projected)
            elif projected.geom_type == 'MultiLineString':
                geometries.extend(projected.geoms)
            elif projected.geom_type == 'GeometryCollection':
                geometries.extend(g for g in projected.geoms if g.geom_type == 'LineString')
    if not geometries:
        raise ValueError('No line geometries found in selected GPKG layers')
    return geometries, selected


def export_lines(geometries, dem, out, origin_x, origin_y, base, spacing, z_offset, preserve_z, exact_vertices=False):
    path = out / 'network_lines.obj'
    idx = 1
    count = 0
    with path.open('w', encoding='utf-8') as obj:
        obj.write('# Polyline edges only (not rendered surfaces)\n')
        obj.write('o Transport_Lines\n')
        for geom in tqdm(geometries, desc="Exporting rail/road lines", unit="line"):
            raw = list(geom.coords)
            if len(raw) < 2:
                continue
            if exact_vertices:
                if not geom.has_z:
                    raise ValueError("Exact-vertex network export requires 3D GPKG polylines")
                pts = raw
            else:
                pts = [raw[0]]
                for a, b in zip(raw, raw[1:]):
                    steps = max(1, math.ceil(math.hypot(b[0]-a[0], b[1]-a[1]) / spacing))
                    for j in range(1, steps+1):
                        t = j/steps
                        pts.append(tuple(a[k]+t*(b[k]-a[k]) for k in range(min(len(a),len(b)))))
            has_z = preserve_z and geom.has_z
            samples = None if has_z else dem.sample([(p[0],p[1]) for p in pts], indexes=1, masked=True)
            chain = []
            for j,p in enumerate(pts):
                z = p[2] if has_z else next(samples)[0]
                if np.ma.is_masked(z) or not np.isfinite(z) or (not has_z and dem.nodata is not None and z == dem.nodata):
                    if len(chain)>1: obj.write('l '+' '.join(map(str,chain))+'\n')
                    chain=[]
                    continue
                precision = 12 if exact_vertices else 5
                obj.write(f'v {p[0]-origin_x:.{precision}f} {p[1]-origin_y:.{precision}f} {float(z)-base+z_offset:.{precision}f}\n')
                chain.append(idx); idx+=1; count+=1
            if len(chain)>1:
                obj.write('l '+' '.join(map(str,chain))+'\n')
    print(f'Exported {count:,} line vertices to {path}', flush=True)



def read_corridors(path, layers, target_crs):
    from shapely.strtree import STRtree
    geometries, selected = load_lines(path, layers, target_crs)
    return STRtree(geometries), selected, geometries


def build_cells(bounds, tree, near, middle, far, buffer, transition, max_cells, corridor_only=False, corridor_radius=100):
    from shapely.geometry import box
    x0, y0, x1, y1 = bounds
    # Integer lattice permits exact shared vertices, eliminating T-junction cracks.
    levels = max(1, math.ceil(math.log2(far / near)))
    scale = 2 ** levels
    step = far / scale
    nx = math.ceil((x1-x0) / far)
    ny = math.ceil((y1-y0) / far)
    cells = []
    def subdivide(ix, iy, size):
        xa, ya = x0 + ix*step, y0 + iy*step
        xb, yb = min(x0+(ix+size)*step, x1), min(y0+(iy+size)*step, y1)
        footprint = box(xa, ya, xb, yb)
        if corridor_only:
            nearby = tree.query(footprint.buffer(corridor_radius))
            if not any(footprint.distance(tree.geometries[int(i)]) <= corridor_radius for i in nearby):
                return
        # Only query lines near the cell; never create a full-area high-resolution raster.
        candidates = tree.query(footprint.buffer(transition))
        distance = min((footprint.distance(tree.geometries[int(i)]) for i in candidates), default=float('inf'))
        target = near if distance <= buffer else middle if distance <= transition else far
        if max(xb-xa, yb-ya) > target and size > 1:
            half = size//2
            for dy in (0, half):
                for dx in (0, half):
                    subdivide(ix+dx, iy+dy, half)
        else:
            cells.append((ix, iy, size))
            if len(cells) > max_cells:
                raise ValueError(f'Exceeded --max-cells={max_cells:,}; increase spacing or reduce buffers')
    for j in tqdm(range(ny), desc="Refining terrain cells", unit="row"):
        for i in range(nx):
            subdivide(i*scale, j*scale, scale)
    return cells, step


def mesh_topology(cells):
    horizontal = defaultdict(set)
    vertical = defaultdict(set)
    for x,y,s in tqdm(cells, desc="Indexing cell edges", unit="cell"):
        for yy in (y,y+s):
            horizontal[yy].update((x,x+s))
        for xx in (x,x+s):
            vertical[xx].update((y,y+s))
    hs = {k:sorted(v) for k,v in horizontal.items()}
    vs = {k:sorted(v) for k,v in vertical.items()}
    vertices = {}
    faces = []
    def index(point):
        if point not in vertices:
            vertices[point] = len(vertices)+1
        return vertices[point]
    for x,y,s in tqdm(cells, desc="Stitching triangles", unit="cell"):
        # Perimeter CCW; intermediate points from finer neighboring cells stitch edges.
        low = hs[y]; high = hs[y+s]; left = vs[x]; right = vs[x+s]
        bottom = low[bisect.bisect_left(low,x):bisect.bisect_right(low,x+s)]
        east = right[bisect.bisect_right(right,y):bisect.bisect_right(right,y+s)]
        top = high[bisect.bisect_left(high,x):bisect.bisect_left(high,x+s)]
        west = left[bisect.bisect_right(left,y):bisect.bisect_left(left,y+s)]
        ring = ([(v,y) for v in bottom] + [(x+s,v) for v in east]
                + [(v,y+s) for v in reversed(top)] + [(x,v) for v in reversed(west)])
        # Remove duplicate corners and any repeated vertices.
        ring = list(dict.fromkeys(ring))
        center = (x+s/2,y+s/2)
        ci = index(center)
        ids = [index(pt) for pt in ring]
        for a,b in zip(ids, ids[1:]+ids[:1]):
            if a != b:
                faces.append((ci,a,b))
    return vertices, faces


def polygon_parts(geom):
    if geom.is_empty:
        return
    if geom.geom_type == 'Polygon':
        if geom.area > 1e-9:
            yield geom
    elif hasattr(geom, 'geoms'):
        for g in geom.geoms:
            yield from polygon_parts(g)


# Workers receive immutable mesh data once, not with every task.
_WORK = {}


def _init_clip_worker(xy, valid, corridor_wkb, x0, y0, tile_m, xmax, ymax):
    from shapely import from_wkb, prepare
    corridor = from_wkb(corridor_wkb)
    prepare(corridor)
    _WORK.clear()
    _WORK.update(xy=xy, valid=valid, corridor=corridor,
                 x0=x0, y0=y0, tile_m=tile_m, xmax=xmax, ymax=ymax)


def _clip_batch(batch):
    from shapely.geometry import Polygon, box
    w = _WORK
    xy, valid, corridor = w['xy'], w['valid'], w['corridor']
    x0, y0, tile_m = w['x0'], w['y0'], w['tile_m']
    xmax, ymax = w['xmax'], w['ymax']
    results = defaultdict(list)
    tile_boxes = {}
    for a, c, d in batch:
        if not (valid[a-1] and valid[c-1] and valid[d-1]):
            continue
        tri = Polygon((xy[a-1], xy[c-1], xy[d-1]))
        if tri.area < 1e-10 or not corridor.intersects(tri):
            continue
        # Most triangles inside the corridor need NO polygon intersection.
        parts = (tri,) if corridor.covers(tri) else tuple(polygon_parts(tri.intersection(corridor)))
        for poly in parts:
            minx, miny, maxx, maxy = poly.bounds
            ix0 = max(0, math.floor((minx-x0)/tile_m))
            iy0 = max(0, math.floor((miny-y0)/tile_m))
            ix1 = min(math.ceil((xmax-x0)/tile_m)-1, math.floor((maxx-x0-1e-9)/tile_m))
            iy1 = min(math.ceil((ymax-y0)/tile_m)-1, math.floor((maxy-y0-1e-9)/tile_m))
            if ix0 == ix1 and iy0 == iy1:
                results[(ix0,iy0)].append((poly,(a,c,d)))
                continue
            for iy in range(iy0,iy1+1):
                for ix in range(ix0,ix1+1):
                    key=(ix,iy)
                    if key not in tile_boxes:
                        tile_boxes[key]=box(x0+ix*tile_m,y0+iy*tile_m,
                                            min(x0+(ix+1)*tile_m,xmax),
                                            min(y0+(iy+1)*tile_m,ymax))
                    tile=tile_boxes[key]
                    piece=poly if tile.covers(poly) else poly.intersection(tile)
                    for part in polygon_parts(piece):
                        results[key].append((part,(a,c,d)))
    return results


def export_corridor_tiled(out, dem, imagery_path, bounds, keys, elevations, valid,
                          faces, step, base, corridor, resolution, tile_pixels,
                          workers=1, clip_batch_size=4000):
    """Clip faces in batches; avoid intersections for interior faces; parallel on request."""
    from shapely import to_wkb
    b=bounds
    tile_m=resolution*tile_pixels
    x0,y0=b.left,b.bottom
    xy=[(min(b.right,x0+ix*step),min(b.top,y0+iy*step)) for ix,iy in keys]
    tile_polygons=defaultdict(list)
    batches=(faces[i:i+clip_batch_size] for i in range(0,len(faces),clip_batch_size))
    total=math.ceil(len(faces)/clip_batch_size)
    start=time.perf_counter()
    init=(xy,valid,to_wkb(corridor),x0,y0,tile_m,b.right,b.top)
    if workers == 1:
        _init_clip_worker(*init)
        iterator=map(_clip_batch,batches)
        for result in tqdm(iterator,total=total,desc='Clipping terrain batches',unit='batch'):
            for key,items in result.items(): tile_polygons[key].extend(items)
    else:
        # Windows uses spawn: top-level functions and __main__ guard are required.
        with ProcessPoolExecutor(max_workers=workers,initializer=_init_clip_worker,initargs=init) as pool:
            iterator=pool.map(_clip_batch,batches,chunksize=1)
            for result in tqdm(iterator,total=total,desc='Clipping terrain batches',unit='batch'):
                for key,items in result.items(): tile_polygons[key].extend(items)
    print(f'Clipping completed in {time.perf_counter()-start:.1f}s using {workers} worker(s)',flush=True)
    if not tile_polygons:
        raise ValueError('No valid corridor terrain faces. Check DEM coverage and corridor location.')
    textures=out/'textures'; textures.mkdir(exist_ok=True)
    material_lines=[]
    vertex_index=0
    with rasterio.open(imagery_path) as imagery:
        if imagery.crs is None: raise ValueError('Imagery has no CRS')
        with (out/'terrain.obj').open('w',encoding='utf8',buffering=1024*1024) as obj:
            obj.write('mtllib terrain.mtl\n')
            for (ix,iy),pieces in tqdm(sorted(tile_polygons.items()),desc='Exporting texture tiles',unit='tile'):
                tx0,ty0=x0+ix*tile_m,y0+iy*tile_m
                tx1,ty1=min(tx0+tile_m,b.right),min(ty0+tile_m,b.top)
                w=max(2,math.ceil((tx1-tx0)/resolution))
                h=max(2,math.ceil((ty1-ty0)/resolution))
                rgb=np.zeros((h,w,3),dtype=np.uint8)
                dst_transform=from_bounds(tx0,ty0,tx1,ty1,w,h)
                for ch in range(3):
                    reproject(source=rasterio.band(imagery,min(ch+1,imagery.count)),
                              destination=rgb[:,:,ch],src_transform=imagery.transform,
                              src_crs=imagery.crs,src_nodata=imagery.nodata,
                              dst_transform=dst_transform,dst_crs=dem.crs,dst_nodata=0,
                              resampling=Resampling.bilinear)
                texname=f'corridor_{ix}_{iy}.png'
                Image.fromarray(rgb).save(textures/texname)
                mat=f'Corridor_{ix}_{iy}'
                material_lines.append(f'newmtl {mat}\nKd 1 1 1\nmap_Kd textures/{texname}\n')
                obj.write(f'o Tile_{ix}_{iy}\nusemtl {mat}\n')
                for part,ids in pieces:
                    a,c,d=ids
                    xA,yA=xy[a-1];xB,yB=xy[c-1];xC,yC=xy[d-1]
                    denom=(yB-yC)*(xA-xC)+(xC-xB)*(yA-yC)
                    if abs(denom)<1e-12:continue
                    # Already-triangular polygons can be exported directly.
                    if len(part.interiors)==0 and len(part.exterior.coords)==4:
                        triangles=(part,)
                    else:
                        triangles=constrained_delaunay_triangles(part).geoms
                    for triangle in triangles:
                        if triangle.area<1e-10:continue
                        coords=list(triangle.exterior.coords)[:3]
                        for x,y in coords:
                            wa=((yB-yC)*(x-xC)+(xC-xB)*(y-yC))/denom
                            wb=((yC-yA)*(x-xC)+(xA-xC)*(y-yC))/denom
                            wc=1-wa-wb
                            z=wa*elevations[a-1]+wb*elevations[c-1]+wc*elevations[d-1]
                            u=(x-tx0)/(tx1-tx0)
                            v=(y-ty0)/(ty1-ty0)
                            obj.write(f'v {x-x0:.5f} {y-y0:.5f} {z-base:.5f}\n')
                            obj.write(f'vt {u:.9f} {v:.9f}\n')
                        obj.write(f'f {vertex_index+1}/{vertex_index+1} {vertex_index+2}/{vertex_index+2} {vertex_index+3}/{vertex_index+3}\n')
                        vertex_index+=3
    (out/'terrain.mtl').write_text('\n'.join(material_lines),encoding='utf8')
    print(f'Corridor export: {len(tile_polygons):,} texture tiles, {vertex_index:,} OBJ vertices',flush=True)
    return {'tiles':len(tile_polygons),'vertices':vertex_index}


def main():
    p = argparse.ArgumentParser(description='Adaptive textured terrain OBJ refined around GeoPackage roads/railways')
    p.add_argument('--dem', required=True)
    p.add_argument('--imagery', help='Satellite GeoTIFF (not required with --network-only)')
    p.add_argument('--gpkg', required=True)
    p.add_argument('--network-only', action='store_true', help='Export only network_lines.obj from GPKG; skip terrain and imagery')
    p.add_argument('--layer', action='append', help='Repeat for multiple layers; default all')
    p.add_argument('--output', default='terrain_output')
    p.add_argument('--near-spacing', type=float, default=3)
    p.add_argument('--transition-spacing', type=float, default=10)
    p.add_argument('--background-spacing', type=float, default=70)
    p.add_argument('--fine-buffer', type=float, default=50)
    p.add_argument('--transition-buffer', type=float, default=200)
    p.add_argument('--line-spacing', type=float, default=3)
    p.add_argument('--line-z-offset', type=float, default=0.5)
    p.add_argument('--max-cells', type=int, default=2_000_000)
    p.add_argument('--max-texture', type=int, default=8192)
    p.add_argument('--base-elevation', type=float)
    p.add_argument('--workers', type=int, default=8, help='Clipping worker processes (1 disables multiprocessing)')
    p.add_argument('--clip-batch-size', type=int, default=4000)
    p.add_argument('--corridor-only', action='store_true', help='Export only terrain inside the rail corridor buffer')
    p.add_argument('--corridor-width', type=float, default=400, help='Total corridor width in meters')
    p.add_argument('--texture-resolution', type=float, default=0.5, help='Texture meters per pixel in corridor mode')
    p.add_argument('--texture-tile-size', type=int, default=2048, help='Maximum texture tile dimension in pixels')
    p.add_argument('--use-dem-line-z', action='store_true', help='Override any existing 3D GPKG Z values with DEM samples')
    args = p.parse_args()
    if not args.network_only and not args.imagery:
        p.error('--imagery is required unless --network-only is specified')
    if not (0 < args.near_spacing <= args.transition_spacing <= args.background_spacing):
        p.error('Require 0 < near-spacing <= transition-spacing <= background-spacing')
    if not (0 <= args.fine_buffer < args.transition_buffer):
        p.error('Require 0 <= fine-buffer < transition-buffer')
    if args.max_texture < 2 or args.line_spacing <= 0 or args.max_cells <= 0:
        p.error('Invalid max-texture, line-spacing or max-cells')
    if args.workers < 1 or args.clip_batch_size < 1:
        p.error('workers and clip-batch-size must be positive')
    if args.corridor_width <= 0 or args.texture_resolution <= 0 or args.texture_tile_size < 2:
        p.error('Corridor width, texture resolution and texture tile size must be positive')
    out = Path(args.output); out.mkdir(parents=True,exist_ok=True)
    with rasterio.open(args.dem) as dem:
        if dem.crs is None or not dem.crs.is_projected or 'met' not in (dem.crs.linear_units or '').lower():
            raise ValueError('DEM must use a projected CRS in meters')
        b = dem.bounds
        if args.network_only:
            lines, layers = load_lines(args.gpkg, args.layer, dem.crs, preserve_original_crs=True)
            # Match the normal terrain export origin and baseline without generating a mesh.
            # Rasterio's masked minimum excludes NoData pixels.
            if args.base_elevation is not None:
                base = args.base_elevation
            else:
                minimum = None
                for _, window in dem.block_windows(1):
                    block = dem.read(1, window=window, masked=True)
                    if block.count():
                        value = float(block.min())
                        minimum = value if minimum is None else min(minimum, value)
                if minimum is None:
                    raise ValueError('DEM contains no valid elevations')
                base = minimum
            if args.use_dem_line_z:
                raise ValueError("--use-dem-line-z is incompatible with --network-only exact vertices")
            export_lines(lines, dem, out, b.left, b.bottom, base,
                         args.line_spacing, 0.0, True, exact_vertices=True)
            (out / 'georeference.json').write_text(json.dumps({
                'crs': str(dem.crs), 'origin_x': b.left, 'origin_y': b.bottom,
                'base_elevation': base, 'layers': layers,
                'existing_line_z_preserved': not args.use_dem_line_z,
                'network_only': True, 'exact_gpkg_vertices': True,
                'vertex_count_modified': False, 'line_z_offset': 0.0
            }, indent=2), encoding='utf-8')
            print(f'Done: network-only export at {out.resolve()}', flush=True)
            return
        tree,layers,lines = read_corridors(args.gpkg,args.layer,dem.crs)
        corridor = None
        if args.corridor_only:
            print('Building corridor polygon...', flush=True)
            corridor = unary_union([g.buffer(args.corridor_width / 2, quad_segs=4) for g in tqdm(lines, desc='Buffering lines', unit='line')])
            corridor = corridor.intersection(box(*dem.bounds))
            if corridor.is_empty:
                raise ValueError('Corridor does not overlap DEM')
            if corridor.geom_type == 'MultiPolygon':
                # One continuous mesh region only, as requested. Do not create separate terrain islands.
                corridor = max(corridor.geoms, key=lambda g: g.area)
                print('Corridor has multiple components: using largest connected polygon only.', flush=True)
            elif corridor.geom_type == 'GeometryCollection':
                polys = [g for g in corridor.geoms if g.geom_type == 'Polygon']
                if not polys: raise ValueError('No polygonal corridor inside DEM')
                corridor = max(polys, key=lambda g: g.area)
            print(f'Corridor area: {corridor.area / 1e6:.3f} km2', flush=True)
        cells,step = build_cells((b.left,b.bottom,b.right,b.top),tree,args.near_spacing,
                                 args.transition_spacing,args.background_spacing,
                                 args.fine_buffer,args.transition_buffer,args.max_cells,
                                 args.corridor_only,args.corridor_width/2)
        print(f'Adaptive cells: {len(cells):,}; building stitched topology...',flush=True)
        vertices,faces = mesh_topology(cells)
        print(f'Vertices: {len(vertices):,}; faces: {len(faces):,}; sampling DEM...',flush=True)
        keys = list(vertices)
        # DEM sampling in bounded batches; sample at actual vertex locations.
        elevations = np.full(len(keys),np.nan,dtype=np.float32)
        for start in tqdm(range(0,len(keys),25000), desc="Sampling DEM", unit="batch"):
            chunk = keys[start:start+25000]
            coords = [(min(b.right-1e-7,b.left+k[0]*step),
                       min(b.top-1e-7,b.bottom+k[1]*step)) for k in chunk]
            samples = dem.sample(coords,indexes=1,masked=True)
            for j,sample in enumerate(samples):
                z = sample[0]
                if not np.ma.is_masked(z) and np.isfinite(z) and (dem.nodata is None or z != dem.nodata):
                    elevations[start+j] = z
        valid = np.isfinite(elevations)
        if not valid.any():
            raise ValueError('No valid DEM elevations at mesh vertices')
        base = float(np.min(elevations[valid])) if args.base_elevation is None else args.base_elevation
        if args.corridor_only:
            stats = export_corridor_tiled(out, dem, args.imagery, b, keys, elevations, valid, faces,
                                          step, base, corridor, args.texture_resolution,
                                          args.texture_tile_size, args.workers, args.clip_batch_size)
            tw = th = 0
        else:
            with rasterio.open(args.imagery) as imagery:
                if imagery.crs is None:
                    raise ValueError('Imagery has no CRS')
                ratio = min(1,args.max_texture/max(imagery.width,imagery.height))
                tw,th = max(2,round(imagery.width*ratio)),max(2,round(imagery.height*ratio))
                dst_transform = from_bounds(b.left,b.bottom,b.right,b.top,tw,th)
                rgb = np.zeros((th,tw,3),dtype=np.uint8)
                for channel in tqdm(range(3), desc="Reprojecting imagery", unit="band"):
                    reproject(source=rasterio.band(imagery,min(channel+1,imagery.count)),
                              destination=rgb[:,:,channel],src_transform=imagery.transform,
                              src_crs=imagery.crs,src_nodata=imagery.nodata,
                              dst_transform=dst_transform,dst_crs=dem.crs,dst_nodata=0,
                              resampling=Resampling.bilinear)
            Image.fromarray(rgb).save(out/'terrain_texture.png')
            with (out/'terrain.obj').open('w',encoding='utf8') as f:
                f.write('mtllib terrain.mtl\no Terrain\n')
                for (ix,iy),z in tqdm(zip(keys,elevations), total=len(keys), desc="Writing OBJ vertices", unit="vertex"):
                    x=min(b.right,b.left+ix*step)-b.left
                    y=min(b.top,b.bottom+iy*step)-b.bottom
                    f.write(f'v {x:.5f} {y:.5f} {float(z-base) if np.isfinite(z) else 0:.5f}\n')
                for ix,iy in tqdm(keys, desc="Writing texture UVs", unit="vertex"):
                    x=min(b.right,b.left+ix*step)
                    y=min(b.top,b.bottom+iy*step)
                    f.write(f'vt {(x-b.left)/(b.right-b.left):.9f} {(y-b.bottom)/(b.top-b.bottom):.9f}\n')
                f.write('usemtl TerrainMaterial\n')
                for a,c,d in tqdm(faces, desc="Writing OBJ faces", unit="face"):
                    if valid[a-1] and valid[c-1] and valid[d-1]:
                        f.write(f'f {a}/{a} {c}/{c} {d}/{d}\n')
            (out/'terrain.mtl').write_text('newmtl TerrainMaterial\nKd 1 1 1\nmap_Kd terrain_texture.png\n')
        export_lines(lines,dem,out,b.left,b.bottom,base,args.line_spacing,args.line_z_offset,not args.use_dem_line_z)
        (out/'georeference.json').write_text(json.dumps({
            'crs':str(dem.crs),'origin_x':b.left,'origin_y':b.bottom,
            'base_elevation':base,'layers':layers,'near_spacing':args.near_spacing,
            'transition_spacing':args.transition_spacing,'background_spacing':args.background_spacing,
            'fine_buffer':args.fine_buffer,'transition_buffer':args.transition_buffer,
            'vertices':len(vertices),'triangles':len(faces),
            'note':'Requested spacing is a maximum target; quadtree levels are powers of two.',
            'existing_line_z_preserved':not args.use_dem_line_z,
            'corridor_only':args.corridor_only, 'corridor_width_m':args.corridor_width,
            'texture_resolution_m_per_pixel':args.texture_resolution,
            'texture_tile_max_pixels':args.texture_tile_size,
            'clip_workers':args.workers
        },indent=2))
        print(f'Done: {out.resolve()} | source mesh {len(vertices):,} vertices, {len(faces):,} triangles' + (' | corridor tiles: '+str(stats['tiles']) if args.corridor_only else f' | texture {tw}x{th}'))

if __name__ == '__main__':
    main()
