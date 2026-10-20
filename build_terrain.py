"""Adaptive, crack-free terrain mesh from a DEM and GeoPackage corridor lines."""
import argparse
import bisect
import json
import math
from collections import defaultdict
from pathlib import Path

from tqdm.auto import tqdm

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform_geom

from shapely.geometry import LineString, MultiLineString, GeometryCollection
from shapely import from_wkb
from shapely.geometry import shape


def load_lines(path, layers, target_crs):
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
            raise ValueError(f"Layer {layer!r} has no stored CRS; assign its verified CRS before proceeding")
        meta, table = pyogrio.read_arrow(path, layer=layer, columns=[], read_geometry=True)
        geometry_column = meta['geometry_name']
        for wkb in table[geometry_column].to_pylist():
            if wkb is None:
                continue
            geom = from_wkb(wkb)
            if geom.is_empty:
                continue
            projected = shape(transform_geom(source_crs, target_crs, geom.__geo_interface__))
            if projected.geom_type == 'LineString':
                geometries.append(projected)
            elif projected.geom_type == 'MultiLineString':
                geometries.extend(projected.geoms)
            elif projected.geom_type == 'GeometryCollection':
                geometries.extend(g for g in projected.geoms if g.geom_type == 'LineString')
    if not geometries:
        raise ValueError('No line geometries found in selected GPKG layers')
    return geometries, selected


def export_lines(geometries, dem, out, origin_x, origin_y, base, spacing, z_offset, preserve_z):
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
                obj.write(f'v {p[0]-origin_x:.5f} {p[1]-origin_y:.5f} {float(z)-base+z_offset:.5f}\n')
                chain.append(idx); idx+=1; count+=1
            if len(chain)>1:
                obj.write('l '+' '.join(map(str,chain))+'\n')
    print(f'Exported {count:,} line vertices to {path}', flush=True)



def read_corridors(path, layers, target_crs):
    from shapely.strtree import STRtree
    geometries, selected = load_lines(path, layers, target_crs)
    return STRtree(geometries), selected, geometries


def build_cells(bounds, tree, near, middle, far, buffer, transition, max_cells):
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


def main():
    p = argparse.ArgumentParser(description='Adaptive textured terrain OBJ refined around GeoPackage roads/railways')
    p.add_argument('--dem', required=True)
    p.add_argument('--imagery', required=True)
    p.add_argument('--gpkg', required=True)
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
    p.add_argument('--use-dem-line-z', action='store_true', help='Override any existing 3D GPKG Z values with DEM samples')
    args = p.parse_args()
    if not (0 < args.near_spacing <= args.transition_spacing <= args.background_spacing):
        p.error('Require 0 < near-spacing <= transition-spacing <= background-spacing')
    if not (0 <= args.fine_buffer < args.transition_buffer):
        p.error('Require 0 <= fine-buffer < transition-buffer')
    if args.max_texture < 2 or args.line_spacing <= 0 or args.max_cells <= 0:
        p.error('Invalid max-texture, line-spacing or max-cells')
    out = Path(args.output); out.mkdir(parents=True,exist_ok=True)
    with rasterio.open(args.dem) as dem:
        if dem.crs is None or not dem.crs.is_projected or 'met' not in (dem.crs.linear_units or '').lower():
            raise ValueError('DEM must use a projected CRS in meters')
        b = dem.bounds
        tree,layers,lines = read_corridors(args.gpkg,args.layer,dem.crs)
        cells,step = build_cells((b.left,b.bottom,b.right,b.top),tree,args.near_spacing,
                                 args.transition_spacing,args.background_spacing,
                                 args.fine_buffer,args.transition_buffer,args.max_cells)
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
            'existing_line_z_preserved':not args.use_dem_line_z
        },indent=2))
        print(f'Done: {out.resolve()} | {len(vertices):,} vertices, {len(faces):,} triangles, texture {tw}x{th}')

if __name__ == '__main__':
    main()
