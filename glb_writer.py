# MAINTAINER NOTES
# GLB 2.0 serialization. Each tile contributes triangles, UV coordinates, and an embedded PNG
# texture. GLB positions use the coordinates passed by the caller; this writer does not
# reproject or rescale geometry.
# These comments describe the existing implementation; no executable statements were changed.

"""Incremental GLB 2.0 writer; terrain positions are unchanged from OBJ output."""
import io,json,struct,tempfile
import numpy as np
from PIL import Image

# PURPOSE: Accumulate binary buffers and glTF metadata incrementally, then write one GLB file.
class StreamingGLB:
    # PURPOSE: Initialize in-memory glTF tables and a temporary binary stream.
    # INPUTS: path.
    def __init__(self,path):
        self.path=path;self.tmp=tempfile.TemporaryFile();self.views=[];self.accessors=[];self.materials=[];self.textures=[];self.images=[];self.meshes=[];self.nodes=[]
    # PURPOSE: Append four-byte-aligned bytes and register their bufferView.
    # INPUTS: data, target.
    def blob(self,data,target=None):
        offset=self.tmp.tell();self.tmp.write(data);self.tmp.write(b'\0'*((-len(data))%4))
        item={'buffer':0,'byteOffset':offset,'byteLength':len(data)}
        if target:item['target']=target
        self.views.append(item);return len(self.views)-1
    # PURPOSE: Create a typed glTF accessor for vertex positions, UVs or other arrays.
    # INPUTS: arr, kind, component, target.
    def accessor(self,arr,kind,component=5126,target=None):
        arr=np.asarray(arr,dtype=np.float32 if component==5126 else np.uint32)
        view=self.blob(arr.tobytes(),target)
        item={'bufferView':view,'componentType':component,'count':len(arr),'type':kind}
        if kind=='VEC3':item['min']=arr.min(axis=0).tolist();item['max']=arr.max(axis=0).tolist()
        self.accessors.append(item);return len(self.accessors)-1
    # PURPOSE: Add a textured triangle mesh as a named glTF node; vertices are already in local model coordinates.
    # INPUTS: name, vertices, uv, image.
    def add_tile(self,name,vertices,uv,image):
        if not vertices:return
        v=np.asarray(vertices,dtype=np.float32);t=np.asarray(uv,dtype=np.float32)
        if len(v)%3:raise ValueError('Triangle vertex count must be multiple of three')
        a=self.accessor(v,'VEC3',target=34962);b=self.accessor(t,'VEC2',target=34962)
        buffer=io.BytesIO();image.save(buffer,format='PNG')
        image_view=self.blob(buffer.getvalue());self.images.append({'bufferView':image_view,'mimeType':'image/png'})
        self.textures.append({'source':len(self.images)-1})
        self.materials.append({'pbrMetallicRoughness':{'baseColorTexture':{'index':len(self.textures)-1},'metallicFactor':0,'roughnessFactor':1},'doubleSided':True})
        self.meshes.append({'name':name,'primitives':[{'attributes':{'POSITION':a,'TEXCOORD_0':b},'material':len(self.materials)-1,'mode':4}]})
        self.nodes.append({'mesh':len(self.meshes)-1,'name':name})
    # PURPOSE: Serialize JSON and binary chunks to a temporary GLB, then replace the destination.
    def finish(self):
        root={'asset':{'version':'2.0','generator':'rail pipeline'},'scene':0,'scenes':[{'nodes':list(range(len(self.nodes)))}], 'nodes':self.nodes,'meshes':self.meshes,'buffers':[{'byteLength':self.tmp.tell()}],'bufferViews':self.views,'accessors':self.accessors,'materials':self.materials,'textures':self.textures,'images':self.images,'samplers':[]}
        js=json.dumps(root,separators=(',',':')).encode();js+=b' '*((-len(js))%4)
        length=self.tmp.tell();self.tmp.seek(0)
        temp=self.path.with_suffix('.partial.glb')
        with temp.open('wb') as f:
            f.write(struct.pack('<4sII',b'glTF',2,12+8+len(js)+8+length))
            f.write(struct.pack('<I4s',len(js),b'JSON'));f.write(js)
            f.write(struct.pack('<I4s',length,b'BIN\0'))
            while True:
                data=self.tmp.read(1024*1024)
                if not data:break
                f.write(data)
        temp.replace(self.path);self.tmp.close()
