import argparse
import math
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform_geom


def segments(geometry):
    if not geometry:
        return
    kind = geometry.get('type')
    coords = geometry.get('coordinates')
    if kind == 'LineString':
        yield coords
    elif kind == 'MultiLineString':
        yield from coords
    elif kind == 'GeometryCollection':
        for part in geometry.get('geometries', []):
            yield from segments(part)


def densify(line, spacing):
    if not line:
        return
    prev = line[0]
    yield prev[:2]
    for point in line[1:]:
        distance = math.hypot(point[0] - prev[0], point[1] - prev[1])
        steps = max(1, math.ceil(distance / spacing))
        for k in range(1, steps + 1):
            t = k / steps
            yield (prev[0] + t * (point[0] - prev[0]),
                   prev[1] + t * (point[1] - prev[1]))
        prev = point


def export_lines(gpkg_path, layers, dem, out, origin_x, origin_y, base, spacing, z_offset):
    try:
        import fiona
    except ImportError as exc:
        raise RuntimeError('GeoPackage support requires Fiona: pip install fiona') from exc
    if not Path(gpkg_path).is_file():
        raise FileNotFoundError(gpkg_path)
    available = fiona.listlayers(gpkg_path)
    selected = layers or available
    missing = sorted(set(selected) - set(available))
    if missing:
        raise ValueError(f'GPKG layers not found: {missing}. Available: {available}')
    obj_path = out / 'network_lines.obj'
    count = 0
    with obj_path.open('w', encoding='utf-8') as obj:
        obj.write('# Elevated polylines; edges only, no faces\n')
        index = 1
        for layer in selected:
            with fiona.open(gpkg_path, layer=layer) as src:
                if not src.crs_wkt and not src.crs:
                    raise ValueError(f'Layer {layer!r} has no CRS')
                source_crs = src.crs_wkt or src.crs
                obj.write(f'o {layer.replace(" ", "_")}\n')
                for feature in src:
                    geom = feature.get('geometry')
                    if geom is None:
                        continue
                    geom = transform_geom(source_crs, dem.crs, dict(geom))
                    for segment in segments(geom):
                        coords = list(densify(segment, spacing))
                        if len(coords) < 2:
                            continue
                        elevations = list(dem.sample(coords, indexes=1, masked=True))
                        chain = []
                        for (x, y), sample in zip(coords, elevations):
                            z = sample[0]
                            if np.ma.is_masked(z) or not np.isfinite(z) or (dem.nodata is not None and z == dem.nodata):
                                if len(chain) > 1:
                                    obj.write('l ' + ' '.join(map(str, chain)) + '\n')
                                chain = []
                                continue
                            obj.write(f'v {x-origin_x:.4f} {y-origin_y:.4f} {float(z)-base+z_offset:.4f}\n')
                            chain.append(index)
                            index += 1
                            count += 1
                        if len(chain) > 1:
                            obj.write('l ' + ' '.join(map(str, chain)) + '\n')
    print(f'Created {obj_path} ({count:,} elevated polyline vertices; layers: {selected})')
    return selected


