"""Offline smoke tests: no network, no user files touched."""
import os
import tempfile
from pathlib import Path


def test():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['RAIL_CACHE_DB'] = str(Path(tmp) / 'test.sqlite')
        os.environ['RAIL_CACHE_BYPASS'] = '0'
        import importlib
        import pipeline_cache as c
        importlib.reload(c)
        output = Path(tmp) / 'data.json'
        output.write_text('{"ok":true}')
        p = {'request': 'abc'}
        assert c.get('test', p) is None
        c.put('test', p, [output])
        assert c.get('test', p, verify='normal') is not None
        assert c.get('test', p, verify='strict') is not None
        output.write_text('{"ok":false}')
        assert c.get('test', p, verify='normal') is None
        c.put('test', p, [output])
        output.write_text('{"ok":true}')
        assert c.get('test', p, verify='strict') is None
        c.put('test', p, [output])
        assert c.get('test', {'request':'changed'}) is None
        assert c.get('test', p, ttl=-1) is None
        output.unlink()
        assert c.get('test', p) is None
        calls=[]
        def fetch():
            calls.append(1)
            return {'elements': []}
        assert c.cached_json('api', {'polygon':1}, fetch)['elements'] == []
        assert c.cached_json('api', {'polygon':1}, fetch)['elements'] == []
        assert len(calls)==1
        os.environ['RAIL_CACHE_REFRESH_SEARCHES']='1'
        c.cached_json('api', {'polygon':1}, fetch)
        assert len(calls)==2
        os.environ['RAIL_CACHE_REFRESH_SEARCHES']='0'
        os.environ['RAIL_CACHE_BYPASS']='1'
        importlib.reload(c)
        assert c.get('api', {'polygon':1}) is None
        os.environ['RAIL_CACHE_BYPASS']='0'
        print('PASS: cache hit, strict hash, invalidation, TTL, missing output, API refresh, bypass')

if __name__ == '__main__':
    test()
