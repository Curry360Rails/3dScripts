# MAINTAINER NOTES
# Terrain generation stages: DEM source resolution, virtual imagery, railway/corridor vector
# loading, adaptive cell refinement, DEM elevation sampling, clipping to corridor, then
# textured OBJ/GLB export. Projected XY units are meters. The exported vertex Z is relative to
# base elevation; retain georeference metadata when importing into another engine. VRT inputs
# still require their source TIFFs.
# These comments describe the existing implementation; no executable statements were changed.

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
from rasterio.warp import reproject, transform_geom, calculate_default_transform
from rasterio.vrt import WarpedVRT
from rasterio.merge import merge
from glb_writer import StreamingGLB
from pipeline_cache import fingerprint, inventory
from contextlib import ExitStack

from shapely.geometry import LineString, MultiLineString, GeometryCollection, box, Polygon
from shapely.ops import unary_union
from shapely import constrained_delaunay_triangles
from shapely import from_wkb
from shapely.geometry import shape



# PURPOSE: Resolve a DEM file or directory into a single readable raster source; validate tile compatibility.
# INPUTS: value, kind, output, target_crs, refresh.
def resolve_raster_input(value, kind, output, target_crs=None, refresh=False):
    """Virtual DEM mosaic: original source TIFFs remain untouched."""
    from xml.etree import ElementTree as ET
    import hashlib
    path=Path(value).expanduser().resolve()
    if path.is_file():return str(path),[str(path)]
    if not path.is_dir():raise FileNotFoundError(path)
    paths=sorted(p for p in path.rglob('*') if p.suffix.lower() in ('.tif','.tiff'))
    if not paths:raise ValueError(f'No {kind} TIFFs in {path}')
    if len(paths)==1:return str(paths[0]),[str(paths[0])]
    if kind!='DEM':raise ValueError('Imagery uses resolve_imagery_virtual instead')
    from contextlib import ExitStack
    with ExitStack() as stack:
        ds=[stack.enter_context(rasterio.open(p)) for p in paths]
        # Report the exact source files and CRS values rather than a generic error.
        # Rasterio CRS objects compare semantically, so equivalent CRS encodings
        # need not be rejected merely because their WKT strings differ.
        missing_crs = [str(p) for p, x in zip(paths, ds) if x.crs is None]
        if missing_crs:
            details = '\n'.join(f'  - {p}: CRS missing' for p in missing_crs)
            raise ValueError(
                f'{kind} CRS missing in {len(missing_crs)} of {len(paths)} TIFF(s):\n'
                f'{details}\nAll selected DEM TIFFs must have a defined, matching CRS.'
            )
        crs_groups = []
        for src, x in zip(paths, ds):
            for group in crs_groups:
                if x.crs == group['crs']:
                    group['files'].append(str(src))
                    break
            else:
                crs_groups.append({'crs': x.crs, 'files': [str(src)]})
        if len(crs_groups) > 1:
            details = []
            for group in crs_groups:
                crs = group['crs']
                code = crs.to_epsg()
                label = f'EPSG:{code}' if code is not None else str(crs)
                details.append(f'  {label} ({len(group["files"])} file(s)):')
                details.extend(f'    - {p}' for p in group['files'])
            raise ValueError(
                f'Selected {kind} TIFFs have {len(crs_groups)} different CRSs '
                f'across {len(paths)} files under {path}:\n'
                + '\n'.join(details)
                + '\nOnly TIFFs with the same CRS can be combined in this VRT. '
                  'Move unrelated TIFFs out of this project folder or reproject them first.'
            )
        if any(x.count<1 for x in ds):raise ValueError('DEM has no elevation band')
        if any(abs(x.transform.b)>1e-9 or abs(x.transform.d)>1e-9 for x in ds):raise ValueError('Rotated DEM not supported')
        rx,ry=abs(ds[0].res[0]),abs(ds[0].res[1]);ref=ds[0].transform
        for x in ds[1:]:
            if abs(abs(x.res[0])-rx)>1e-5 or abs(abs(x.res[1])-ry)>1e-5:
                raise ValueError(f'DEM resolution mismatch: {x.name}')
            if abs(((x.transform.c-ref.c)/rx)-round((x.transform.c-ref.c)/rx))>1e-4 or abs(((ref.f-x.transform.f)/ry)-round((ref.f-x.transform.f)/ry))>1e-4:
                raise ValueError(f'DEM pixel grids not aligned: {x.name}')
        left=min(x.bounds.left for x in ds);top=max(x.bounds.top for x in ds)
        right=max(x.bounds.right for x in ds);bottom=min(x.bounds.bottom for x in ds)
        w=round((right-left)/rx);h=round((top-bottom)/ry)
        root=ET.Element('VRTDataset',rasterXSize=str(w),rasterYSize=str(h))
        ET.SubElement(root,'SRS').text=ds[0].crs.to_wkt()
        ET.SubElement(root,'GeoTransform').text=f'{left}, {rx}, 0, {top}, 0, {-ry}'
        band=ET.SubElement(root,'VRTRasterBand',dataType='Float32',band='1')
        nodata=ds[0].nodata if ds[0].nodata is not None else -9999
        ET.SubElement(band,'NoDataValue').text=str(nodata)
        for src,x in zip(paths,ds):
            e=ET.SubElement(band,'ComplexSource')
            ET.SubElement(e,'SourceFilename',relativeToVRT='0').text=str(src)
            ET.SubElement(e,'SourceBand').text='1'
            ET.SubElement(e,'SourceProperties',RasterXSize=str(x.width),RasterYSize=str(x.height),DataType='Float32',BlockXSize='256',BlockYSize='256')
            ET.SubElement(e,'SrcRect',xOff='0',yOff='0',xSize=str(x.width),ySize=str(x.height))
            ET.SubElement(e,'DstRect',xOff=str(round((x.bounds.left-left)/rx)),yOff=str(round((top-x.bounds.top)/ry)),xSize=str(x.width),ySize=str(x.height))
            if x.nodata is not None:ET.SubElement(e,'NODATA').text=str(x.nodata)
    cache=output/'_raster_mosaics';cache.mkdir(parents=True,exist_ok=True)
    dst=cache/'dem_mosaic.vrt';stamp=cache/'dem_mosaic.sources.json'
    signature=hashlib.sha256(json.dumps([(str(x),x.stat().st_size,x.stat().st_mtime_ns) for x in paths]).encode()).hexdigest()
    if not refresh and dst.exists() and stamp.exists():
        try:
            if json.loads(stamp.read_text())['signature']==signature:return str(dst),list(map(str,paths))
        except (OSError,ValueError,KeyError):pass
    temp=dst.with_suffix('.partial.vrt');ET.ElementTree(root).write(temp,encoding='utf-8',xml_declaration=True);temp.replace(dst)
    stamp.write_text(json.dumps({'signature':signature},indent=2))
    print(f'Virtual DEM mosaic: {len(paths)} TIFFs -> {dst}',flush=True)
    return str(dst),list(map(str,paths))