def main():
    p = argparse.ArgumentParser(description='Build textured terrain OBJ and optionally elevated GPKG polylines.')
    p.add_argument('--dem', required=True, help='Elevation GeoTIFF')
    p.add_argument('--imagery', required=True, help='Satellite imagery GeoTIFF')
    p.add_argument('--output', default='terrain_output')
    p.add_argument('--spacing', type=float, default=10, help='Target terrain vertex spacing in meters; smaller = more detail')
    p.add_argument('--max-vertices', type=int, default=3_000_000)
    p.add_argument('--max-texture', type=int, default=8192)
    p.add_argument('--base-elevation', type=float, default=None)
    p.add_argument('--gpkg', help='Optional GeoPackage with roads/railways')
    p.add_argument('--layer', action='append', help='GPKG layer to include; repeat for multiple. Default: all layers')
    p.add_argument('--line-spacing', type=float, default=5, help='Maximum polyline segment length in meters')
    p.add_argument('--line-z-offset', type=float, default=0.5, help='Meters above DEM to prevent z-fighting')
    args = p.parse_args()
    if args.spacing <= 0 or args.line_spacing <= 0 or args.max_texture < 2:
        p.error('Spacing values must be positive and max-texture must be >= 2')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    with rasterio.open(args.dem) as dem:
        if dem.crs is None or not dem.crs.is_projected or 'met' not in (dem.crs.linear_units or '').lower():
            raise ValueError('DEM must use a projected CRS with meter units')
        bounds = dem.bounds
        width = max(2, math.ceil((bounds.right - bounds.left) / args.spacing))
        height = max(2, math.ceil((bounds.top - bounds.bottom) / args.spacing))
        if width * height > args.max_vertices:
            raise ValueError(f'Mesh would contain {width*height:,} vertices; increase --spacing or --max-vertices. '
                             'Warning: very large meshes can be slow in Blender and AnyLogic.')
        transform = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, width, height)
        elevation = np.full((height, width), np.nan, dtype='float32')
        reproject(source=rasterio.band(dem, 1), destination=elevation,
                  src_transform=dem.transform, src_crs=dem.crs, src_nodata=dem.nodata,
                  dst_transform=transform, dst_crs=dem.crs, dst_nodata=np.nan,
                  resampling=Resampling.bilinear)
        valid = np.isfinite(elevation)
        if not valid.any():
            raise ValueError('DEM has no valid elevation cells')
        base = float(np.nanmin(elevation)) if args.base_elevation is None else args.base_elevation
        with rasterio.open(args.imagery) as imagery:
            if imagery.crs is None:
                raise ValueError('Imagery must have a CRS')
            scale = min(1.0, args.max_texture / max(imagery.width, imagery.height))
            tex_w = max(2, round(imagery.width * scale))
            tex_h = max(2, round(imagery.height * scale))
            tex_transform = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, tex_w, tex_h)
            rgb = np.zeros((tex_h, tex_w, 3), dtype=np.uint8)
            for channel in range(3):
                reproject(source=rasterio.band(imagery, min(channel+1, imagery.count)),
                          destination=rgb[:, :, channel], src_transform=imagery.transform,
                          src_crs=imagery.crs, src_nodata=imagery.nodata,
                          dst_transform=tex_transform, dst_crs=dem.crs, dst_nodata=0,
                          resampling=Resampling.bilinear)
        Image.fromarray(rgb).save(out / 'terrain_texture.png')
        xs = transform.c + (np.arange(width) + 0.5) * transform.a
        ys = transform.f + (np.arange(height) + 0.5) * transform.e
        with (out / 'terrain.obj').open('w', encoding='utf-8') as obj:
            obj.write('mtllib terrain.mtl\no Terrain\n')
            for r in range(height):
                for c in range(width):
                    z = float(elevation[r, c] - base) if valid[r, c] else 0.0
                    obj.write(f'v {xs[c]-bounds.left:.4f} {ys[r]-bounds.bottom:.4f} {z:.4f}\n')
            for r in range(height):
                for c in range(width):
                    obj.write(f'vt {(c+.5)/width:.8f} {1-(r+.5)/height:.8f}\n')
            obj.write('usemtl TerrainMaterial\n')
            for r in range(height-1):
                for c in range(width-1):
                    if not valid[r:r+2, c:c+2].all():
                        continue
                    a = r*width+c+1
                    b, d = a+1, a+width
                    e = d+1
                    obj.write(f'f {a}/{a} {d}/{d} {b}/{b}\n')
                    obj.write(f'f {b}/{b} {d}/{d} {e}/{e}\n')
        (out / 'terrain.mtl').write_text('newmtl TerrainMaterial\nKd 1 1 1\nmap_Kd terrain_texture.png\n', encoding='utf-8')
        layers = []
        if args.gpkg:
            layers = export_lines(args.gpkg, args.layer, dem, out, bounds.left, bounds.bottom,
                                  base, args.line_spacing, args.line_z_offset)
        (out / 'georeference.txt').write_text(
            f'CRS: {dem.crs}\nSouthwest bounds origin X: {bounds.left}\n'
            f'Southwest bounds origin Y: {bounds.bottom}\nBase elevation: {base}\n'
            f'Mesh dimensions: {width} x {height}\nActual spacing X: {transform.a}\n'
            f'Actual spacing Y: {-transform.e}\nPolyline layers: {layers}\n'
            f'Polyline Z offset: {args.line_z_offset}\n', encoding='utf-8')
        print(f'Created {out / "terrain.obj"} ({width*height:,} vertices), texture {tex_w} x {tex_h}')
        print('Import terrain.obj and network_lines.obj (if generated) separately into Blender.')
        print('Note: network_lines.obj contains edges, not rendered road surfaces or AnyLogic network elements.')


if __name__ == '__main__':
    main()
