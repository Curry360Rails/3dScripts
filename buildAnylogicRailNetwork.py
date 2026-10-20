"""Import topology-preserving 3D railway GeoJSON to AnyLogic Design-Time API.

Run 01_build_rail_3d_geojson.py first. Always review DRY_RUN output.
OSM node IDs determine connectivity; no arbitrary nearby-track snapping.
Only junctions with >=3 incident track ends create switches. Degree-2 joins
are merged, including curves, without imposing an angle cutoff.
"""
import json
import math
from collections import defaultdict
from pathlib import Path

HERE=Path(__file__).resolve().parent
DATA_DIR=HERE/'rail_3d_output'
TOKEN='d68e12ab-4988-447b-80b1-fe92bc82fc77'
MODEL_NAME='CEMEX_Victorville'
AGENT_NAME='Main'
LEVEL_INDEX=0
DRY_RUN=False
EXPECTED_NETWORKS=4
MODEL_UNITS_PER_METER=0.03
FLIP_Y=True
NAME_PREFIX='osmRail'
# For aligning with the previously exported terrain OBJ, use its georeference.json
# (origin_x, origin_y, base_elevation). Override if necessary.
TERRAIN_GEOREFERENCE=DATA_DIR/'georeference.json'
# Crop boundary endpoints without OSM IDs remain unconnected, except when
# exactly coincident (not merely close) within this very small tolerance.
CROP_ENDPOINT_TOLERANCE_M=0.01

class DSU:
    def __init__(self,n):self.p=list(range(n))
    def find(self,i):
        while self.p[i]!=i:self.p[i]=self.p[self.p[i]];i=self.p[i]
        return i
    def union(self,a,b):self.p[self.find(a)]=self.find(b)

