"""Heuristic DEM-anomaly correction for OSM railway alignments.

No engineering-grade assertion: inferred spans require review. Uses rasterio for CRS
transforms, and the original OSM node IDs for a shared AnyLogic elevation profile.
"""
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from rasterio.warp import transform


def _sample(dem, coords):
    result = []
    for item in dem.sample(coords, indexes=1, masked=True):
        z = item[0]
        result.append(float(z) if not np.ma.is_masked(z) and math.isfinite(float(z))
                      and (dem.nodata is None or float(z) != dem.nodata) else float('nan'))
    return np.array(result, dtype=float)


def _chains(ways):
    """Join unbranched OSM ways through degree-two shared endpoint nodes."""
    ends = defaultdict(list)
    for i, w in enumerate(ways):
        ends[w['nodes'][0]].append(i)
        ends[w['nodes'][-1]].append(i)
    visited = set()
    for start in range(len(ways)):
        if start in visited:
            continue
        w = ways[start]
        # Prefer starting at a non-degree-two end.
        node = next((n for n in (w['nodes'][0], w['nodes'][-1]) if len(ends[n]) != 2), w['nodes'][0])
        chain = []
        members = []
        current = start
        while current not in visited:
            visited.add(current)
            way = ways[current]
            seq = list(way['nodes'])
            if seq[-1] == node:
                seq.reverse()
            elif seq[0] != node:
                break
            chain.extend(seq if not chain else seq[1:])
            members.append(way['id'])
            node = seq[-1]
            nxt = [i for i in ends[node] if i not in visited]
            if len(ends[node]) != 2 or len(nxt) != 1:
                break
            current = nxt[0]
        if len(chain) >= 2:
            yield chain, members


def _interpolate_profile(dist, z, spacing):
    samples = np.arange(0, dist[-1], spacing, dtype=float)
    if not len(samples) or samples[-1] != dist[-1]:
        samples = np.append(samples, dist[-1])
    return samples, np.interp(samples, dist, z)


