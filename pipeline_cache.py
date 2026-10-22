"""Persistent, concurrent-safe cache for the rail terrain pipeline.

Keys are hashes of canonical JSON. Completed records are committed only after
outputs have been validated. File SHA256 is computed at registration and during
strict verification; routine checks use size, mtime and format metadata.
"""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.environ.get('RAIL_CACHE_DB', Path(__file__).with_name('pipeline_cache.sqlite')))
MODE = os.environ.get('RAIL_CACHE_VERIFY', 'normal')
BYPASS = os.environ.get('RAIL_CACHE_BYPASS', '0') == '1'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def hash_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def file_signature(path):
    p = Path(path)
    if p.is_file():
        st = p.stat()
        return {'path': str(p.resolve()), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}
    if p.is_dir():
        # Ignore incomplete temp files and registry itself.
        files = sorted(x for x in p.rglob('*') if x.is_file() and
                       not x.name.endswith(('.part', '.partial.tif', '.extracting', '.sqlite', '.sqlite-wal', '.sqlite-shm')))
        return {'dir': str(p.resolve()), 'files': [file_signature(x) for x in files]}
    return {'missing': str(p)}


def _connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB), timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=30000')
    con.execute('CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, kind TEXT NOT NULL, completed REAL NOT NULL, outputs TEXT NOT NULL, metadata TEXT)')
    return con


def _format_ok(path):
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in ('.json', '.geojson'):
        with p.open('r', encoding='utf-8-sig') as f:
            json.load(f)
    elif suffix in ('.tif', '.tiff'):
        import rasterio
        with rasterio.open(p) as ds:
            if ds.width < 1 or ds.height < 1 or ds.count < 1 or ds.crs is None:
                return False
    elif suffix == '.vrt':
        import rasterio
        with rasterio.open(p) as ds:
            if ds.width < 1 or ds.height < 1 or ds.count < 1 or ds.crs is None:
                return False
    elif suffix == '.zip':
        import zipfile
        with zipfile.ZipFile(p) as z:
            if not z.namelist():
                return False
    elif suffix == '.glb':
        with p.open('rb') as f:
            if f.read(4) != b'glTF':
                return False
    elif suffix == '.kml':
        import xml.etree.ElementTree as ET
        ET.parse(p)
    return True


def _check_one(saved, mode):
    p = Path(saved['path'])
    if not p.is_file() or p.stat().st_size <= 0:
        return False
    st = p.stat()
    if st.st_size != saved['size']:
        return False
    # mtime is a cheap change detector; strict hash can accept same content with a changed timestamp.
    if mode != 'strict' and st.st_mtime_ns != saved['mtime_ns']:
        return False
    if mode in ('normal', 'strict'):
        try:
            if not _format_ok(p):
                return False
        except Exception:
            return False
    if mode == 'strict' and hash_file(p) != saved['sha256']:
        return False
    return True


def get(kind, params, ttl=None, verify=None):
    if BYPASS:
        return None
    key = fingerprint([kind, params])
    with _connect() as con:
        row = con.execute('SELECT completed,outputs,metadata FROM operations WHERE key=?', (key,)).fetchone()
    if not row or (ttl is not None and time.time() - row[0] > ttl):
        return None
    saved = json.loads(row[1])
    if not all(_check_one(s, verify or MODE) for s in saved):
        return None
    return {'outputs': [Path(s['path']) for s in saved], 'metadata': json.loads(row[2] or '{}')}


def put(kind, params, outputs, metadata=None):
    if BYPASS:
        return
    saved = []
    for output in outputs:
        p = Path(output).resolve()
        if not p.is_file() or p.stat().st_size <= 0:
            raise ValueError(f'Cannot register incomplete output: {p}')
        if not _format_ok(p):
            raise ValueError(f'Cannot register invalid output: {p}')
        st = p.stat()
        saved.append({'path': str(p), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'sha256': hash_file(p)})
    with _connect() as con:
        con.execute('INSERT OR REPLACE INTO operations (key,kind,completed,outputs,metadata) VALUES (?,?,?,?,?)',
                    (fingerprint([kind, params]), kind, time.time(), json.dumps(saved), json.dumps(metadata or {}, default=str)))


def cached_json(kind, params, fetch, ttl=None):
    """Cache an API JSON response in the shared response directory."""
    path = DB.parent / '_api_cache' / (fingerprint([kind, params]) + '.json')
    hit = None if os.environ.get('RAIL_CACHE_REFRESH_SEARCHES') == '1' else get(kind, params, ttl=ttl)
    if hit and path in hit['outputs']:
        print(f'CACHE HIT: {kind}', flush=True)
        return json.loads(path.read_text(encoding='utf-8'))
    value = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.json.part')
    temp.write_text(json.dumps(value), encoding='utf-8')
    temp.replace(path)
    put(kind, params, [path])
    return value


def is_valid_file(path, mode='normal'):
    try:
        return Path(path).is_file() and Path(path).stat().st_size > 0 and (mode == 'fast' or _format_ok(path))
    except Exception:
        return False
