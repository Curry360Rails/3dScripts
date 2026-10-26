"""Project-scoped SQLite cache with validated artifacts and atomic records."""
import hashlib, json, sqlite3, time
from pathlib import Path

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()

def inventory(path):
    path=Path(path)
    if not path.exists(): return {'missing':str(path)}
    files=sorted(p for p in path.rglob('*') if p.is_file()) if path.is_dir() else [path]
    return [(str(p.resolve()),p.stat().st_size,p.stat().st_mtime_ns) for p in files if p.suffix.lower() not in ('.pyc','.part')]

class Cache:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            # Keep legacy tables intact; use a stable, versioned schema for this pipeline.
            c.execute('CREATE TABLE IF NOT EXISTS operations_v2 (key TEXT PRIMARY KEY, signature TEXT, outputs TEXT, finished REAL)')
            old = {r[1] for r in c.execute('PRAGMA table_info(operations)')}
            if {'key', 'signature', 'outputs', 'finished'} <= old:
                c.execute('INSERT OR IGNORE INTO operations_v2 (key, signature, outputs, finished) '
                          'SELECT key, signature, outputs, finished FROM operations')
    def connect(self):
        c=sqlite3.connect(self.path,timeout=60)
        c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=60000')
        return c
    def valid(self,key,signature,outputs):
        with self.connect() as c: row=c.execute('SELECT signature,outputs FROM operations_v2 WHERE key=?',(key,)).fetchone()
        if not row or row[0]!=signature:return False
        try:
            recorded=json.loads(row[1]); current=[inventory(p) for p in outputs]
            return json.loads(json.dumps(current)) == recorded and all(Path(p).exists() for p in outputs)
        except (OSError,ValueError):return False
    def record(self,key,signature,outputs):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO operations_v2 (key, signature, outputs, finished) VALUES (?,?,?,?)',(key,signature,json.dumps([inventory(p) for p in outputs]),time.time()))
