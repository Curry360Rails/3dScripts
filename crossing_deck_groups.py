"""Group compatible inferred railway spans and build one textured surface per group.

No track topology is changed. Coordinates are projected meters. These are
visualization estimates, not surveyed bridge elevations.
"""
import csv
import json
import math
from pathlib import Path
import numpy as np
from shapely.geometry import LineString, mapping, Point
from shapely.ops import unary_union, triangulate
from shapely import constrained_delaunay_triangles
from PIL import Image


def _height_near(record, xy):
    pts = record['profile_xyz']
    px,py=xy
    best=(float('inf'),float('nan'))
    for a,b in zip(pts,pts[1:]):
        dx=b[0]-a[0];dy=b[1]-a[1]
        t=max(0,min(1,((px-a[0])*dx+(py-a[1])*dy)/max(1e-12,dx*dx+dy*dy)))
        x=a[0]+t*dx;y=a[1]+t*dy
        d=math.hypot(px-x,py-y)
        if d<best[0]:best=(d,a[2]+t*(b[2]-a[2]))
    return best


def group_crossings(records, width=6., gap=3., vertical_tolerance=1.):
    candidates=[]
    for r in records:
        pts=r.get('profile_xyz',[])
        if len(pts)<2:continue
        line=LineString([(p[0],p[1]) for p in pts])
        if line.length<1e-4:continue
        candidates.append((r,line,line.buffer(width/2,cap_style=2,join_style=2)))
    n=len(candidates)
    parent=list(range(n))
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    def join(i,j):parent[find(j)]=find(i)
    from shapely.strtree import STRtree
    polygons=[x[2] for x in candidates]
    tree=STRtree(polygons) if polygons else None
    for i,(r,line,poly) in enumerate(candidates):
        for j in tree.query(poly.buffer(gap)):
            j=int(j)
            if j<=i:continue
            rr,ll,other=candidates[j]
            if poly.distance(other)>gap:continue
            # Check local elevations where the geometries are closest, not just
            # overall average heights, to avoid merging stacked structures.
            from shapely.ops import nearest_points
            p,q=nearest_points(line,ll)
            _,za=_height_near(r,(p.x,p.y))
            _,zb=_height_near(rr,(q.x,q.y))
            if abs(za-zb)<=vertical_tolerance:join(i,j)
    components={}
    for i in range(n):components.setdefault(find(i),[]).append(i)
    groups=[]
    for ids in components.values():
        members=[candidates[i][0] for i in ids]
        polys=[candidates[i][2] for i in ids]
        # Buffer out and back to close only small gaps; preserve separate islands.
        footprint=unary_union([p.buffer(gap/2,join_style=2) for p in polys]).buffer(-gap/2,join_style=2)
        parts=[footprint] if footprint.geom_type=='Polygon' else list(footprint.geoms)
        for part in parts:
            if part.is_empty or part.area<1:continue
            submembers=[r for r,_,p in (candidates[i] for i in ids) if p.intersects(part)]
            groups.append({'id':f'crossing_group_{len(groups)+1:04d}',
                           'members':submembers,'footprint':part,
                           'source_ids':[r['id'] for r in submembers]})
    return groups


def save_groups(output,groups,crs):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    features=[]
    with (output/'crossing_groups.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.writer(f);writer.writerow(['group_id','member_count','source_ids','area_m2','status'])
        for g in groups:
            writer.writerow([g['id'],len(g['members']),';'.join(g['source_ids']),round(g['footprint'].area,2),'inferred_review_required'])
            features.append({'type':'Feature','properties':{'group_id':g['id'],'source_ids':g['source_ids'],
                'member_count':len(g['members']),'status':'inferred_review_required'},
                'geometry':mapping(g['footprint'])})
    (output/'crossing_groups.geojson').write_text(json.dumps({'type':'FeatureCollection',
        'name':'Inferred railway bridge deck groups','crs':{'type':'name','properties':{'name':str(crs)}},
        'features':features},indent=2),encoding='utf-8')


def append_group_decks(glb,groups,imagery,bounds,base,resolution,max_texture_px=2048):
    from tqdm.auto import tqdm
    count=0
    for g in tqdm(groups,desc='Exporting grouped crossing decks',unit='group'):
        poly=g['footprint'].simplify(0.3,preserve_topology=True)
        # Clip Delaunay triangles to the unified footprint, so no overlapping
        # strips are exported. Avoid holes and stray triangles outside footprint.
        triangles=[tri for tri in constrained_delaunay_triangles(poly).geoms
                   if tri.area>1e-8]
        if not triangles:continue
        xmin,ymin,xmax,ymax=poly.bounds
        w=min(max_texture_px,max(2,math.ceil((xmax-xmin)/resolution)))
        h=min(max_texture_px,max(2,math.ceil((ymax-ymin)/resolution)))
        rgb=imagery.render((xmin,ymin,xmax,ymax),w,h)
        vertices=[];uv=[]
        cache={}
        for tri in triangles:
            coords=list(tri.exterior.coords)[:3]
            cross=(coords[1][0]-coords[0][0])*(coords[2][1]-coords[0][1])-(coords[1][1]-coords[0][1])*(coords[2][0]-coords[0][0])
            if cross<0:coords[1],coords[2]=coords[2],coords[1]
            for x,y in coords:
                key=(round(x,6),round(y,6))
                if key not in cache:
                    nearby=sorted((_height_near(r,(x,y)) for r in g['members']),key=lambda t:t[0])
                    # Blend only profiles with comparable local heights.
                    best=nearby[0][1]
                    matches=[(d,z) for d,z in nearby if abs(z-best)<=1.0 and d<=15]
                    cache[key]=(sum(z/(d+0.5) for d,z in matches)/sum(1/(d+0.5) for d,z in matches)) if matches else best
                vertices.append((x-bounds.left,y-bounds.bottom,cache[key]-base))
                uv.append(((x-xmin)/(xmax-xmin),1-(y-ymin)/(ymax-ymin)))
        glb.add_tile(g['id'],vertices,uv,Image.fromarray(rgb));count+=1
    return count
