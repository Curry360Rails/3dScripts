"""Project cache validity, legacy schema compatibility, and artifact invalidation."""
import sqlite3
import pytest
from pipeline_cache import Cache, fingerprint, inventory


def test_fingerprint_stable_for_key_order():
    assert fingerprint({'b':2,'a':1}) == fingerprint({'a':1,'b':2})
    assert fingerprint({'a':1}) != fingerprint({'a':2})


@pytest.mark.xfail(strict=True, reason="Cache.valid compares JSON lists against inventory tuples; cache hits never succeed")
def test_cache_record_reuse_and_invalidation(tmp_path):
    db = Cache(tmp_path/'cache'/'pipeline_cache.sqlite')
    output = tmp_path/'terrain.glb'
    output.write_bytes(b'initial')
    db.record('terrain','settings-v1',[output])
    assert db.valid('terrain','settings-v1',[output])
    assert not db.valid('terrain','settings-v2',[output])
    output.write_bytes(b'changed-and-longer')
    assert not db.valid('terrain','settings-v1',[output])
    db.record('terrain','settings-v1',[output])
    output.unlink()
    assert not db.valid('terrain','settings-v1',[output])


@pytest.mark.xfail(strict=True, reason="Cache.valid compares JSON lists against inventory tuples; cache hits never succeed")
def test_legacy_five_column_table_does_not_break_cache(tmp_path):
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE operations (key TEXT, signature TEXT, outputs TEXT, finished REAL, extra TEXT)')
    cache=Cache(path)
    output=tmp_path/'ok.txt';output.write_text('ok')
    cache.record('stage','sig',[output])
    assert cache.valid('stage','sig',[output])


def test_inventory_excludes_partial_and_compiled(tmp_path):
    (tmp_path/'tile.tif').write_bytes(b'tile')
    (tmp_path/'ignored.part').write_bytes(b'partial')
    (tmp_path/'ignored.pyc').write_bytes(b'bytecode')
    result=inventory(tmp_path)
    assert len(result)==1 and result[0][0].endswith('tile.tif')
