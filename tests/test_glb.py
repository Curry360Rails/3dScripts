"""GLB container smoke test: headers, mesh attributes, texture, and positions."""
import json
import struct
from PIL import Image
import pytest
from glb_writer import StreamingGLB


def test_streaming_glb_embeds_mesh_and_texture(tmp_path):
    path=tmp_path/'terrain.glb'
    writer=StreamingGLB(path)
    writer.add_tile('tile',[(0,0,0),(10,0,1),(0,10,2)],[(0,0),(1,0),(0,1)],Image.new('RGB',(2,2),(10,20,30)))
    writer.finish()
    raw=path.read_bytes()
    magic,version,total=struct.unpack_from('<4sII',raw,0)
    assert magic==b'glTF' and version==2 and total==len(raw)
    json_length,json_type=struct.unpack_from('<I4s',raw,12)
    assert json_type==b'JSON'
    obj=json.loads(raw[20:20+json_length])
    assert obj['meshes'][0]['name']=='tile'
    assert obj['images'][0]['mimeType']=='image/png'
    assert obj['accessors'][0]['min']==[0.0,0.0,0.0]
    assert obj['accessors'][0]['max']==[10.0,10.0,2.0]
    assert not path.with_suffix('.partial.glb').exists()


def test_glb_rejects_incomplete_triangles(tmp_path):
    writer=StreamingGLB(tmp_path/'bad.glb')
    try:
        with pytest.raises(ValueError,match='multiple of three'):
            writer.add_tile('bad',[(0,0,0),(1,0,0)],[(0,0),(1,0)],Image.new('RGB',(1,1)))
    finally:
        writer.tmp.close()
