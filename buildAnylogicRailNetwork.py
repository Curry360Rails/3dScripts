"""Reconstruct cropped OSM railway geometry in EPSG:26911 and sample DEM elevations.

pip install rasterio pyproj shapely numpy
python 01_build_rail_3d_geojson.py --dem path/to/dem.tif --spacing 1

The cropped GeoJSON is the inclusion authority: excluded parts of original OSM ways
are never restored. OSM node IDs are attached only when their projected positions
match the retained geometry. Z values are DEM-derived, not surveyed rail heights.
"""
import argparse
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from shapely.geometry import LineString, Point

HERE = Path(__file__).resolve().parent
CRS = 'EPSG:26911'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def geometry_parts(feature):
    geom = feature['geometry']
    if geom['type'] == 'LineString':
        return [geom['coordinates']]
    if geom['type'] == 'MultiLineString':
        return geom['coordinates']
    return []


def densify(points, spacing):
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        length = math.dist(a[:2], b[:2])
        n = max(1, math.ceil(length / spacing))
        for j in range(1, n + 1):
            t = j / n
            out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
    return out


def sample_z(dem, points, mode):
    """Return sampled heights, raising on nodata or outside DEM coverage."""
    if mode == 'nearest':
        samples = list(dem.sample(points, indexes=1, masked=True))
        values = np.array([float(s[0]) if not np.ma.is_masked(s[0]) else np.nan for s in samples])
    else:
        # Bilinear interpolation between pixel centers; reads a minimal 2x2 window.
        from rasterio.windows import Window
        inv = ~dem.transform
        values = []
        for x, y in points:
            col, row = inv * (x, y)
            col -= 0.5
            row -= 0.5
            c, r = math.floor(col), math.floor(row)
            dx, dy = col - c, row - r
            if c < 0 or r < 0 or c + 1 >= dem.width or r + 1 >= dem.height:
                values.append(np.nan)
                continue
            block = dem.read(1, window=Window(c, r, 2, 2), masked=True)
            if np.ma.getmaskarray(block).any():
                values.append(np.nan)
            else:
                values.append(float((1-dx)*(1-dy)*block[0,0] + dx*(1-dy)*block[0,1] +
                                    (1-dx)*dy*block[1,0] + dx*dy*block[1,1]))
        values = np.asarray(values)
    if not np.isfinite(values).all():
        raise ValueError(f'{np.count_nonzero(~np.isfinite(values))} DEM samples are invalid. Check coverage/nodata.')
    return values.tolist()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dem', required=True)
    ap.add_argument('--osm', default=str(HERE / 'victorvilleRailData.json'))
    ap.add_argument('--lines', default=str(HERE / 'croppedVictorvilleRailData_NAD83_UTM.geojson'))
    ap.add_argument('--switches', default=str(HERE / 'victorvilleRailDataSwitches.geojson'))
    ap.add_argument('--output', default=str(HERE / 'rail_3d_output'))
    ap.add_argument('--spacing', type=float, default=1.0)
    ap.add_argument('--node-tolerance', type=float, default=0.30, help='OSM node to cropped geometry match in meters')
    ap.add_argument('--sample', choices=['nearest','bilinear'], default='bilinear')
    args = ap.parse_args()
    if args.spacing <= 0 or args.node_tolerance <= 0:
        ap.error('Spacing and node tolerance must be positive')
    osm, cropped, switch_fc = load(args.osm), load(args.lines), load(args.switches)
    to_utm = Transformer.from_crs('EPSG:4326', CRS, always_xy=True)
    nodes = {e['id']: to_utm.transform(e['lon'], e['lat']) for e in osm['elements'] if e['type']=='node'}
    ways = {e['id']: e for e in osm['elements'] if e['type']=='way'}
    tagged_switch_ids = {e['id'] for e in osm['elements'] if e['type']=='node' and e.get('tags',{}).get('railway')=='switch'}
    line_features = []
    matched_nodes = set()
    for f in cropped['features']:
        props = dict(f.get('properties') or {})
        osm_id = props.get('@id') or props.get('id')
        if not osm_id or not str(osm_id).startswith('way/'):
            raise ValueError(f'Line feature missing OSM way ID: {osm_id}')
        way_id = int(str(osm_id).split('/')[-1])
        if way_id not in ways:
            raise ValueError(f'Cropped way {way_id} absent from OSM JSON')
        way_nodes = ways[way_id]['nodes']
        for part_i, part in enumerate(geometry_parts(f)):
            if len(part) < 2: continue
            source_line = LineString([p[:2] for p in part])
            # Build ordered original nodes along the *retained* geometry, inserting
            # their exact projected positions. Cropped endpoints remain unchanged.
            candidates = []
            for nid in way_nodes:
                xy = nodes.get(nid)
                if xy is None: continue
                pt = Point(xy)
                along = source_line.project(pt)
                if pt.distance(source_line) <= args.node_tolerance and 1e-6 < along < source_line.length-1e-6:
                    candidates.append((along, nid, xy))
            events = [(0., None, list(part[0][:2]))]
            events += sorted(candidates)
            events += [(source_line.length, None, list(part[-1][:2]))]
            # For every original cropped vertex, retain its precise XY geometry.
            for p in part[1:-1]:
                xy = list(p[:2]); events.append((source_line.project(Point(xy)), None, xy))
            events.sort(key=lambda v:v[0])
            anchors = []
            for distance, nid, xy in events:
                if anchors and math.dist(anchors[-1][2], xy) < 1e-7:
                    if nid is not None: anchors[-1] = (distance,nid,xy)
                else:
                    anchors.append((distance,nid,xy))
            # Associate retained endpoints with exact OSM IDs if they coincide.
            for i in (0, len(anchors)-1):
                d,nid,xy = anchors[i]
                matches = [(math.dist(xy,nodes[k]),k) for k in way_nodes if k in nodes and math.dist(xy,nodes[k]) <= args.node_tolerance]
                if matches:
                    _, nid = min(matches)
                    anchors[i] = (d,nid,xy)
            coords=[]; vertex_ids=[]
            for j,(distance,nid,xy) in enumerate(anchors):
                if j:
                    a=anchors[j-1][2]; b=xy
                    n=max(1,math.ceil(math.dist(a,b)/args.spacing))
                    for step in range(1,n):
                        t=step/n
                        coords.append([a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])]);vertex_ids.append(None)
                coords.append(list(xy));vertex_ids.append(nid)
                if nid is not None: matched_nodes.add(nid)
            # Collapse duplicate XY vertices (rare OSM duplicate nodes).
            compact=[]; compact_ids=[]
            for xy,nid in zip(coords,vertex_ids):
                if compact and math.dist(compact[-1],xy)<1e-8:
                    if nid is not None: compact_ids[-1]=nid
                else:compact.append(xy);compact_ids.append(nid)
            line_features.append({'type':'Feature','properties':{'osm_way_id':way_id,'source_part':part_i,
                'osm_node_ids':compact_ids,'railway':props.get('railway'),'service':props.get('service'),
                'bridge':props.get('bridge'),'layer':props.get('layer')},
                'geometry':{'type':'LineString','coordinates':compact}})
    switch_features=[]
    for f in switch_fc['features']:
        sid=str((f.get('properties') or {}).get('@id') or (f.get('properties') or {}).get('id',''))
        if not sid.startswith('node/'):continue
        nid=int(sid.split('/')[-1])
        if nid not in tagged_switch_ids or nid not in nodes:continue
        p=nodes[nid]
        switch_features.append({'type':'Feature','properties':{'osm_node_id':nid,'railway':'switch',
            'in_retained_lines':nid in matched_nodes},'geometry':{'type':'Point','coordinates':list(p)}})
    with rasterio.open(args.dem) as dem:
        if dem.crs is None or dem.crs.to_epsg()!=26911:
            raise ValueError(f'DEM must use EPSG:26911, found {dem.crs}')
        all_xy=[p for f in line_features for p in f['geometry']['coordinates']]
        all_xy += [f['geometry']['coordinates'] for f in switch_features]
        zs=sample_z(dem,all_xy,args.sample)
        cursor=0
        for f in line_features:
            coords=f['geometry']['coordinates'];n=len(coords)
            f['geometry']['coordinates']=[p+[z] for p,z in zip(coords,zs[cursor:cursor+n])];cursor+=n
        for f in switch_features:
            f['geometry']['coordinates'].append(zs[cursor]);cursor+=1
        origin={'crs':CRS,'origin_x':dem.bounds.left,'origin_y':dem.bounds.bottom,
                'base_elevation':float(min(zs)),'dem':str(Path(args.dem).resolve()),
                'vertical_units':'DEM native units (verify meters and vertical datum)',
                'sample_method':args.sample,'spacing_m':args.spacing}
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    for name,features in [('rail_lines_3d.geojson',line_features),('rail_switches_3d.geojson',switch_features)]:
        (out/name).write_text(json.dumps({'type':'FeatureCollection','name':name,'crs':{'type':'name','properties':{'name':CRS}},'features':features}),encoding='utf-8')
    (out/'georeference.json').write_text(json.dumps(origin,indent=2),encoding='utf-8')
    counts=Counter(nid for f in line_features for nid in f['properties']['osm_node_ids'] if nid is not None)
    print(f'Wrote {len(line_features)} cropped line parts and {len(switch_features)} tagged switch points to {out}')
    print(f'Matched {len(matched_nodes)} original OSM node IDs; {sum(v>1 for v in counts.values())} occur on multiple retained lines.')
    print(f'Switches on retained lines: {sum(f["properties"]["in_retained_lines"] for f in switch_features)}')
    print('DEM elevations are terrain samples, not surveyed rail elevations.')

if __name__=='__main__': main()
