#!/usr/bin/env python3
"""Sample railway OSM nodes against DEM, writing railway_nodes_xyz.json.

Run this before buildAnylogicRailNetwork.py. Reuses an existing compatible cache
unless --force is specified. Elevations are raw DEM meters; the AnyLogic importer
adds RAIL_Z_OFFSET_M when constructing tracks.
"""
import argparse
import hashlib
import json
import math
import tempfile
from contextlib import ExitStack
from pathlib import Path

HERE = Path(__file__).resolve().parent

def sample_dem(projected, crs, dem_mosaic, dem_folder):
    """Sample original 3DEP TIFFs or cached mosaic, once per OSM node ID.

    No horizontal densification, snapping, or line interpolation is performed.
    Multiple source tiles are supported; missing/NoData values remain missing.
    """
    import rasterio
    from tqdm.auto import tqdm

    if dem_mosaic.is_file():
        paths = [dem_mosaic]
        print(f'Using cached DEM mosaic: {dem_mosaic}')
    else:
        paths = sorted(p for p in dem_folder.rglob('*') if p.suffix.lower() in ('.tif', '.tiff')) if dem_folder.is_dir() else []
        if not paths:
            raise FileNotFoundError(f'No DEM mosaic at {dem_mosaic} and no TIFFs under {dem_folder}')
        print(f'Using {len(paths)} source DEM tiles from {dem_folder}')
    missing = dict(projected)
    values = {}
    with ExitStack() as stack:
        datasets = [stack.enter_context(rasterio.open(path)) for path in paths]
        for ds in datasets:
            if ds.crs is None or rasterio.crs.CRS.from_user_input(crs) != ds.crs:
                raise ValueError(f'DEM tile CRS mismatch: {ds.name} ({ds.crs}) != {crs}. '
                                 'Build the cached mosaic in the terrain generator first.')
        for ds in tqdm(datasets, desc='Sampling DEM tiles', unit='tile'):
            b = ds.bounds
            subset = [(nid, xy) for nid, xy in missing.items()
                      if b.left <= xy[0] < b.right and b.bottom < xy[1] <= b.top]
            if not subset:
                continue
            for (nid, _), sample in zip(subset, ds.sample([xy for _, xy in subset], indexes=1, masked=True)):
                z = sample[0]
                if not math.isfinite(float(z)) or bool(getattr(z, 'mask', False)):
                    continue
                if ds.nodata is not None and float(z) == ds.nodata:
                    continue
                values[nid] = float(z)
                missing.pop(nid, None)
    print(f'DEM sampling: {len(values)} nodes valid; {len(missing)} outside coverage/NoData')
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--osm', type=Path, default=HERE / 'railway_raw_osm.json')
    parser.add_argument('--georeference', type=Path, default=HERE / 'georeference.json')
    parser.add_argument('--dem-mosaic', type=Path, default=HERE / '_raster_mosaics' / 'dem_mosaic.tif')
    parser.add_argument('--dem-folder', type=Path, default=HERE / '3dep_dem')
    parser.add_argument('--output', type=Path, default=HERE / 'railway_nodes_xyz.json')
    parser.add_argument('--force', action='store_true', help='Resample even when a valid XYZ cache exists')
    args = parser.parse_args(argv)
    from rasterio.warp import transform
    ref = json.loads(args.georeference.read_text(encoding='utf-8'))
    crs = str(ref['crs'])
    raw = args.osm.read_bytes()
    osm_hash = hashlib.sha256(raw).hexdigest()
    elements = json.loads(raw.decode('utf-8-sig')).get('elements')
    if not isinstance(elements, list):
        raise ValueError('Expected raw Overpass JSON with an elements array')
    nodes = {int(e['id']): (float(e['lon']), float(e['lat']))
             for e in elements if e.get('type') == 'node' and 'lon' in e and 'lat' in e}
    ways = [e for e in elements if e.get('type') == 'way'
            and e.get('tags', {}).get('railway') == 'rail']
    if not ways:
        raise ValueError('No railway=rail ways in OSM data')
    switch_ids = {int(e['id']) for e in elements if e.get('type') == 'node'
                  and e.get('tags', {}).get('railway') == 'switch'}
    used = {int(nid) for way in ways for nid in way.get('nodes', [])}
    used.update(switch_ids & nodes.keys())
    missing = used - nodes.keys()
    if missing:
        raise ValueError(f'Missing coordinates for {len(missing)} OSM nodes')
    ordered = sorted(used)
    if args.output.is_file() and not args.force:
        cached = json.loads(args.output.read_text(encoding='utf-8'))
        if (cached.get('version') == 1 and cached.get('crs') == crs
                and cached.get('osm_sha256') == osm_hash
                and set(cached.get('nodes', {})) == {str(n) for n in ordered}):
            print(f'Using existing railway XYZ cache: {args.output}')
            return
    east, north = transform('EPSG:4326', crs,
                            [nodes[n][0] for n in ordered],
                            [nodes[n][1] for n in ordered])
    projected = dict(zip(ordered, zip(east, north)))
    heights = sample_dem(projected, crs, args.dem_mosaic, args.dem_folder)
    payload = {'version': 1, 'crs': crs, 'osm_sha256': osm_hash,
               'elevation': 'raw DEM meters, before RAIL_Z_OFFSET_M',
               'nodes': {str(n): [projected[n][0], projected[n][1], heights.get(n)]
                         for n in ordered}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                     dir=args.output.parent, suffix='.tmp', delete=False) as tmp:
        json.dump(payload, tmp, separators=(',', ':'))
        temp_path = Path(tmp.name)
    temp_path.replace(args.output)
    print(f'Saved {len(heights)}/{len(ordered)} railway XYZ nodes: {args.output}')


if __name__ == '__main__':
    main()