def _detect_spans(s, z, cfg):
    """Pair suspicious DEM grade discontinuities; require a valley below a plausible chord."""
    spacing = cfg['sample_spacing_m']
    if len(s) < 4:
        return []
    # Candidate discontinuities are abrupt grade changes or implausibly steep grades.
    grade = np.diff(z) / np.maximum(np.diff(s), 1e-6)
    curvature = np.diff(grade)
    suspicious = np.zeros(len(s), dtype=bool)
    suspicious[:-1] |= np.abs(grade) > cfg['grade_limit']
    suspicious[1:] |= np.abs(grade) > cfg['grade_limit']
    # 0.05/0.10 percentage points per 100 ft are expressed as grade-fraction changes.
    sag = cfg['sag_change_pct_per_100ft'] / 100.0 * spacing / 30.48
    crest = cfg['crest_change_pct_per_100ft'] / 100.0 * spacing / 30.48
    suspicious[1:-1] |= (curvature > sag) | (curvature < -crest)
    events = np.flatnonzero(suspicious & np.isfinite(z))
    spans = []
    min_len, max_len = cfg['min_span_m'], cfg['max_span_m']
    max_steps = max(2, math.ceil(max_len / spacing))
    # Start with a discontinuity, find the shortest plausible span which bridges a dip.
    for i in events:
        if i < 1 or i >= len(s)-2:
            continue
        best = None
        for j in range(i+2, min(len(s)-1, i+max_steps)+1):
            length = s[j]-s[i]
            if length < min_len or length > max_len:
                continue
            if not np.isfinite(z[i:j+1]).all():
                continue
            chord = z[i] + (z[j]-z[i]) * (s[i:j+1]-s[i])/length
            delta = chord-z[i:j+1]
            depth = float(np.max(delta))
            if depth < cfg['min_depth_m'] or abs((z[j]-z[i])/length) > cfg['grade_limit']:
                continue
            # Most interior deviations must be positive: a DEM valley, not a crest.
            if np.sum(delta > cfg['min_depth_m']*0.35) < 2:
                continue
            # Require an actual sharp fall and rise, rather than smooth natural grade.
            segment_grade = grade[i:j]
            if not (np.min(segment_grade) < -cfg['grade_limit'] or
                    np.max(segment_grade) > cfg['grade_limit']):
                continue
            # Anchor stability: approaches should not themselves be sharp spikes.
            if i >= 2 and abs(grade[i-2]) > cfg['grade_limit']:
                continue
            if j < len(grade)-1 and abs(grade[j+1]) > cfg['grade_limit']:
                continue
            score = depth / (1 + 0.005*length)
            if best is None or score > best[0]:
                best = (score, i, j, depth)
        if best:
            spans.append(best)
    # Non-overlapping intervals, prefer the strongest discrepancy.
    chosen = []
    for _, i, j, depth in sorted(spans, reverse=True):
        if any(not (j <= a or i >= b) for a,b,_ in chosen):
            continue
        chosen.append((i,j,depth))
    # Refine both limits on the original sampled lattice (5 m by default).
    # Expand toward the first sustained departure from the plausible chord,
    # not merely the largest grade spike. This is deliberately local.
    refined = []
    pad_steps = max(1, round(cfg.get('boundary_search_m', 30.0) / spacing))
    departure = cfg.get('boundary_deviation_m', 0.5)
    for i, j, depth in sorted(chosen):
        left = max(0, i-pad_steps)
        right = min(len(s)-1, j+pad_steps)
        if not np.isfinite(z[left:right+1]).all():
            refined.append((i,j,depth)); continue
        # The bridge chord uses stable elevations at the candidate's ends.
        chord = z[i] + (z[j]-z[i]) * (s[left:right+1]-s[i]) / max(1e-6,s[j]-s[i])
        residual = chord-z[left:right+1]
        bad = residual >= departure
        # Look for the earliest sustained discrepancy adjacent to the detected span.
        a, b = i, j
        for k in range(i-1, left-1, -1):
            if bad[k-left] and (k+1 >= len(s) or bad[k+1-left]):
                a = k
            elif k < a-1:
                break
        for k in range(j+1, right+1):
            if bad[k-left] and bad[k-1-left]:
                b = k
            elif k > b+1:
                break
        # Preserve stable end anchors outside the anomaly, within available range.
        a = max(left, a-1)
        b = min(right, b+1)
        if s[b]-s[a] <= max_len and s[b]-s[a] >= min_len:
            refined.append((a,b,max(depth,float(np.max(np.interp(s[a:b+1],
                [s[a],s[b]],[z[a],z[b]])-z[a:b+1])))))
        else:
            refined.append((i,j,depth))
    # Merge newly overlapping intervals on the same railway chain.
    merged = []
    for a,b,depth in sorted(refined):
        if merged and a <= merged[-1][1]:
            old_a,old_b,old_depth = merged[-1]
            if s[max(b,old_b)]-s[old_a] <= max_len:
                merged[-1] = (old_a,max(b,old_b),max(depth,old_depth))
                continue
        merged.append((a,b,depth))
    return merged