def build():
    data=json.loads((DATA_DIR/'rail_lines_3d.geojson').read_text(encoding='utf-8'))
    switch_fc=json.loads((DATA_DIR/'rail_switches_3d.geojson').read_text(encoding='utf-8'))
    switch_ids={int(f['properties']['osm_node_id']) for f in switch_fc['features']}
    # Original OSM IDs for every vertex, including inserted switch/shared nodes.
    # Split into edges between topological nodes, retaining all intermediate XYZ.
    features=[]
    for f in data['features']:
        pts=[tuple(p) for p in f['geometry']['coordinates']]
        ids=f['properties']['osm_node_ids']
        if len(pts)!=len(ids):raise ValueError('Coordinate / node ID count mismatch')
        features.append((pts,ids,f['properties']['osm_way_id']))
    counts=defaultdict(set)
    for fi,(pts,ids,way) in enumerate(features):
        for i,nid in enumerate(ids):
            if nid is not None:counts[nid].add((fi,i))
    # Split at shared OSM IDs and at explicitly tagged switches. No interpolation
    # needed: stage 1 already inserted the exact projected OSM positions.
    edges=[]
    for fi,(pts,ids,way) in enumerate(features):
        split={0,len(pts)-1}
        split.update(i for i,nid in enumerate(ids) if nid is not None and
                     (len(counts[nid])>1 or nid in switch_ids))
        cuts=sorted(split)
        for a,b in zip(cuts,cuts[1:]):
            if b<=a or math.dist(pts[a],pts[b])<1e-8:continue
            edges.append((pts[a:b+1],ids[a],ids[b],way))
    # Topological node IDs: use original OSM node IDs; crop ends are distinct
    # unless they are practically identical in both XY and Z.
    end_keys=[];end_xyz=[]
    for ei,(pts,a,b,way) in enumerate(edges):
        for pos,nid in ((pts[0],a),(pts[-1],b)):
            end_keys.append(('osm',int(nid)) if nid is not None else ('crop',len(end_keys)))
            end_xyz.append(pos)
    dsu=DSU(len(end_keys));seen={}
    for i,key in enumerate(end_keys):
        if key[0]=='osm':
            if key in seen:dsu.union(i,seen[key])
            else:seen[key]=i
    # Only exact crop-to-crop endpoint coincidence; never join distinct OSM IDs.
    crop=[i for i,k in enumerate(end_keys) if k[0]=='crop']
    bins=defaultdict(list)
    tol=CROP_ENDPOINT_TOLERANCE_M
    for i in crop:
        p=end_xyz[i]
        cell=tuple(math.floor(v/tol) for v in p)
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                for dz in (-1,0,1):
                    for j in bins[(cell[0]+dx,cell[1]+dy,cell[2]+dz)]:
                        if math.dist(p,end_xyz[j])<=tol:dsu.union(i,j)
        bins[cell].append(i)
    groups=defaultdict(list)
    for i in range(len(end_keys)):groups[dsu.find(i)].append(i)
    node_for={}; node_members={};node_coords={}
    for members in groups.values():
        node=min(members)
        # Prefer coordinates of OSM endpoints, avoiding average drift.
        osm_ends=[i for i in members if end_keys[i][0]=='osm']
        center=end_xyz[osm_ends[0] if osm_ends else members[0]]
        node_members[node]=members;node_coords[node]=center
        for i in members:node_for[i]=node
    coords=[list(e[0]) for e in edges]
    for i in range(len(end_keys)):
        coords[i//2][0 if i%2==0 else -1]=node_coords[node_for[i]]
    switch_nodes={n:node_coords[n] for n,m in node_members.items() if len(m)>=3}
    # Merge maximal degree-2 chains. A degree-2 tagged switch is not a turnout.
    used=set(); merged=[]
    def walk(edge,entry):
        pts=[];first=entry
        while edge not in used:
            used.add(edge)
            a,b=node_for[2*edge],node_for[2*edge+1]
            forward=(a==entry)
            segment=coords[edge] if forward else coords[edge][::-1]
            pts.extend(segment if not pts else segment[1:])
            nxt=b if forward else a
            if len(node_members[nxt])!=2:return pts,first,nxt
            options=[i//2 for i in node_members[nxt] if i//2!=edge]
            if not options or options[0] in used:return pts,first,nxt
            edge,entry=options[0],nxt
        return pts,first,entry
    for node,members in node_members.items():
        if len(members)==2:continue
        for end in members:
            if end//2 not in used:merged.append(walk(end//2,node))
    for ei in range(len(edges)):
        if ei not in used:merged.append(walk(ei,node_for[2*ei]))
    # Network count is derived, not forced.
    comp=DSU(len(merged));incident={}
    for i,(_,a,b) in enumerate(merged):
        for n in (a,b):
            if n in incident:comp.union(i,incident[n])
            else:incident[n]=i
    networks=defaultdict(list)
    for i in range(len(merged)):networks[comp.find(i)].append(i)
    sizes=sorted((len(x) for x in networks.values()),reverse=True)
    unmatched=[sid for sid in switch_ids if ('osm',sid) not in seen]
    print(f'Input features: {len(features)}; split edges: {len(edges)}')
    print(f'Merged RailTracks: {len(merged)}; 3+-leg switches: {len(switch_nodes)}')
    print(f'Explicit OSM switch IDs not represented by retained line endpoints: {len(unmatched)}')
    print(f'CONNECTED RAIL NETWORKS: {len(networks)}; sizes: {sizes}')
    if len(networks)!=EXPECTED_NETWORKS:
        print(f'WARNING: expected {EXPECTED_NETWORKS}; no artificial connections added.')
    return merged,switch_nodes

def main():
    merged,switch_nodes=build()
    if DRY_RUN:
        print('DRY_RUN=True: nothing added to AnyLogic.')
        return
    if TOKEN=='PASTE_ANYLOGIC_CONNECTOR_TOKEN':
        raise ValueError('Set TOKEN to your AnyLogic Design-Time API connector token')
    from anylogic_design_time_api import Workspace, Utils
    from anylogic_design_time_api.generated import RailTrack,RailroadSwitch
    ref=json.loads(TERRAIN_GEOREFERENCE.read_text(encoding='utf-8'))
    if str(ref['crs']).upper() not in ('EPSG:26911','26911'):
        raise ValueError('Terrain georeference must be EPSG:26911')
    ox,oy,base=ref['origin_x'],ref['origin_y'],ref['base_elevation']
    def cv(p):
        return ((p[0]-ox)*MODEL_UNITS_PER_METER,
                ((oy-p[1]) if FLIP_Y else (p[1]-oy))*MODEL_UNITS_PER_METER,
                (p[2]-base)*MODEL_UNITS_PER_METER)
    ws=Workspace(TOKEN)
    level=ws.getModel(MODEL_NAME).getAgent(AGENT_NAME).getLevels()[LEVEL_INDEX]
    switches={}
    for n,c in switch_nodes.items():
        x,y,z=cv(c)
        sw=RailroadSwitch.create(level);sw.setX(x);sw.setY(y);sw.setZ(z)
        switches[n]=sw
    failures=0
    for i,(points,a,b) in enumerate(merged,1):
        pts=[cv(p) for p in points]
        x0,y0,z0=pts[0]
        track=RailTrack.create(level)
        track.setX(x0);track.setY(y0);track.setZ(z0)
        rel=[v for x,y,z in pts for v in (x-x0,y-y0,z-z0)]
        track.setPointList(Utils.createStraightPointsList(track,rel))
        try:track.setName(f'{NAME_PREFIX}{i:04d}')
        except Exception:pass
        for node,method in ((a,track.setSource),(b,track.setTarget)):
            if node in switches:
                try:method(switches[node])
                except Exception as exc:
                    failures+=1;print(f'Connection failure on track {i} at node {node}: {exc}')
        if i%50==0:print(f'Created {i}/{len(merged)} tracks')
    print(f'Done: {len(merged)} RailTracks, {len(switches)} switches, {failures} connection failures.')
    if failures:print('Inspect failed switch connections; some multi-leg OSM junctions may not be supported by AnyLogic.')

if __name__=='__main__':main()
