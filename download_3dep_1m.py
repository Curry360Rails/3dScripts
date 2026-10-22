#!/usr/bin/env python3
"""Search and download original USGS 3DEP 1-meter DEM products using TNM Access API.

Coordinates are WGS84 longitude,latitude decimal degrees. Searches product footprints
intersecting the polygon; does not crop source DEMs to the polygon.
"""
import argparse
from datetime import date
import json
import re
import sys
import time
import zipfile
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from pipeline_cache import cached_json, get as cache_get, put as cache_put, is_valid_file
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
    if args.start_date or args.end_date:
        base['dateType'] = args.date_type
        if args.start_date:
            base['start'] = args.start_date
        if args.end_date:
            base['end'] = args.end_date
    found = []
    offset = 0
    while True:
        params = dict(base, offset=offset)
        def fetch_page():
            r = s.get(API, params=params, timeout=(20, 120))
            if not r.ok:
                raise RuntimeError(f'TNM API HTTP {r.status_code}: {r.text[:700]}\nURL: {r.url}')
            return r.json()
        payload = cached_json('tnm-products-page', {'url': API, 'params': params}, fetch_page,
                              ttl=int(__import__('os').environ.get('RAIL_DEM_SEARCH_TTL_DAYS','30')) * 86400)
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


def download_one(job, out, overwrite=False, extract=False):
    """Per-product cache and atomic download/extraction; no false cache hits on partial files."""
    index, item = job
    url = item['downloadURL']
    dest = out / safe_name(url, item, index)
    is_zip = dest.suffix.lower() == '.zip'
    extracted = out / dest.stem
    params = {'url': url, 'product_id': item.get('sourceId') or item.get('id'), 'extract': extract}
    if not overwrite:
        hit = cache_get('3dep-product', params)
        if hit:
            return ('cached', str(hit['outputs'][0]))
    part = dest.with_name(dest.name + '.part')
    try:
        if not overwrite and is_valid_file(dest):
            print(f'Existing validated archive: {dest.name}', flush=True)
        else:
            part.unlink(missing_ok=True)
            with session_with_retries() as session:
                with session.get(url, stream=True, timeout=(30, 180)) as response:
                    response.raise_for_status()
                    expected = response.headers.get('Content-Length')
                    with part.open('wb') as file:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                file.write(chunk)
                    if expected and part.stat().st_size != int(expected):
                        raise IOError(f'Incomplete download: {dest.name}')
            part.replace(dest)
        if extract and is_zip:
            temp_dir = out / (dest.stem + '.extracting')
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            temp_dir.mkdir(parents=True)
            try:
                with zipfile.ZipFile(dest) as archive:
                    if archive.testzip() is not None:
                        raise ValueError(f'Corrupt ZIP: {dest.name}')
                    base = temp_dir.resolve()
                    for member in archive.infolist():
                        target = (temp_dir / member.filename).resolve()
                        if not target.is_relative_to(base):
                            raise ValueError(f'Unsafe ZIP member: {member.filename}')
                        if ((member.external_attr >> 16) & 0o170000) == 0o120000:
                            raise ValueError(f'Symlink in ZIP: {member.filename}')
                    archive.extractall(temp_dir)
                if extracted.exists():
                    shutil.rmtree(extracted)
                temp_dir.rename(extracted)
            finally:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
            files = sorted(x for x in extracted.rglob('*') if x.is_file())
            if not files:
                raise ValueError(f'Empty extraction: {dest.name}')
            cache_put('3dep-product', params, files)
            return ('extracted', str(extracted))
        cache_put('3dep-product', params, [dest])
        return ('downloaded', str(dest))
    finally:
        part.unlink(missing_ok=True)


def download_all(products, out, workers=0, overwrite=False, extract=False):
    """workers=0 means one worker per matching product (all concurrently)."""
    jobs = list(enumerate(products, 1))
    if not jobs:
        return 0
    concurrency = len(jobs) if workers == 0 else min(workers, len(jobs))
    print(f'Downloading {len(jobs)} 3DEP products using {concurrency} concurrent workers', flush=True)
    failed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(download_one, job, out, overwrite, extract): job for job in jobs}
        for future in as_completed(futures):
            index, item = futures[future]
            try:
                status, path = future.result()
                print(f'[{index}/{len(jobs)}] {status.upper()}: {path}', flush=True)
            except Exception as exc:
                failed += 1
                print(f'[{index}/{len(jobs)}] FAILED: {item.get("downloadURL")}: {exc}', file=sys.stderr, flush=True)
    return failed


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--polygon', nargs='+', metavar='LON,LAT', help='WGS84 longitude,latitude pairs')
    source.add_argument('--geojson', help='WGS84 Polygon GeoJSON file')
    p.add_argument('--output', default='3dep_1m_downloads')
    p.add_argument('--bbox-only', action='store_true', help='Search polygon bounding box instead')
    p.add_argument('--geotiff-only', action='store_true', help='Filter product format to GeoTIFF (may exclude IMG or ZIP products)')
    p.add_argument('--start-date', help='Inclusive date filter start (YYYY-MM-DD)')
    p.add_argument('--end-date', help='Inclusive date filter end (YYYY-MM-DD)')
    p.add_argument('--date-type', choices=['Publication', 'dateCreated', 'lastUpdated'],
                   default='Publication', help='Product metadata date field (default: Publication)')
    p.add_argument('--page-size', type=int, default=100)
    p.add_argument('--max-results', type=int, default=10000)
    p.add_argument('--download', action='store_true', help='Download products (default: search and save manifest only)')
    p.add_argument('--extract', action='store_true', help='Extract downloaded ZIP archives')
    p.add_argument('--overwrite', action='store_true')
    p.add_argument('--workers', type=int, default=0, help='Concurrent downloads: 0 (default) downloads all matching products simultaneously; specify e.g. 8 to limit concurrency')
    args = p.parse_args()
    if args.workers < 0:
        p.error('--workers must be 0 (all) or a positive integer')
    if args.page_size < 1 or args.max_results < 1:
        p.error('page-size and max-results must be positive')
    if args.start_date and not args.end_date:
        p.error('--end-date is required when --start-date is supplied')
    for flag, value in (('--start-date', args.start_date), ('--end-date', args.end_date)):
        if value:
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError()
            except ValueError:
                p.error(f'{flag} must be a valid date in YYYY-MM-DD format')
    if args.start_date and args.end_date and args.start_date > args.end_date:
        p.error('--start-date must not be later than --end-date')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    try:
        pts = parse_polygon(args)
        session = session_with_retries()
        products = get_products(session, pts, args)
        manifest = out / 'products.json'
        manifest.write_text(json.dumps({'dataset': DATASET, 'polygon_lon_lat': pts, 'date_filter': {'type': args.date_type, 'start': args.start_date, 'end': args.end_date}, 'items': products}, indent=2), encoding='utf-8')
        print(f'Found {len(products)} downloadable products; manifest: {manifest}')
        for i, item in enumerate(products, 1):
            print(f'  {i:3}. {item.get("title", "Untitled")} | published: {item.get("publicationDate", "unknown")} | {item.get("downloadURL", "")}')
        if not args.download:
            print('Search-only mode. Add --download to fetch products.')
            return
        failures = download_all(products, out, args.workers, args.overwrite, args.extract)
        if failures:
            raise RuntimeError(f'{failures} of {len(products)} 3DEP downloads failed')
        print('Finished. Original products are not cropped or mosaicked.')
    except (ValueError, requests.RequestException, RuntimeError) as exc:
        p.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    main()
