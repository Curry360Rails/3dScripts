import argparse
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject
from PIL import Image


def main():
    p = argparse.ArgumentParser(description='Create georeferenced terrain OBJ, MTL, and texture from GeoTIFFs.')
    p.add_argument('--dem', required=True)
    p.add_argument('--imagery', required=True)
    p.add_argument('--output', default='terrain_output')
    p.add_argument('--spacing', type=float, default=10.0, help='Target spacing in DEM CRS units (meters for projected metric CRS).')
    p.add_argument('--max-texture', type=int, default=8192)
    p.add_argument('--base-elevation', type=float, default=None)
    args = p.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    with rasterio.open(args.dem) as dem:
        if dem.crs is None or not dem.crs.is_projected:
            raise ValueError('DEM must have a projected CRS. Reproject to a metric projected CRS first.')
        unit = dem.crs.linear_units.lower() if dem.crs.linear_units else ''
        if 'met' not in unit:
            raise ValueError(f'DEM CRS units are {dem.crs.linear_units!r}; use a meter-based projected CRS.')
        if dem.count < 1:
            raise ValueError('DEM has no bands.')
        # Use a north-up output grid at requested spacing; preserve full raster bounds.
        bounds = dem.bounds
        width = max(2, int(np.ceil((bounds.right - bounds.left) / args.spacing)))
        height = max(2, int(np.ceil((bounds.top - bounds.bottom) / args.spacing)))
        if width * height > 3_000_000:
            raise ValueError('Mesh exceeds 3 million vertices. Increase --spacing.')
        from rasterio.transform import from_bounds
        transform = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, width, height)
        elevation = np.full((height, width), np.nan, dtype='float32')
        reproject(source=rasterio.band(dem, 1), destination=elevation,
                  src_transform=dem.transform, src_crs=dem.crs,
                  src_nodata=dem.nodata, dst_transform=transform, dst_crs=dem.crs,
                  dst_nodata=np.nan, resampling=Resampling.bilinear)
        valid = np.isfinite(elevation)
        if not valid.any():
            raise ValueError('No valid elevations in DEM.')
        base = float(np.nanmin(elevation)) if args.base_elevation is None else args.base_elevation
        # Imagery is warped onto the same bounds with independent texture resolution.
        tex_w = min(args.max_texture, max(width, 2))
        tex_h = min(args.max_texture, max(height, 2))
        # Preserve imagery detail independently of terrain mesh density.
        with rasterio.open(args.imagery) as imagery:
            if imagery.crs is None:
                raise ValueError('Imagery has no CRS.')
            scale = min(1.0, args.max_texture / max(imagery.width, imagery.height))
            tex_w = max(2, int(round(imagery.width * scale)))
            tex_h = max(2, int(round(imagery.height * scale)))
            tex_transform = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, tex_w, tex_h)
            rgb = np.zeros((tex_h, tex_w, 3), dtype=np.uint8)
            for channel in range(3):
                band = min(channel + 1, imagery.count)
                reproject(source=rasterio.band(imagery, band), destination=rgb[:, :, channel],
                          src_transform=imagery.transform, src_crs=imagery.crs,
                          src_nodata=imagery.nodata, dst_transform=tex_transform, dst_crs=dem.crs,
                          dst_nodata=0, resampling=Resampling.bilinear)
        Image.fromarray(rgb).save(out / 'terrain_texture.png')
        # Vertex grid is pixel-centered; coordinates are relative to the SW bound.
        xs = transform.c + (np.arange(width) + 0.5) * transform.a
        ys = transform.f + (np.arange(height) + 0.5) * transform.e
        obj_path = out / 'terrain.obj'
        with obj_path.open('w', encoding='utf-8') as f:
            f.write('mtllib terrain.mtl\no Terrain\n')
            for r in range(height):
                for c in range(width):
                    z = float(elevation[r, c] - base) if valid[r, c] else 0.0
                    f.write(f'v {xs[c]-bounds.left:.4f} {ys[r]-bounds.bottom:.4f} {z:.4f}\n')
            for r in range(height):
                for c in range(width):
                    u = (c + 0.5) / width
                    v = 1.0 - (r + 0.5) / height
                    f.write(f'vt {u:.8f} {v:.8f}\n')
            f.write('usemtl TerrainMaterial\n')
            for r in range(height - 1):
                for c in range(width - 1):
                    if not valid[r:r+2, c:c+2].all():
                        continue
                    a = r * width + c + 1
                    b, d = a + 1, a + width
                    e = d + 1
                    f.write(f'f {a}/{a} {d}/{d} {b}/{b}\n')
                    f.write(f'f {b}/{b} {d}/{d} {e}/{e}\n')
        (out / 'terrain.mtl').write_text('newmtl TerrainMaterial\nKd 1 1 1\nmap_Kd terrain_texture.png\n', encoding='utf-8')
        (out / 'georeference.txt').write_text(
            f'CRS: {dem.crs}\nSouthwest bounds origin X: {bounds.left}\nSouthwest bounds origin Y: {bounds.bottom}\n'
            f'Base elevation: {base}\nMesh dimensions: {width} x {height}\n'
            f'Actual spacing X: {transform.a}\nActual spacing Y: {-transform.e}\n', encoding='utf-8')
        print(f'Created {obj_path} ({width*height:,} vertices); texture {tex_w} x {tex_h}')
        print('Import OBJ into Blender, verify UV/material, then export Collada DAE.')

if __name__ == '__main__':
    main()
