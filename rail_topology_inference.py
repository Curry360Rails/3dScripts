"""Topology-aware 1 m DEM grade screening and approach-to-approach interpolation.
Estimated visual elevations only; review all crossings before operational use.
"""
import math
import json
from collections import defaultdict, deque
import numpy as np
from rasterio.warp import transform
from rail_elevation_inference import _sample


def infer_topology_profiles(osm_path, dem, cfg):
    data=json.loads(open(osm_path,encoding='utf-8-sig').read())
    elements=data['elements']
    raw={int(e['id']):(e['lon'],e['lat']) for e in elements if e['type']=='node' and 'lon' in e}
    ways=[e for e in elements if e['type']=='way' and e.get('tags',{}).get('railway')=='rail' and len(e.get('nodes',[]))>1 and all(n in raw for n in e['nodes'])]
    ids=sorted({n for w in ways for n in w['nodes']})
    xx,yy=transform('EPSG:4326',dem.crs,[raw[n][0] for n in ids],[raw[n][1] for n in ids])
    original=dict(zip(ids,zip(xx,yy)))
    # Sample every edge at <=1 m, including intermediate positions at switches.
    vertices={n:tuple(original[n]) for n in ids}; adjacency=defaultdict(list)
    next_id=-1
    for w in ways:
        for a,b in zip(w['nodes'],w['nodes'][1:]):
            p,q=original[a],original[b]; length=math.dist(p,q)
            if length<0.01:continue
            steps=max(1,math.ceil(length/cfg['sample_spacing_m']))
            previous=a
            for k in range(1,steps+1):
                if k==steps: node=b
                else:
                    node=next_id;next_id-=1
                    t=k/steps;vertices[node]=(p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1]))
                d=length/steps
                adjacency[previous].append((node,d));adjacency[node].append((previous,d))
                previous=node
    all_nodes=list(vertices)
    zvals=_sample(dem,[vertices[n] for n in all_nodes]); z=dict(zip(all_nodes,map(float,zvals)))
    limit=cfg['grade_limit'];stable_m=cfg['stable_m'];max_span=cfg['max_span_m']
    min_drop=cfg['min_depth_m'];angle_cos=math.cos(math.radians(cfg['max_turn_deg']))
    def direction(a,b):
        x,y=vertices[a];u,v=vertices[b];d=math.hypot(u-x,v-y)
        return ((u-x)/d,(v-y)/d) if d else (0,0)
    # Find the nearest stable 20 m approach on each side of a suspect edge.
    # Search is directional: no U-turns, even across OSM ways with reversed order.
    def outward(anchor, blocked):
        heading=direction(blocked,anchor)
        queue=deque([(anchor,blocked,heading,0.0,0.0,[anchor],None)])
        seen=set(); explored=0; found=[]
        while queue and explored<cfg['max_states']:
            cur,prev,heading,travel,stable,path,stable_start=queue.popleft()
            explored+=1
            if stable>=stable_m:
                found.append((path,stable_start))
                if len(found)>=cfg.get('max_approach_candidates',24):break
                # Continue past this section to find a later stable approach
                # when the first one lies on the roadbed below a bridge.
            if travel>=max_span:continue
            for nxt,d in adjacency[cur]:
                if nxt==prev or nxt in path:continue
                ndir=direction(cur,nxt)
                if heading[0]*ndir[0]+heading[1]*ndir[1]<angle_cos:continue
                if not(math.isfinite(z[cur]) and math.isfinite(z[nxt])):continue
                ok=abs(z[nxt]-z[cur])/d<=limit
                newstable=stable+d if ok else 0.0
                newstart=(stable_start if stable>0 else cur) if ok else None
                state=(cur,nxt,int(newstable),int((travel+d)/2))
                if state in seen:continue
                seen.add(state)
                queue.append((nxt,cur,ndir,travel+d,newstable,path+[nxt],newstart))
        return found

    # Fit a slope to the reliable 20 m approach using cumulative XY distance.
    # The fitted line is an expectation, not a rigid constraint on the final deck.
    def approach_fit(path, boundary):
        k=path.index(boundary)
        sample=path[k:]
        distances=[0.0]
        for u,v in zip(sample,sample[1:]):
            distances.append(distances[-1]+math.dist(vertices[u],vertices[v]))
        if distances[-1]<stable_m-1.01:return None
        slope=float(np.polyfit(distances,[z[n] for n in sample],1)[0])
        return float(z[boundary]),slope

    # A candidate must be consistent with the first stable approach when
    # projected over its cumulative path length. The allowable vertical envelope
    # expands with distance to permit realistic changes in grade.
    tolerance=cfg.get('height_tolerance_m',1.5)
    slope_slack=cfg.get('grade_envelope_slack',limit)
    grade_similarity=cfg.get('approach_grade_tolerance',0.01)
    bad=[]
    for a in adjacency:
        for b,d in adjacency[a]:
            if a>=b:continue
            if math.isfinite(z[a]) and math.isfinite(z[b]) and abs(z[b]-z[a])/d>limit:
                bad.append((a,b))
    spans=[]; used=set(); rejected=defaultdict(int)
    for a,b in bad:
        if tuple(sorted((a,b))) in used:continue
        left=outward(a,b)
        right=outward(b,a)
        if not left or not right:
            rejected['no_stable_approach']+=1;continue
        chosen=None
        # Try nearby stable sections first, but do not stop at a false
        # stable section on the floor of a DEM depression.
        for lp,la in left:
            lf=approach_fit(lp,la)
            if lf is None:continue
            for rp,ra in right:
                rf=approach_fit(rp,ra)
                if rf is None:continue
                li=lp.index(la);ri=rp.index(ra)
                path=list(reversed(lp[:li+1]))+rp[:ri+1]
                if len(path)<2 or len(path)!=len(set(path)):continue
                dist=[0.0]
                for u,v in zip(path,path[1:]):
                    dist.append(dist[-1]+math.dist(vertices[u],vertices[v]))
                length=dist[-1]
                if not(cfg['min_span_m']<=length<=max_span):continue
                za,sa=lf;zb,sb=rf
                forward_slope=-sa
                if abs(forward_slope-sb)>grade_similarity:continue
                projected=za+forward_slope*length
                if abs(zb-projected)>tolerance+slope_slack*length:continue
                if abs(zb-za)/length>limit:continue
                expected=np.interp(dist,[0,length],[za,zb])
                observed=np.array([z[n] for n in path])
                if not np.isfinite(observed).all():continue
                if np.max(np.abs(expected-observed))<min_drop:continue
                chosen=(path,dist,expected)
                break
            if chosen is not None:break
        if chosen is None:
            rejected['no_compatible_approach_pair']+=1;continue
        path,dist,expected=chosen
        spans.append((path,dist,expected))
        for u,v in zip(path,path[1:]):used.add(tuple(sorted((u,v))))
    # Lock the two accepted approach endpoints to the local DEM surface.  The
    # terrain is sampled at the SAME XY locations as the railway endpoints.
    # A small offset represents rail/deck height above the DEM surface.
    clearance=float(cfg.get('endpoint_clearance_m',0.25))
    endpoint_samples=[]
    for path,dist,expected in spans:
        endpoint_samples.extend([vertices[path[0]],vertices[path[-1]]])
    endpoint_dem=_sample(dem,endpoint_samples) if endpoint_samples else []
    corrected=defaultdict(list); crossings=[]
    for i,(path,dist,expected) in enumerate(spans,1):
        expected=np.array(expected,dtype=float,copy=True)
        # Correct endpoint discrepancies without flattening the longitudinal
        # profile: interpolate endpoint corrections across cumulative distance.
        left_dem=float(endpoint_dem[2*(i-1)])
        right_dem=float(endpoint_dem[2*(i-1)+1])
        left_target=max(float(expected[0]),left_dem+clearance) if math.isfinite(left_dem) else float(expected[0])
        right_target=max(float(expected[-1]),right_dem+clearance) if math.isfinite(right_dem) else float(expected[-1])
        length=float(dist[-1])
        if length>0:
            expected+=np.interp(dist,[0,length],[left_target-expected[0],right_target-expected[-1]])
        for n,h in zip(path,expected):corrected[n].append(float(h))
        profile=[[float(vertices[n][0]),float(vertices[n][1]),float(h)] for n,h in zip(path,expected)]
        crossings.append({'id':f'topology_{i}','way_ids':[],'profile_xyz':profile,
            'start_xyz':profile[0],'end_xyz':profile[-1], 'length_m':float(dist[-1]),
            'max_dem_drop_m':float(max(abs(z[n]-h) for n,h in zip(path,expected))),
            'status':'inferred_review_required','source':'topology-aware 20 m stable approaches',
            'endpoint_dem_z_m':[left_dem,right_dem],
            'endpoint_target_z_m':[left_target,right_target]})
    nodes={}
    for n in ids:
        raw_z=z[n]
        if not math.isfinite(raw_z) and n not in corrected:continue
        height=float(np.median(corrected[n])) if n in corrected else float(raw_z)
        nodes[str(n)]={'xy':list(map(float,vertices[n])),
                       'dem_z_m':float(raw_z) if math.isfinite(raw_z) else None,
                       'corrected_z_m':height,
                       'delta_z_m':height-raw_z if math.isfinite(raw_z) else None}
    return {'crs':str(dem.crs),'method':'topology-aware 1m stable approaches',
            'config':cfg,'nodes':nodes,'crossings':crossings,
            'rail_ways':len(ways),'chains_processed':len(ways),
            'bad_grade_edges':len(bad),'rejected_candidates':dict(rejected)}