# PURPOSE: Read multiple orthophoto rasters as one virtual imagery source without a physical mosaic.
class VirtualImagery:
    """On-demand virtual mosaic: source TIFFs stay untouched on disk.

    Each source is opened as a GDAL-backed Rasterio WarpedVRT in the DEM CRS.
    Only source footprints intersecting a requested output window are read.
    First source with valid data wins, matching the old merge(method='first').
    """
    # PURPOSE: Open and prepare source rasters for on-demand texture rendering.
    # INPUTS: sources, target_crs.
    def __init__(self, sources, target_crs):
        from rasterio.warp import transform_bounds
        self.sources = [str(Path(p).resolve()) for p in sources]
        self.crs = target_crs
        self.stack = ExitStack()
        self.items = []
        try:
            for path in tqdm(self.sources, desc='Indexing virtual imagery', unit='file'):
                ds = self.stack.enter_context(rasterio.open(path))
                if ds.crs is None or ds.count < 3:
                    raise ValueError(f'Imagery needs a CRS and at least 3 bands: {path}')
                vrt = self.stack.enter_context(WarpedVRT(
                    ds, crs=target_crs, resampling=Resampling.bilinear,
                    src_nodata=ds.nodata))
                # Use the source footprint, not the large full virtual grid.
                footprint = transform_bounds(ds.crs, target_crs, *ds.bounds, densify_pts=21)
                self.items.append((vrt, footprint))
        except Exception:
            self.stack.close()
            raise

    # PURPOSE: Release opened raster datasets and associated resources.
    def close(self):
        self.stack.close()

    # PURPOSE: Enter the imagery context manager.
    def __enter__(self):
        return self

    # PURPOSE: Close imagery resources on context exit, including exceptions.
    # INPUTS: exc_type, exc, tb.
    def __exit__(self, exc_type, exc, tb):
        self.close()

    # PURPOSE: Render a requested projected bounding box into an RGB image of specified pixel dimensions.
    # INPUTS: bounds, width, height.
    def render(self, bounds, width, height):
        from rasterio.windows import from_bounds as window_from_bounds
        from rasterio.windows import Window
        from rasterio.enums import Resampling as R
        xmin, ymin, xmax, ymax = bounds
        result = np.zeros((height, width, 3), dtype=np.uint8)
        filled = np.zeros((height, width), dtype=bool)
        for vrt, (left, bottom, right, top) in self.items:
            if right <= xmin or left >= xmax or top <= ymin or bottom >= ymax:
                continue
            window = window_from_bounds(xmin, ymin, xmax, ymax, transform=vrt.transform)
            # Boundless reads are not supported by WarpedVRT in some Rasterio builds;
            # clip to the VRT extent and copy the relevant output pixel rectangle.
            x0 = max(0, int(math.floor((max(xmin, left) - xmin) / (xmax-xmin) * width)))
            x1 = min(width, int(math.ceil((min(xmax, right) - xmin) / (xmax-xmin) * width)))
            y0 = max(0, int(math.floor((ymax - min(ymax, top)) / (ymax-ymin) * height)))
            y1 = min(height, int(math.ceil((ymax - max(ymin, bottom)) / (ymax-ymin) * height)))
            if x1 <= x0 or y1 <= y0:
                continue
            sub_bounds = (
                xmin + x0*(xmax-xmin)/width,
                ymax - y1*(ymax-ymin)/height,
                xmin + x1*(xmax-xmin)/width,
                ymax - y0*(ymax-ymin)/height,
            )
            subwindow = window_from_bounds(*sub_bounds, transform=vrt.transform)
            data = vrt.read(indexes=[1,2,3], window=subwindow,
                            out_shape=(3,y1-y0,x1-x0),
                            resampling=R.bilinear, masked=True)
            mask = ~np.ma.getmaskarray(data).any(axis=0)
            # Explicit dataset mask guards against missing NoData metadata.
            source_mask = vrt.read_masks(1, window=subwindow,
                                         out_shape=(y1-y0,x1-x0),
                                         resampling=R.nearest) > 0
            mask &= source_mask
            free = mask & ~filled[y0:y1, x0:x1]
            if free.any():
                patch = result[y0:y1, x0:x1]
                patch[free] = np.moveaxis(np.asarray(data.filled(0)),0,-1)[free]
                filled[y0:y1, x0:x1][free] = True
            if filled.all():
                break
        return result


