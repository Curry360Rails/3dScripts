#!/usr/bin/env python3
"""Search and download original USGS 3DEP 1-meter DEM products using TNM Access API.

Coordinates are WGS84 longitude,latitude decimal degrees. Searches product footprints
intersecting the polygon; does not crop source DEMs to the polygon.
"""
import argparse
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = 'https://tnmaccess.nationalmap.gov/api/v1/products'
DATASET = 'Digital Elevation Model (DEM) 1 meter'


def parse_polygon(args):
    if args.geojson:
        data = json.loads(Path(args.geojson).read_text(encoding='utf-8'))
        if data.get('type') == 'FeatureCollection':
            features = data.get('features', [])
            if len(features) != 1:
                raise ValueError('GeoJSON FeatureCollection must contain exactly one polygon feature')
            data = features[0]['geometry']
        elif data.get('type') == 'Feature':
            data = data['geometry']
        if data.get('type') != 'Polygon':
            raise ValueError('Expected GeoJSON Polygon; MultiPolygon is not supported')
        pts = data['coordinates'][0]
    else:
        pts = []
        for token in args.polygon:
            parts = token.split(',')
            if len(parts) != 2:
                raise ValueError(f'Invalid coordinate {token!r}; expected longitude,latitude')
            pts.append([float(parts[0]), float(parts[1])])
    pts = [(float(p[0]), float(p[1])) for p in pts]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    if len(pts) < 3:
        raise ValueError('Polygon requires at least three distinct coordinate pairs')
    if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in pts):
        raise ValueError('Invalid WGS84 longitude/latitude range')
    return pts


def session_with_retries():
    s = requests.Session()
    s.headers['User-Agent'] = '3DEPPolygonDownloader/1.0 (Python requests; USGS TNM Access)'
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])
    s.mount('https://', HTTPAdapter(max_retries=retry))
    return s


def get_products(s, pts, args):
    # TNM API accepts a polygon with 3 to 23 lon/lat coordinate pairs.
    # Larger polygons are queried using their bbox, then optionally filtered
    # locally by product bounding boxes when the API returns spatial metadata.
    polygon_ok = len(pts) <= 23 and not args.bbox_only
    if not polygon_ok:
        print('Polygon has >23 vertices or --bbox-only requested; querying bounding rectangle.')
    west, east = min(p[0] for p in pts), max(p[0] for p in pts)
    south, north = min(p[1] for p in pts), max(p[1] for p in pts)
    base = {
        'datasets': DATASET,
        'max': min(args.page_size, 1000),
        'offset': 0,
        'outputFormat': 'JSON',
    }
    if polygon_ok:
        base['polygon'] = ','.join(f'{lon:.9f} {lat:.9f}' for lon, lat in pts)
    else:
        base['bbox'] = f'{west:.9f},{south:.9f},{east:.9f},{north:.9f}'
    if args.geotiff_only:
        base['prodFormats'] = 'GeoTIFF'
    found = []
    offset = 0
    while True:
        params = dict(base, offset=offset)
        r = s.get(API, params=params, timeout=(20, 120))
        if not r.ok:
            raise RuntimeError(f'TNM API HTTP {r.status_code}: {r.text[:700]}\nURL: {r.url}')
        payload = r.json()
        if payload.get('errors'):
            raise RuntimeError(f'TNM API errors: {payload["errors"]}')
        items = payload.get('items', [])
        total = payload.get('total', '?')
        print(f'Search: received {len(items)} products at offset {offset} (reported total {total})')
        found.extend(items)
        if len(items) < params['max'] or (isinstance(total, int) and offset + len(items) >= total):
            break
        offset += len(items)
        if offset >= args.max_results:
            print(f'Stopping search at --max-results {args.max_results}')
            break
    unique = {}
    for item in found:
        url = item.get('downloadURL')
        if url:
            unique[url] = item
    return list(unique.values())


def safe_name(url, item, index):
    name = Path(unquote(urlparse(url).path)).name
    if not name or '.' not in name:
        name = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(item.get('title') or f'product_{index}')) + '.bin'
    return name


def download(s, item, out, index, overwrite=False):
    url = item['downloadURL']
    dest = out / safe_name(url, item, index)
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        print(f'Skipping existing: {dest.name}')
        return dest
    part = dest.with_name(dest.name + '.part')
    print(f'Downloading {index}: {dest.name}')
    with s.get(url, stream=True, timeout=(30, 180)) as r:
        r.raise_for_status()
        with part.open('wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    part.replace(dest)
    return dest


def extract_zip(path, out):
    if not zipfile.is_zipfile(path):
        return
    dest = out / path.stem
    dest.mkdir(exist_ok=True)
    with zipfile.ZipFile(path) as z:
        # Avoid unsafe paths in archives.
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f'Unsafe ZIP path: {info.filename}')
        z.extractall(dest)
    tifs = list(dest.rglob('*.tif')) + list(dest.rglob('*.tiff'))
    print(f'Extracted {path.name}: {len(tifs)} TIFF files (other product files may also be present)')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--polygon', nargs='+', metavar='LON,LAT', help='WGS84 longitude,latitude pairs')
    source.add_argument('--geojson', help='WGS84 Polygon GeoJSON file')
    p.add_argument('--output', default='3dep_1m_downloads')
    p.add_argument('--bbox-only', action='store_true', help='Search polygon bounding box instead')
    p.add_argument('--geotiff-only', action='store_true', help='Filter product format to GeoTIFF (may exclude IMG or ZIP products)')
    p.add_argument('--page-size', type=int, default=100)
    p.add_argument('--max-results', type=int, default=10000)
    p.add_argument('--download', action='store_true', help='Download products (default: search and save manifest only)')
    p.add_argument('--extract', action='store_true', help='Extract downloaded ZIP archives')
    p.add_argument('--overwrite', action='store_true')
    args = p.parse_args()
    if args.page_size < 1 or args.max_results < 1:
        p.error('page-size and max-results must be positive')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    try:
        pts = parse_polygon(args)
        session = session_with_retries()
        products = get_products(session, pts, args)
        manifest = out / 'products.json'
        manifest.write_text(json.dumps({'dataset': DATASET, 'polygon_lon_lat': pts, 'items': products}, indent=2), encoding='utf-8')
        print(f'Found {len(products)} downloadable products; manifest: {manifest}')
        for i, item in enumerate(products, 1):
            print(f'  {i:3}. {item.get("title", "Untitled")} | {item.get("downloadURL", "")}')
        if not args.download:
            print('Search-only mode. Add --download to fetch products.')
            return
        for i, item in enumerate(products, 1):
            try:
                path = download(session, item, out, i, args.overwrite)
                if args.extract:
                    extract_zip(path, out)
            except (requests.RequestException, OSError, zipfile.BadZipFile) as exc:
                print(f'WARNING: failed product {i}: {exc}', file=sys.stderr)
        print('Finished. Original products are not cropped or mosaicked.')
    except (ValueError, requests.RequestException, RuntimeError) as exc:
        p.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    main()
