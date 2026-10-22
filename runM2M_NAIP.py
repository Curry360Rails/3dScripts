#!/usr/bin/env python3
"""USGS M2M NAIP: paginate scenes, select newest downloadable scene per tile, download concurrently.

Set USGS_M2M_USERNAME and USGS_M2M_TOKEN environment variables.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import time
import zipfile
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from pipeline_cache import cached_json, get as cache_get, put as cache_put, is_valid_file

API = 'https://m2m.cr.usgs.gov/api/api/json/stable/'
ID_PATTERN = re.compile(r'^(.*)_(\d{8})$')


def call(session, endpoint, payload, retries=3):
    for attempt in range(retries):
        try:
            r = session.post(API + endpoint, json=payload, timeout=(20, 120))
            if not r.ok:
                raise RuntimeError(f'{endpoint}: HTTP {r.status_code}: {r.text[:450]}')
            try:
                result = r.json()
            except ValueError as exc:
                raise RuntimeError(f'{endpoint}: non-JSON response: {r.text[:450]}') from exc
            if result.get('errorCode') is not None:
                raise RuntimeError(f"{endpoint}: {result.get('errorCode')}: {result.get('errorMessage')}")
            return result.get('data')
        except (requests.RequestException, RuntimeError) as exc:
            if attempt == retries - 1:
                raise
            print(f'  Retry {endpoint} after error: {exc}', flush=True)
            time.sleep(min(15, 2 ** attempt))


def scene_date(scene):
    name = str(scene.get('entityId', ''))
    match = ID_PATTERN.match(name)
    raw = scene.get('acquisitionDate') or scene.get('temporalCoverage', {}).get('startDate') if isinstance(scene.get('temporalCoverage'), dict) else scene.get('acquisitionDate')
    if raw:
        try:
            return dt.date.fromisoformat(str(raw)[:10])
        except ValueError:
            pass
    if match:
        try:
            return dt.datetime.strptime(match.group(2), '%Y%m%d').date()
        except ValueError:
            pass
    return dt.date.min


def tile_key(scene):
    name = str(scene.get('entityId', ''))
    match = ID_PATTERN.match(name)
    if match:
        return match.group(1)
    # Do not accidentally merge unrelated scenes with unrecognized IDs.
    return name


def search_all(session, dataset, spatial_filter, start, end, page_size):
    results = []
    offset = 1
    seen = set()
    while True:
        payload = {
            'datasetName': dataset,
            'maxResults': page_size,
            'startingNumber': offset,
            'sceneFilter': {
                'spatialFilter': spatial_filter,
                'acquisitionFilter': {'start': start, 'end': end},
            },
        }
        data = cached_json('naip-scene-page', {'endpoint':'scene-search','payload':payload}, lambda: call(session, 'scene-search', payload) or {}, ttl=int(os.environ.get('RAIL_NAIP_SEARCH_TTL_DAYS','7'))*86400)
        page = data.get('results') or []
        for scene in page:
            entity = scene.get('entityId')
            if entity and entity not in seen:
                results.append(scene)
                seen.add(entity)
        total = data.get('totalHits')
        print(f'Scene search: received {len(page)}; unique {len(results)}; totalHits={total}', flush=True)
        if not page:
            break
        offset += len(page)
        if total is not None and offset > int(total):
            break
        if len(page) < page_size:
            break
    return results


def download_options(session, dataset, ids, batch_size=500):
    result = {}
    for i in range(0, len(ids), batch_size):
        request = {'datasetName': dataset, 'entityIds': ids[i:i+batch_size]}
        options = cached_json('naip-download-options', request, lambda: call(session, 'download-options', request) or [], ttl=int(os.environ.get('RAIL_NAIP_SEARCH_TTL_DAYS','7'))*86400)
        for option in options:
            entity = option.get('entityId')
            if entity:
                result.setdefault(entity, []).append(option)
        print(f'Download options: checked {min(i+batch_size,len(ids))}/{len(ids)} scenes', flush=True)
    return result


def choose_scenes(scenes, options):
    groups = {}
    for scene in scenes:
        groups.setdefault(tile_key(scene), []).append(scene)
    selected = []
    unavailable = []
    for key, candidates in sorted(groups.items()):
        candidates.sort(key=lambda s: (scene_date(s), str(s.get('entityId'))), reverse=True)
        chosen = None
        for scene in candidates:
            entity = scene['entityId']
            available = [o for o in options.get(entity, []) if o.get('available') is True and o.get('id') is not None]
            if available:
                # Prefer a primary product where the API identifies one; otherwise first available.
                available.sort(key=lambda o: (not bool(o.get('isPrimary')), str(o.get('id'))))
                chosen = (scene, available[0])
                break
        if chosen:
            selected.append(chosen)
        else:
            unavailable.append(key)
    return selected, unavailable


def gather_urls(session, dataset, selected, poll_seconds, poll_attempts, request_batch):
    downloads = [{'entityId': scene['entityId'], 'productId': option['id']} for scene, option in selected]
    urls = {}
    for i in range(0, len(downloads), request_batch):
        batch = downloads[i:i+request_batch]
        label = f'naip_{dt.datetime.now().strftime("%Y%m%d_%H%M%S")}_{i}'
        response = call(session, 'download-request', {'downloads': batch, 'label': label}) or {}
        for item in response.get('availableDownloads') or []:
            if item.get('url'):
                urls[str(item.get('downloadId') or item.get('entityId') or item['url'])] = item['url']
        expected = len(batch) - len(response.get('failed') or [])
        # Poll to include products which USGS prepares asynchronously.
        for attempt in range(poll_attempts):
            if len(urls) >= i + expected:
                break
            retrieved = call(session, 'download-retrieve', {'label': label}) or {}
            for item in (retrieved.get('available') or []):
                if item.get('url'):
                    urls[str(item.get('downloadId') or item.get('entityId') or item['url'])] = item['url']
            # Some API responses also provide requested URLs that are already downloadable.
            if len(urls) >= i + expected:
                break
            if attempt < poll_attempts - 1:
                print(f'Waiting for prepared downloads: {len(urls)} URLs total...', flush=True)
                time.sleep(poll_seconds)
    return list(dict.fromkeys(urls.values()))


def filename_from_response(response, url, number):
    disposition = response.headers.get('Content-Disposition', '')
    match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', disposition, re.I)
    if match:
        name = unquote(match.group(1).strip())
    else:
        name = unquote(Path(urlparse(url).path).name)
    name = Path(name.replace('\\', '/')).name
    return name if name and name not in ('.', '/') else f'naip_download_{number}.zip'


def extract_and_remove_zip(archive):
    """Extract into a folder named after the archive; delete ZIP only on success."""
    archive = Path(archive)
    target = archive.parent / archive.stem
    temp_target = archive.parent / (archive.stem + '.extracting')
    if temp_target.exists():
        shutil.rmtree(temp_target)
    temp_target.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            if not members:
                raise ValueError(f'Empty ZIP archive: {archive.name}')
            root = temp_target.resolve()
            for member in members:
                # Reject absolute paths, traversal, and unsafe links.
                member_path = (temp_target / member.filename).resolve()
                if not member_path.is_relative_to(root):
                    raise ValueError(f'Unsafe ZIP member: {member.filename}')
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f'Symlink in ZIP: {member.filename}')
            bad = zf.testzip()
            if bad is not None:
                raise ValueError(f'Corrupt ZIP member: {bad}')
            zf.extractall(temp_target)
        if target.exists():
            # Avoid silently overwriting an existing extracted dataset.
            raise FileExistsError(f'Extraction destination already exists: {target}')
        temp_target.rename(target)
        archive.unlink()
        return target
    except Exception:
        if temp_target.exists():
            shutil.rmtree(temp_target)
        raise


def download_one(job, output, overwrite, extract_zips=True):
    number, url = job
    cache_params = {'url': url, 'extract_zips': extract_zips}
    if not overwrite:
        hit = cache_get('naip-product', cache_params)
        if hit:
            return ('skipped', str(hit['outputs'][0]))
    for attempt in range(3):
        try:
            with requests.get(url, stream=True, timeout=(25, 180)) as response:
                response.raise_for_status()
                name = filename_from_response(response, url, number)
                dest = output / name
                if dest.exists() and is_valid_file(dest) and not overwrite:
                    if extract_zips and dest.suffix.lower() == '.zip':
                        extracted = extract_and_remove_zip(dest)
                        cache_put('naip-product', cache_params, sorted(p for p in extracted.rglob('*') if p.is_file()))
                        return ('extracted', str(extracted))
                    cache_put('naip-product', cache_params, [dest])
                    return ('skipped', str(dest))
                temp = output / (name + '.part')
                with temp.open('wb') as file:
                    for chunk in response.iter_content(chunk_size=1024*1024):
                        if chunk:
                            file.write(chunk)
                expected = response.headers.get('Content-Length')
                if expected and temp.stat().st_size != int(expected):
                    raise IOError(f'Incomplete download: {name}')
                temp.replace(dest)
                if extract_zips and dest.suffix.lower() == '.zip':
                    extracted = extract_and_remove_zip(dest)
                    cache_put('naip-product', cache_params, sorted(p for p in extracted.rglob('*') if p.is_file()))
                    return ('extracted', str(extracted))
                cache_put('naip-product', cache_params, [dest])
                return ('downloaded', str(dest))
        except (requests.RequestException, OSError, ValueError, zipfile.BadZipFile) as exc:
            if attempt == 2:
                return ('failed', f'{url}: {exc}')
            time.sleep(2 ** attempt)


def main():
    p = argparse.ArgumentParser(description='Download latest available NAIP image for each geographic tile.')
    p.add_argument('--username', default=os.getenv('USGS_M2M_USERNAME'))
    p.add_argument('--token', default=os.getenv('USGS_M2M_TOKEN'))
    p.add_argument('--dataset', default='NAIP')
    p.add_argument('--bbox', nargs=4, type=float, metavar=('WEST','SOUTH','EAST','NORTH'),
                   default=[-117.350464,34.522964,-117.097778,34.626145])
    p.add_argument('--start', default='2023-01-01')
    p.add_argument('--end', default=dt.date.today().isoformat())
    p.add_argument('--page-size', type=int, default=1000)
    p.add_argument('--workers', type=int, default=5)
    p.add_argument('--output', type=Path, default=Path('naip_downloads'))
    p.add_argument('--poll-seconds', type=int, default=30)
    p.add_argument('--poll-attempts', type=int, default=12)
    p.add_argument('--request-batch', type=int, default=100)
    p.add_argument('--overwrite', action='store_true')
    p.add_argument('--keep-zips', action='store_true', help='Keep ZIP files without extracting (default: extract and delete after success)')
    p.add_argument('--list-only', action='store_true', help='List selected latest downloadable scenes; do not request files')
    args = p.parse_args()
    if not args.username or not args.token:
        p.error('Set USGS_M2M_USERNAME and USGS_M2M_TOKEN (or supply --username/--token).')
    if min(args.page_size, args.workers, args.request_batch, args.poll_attempts) < 1:
        p.error('page-size, workers, request-batch and poll-attempts must be positive')
    west, south, east, north = args.bbox
    if west >= east or south >= north:
        p.error('Invalid bounding box')
    spatial = {'filterType':'mbr', 'lowerLeft':{'latitude':south,'longitude':west},
               'upperRight':{'latitude':north,'longitude':east}}
    args.output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    api_key = None
    try:
        api_key = call(session, 'login-token', {'username':args.username,'token':args.token})
        if not api_key:
            raise RuntimeError('login-token returned no API key')
        session.headers['X-Auth-Token'] = api_key
        scenes = search_all(session, args.dataset, spatial, args.start, args.end, args.page_size)
        if not scenes:
            print('No matching scenes found.')
            return
        options = download_options(session, args.dataset, [s['entityId'] for s in scenes])
        selected, unavailable = choose_scenes(scenes, options)
        selection_params = {'dataset': args.dataset, 'bbox': args.bbox,
                            'selected': sorted((scene['entityId'], str(option['id'])) for scene, option in selected),
                            'extract_zips': not args.keep_zips}
        selection_hit = None if args.overwrite else cache_get('naip-selection', selection_params)
        if selection_hit and not args.list_only:
            print(f'CACHE HIT: {len(selected)} selected NAIP scenes already downloaded and verified; no download requests needed', flush=True)
            return
        with (args.output/'selected_scenes.csv').open('w',newline='',encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['tile','entityId','acquisitionDate','productId'])
            for scene, option in selected:
                writer.writerow([tile_key(scene),scene['entityId'],scene_date(scene).isoformat(),option['id']])
        print(f'Found {len(scenes)} scenes; {len(set(tile_key(s) for s in scenes))} tiles; '
              f'selected {len(selected)} latest downloadable scenes; unavailable tiles {len(unavailable)}')
        if unavailable:
            (args.output/'unavailable_tiles.txt').write_text('\n'.join(unavailable)+'\n',encoding='utf-8')
        if args.list_only or not selected:
            print('Selection saved to selected_scenes.csv')
            return
        urls = gather_urls(session,args.dataset,selected,args.poll_seconds,args.poll_attempts,args.request_batch)
        print(f'Received {len(urls)} unique download URLs for {len(selected)} selected tiles')
        counts = {'downloaded':0,'extracted':0,'skipped':0,'failed':0}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(download_one,(i,url),args.output,args.overwrite,not args.keep_zips) for i,url in enumerate(urls,1)]
            for future in as_completed(futures):
                status, detail = future.result()
                counts[status] += 1
                print(f'{status.upper()}: {detail}',flush=True)
        print('Summary:', counts)
        if counts['failed'] == 0 and len(urls) >= len(selected):
            final_files = sorted(p for p in args.output.rglob('*') if p.is_file() and
                                 p.suffix.lower() in ('.tif', '.tiff', '.jp2', '.zip'))
            if final_files:
                cache_put('naip-selection', selection_params, final_files)

        if len(urls) < len(selected):
            raise RuntimeError(f'Incomplete NAIP download preparation: {len(urls)} URLs for {len(selected)} scenes')
        if counts['failed']:
            raise RuntimeError(f"{counts['failed']} NAIP downloads failed")
    finally:
        if api_key:
            try:
                call(session, 'logout', None, retries=1)
            except Exception as exc:
                print(f'Logout warning: {exc}')
        session.close()


if __name__ == '__main__':
    main()