# PURPOSE: Collect imagery sources and prepare them for virtual per-tile reads.
# INPUTS: value, target_crs.
def resolve_imagery_virtual(value, target_crs):
    """Return TIFF paths without ever writing an imagery mosaic."""
    path = Path(value).expanduser().resolve()
    if path.is_file() and path.suffix.lower() in ('.tif', '.tiff'):
        sources = [path]
    elif path.is_dir():
        sources = sorted(p for p in path.rglob('*') if p.is_file() and p.suffix.lower() in ('.tif', '.tiff'))
    else:
        raise FileNotFoundError(f'Imagery TIFF or folder not found: {path}')
    if not sources:
        raise ValueError(f'No imagery TIFFs found under {path}')
    print(f'Using {len(sources)} source imagery TIFFs via on-demand WarpedVRT (no physical mosaic)', flush=True)
    return [str(p) for p in sources]

# PURPOSE: Read and reproject supported vector network linework from a GIS source.
# INPUTS: path, layers, target_crs, preserve_original_crs.
def load_lines(path, layers, target_crs, preserve_original_crs=False):
    import pyogrio
    from pyogrio.raw import read as read_raw

    available = pyogrio.list_layers(path)[:, 0].tolist()
    selected = layers or available
    missing = set(selected) - set(available)
    if missing:
        raise ValueError(f"Unknown GPKG layers: {sorted(missing)}")

    geometries = []
    for layer in tqdm(selected, desc="Reading GPKG layers", unit="layer"):
        info = pyogrio.read_info(path, layer=layer)
        source_crs = info.get('crs')

        if not source_crs:
            if preserve_original_crs:
                print(f"WARNING: Layer {layer!r} has no stored CRS; preserving original coordinates. "
                      "Confirm they use the DEM's CRS for correct alignment.")
            else:
                raise ValueError(f"Layer {layer!r} has no stored CRS; cannot safely reproject")
        elif preserve_original_crs:
            if rasterio.crs.CRS.from_user_input(source_crs) != rasterio.crs.CRS.from_user_input(target_crs):
                raise ValueError(f"Exact-vertex export requires matching CRS: {source_crs} != {target_crs}")

        _, _, wkbs, _ = read_raw(path, layer=layer, columns=[], read_geometry=True)
        for wkb in wkbs:
            if wkb is None:
                continue
            geom = from_wkb(wkb)
            if geom.is_empty:
                continue
            if preserve_original_crs:
                projected = geom
            else:
                same_crs = rasterio.crs.CRS.from_user_input(source_crs) == rasterio.crs.CRS.from_user_input(target_crs)
                projected = geom if same_crs else shape(transform_geom(source_crs, target_crs, geom.__geo_interface__))

            if projected.geom_type == 'LineString':
                geometries.append(projected)
            elif projected.geom_type == 'MultiLineString':
                geometries.extend(projected.geoms)
            elif projected.geom_type == 'GeometryCollection':
                geometries.extend(g for g in projected.geoms if g.geom_type == 'LineString')

    if not geometries:
        raise ValueError('No line geometries found in selected GPKG layers')
    return geometries, selected



