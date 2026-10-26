"""Synthetic DEMs verify VRT behavior without USGS downloads."""
import pytest
import rasterio
from build_terrain import resolve_raster_input


def test_adjacent_dem_tiles_create_readable_vrt(tmp_path,dem_writer):
    src=tmp_path/'dem';out=tmp_path/'output'
    dem_writer(src/'a.tif',11,x=0)
    dem_writer(src/'b.tif',22,x=10)
    vrt,files=resolve_raster_input(src,'DEM',out)
    assert len(files)==2
    with rasterio.open(vrt) as ds:
        assert ds.width==20 and ds.height==10
        assert ds.read(1)[5,5]==11
        assert ds.read(1)[5,15]==22
    assert (out/'_raster_mosaics'/'dem_mosaic.sources.json').is_file()
    assert resolve_raster_input(src,'DEM',out)[0]==vrt


def test_mixed_crs_error_names_files_and_crs(tmp_path,dem_writer):
    src=tmp_path/'dem'
    dem_writer(src/'correct.tif',1,crs='EPSG:26911')
    dem_writer(src/'wrong.tif',2,crs='EPSG:26913',x=10)
    with pytest.raises(ValueError) as e:
        resolve_raster_input(src,'DEM',tmp_path/'out')
    message=str(e.value)
    assert 'correct.tif' in message and 'wrong.tif' in message
    assert '26911' in message and '26913' in message


def test_unaligned_dem_grid_is_rejected(tmp_path,dem_writer):
    src=tmp_path/'dem'
    dem_writer(src/'a.tif',1,x=0)
    dem_writer(src/'offset.tif',2,x=10.5)
    with pytest.raises(ValueError,match='not aligned'):
        resolve_raster_input(src,'DEM',tmp_path/'out')


def test_single_dem_does_not_need_vrt(tmp_path,dem_writer):
    path=dem_writer(tmp_path/'dem'/'only.tif',5)
    result,files=resolve_raster_input(path.parent,'DEM',tmp_path/'out')
    assert result==str(path) and files==[str(path)]
