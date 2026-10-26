"""Shared test configuration; no network or AnyLogic connection is required."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

@pytest.fixture
def sample_polygon(tmp_path):
    import json
    path = tmp_path / 'test_area.geojson'
    path.write_text(json.dumps({'type':'Feature','geometry':{'type':'Polygon','coordinates':[[[-117.0,34.0],[-116.9,34.0],[-116.9,34.1],[-117.0,34.0]]]},'properties':{}}))
    return path

@pytest.fixture
def dem_writer():
    def write(path, value, crs='EPSG:26911', x=0, y=10, resolution=1, width=10, height=10):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        path.parent.mkdir(parents=True,exist_ok=True)
        with rasterio.open(path,'w',driver='GTiff',width=width,height=height,count=1,dtype='float32',crs=crs,transform=from_origin(x,y,resolution,resolution),nodata=-9999) as dst:
            dst.write(np.full((height,width),value,dtype='float32'),1)
        return path
    return write