# PURPOSE: Parse network lines from OSM JSON, GeoJSON or KML and transform them to the DEM CRS.
# INPUTS: path, layers, target_crs, preserve_original_crs.
def load_vector_lines(path, layers, target_crs, preserve_original_crs=False):
    """Read a single GPKG, Overpass JSON, GeoJSON, or KML vector source.

    GeoJSON, Overpass JSON, and KML coordinates are WGS84 lon/lat.
    Reproject X/Y into DEM CRS; preserve original vertex count and any Z.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == '.gpkg':
        return load_lines(str(path), layers, target_crs, preserve_original_crs)
    if layers:
        raise ValueError('--layer is supported only for GeoPackage inputs')
    geometries = []
    if suffix in ('.geojson', '.json'):
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(data, dict) and isinstance(data.get('elements'), list):
            # Overpass raw OSM JSON: ways reference shared OSM node IDs.
            nodes = {e['id']: (e['lon'], e['lat']) for e in data['elements']
                     if e.get('type') == 'node' and 'lon' in e and 'lat' in e}
            missing = 0
            for element in data['elements']:
                if element.get('type') != 'way':
                    continue
                tags = element.get('tags', {})
                if not (tags.get('railway') or tags.get('highway')):
                    continue
                coords = []
                for node_id in element.get('nodes', []):
                    if node_id not in nodes:
                        missing += 1
                        break
                    coords.append(nodes[node_id])
                else:
                    if len(coords) >= 2:
                        geometries.append(LineString(coords))
            if missing:
                print(f'WARNING: {missing} OSM ways have missing referenced nodes; skipped.', flush=True)
        else:
            features = data.get('features', []) if data.get('type') == 'FeatureCollection' else [data]
            crs = data.get('crs') if isinstance(data, dict) else None
            if crs:
                name = crs.get('properties', {}).get('name', '')
                if name and rasterio.crs.CRS.from_user_input(name) != rasterio.crs.CRS.from_epsg(4326):
                    raise ValueError('GeoJSON source must be WGS84 (EPSG:4326)')
            for feature in features:
                geometry = feature.get('geometry') if feature.get('type') == 'Feature' else feature
                if geometry:
                    geometries.extend(_line_parts(shape(geometry)))
    elif suffix == '.kml':
        import xml.etree.ElementTree as ET
        root = ET.parse(path).getroot()
        for element in root.iter():
            if element.tag.rsplit('}', 1)[-1] != 'LineString':
                continue
            coord_el = next((child for child in element if child.tag.rsplit('}', 1)[-1] == 'coordinates'), None)
            if coord_el is None or not coord_el.text:
                continue
            coords = []
            for token in coord_el.text.split():
                values = token.split(',')
                if len(values) >= 2:
                    coords.append(tuple(float(v) for v in values[:3]))
            if len(coords) >= 2:
                geometries.append(LineString(coords))
    else:
        raise ValueError('Vector input must be .gpkg, .json (Overpass), .geojson, or .kml')
    if not geometries:
        raise ValueError(f'No railway/roadway polylines found in {path}')
    # Source coordinates are WGS84; DEM coordinates are projected meters.
    # Transform without densification, retaining original vertex order and Z.
    transformed = []
    for geom in tqdm(geometries, desc='Projecting network lines', unit='line'):
        projected = shape(transform_geom('EPSG:4326', target_crs, geom.__geo_interface__))
        transformed.extend(_line_parts(projected))
    print(f'Loaded {len(transformed):,} polylines from {path.name}', flush=True)
    return transformed, [path.name]


# PURPOSE: Yield linear pieces from simple or multipart geometries.
# INPUTS: geom.
def _line_parts(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == 'LineString':
        return [geom]
    if geom.geom_type in ('MultiLineString', 'GeometryCollection'):
        return [line for part in geom.geoms for line in _line_parts(part)]
    return []

# PURPOSE: Export 3D line geometry, preserving XY topology and applying configured Z sampling/offset rules.
# INPUTS: geometries, dem, out, origin_x, origin_y, base, spacing, z_offset, preserve_z (and additional options).
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
                # 2D OSM lines keep their original vertex count; Z is sampled from DEM.
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



# PURPOSE: Load corridor line geometries for proximity-based terrain refinement.
# INPUTS: path, layers, target_crs.
def read_corridors(path, layers, target_crs):
    from shapely.strtree import STRtree
    geometries, selected = load_vector_lines(path, layers, target_crs)
    return STRtree(geometries), selected, geometries


# Top-level quadtree tiles are independent; workers share a fixed integer lattice.
_REFINE = {}

# PURPOSE: Initialize per-process spatial and elevation data for parallel adaptive cell refinement.
# INPUTS: lines_wkb, bounds, near, middle, far, buffer, transition, corridor_only, corridor_radius (and additional options).
def _init_refine(lines_wkb, bounds, near, middle, far, buffer, transition,
                 corridor_only, corridor_radius, dem_path, max_error, flat_spacing):
    from shapely import from_wkb
    from shapely.strtree import STRtree
    lines = [from_wkb(w) for w in lines_wkb]
    _REFINE.clear()
    _REFINE.update(tree=STRtree(lines), bounds=bounds, near=near, middle=middle,
                   far=far, buffer=buffer, transition=transition,
                   corridor_only=corridor_only, radius=corridor_radius,
                   dem_path=dem_path, max_error=max_error, flat_spacing=flat_spacing)


# PURPOSE: Recursively refine a spatial cell task according to corridor proximity and optional elevation error.
# INPUTS: task.
def _refine_task(task):
    from shapely.geometry import box
    ix_start, ix_end, j, scale, step = task
    w = _REFINE
    x0,y0,x1,y1 = w['bounds']
    tree = w['tree']
    cells = []
    dem = rasterio.open(w['dem_path']) if w['max_error'] is not None else None
    elevation_cache = {}

    def elevation(x,y):
        key=(round(x,5),round(y,5))
        if key not in elevation_cache:
            val = next(dem.sample([(min(x,dem.bounds.right-1e-7),
                                    min(y,dem.bounds.top-1e-7))], indexes=1, masked=True))[0]
            elevation_cache[key] = None if np.ma.is_masked(val) or not np.isfinite(val) or (dem.nodata is not None and val==dem.nodata) else float(val)
        return elevation_cache[key]

    def flat_enough(xa,ya,xb,yb):
        # Test five samples against the bilinear surface through the corners.
        # This is a sampled tolerance, NOT a rigorous bound on the entire DEM.
        z=[elevation(xa,ya), elevation(xb,ya), elevation(xa,yb), elevation(xb,yb)]
        if any(v is None for v in z): return False
        tests=[((xa+xb)/2,(ya+yb)/2,sum(z)/4),
               ((xa+xb)/2,ya,(z[0]+z[1])/2),
               ((xa+xb)/2,yb,(z[2]+z[3])/2),
               (xa,(ya+yb)/2,(z[0]+z[2])/2),
               (xb,(ya+yb)/2,(z[1]+z[3])/2)]
        return all((v:=elevation(x,y)) is not None and abs(v-est)<=w['max_error']
                   for x,y,est in tests)

    def subdivide(ix,iy,size):
        xa,ya=x0+ix*step,y0+iy*step
        xb,yb=min(x0+(ix+size)*step,x1),min(y0+(iy+size)*step,y1)
        if xa>=xb or ya>=yb:return
        footprint=box(xa,ya,xb,yb)
        if w['corridor_only']:
            nearby=tree.query(footprint.buffer(w['radius']))
            if not any(footprint.distance(tree.geometries[int(k)])<=w['radius'] for k in nearby):return
        candidates=tree.query(footprint.buffer(w['transition']))
        distance=min((footprint.distance(tree.geometries[int(k)]) for k in candidates),default=float('inf'))
        target=w['near'] if distance<=w['buffer'] else w['middle'] if distance<=w['transition'] else w['far']
        width=max(xb-xa,yb-ya)
        should_split=width>target and size>1
        if should_split and dem is not None and width<=w['flat_spacing'] and flat_enough(xa,ya,xb,yb):
            should_split=False
        if should_split:
            half=size//2
            for dy in (0,half):
                for dx in (0,half):subdivide(ix+dx,iy+dy,half)
        else:cells.append((ix,iy,size))
    try:
        for i in range(ix_start,ix_end):subdivide(i*scale,j*scale,scale)
    finally:
        if dem is not None:dem.close()
    return cells


# PURPOSE: Create adaptive terrain cells and enforce the maximum cell budget.
# INPUTS: bounds, tree, near, middle, far, buffer, transition, max_cells, corridor_only (and additional options).
def build_cells(bounds, tree, near, middle, far, buffer, transition, max_cells,
                corridor_only=False, corridor_radius=100, workers=1,
                dem_path=None, max_error=None, flat_spacing=20,
                corridor_bounds=None, task_columns=16):
    from shapely import to_wkb
    x0,y0,x1,y1=bounds
    levels=max(1,math.ceil(math.log2(far/near)))
    scale=2**levels
    step=far/scale
    nx=math.ceil((x1-x0)/far)
    ny=math.ceil((y1-y0)/far)
    if corridor_only and corridor_bounds is not None:
        cx0,cy0,cx1,cy1=corridor_bounds
        # Restrict top-level work to the corridor, keeping DEM origin unchanged.
        i0=max(0,math.floor((cx0-x0)/far)-1)
        i1=min(nx,math.ceil((cx1-x0)/far)+1)
        j0=max(0,math.floor((cy0-y0)/far)-1)
        j1=min(ny,math.ceil((cy1-y0)/far)+1)
    else:i0,i1,j0,j1=0,nx,0,ny
    tasks=[(i,min(i+task_columns,i1),j,scale,step)
           for j in range(j0,j1) for i in range(i0,i1,task_columns)]
    print(f'Refinement: {len(tasks):,} blocks, {workers} workers, lattice step {step:.4f} m',flush=True)
    init=([to_wkb(g) for g in tree.geometries],bounds,near,middle,far,
          buffer,transition,corridor_only,corridor_radius,dem_path,max_error,flat_spacing)
    cells=[]
    if workers==1:
        _init_refine(*init)
        iterator=map(_refine_task,tasks)
        for part in tqdm(iterator,total=len(tasks),desc='Refining terrain blocks',unit='block'):
            cells.extend(part)
            if len(cells)>max_cells:raise ValueError(f'Exceeded --max-cells={max_cells:,}')
    else:
        with ProcessPoolExecutor(max_workers=workers,initializer=_init_refine,initargs=init) as pool:
            for part in tqdm(pool.map(_refine_task,tasks,chunksize=1),total=len(tasks),desc='Refining terrain blocks',unit='block'):
                cells.extend(part)
                if len(cells)>max_cells:raise ValueError(f'Exceeded --max-cells={max_cells:,}')
    return cells,step


# PURPOSE: Sample terrain vertex elevations in batches to reduce raster I/O overhead.
# INPUTS: dem, keys, bounds, step, batch_size.
def sample_dem_blockwise(dem, keys, bounds, step, batch_size=250000):
    # Read each raster block once per batch rather than call dem.sample millions of times.
    from rasterio.windows import Window
    elevations=np.full(len(keys),np.nan,dtype=np.float32)
    block_h,block_w=dem.block_shapes[0]
    transform=dem.transform
    if not transform.is_rectilinear:
        raise ValueError('Vectorized DEM sampling requires a north-up raster')
    for start in tqdm(range(0,len(keys),batch_size),desc='Sampling DEM blocks',unit='batch'):
        end=min(start+batch_size,len(keys))
        part=keys[start:end]
        xs=np.array([min(bounds.right-1e-7,bounds.left+k[0]*step) for k in part])
        ys=np.array([min(bounds.top-1e-7,bounds.bottom+k[1]*step) for k in part])
        cols=np.floor((xs-transform.c)/transform.a).astype(np.int64)
        rows=np.floor((ys-transform.f)/transform.e).astype(np.int64)
        good=(rows>=0)&(rows<dem.height)&(cols>=0)&(cols<dem.width)
        block_ids=(rows//block_h)*((dem.width+block_w-1)//block_w)+(cols//block_w)
        for bid in np.unique(block_ids[good]):
            indices=np.flatnonzero(good & (block_ids==bid))
            r0=int(rows[indices[0]]//block_h*block_h)
            c0=int(cols[indices[0]]//block_w*block_w)
            window=Window(c0,r0,min(block_w,dem.width-c0),min(block_h,dem.height-r0))
            arr=dem.read(1,window=window,masked=True)
            values=np.ma.asarray(arr[rows[indices]-r0,cols[indices]-c0])
            data=np.asarray(values.astype(np.float32).filled(np.nan),dtype=np.float32)
            if dem.nodata is not None:data[data==dem.nodata]=np.nan
            data[~np.isfinite(data)]=np.nan
            elevations[start+indices]=data
    return elevations


# PURPOSE: Deduplicate terrain vertices and build indexed triangle connectivity from cells.
# INPUTS: cells.
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


# PURPOSE: Iterate polygonal components after clipping or intersection.
# INPUTS: geom.
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


# PURPOSE: Initialize per-worker clipping geometry and tile layout state.
# INPUTS: xy, valid, corridor_wkb, x0, y0, tile_m, xmax, ymax.
def _init_clip_worker(xy, valid, corridor_wkb, x0, y0, tile_m, xmax, ymax):
    from shapely import from_wkb, prepare
    corridor = from_wkb(corridor_wkb)
    prepare(corridor)
    _WORK.clear()
    _WORK.update(xy=xy, valid=valid, corridor=corridor,
                 x0=x0, y0=y0, tile_m=tile_m, xmax=xmax, ymax=ymax)


# PURPOSE: Clip a batch of terrain faces against the corridor and distribute pieces into texture tiles.
# INPUTS: batch.
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


# PURPOSE: Export clipped terrain as tiled textured OBJ or GLB, using on-demand imagery reads.
# INPUTS: out, dem, imagery_sources, bounds, keys, elevations, valid, faces, step (and additional options).
def export_corridor_tiled(out, dem, imagery_sources, bounds, keys, elevations, valid,
                          faces, step, base, corridor, resolution, tile_pixels,
                          workers=1, clip_batch_size=4000, texture_scale=1.0, output_format="glb", texture_cache=None):
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
    is_glb=output_format=='glb'
    glb=StreamingGLB(out/'terrain.glb') if is_glb else None
    texture_cache=Path(texture_cache or (out/'texture_cache'));texture_cache.mkdir(parents=True,exist_ok=True)
    source_signature=fingerprint(inventory(imagery_sources[0]) if len(imagery_sources)==1 else [inventory(p) for p in imagery_sources])
    hits=misses=0
    material_lines=[]
    vertex_index=0
    with VirtualImagery(imagery_sources, dem.crs) as imagery:
        with ((out/'terrain.obj').open('w',encoding='utf8',buffering=1024*1024) if not is_glb else __import__('contextlib').nullcontext(None)) as obj:
            if obj:obj.write('mtllib terrain.mtl\n')
            for (ix,iy),pieces in tqdm(sorted(tile_polygons.items()),desc='Exporting texture tiles',unit='tile'):
                tx0,ty0=x0+ix*tile_m,y0+iy*tile_m
                tx1,ty1=min(tx0+tile_m,b.right),min(ty0+tile_m,b.top)
                w=max(2,math.ceil((tx1-tx0)/resolution*texture_scale))
                h=max(2,math.ceil((ty1-ty0)/resolution*texture_scale))
                cache_key=fingerprint([source_signature,tx0,ty0,tx1,ty1,w,h,str(dem.crs)])
                cache_file=texture_cache/(cache_key+'.png')
                if cache_file.exists():
                    try:
                        with Image.open(cache_file) as cached:
                            if cached.size!=(w,h):raise ValueError('Texture dimensions changed')
                            rgb=np.array(cached.convert('RGB'))
                        hits+=1
                    except (OSError,ValueError):cache_file.unlink(missing_ok=True)
                if not cache_file.exists():
                    rgb=imagery.render((tx0,ty0,tx1,ty1),w,h)
                    temp=cache_file.with_suffix('.partial.png');Image.fromarray(rgb).save(temp);temp.replace(cache_file)
                    misses+=1
                texname=f'corridor_{ix}_{iy}.png'
                if not is_glb:Image.fromarray(rgb).save(textures/texname)
                mat=f'Corridor_{ix}_{iy}'
                if obj:
                    material_lines.append(f'newmtl {mat}\nKd 1 1 1\nmap_Kd textures/{texname}\n')
                    obj.write(f'o Tile_{ix}_{iy}\nusemtl {mat}\n')
                tile_vertices=[];tile_uv=[]
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
                            if is_glb:
                                tile_vertices.append((x-x0,y-y0,z-base));tile_uv.append((u,1-v))
                            else:
                                obj.write(f'v {x-x0:.5f} {y-y0:.5f} {z-base:.5f}\n')
                                obj.write(f'vt {u:.9f} {v:.9f}\n')
                        if obj:obj.write(f'f {vertex_index+1}/{vertex_index+1} {vertex_index+2}/{vertex_index+2} {vertex_index+3}/{vertex_index+3}\n')
                        vertex_index+=3
                if glb:glb.add_tile(mat,tile_vertices,tile_uv,Image.fromarray(rgb))
    if glb:glb.finish()
    else:(out/'terrain.mtl').write_text('\n'.join(material_lines),encoding='utf8')
    print(f'Texture cache: {hits} hits, {misses} misses',flush=True)
    print(f'Corridor export: {len(tile_polygons):,} texture tiles, {vertex_index:,} OBJ vertices',flush=True)
    return {'tiles':len(tile_polygons),'vertices':vertex_index}


# PURPOSE: Coordinate raster resolution, vector parsing, mesh generation, texture tiling and export.
def main():
    p = argparse.ArgumentParser(description='Adaptive textured terrain OBJ refined around GeoPackage roads/railways')
    p.add_argument('--format', choices=['glb','obj'],default='glb')
    p.add_argument('--texture-cache')
    p.add_argument('--mesh-mode',choices=['adaptive','full','flat'],default='adaptive')
    p.add_argument('--dem', required=True, help='DEM GeoTIFF or folder of DEM TIFFs')
    p.add_argument('--imagery', help='Imagery GeoTIFF or folder of imagery TIFFs (not required with --network-only)')
    p.add_argument('--rebuild-mosaics', action='store_true', help='Force recreation of cached DEM mosaic (imagery is virtual)')
    p.add_argument('--gpkg', help='GPKG, raw Overpass OSM JSON, GeoJSON, or KML input (legacy flag)')
    p.add_argument('--network', help='Network vector file: .gpkg, .json, .geojson, or .kml')
    p.add_argument('--network-only', action='store_true', help='Export only network_lines.obj from vector source; skip terrain and imagery')
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
    p.add_argument('--refine-workers', type=int, default=12, help='CPU processes for quadtree refinement')
    p.add_argument('--max-elevation-error', type=float, default=None, help='Optional sampled vertical error tolerance in meters; experimental')
    p.add_argument('--flat-spacing', type=float, default=20, help='Maximum cell width eligible for error-based early stop (m)')
    p.add_argument('--texture-scale', type=float, default=1.0, help='Texture linear resolution multiplier (0,1]')
    p.add_argument('--corridor-only', action='store_true', help='Export only terrain inside the rail corridor buffer')
    p.add_argument('--corridor-width', type=float, default=400, help='Total corridor width in meters')
    p.add_argument('--texture-resolution', type=float, default=0.5, help='Texture meters per pixel in corridor mode')
    p.add_argument('--texture-tile-size', type=int, default=2048, help='Maximum texture tile dimension in pixels')
    p.add_argument('--use-dem-line-z', action='store_true', help='Override any existing 3D GPKG Z values with DEM samples')
    args = p.parse_args()
    vector_path = args.network or args.gpkg
    if not vector_path:
        p.error('Provide --network or --gpkg')
    if not args.network_only and not args.imagery:
        p.error('--imagery is required unless --network-only is specified')
    if not (0 < args.near_spacing <= args.transition_spacing <= args.background_spacing):
        p.error('Require 0 < near-spacing <= transition-spacing <= background-spacing')
    if not (0 <= args.fine_buffer < args.transition_buffer):
        p.error('Require 0 <= fine-buffer < transition-buffer')
    if args.max_texture < 2 or args.line_spacing <= 0 or args.max_cells <= 0:
        p.error('Invalid max-texture, line-spacing or max-cells')
    if args.workers < 1 or args.refine_workers < 1 or args.clip_batch_size < 1:
        p.error('workers and clip-batch-size must be positive')
    if not (0 < args.texture_scale <= 1) or args.flat_spacing <= 0 or (args.max_elevation_error is not None and args.max_elevation_error < 0):
        p.error('Invalid texture-scale, flat-spacing or max-elevation-error')
    if args.corridor_width <= 0 or args.texture_resolution <= 0 or args.texture_tile_size < 2:
        p.error('Corridor width, texture resolution and texture tile size must be positive')
    out = Path(args.output); out.mkdir(parents=True,exist_ok=True)
    dem_path, dem_sources = resolve_raster_input(args.dem, 'DEM', out, refresh=args.rebuild_mosaics)
    with rasterio.open(dem_path) as dem:
        imagery_path = None
        imagery_sources = []
        if not args.network_only:
            imagery_sources = resolve_imagery_virtual(args.imagery, dem.crs)
        if dem.crs is None or not dem.crs.is_projected or 'met' not in (dem.crs.linear_units or '').lower():
            raise ValueError('DEM must use a projected CRS in meters')
        b = dem.bounds
        if args.network_only:
            lines, layers = load_vector_lines(vector_path, args.layer, dem.crs, preserve_original_crs=True)
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
                'network_only': True, 'original_network_vertices': True,
                'vertex_count_modified': False, 'line_z_offset': 0.0,
                'source_network': str(vector_path),
                'xy_reprojected_to_dem_crs': Path(vector_path).suffix.lower() != '.gpkg'
            }, indent=2), encoding='utf-8')
            print(f'Done: network-only export at {out.resolve()}', flush=True)
            return
        tree,layers,lines = read_corridors(vector_path,args.layer,dem.crs)
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
        if args.mesh_mode=='full':
            args.near_spacing=args.transition_spacing=args.background_spacing=min(abs(dem.res[0]),abs(dem.res[1]))
            args.max_elevation_error=None
            print('FULL mode: native DEM cell size; check --max-cells and available RAM',flush=True)
        cells,step = build_cells((b.left,b.bottom,b.right,b.top),tree,args.near_spacing,
                                 args.transition_spacing,args.background_spacing,
                                 args.fine_buffer,args.transition_buffer,args.max_cells,
                                 args.corridor_only,args.corridor_width/2,
                                 workers=args.refine_workers,dem_path=dem_path,
                                 max_error=args.max_elevation_error,flat_spacing=args.flat_spacing,
                                 corridor_bounds=corridor.bounds if corridor is not None else None)
        print(f'Adaptive cells: {len(cells):,}; building stitched topology...',flush=True)
        vertices,faces = mesh_topology(cells)
        print(f'Vertices: {len(vertices):,}; faces: {len(faces):,}; sampling DEM...',flush=True)
        keys = list(vertices)
        # DEM sampling in bounded batches; sample at actual vertex locations.
        elevations = sample_dem_blockwise(dem, keys, b, step)
        valid = np.isfinite(elevations)
        if not valid.any():
            raise ValueError('No valid DEM elevations at mesh vertices')
        base = float(np.min(elevations[valid])) if args.base_elevation is None else args.base_elevation
        if args.mesh_mode=='flat':elevations[valid]=base
        if args.corridor_only:
            stats = export_corridor_tiled(out, dem, imagery_sources, b, keys, elevations, valid, faces,
                                          step, base, corridor, args.texture_resolution,
                                          args.texture_tile_size, args.workers, args.clip_batch_size, args.texture_scale, args.format,args.texture_cache)
            tw = th = 0
        else:
            if args.format=='glb':raise ValueError('Direct GLB currently requires --corridor-only')
            with VirtualImagery(imagery_sources, dem.crs) as imagery:
                # Keep existing max-texture behavior without creating a source mosaic.
                ratio = min(1.0, args.max_texture / max(dem.width, dem.height))
                tw, th = max(2, round(dem.width*ratio)), max(2, round(dem.height*ratio))
                rgb = imagery.render((b.left,b.bottom,b.right,b.top),tw,th)
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
            'source_network': str(vector_path),
            'corridor_only':args.corridor_only, 'corridor_width_m':args.corridor_width,
            'texture_resolution_m_per_pixel':args.texture_resolution,
            'texture_tile_max_pixels':args.texture_tile_size,
            'clip_workers':args.workers, 'refine_workers':args.refine_workers,
            'max_elevation_error':args.max_elevation_error, 'flat_spacing':args.flat_spacing,
            'texture_scale':args.texture_scale,
            'dem_sources':dem_sources, 'imagery_sources':imagery_sources,
            'imagery_mode':'on_demand_warped_vrt'
        },indent=2))
        print(f'Done: {out.resolve()} | source mesh {len(vertices):,} vertices, {len(faces):,} triangles' + (' | corridor tiles: '+str(stats['tiles']) if args.corridor_only else f' | texture {tw}x{th}'))

if __name__ == '__main__':
    main()