def infer_profiles(osm_path, dem, cfg=None):
    cfg = dict(DEFAULTS, **(cfg or {}))
    data = json.loads(Path(osm_path).read_text(encoding='utf-8-sig'))
    elements = data.get('elements', [])
    nodes = {int(e['id']): (float(e['lon']), float(e['lat'])) for e in elements
             if e.get('type') == 'node' and 'lon' in e and 'lat' in e}
    ways = [e for e in elements if e.get('type') == 'way' and e.get('tags',{}).get('railway') == 'rail'
            and len(e.get('nodes', [])) >= 2 and all(n in nodes for n in e['nodes'])]
    all_ids = sorted({n for w in ways for n in w['nodes']})
    east, north = transform('EPSG:4326', dem.crs, [nodes[n][0] for n in all_ids],
                            [nodes[n][1] for n in all_ids])
    xy = dict(zip(all_ids, zip(east, north)))
    dem_at_nodes = dict(zip(all_ids, _sample(dem, [xy[n] for n in all_ids])))
    corrected = defaultdict(list)
    crossings = []
    chains_count = 0
    for ids, members in _chains(ways):
        chains_count += 1
        coords = [xy[n] for n in ids]
        distances = np.concatenate(([0.0], np.cumsum([math.dist(a,b) for a,b in zip(coords,coords[1:])])))
        if distances[-1] < cfg['min_span_m']:
            continue
        # Sample DEM every N meters along the entire continuous alignment.
        good = np.concatenate(([True], np.diff(distances)>1e-6))
        unique_s = distances[good]
        unique_xy = np.asarray(coords)[good]
        ss = np.arange(0, unique_s[-1], cfg['sample_spacing_m'])
        ss = np.append(ss, unique_s[-1])
        xx = np.interp(ss, unique_s, unique_xy[:,0])
        yy = np.interp(ss, unique_s, unique_xy[:,1])
        zz = _sample(dem, list(zip(xx,yy)))
        # Fill only short NoData gaps for screening; do not create spans over missing data.
        valid = np.isfinite(zz)
        if valid.sum() < 3:
            continue
        spans = []
        for run in np.split(np.flatnonzero(valid), np.where(np.diff(np.flatnonzero(valid))>1)[0]+1):
            if len(run) < 4:
                continue
            spans.extend((run[i],run[j],depth) for i,j,depth in
                         _detect_spans(ss[run], zz[run], cfg))
        # Refine only detected coarse intervals at 0.5 m (configurable).
        # Detect the first/last sustained departure, not a late grade spike.
        fine_spacing = float(cfg.get('boundary_spacing_m', 0.5))
        search_m = float(cfg.get('boundary_search_m', 30.0))
        departure = float(cfg.get('boundary_deviation_m', 0.5))
        if fine_spacing <= 0:
            raise ValueError('boundary_spacing_m must be positive')
        refined = []
        for i, j, depth in spans:
            left = max(0.0, float(ss[i]) - search_m)
            right = min(float(ss[-1]), float(ss[j]) + search_m)
            fs = np.arange(left, right, fine_spacing, dtype=float)
            fs = np.unique(np.r_[fs, right, ss[i], ss[j]])
            fx = np.interp(fs, unique_s, unique_xy[:, 0])
            fy = np.interp(fs, unique_s, unique_xy[:, 1])
            fz = _sample(dem, list(zip(fx, fy)))
            if not np.isfinite(fz).all():
                continue
            # Use the coarse anchors only to find the anomalous interior.
            # np.interp clamps outside the anchor interval; extrapolate instead.
            slope = (zz[j] - zz[i]) / max(1e-9, ss[j] - ss[i])
            expected = zz[i] + slope * (fs - ss[i])
            residual = expected - fz
            bad = residual >= departure
            sustain = max(2, int(math.ceil(2.0 / fine_spacing)))
            # Require a sustained run; isolated LiDAR/DEM noise is ignored.
            runs = []
            start = None
            for k, flag in enumerate(bad):
                if flag and start is None:
                    start = k
                if start is not None and (not flag or k == len(bad)-1):
                    stop = k if flag and k == len(bad)-1 else k-1
                    if stop-start+1 >= sustain:
                        runs.append((start, stop))
                    start = None
            # Retain only anomalous runs intersecting the coarse candidate.
            core = [(a,b) for a,b in runs if fs[b] >= ss[i] and fs[a] <= ss[j]]
            if not core:
                continue
            first, last = min(a for a,_ in core), max(b for _,b in core)
            # Include nearby runs belonging to the same depression, but not
            # unrelated anomalies farther away in the search window.
            for a,b in runs:
                if b < first and fs[first]-fs[b] <= 2.0:
                    first = a
                elif a > last and fs[a]-fs[last] <= 2.0:
                    last = b
            # Anchor just outside the first departure and final recovery.
            ia = max(0, first-1)
            ib = min(len(fs)-1, last+1)
            start_s, end_s = float(fs[ia]), float(fs[ib])
            if not cfg['min_span_m'] <= end_s-start_s <= cfg['max_span_m']:
                continue
            refined.append((start_s, end_s, depth))

        # Merge overlapping / nearly adjacent refined intervals on the SAME
        # continuous railway chain before creating deck geometry.
        merged = []
        for start_s, end_s, depth in sorted(refined):
            if merged and start_s <= merged[-1][1] + 2.0 and end_s-merged[-1][0] <= cfg['max_span_m']:
                a,b,d = merged[-1]
                merged[-1] = (a,max(b,end_s),max(d,depth))
            else:
                merged.append((start_s,end_s,depth))
        fixed = zz.copy()
        corrections = []
        for start_s, end_s, depth in merged:
            # Re-sample the *actual* fine-grid endpoints; don't round them
            # back to the coarse 5 m lattice.
            endpoint_xy = [(float(np.interp(t,unique_s,unique_xy[:,0])),
                            float(np.interp(t,unique_s,unique_xy[:,1])))
                           for t in (start_s,end_s)]
            endpoint_z = _sample(dem, endpoint_xy)
            if not np.isfinite(endpoint_z).all():
                continue
            corrections.append((start_s, end_s, endpoint_z.copy()))
            inside = (ss >= start_s) & (ss <= end_s)
            fixed[inside] = np.interp(ss[inside], [start_s,end_s], endpoint_z)
            fine_s = np.arange(start_s,end_s,fine_spacing,dtype=float)
            fine_s = np.unique(np.r_[fine_s,end_s])
            profile = [[float(np.interp(t,unique_s,unique_xy[:,0])),
                        float(np.interp(t,unique_s,unique_xy[:,1])),
                        float(np.interp(t,[start_s,end_s],endpoint_z))]
                       for t in fine_s]
            crossings.append({'id':f'inferred_{chains_count}_{len(crossings)+1}',
                'way_ids':members,'start_s_m':start_s,'end_s_m':end_s,
                'length_m':end_s-start_s,'max_dem_drop_m':float(depth),
                'start_xyz':profile[0],'end_xyz':profile[-1],
                'profile_xyz':profile,'type':'railway',
                'status':'inferred_review_required',
                'source':'DEM anomaly; not a surveyed bridge'})
        for nid, s in zip(ids, distances):
            # Keep original OSM node positions and sample interpolated corrected Z.
            z = float(np.interp(s, ss[valid], fixed[valid]))
            for start_s, end_s, endpoint_z in corrections:
                if start_s <= s <= end_s:
                    z = float(np.interp(s, [start_s, end_s], endpoint_z))
                    break
            if math.isfinite(z):
                corrected[nid].append(z)
    nodes_out = {}
    for nid in all_ids:
        raw = dem_at_nodes[nid]
        estimates = corrected.get(nid, [])
        z = float(np.median(estimates)) if estimates else float(raw)
        if math.isfinite(z):
            nodes_out[str(nid)] = {'xy':[float(v) for v in xy[nid]],
                'dem_z_m':float(raw) if math.isfinite(raw) else None,
                'corrected_z_m':z,'delta_z_m':z-float(raw) if math.isfinite(raw) else None}
    return {'crs':str(dem.crs),'method':'heuristic DEM grade anomaly / chord interpolation',
            'config':cfg,'nodes':nodes_out,'crossings':crossings,
            'chains_processed':chains_count,'rail_ways':len(ways)}


def save_profiles(output, result):
    out = Path(output)
    out.mkdir(parents=True,exist_ok=True)
    (out/'railway_elevation_profiles.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    with (out/'inferred_crossings.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.writer(f)
        writer.writerow(['id','way_ids','length_m','max_dem_drop_m','status','start_x','start_y','end_x','end_y'])
        for r in result['crossings']:
            writer.writerow([r['id'],';'.join(map(str,r['way_ids'])),round(r['length_m'],2),
                             round(r['max_dem_drop_m'],2),r['status'],*r['start_xyz'][:2],*r['end_xyz'][:2]])


DEFAULTS = {
    'sample_spacing_m':5.0, 'grade_limit':0.03,
    'sag_change_pct_per_100ft':0.05, 'crest_change_pct_per_100ft':0.10,
    'min_span_m':10.0, 'max_span_m':150.0, 'min_depth_m':1.5,
    'boundary_search_m':30.0, 'boundary_spacing_m':0.5, 'boundary_deviation_m':0.5,
}
