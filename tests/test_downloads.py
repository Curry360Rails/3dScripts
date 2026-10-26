"""Offline polygon, naming, ZIP safety, and atomic download tests."""
import argparse
import io
import zipfile
import pytest
from download_3dep_1m import parse_polygon,safe_name,extract_zip,download


def test_polygon_geojson_parsing(sample_polygon):
    pts=parse_polygon(argparse.Namespace(geojson=str(sample_polygon),polygon=None))
    assert len(pts)==3
    assert pts[0]==(-117.0,34.0)


def test_invalid_polygon_rejected():
    with pytest.raises(ValueError,match='at least three'):
        parse_polygon(argparse.Namespace(geojson=None,polygon=['1,2','2,3']))


def test_safe_name_uses_url_or_title():
    assert safe_name('https://example.com/data/tile.tif',{},0)=='tile.tif'
    assert safe_name('https://example.com/download',{'title':'Tile 1'},0)=='Tile_1.bin'


def test_zip_extract_and_reject_path_traversal(tmp_path):
    path=tmp_path/'good.zip'
    with zipfile.ZipFile(path,'w') as z:z.writestr('tile.tif',b'data')
    extract_zip(path,tmp_path)
    assert (tmp_path/'good'/'tile.tif').read_bytes()==b'data'
    evil=tmp_path/'evil.zip'
    with zipfile.ZipFile(evil,'w') as z:z.writestr('../escape.txt',b'bad')
    with pytest.raises(ValueError,match='Unsafe ZIP path'):
        extract_zip(evil,tmp_path)
    assert not (tmp_path/'escape.txt').exists()


class FakeResponse:
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def raise_for_status(self):pass
    def iter_content(self,chunk_size):yield b'abc';yield b'def'

class FakeSession:
    def __init__(self):self.calls=0
    def get(self,*args,**kwargs):self.calls+=1;return FakeResponse()


def test_download_atomic_and_skips_existing(tmp_path):
    s=FakeSession();item={'downloadURL':'https://example.com/test.tif'}
    result=download(s,item,tmp_path,1)
    assert result.read_bytes()==b'abcdef'
    assert not (tmp_path/'test.tif.part').exists()
    assert download(s,item,tmp_path,1)==result
    assert s.calls==1
