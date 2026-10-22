"""Resolve elevations once per grouped bridge, ignoring internal inferred anchors.

Visual estimates only. This module never modifies railway XY or connectivity.
"""
import math
import numpy as np
from shapely.geometry import Point
from shapely.ops import nearest_points


def _axis(members):
    coords = np.asarray([(p[0], p[1]) for r in members for p in r['profile_xyz']], float)
    center = coords.mean(axis=0)
    _, _, vh = np.linalg.svd(coords - center, full_matrices=False)
    direction = vh[0]
    return center, direction


def solve_group_elevations(groups, result, dem, clearance=0.25, grade_limit=0.03,
                           anchor_band_m=12.0, min_anchor_count=1):
    """Apply common longitudinal solution to group members and OSM nodes.

    Outer anchors come only from the far ends of the entire grouped crossing,
    never from the endpoints of nested spans. Anchor heights are DEM samples at
    those endpoints, with a conservative high-side selection to avoid a roadbed
    sampled beneath the actual bridge. Review is required if neither end has a
    reliable stable approach.
    """
    from rail_elevation_inference import _sample
    diagnostics = []
    for group in groups:
        members = group['members']
        if not members:
            continue
        center, direction = _axis(members)
        points = [(r, p) for r in members for p in r['profile_xyz']]
        stations = np.array([np.dot(np.asarray(p[:2])-center, direction) for _,p in points])
        lo, hi = float(stations.min()), float(stations.max())
        if hi-lo < 1.0:
            continue
        # Only span endpoints near the GROUP's outer limits can act as anchors.
        endpoint_candidates=[]
        for r in members:
            for p in (r['profile_xyz'][0], r['profile_xyz'][-1]):
                s=float(np.dot(np.asarray(p[:2])-center,direction))
                if s-lo <= anchor_band_m or hi-s <= anchor_band_m:
                    endpoint_candidates.append((s,p,r))
        coords=[p[:2] for _,p,_ in endpoint_candidates]
        sampled=_sample(dem,coords) if coords else []
        left=[];right=[]
        for (s,p,r),raw in zip(endpoint_candidates,sampled):
            # Prefer verified original DEM anchors over interpolated member Z.
            if not math.isfinite(float(raw)):
                continue
            item=(s,float(raw)+clearance,p,r)
            if s-lo<=anchor_band_m:left.append(item)
            if hi-s<=anchor_band_m:right.append(item)
        if len(left)<min_anchor_count or len(right)<min_anchor_count:
            diagnostics.append({'group':group['id'],'status':'skipped_missing_outer_dem_anchors'})
            continue
        # High-side median: suppress lower DEM samples that lie on the roadbed.
        def robust_anchor(values):
            zs=np.array([v[1] for v in values],float)
            threshold=np.quantile(zs,0.65)
            good=[v for v in values if v[1]>=threshold-1e-6]
            return float(np.median([v[0] for v in good])),float(np.median([v[1] for v in good]))
        s0,z0=robust_anchor(left);s1,z1=robust_anchor(right)
        if s1-s0<1.0:
            diagnostics.append({'group':group['id'],'status':'skipped_invalid_anchor_stations'})
            continue
        slope=(z1-z0)/(s1-s0)
        if abs(slope)>grade_limit:
            diagnostics.append({'group':group['id'],'status':'skipped_unreasonable_outer_grade',
                                'grade':slope,'left_z':z0,'right_z':z1})
            continue
        # Preserve legitimate lateral differences only when supported by outer
        # anchors. Internal profiles are never trusted as vertical constraints.
        lateral_anchors=[]
        for s,z,p,_ in left+right:
            lateral=float(np.cross(direction,np.asarray(p[:2])-center))
            predicted=z0+slope*(s-s0)
            lateral_anchors.append((lateral,z-predicted))
        def lateral_offset(xy):
            lat=float(np.cross(direction,np.asarray(xy)-center))
            weights=[1.0/(abs(lat-a)+2.0)**2 for a,_ in lateral_anchors]
            return float(sum(w*delta for w,(_,delta) in zip(weights,lateral_anchors))/sum(weights))
        def surface(xy):
            s=float(np.dot(np.asarray(xy)-center,direction))
            return z0+slope*(s-s0)+lateral_offset(xy)
        group['height_solution']=surface
        group['height_solution_metadata']={'left_station':s0,'right_station':s1,
                                           'left_z':z0,'right_z':z1,'grade':slope}
        for r in members:
            r['profile_xyz']=[[float(p[0]),float(p[1]),float(surface(p[:2]))]
                              for p in r['profile_xyz']]
            r['start_xyz']=r['profile_xyz'][0]
            r['end_xyz']=r['profile_xyz'][-1]
            r['group_id']=group['id']
        # All original OSM nodes within the grouped footprint use the same
        # surface, including switches and nodes on internal inferred spans.
        count=0
        for node in result.get('nodes',{}).values():
            xy=node.get('xy')
            if xy is None or not group['footprint'].covers(Point(xy)):
                continue
            corrected=float(surface(xy))
            node['corrected_z_m']=corrected
            raw=node.get('dem_z_m')
            node['delta_z_m']=corrected-float(raw) if raw is not None else None
            count+=1
        diagnostics.append({'group':group['id'],'status':'solved','nodes_corrected':count,
                            'left_z':z0,'right_z':z1,'grade':slope,
                            'members':len(members)})
    result['group_elevation_diagnostics']=diagnostics
    result['method']=result.get('method','')+' + group outer-anchor elevation reconciliation'
    return diagnostics
