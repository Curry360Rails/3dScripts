"""Download OSM railways, switches and roads within a WGS84 GeoJSON boundary.

Produces separate railway-only and complete-infrastructure OSM JSON,
GeoJSON and KML exports. No DEM heights are inferred here.
"""
import argparse
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import requests


def load_polygon(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if data.get('type') == 'FeatureCollection':
        features = data.get('features', [])
        if len(features) != 1:
            raise ValueError('Boundary must contain exactly one polygon feature')
        data = features[0]['geometry']
    elif data.get('type') == 'Feature':
        data = data['geometry']
    if data.get('type') != 'Polygon':
        raise ValueError('Boundary must be a GeoJSON Polygon in WGS84')
    ring = data['coordinates'][0]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        raise ValueError('Boundary requires at least three vertices')
    for lon, lat, *_ in ring:
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError('Invalid longitude/latitude in boundary')
    return ring


def make_features(elements, include_roads):
    nodes = {e['id']: [e['lon'], e['lat']] for e in elements
             if e.get('type') == 'node' and 'lon' in e and 'lat' in e}
    features = []
    skipped = 0
    for item in elements:
        kind = item.get('type')
        tags = item.get('tags') or {}
        if kind == 'way' and (tags.get('railway') == 'rail' or (include_roads and 'highway' in tags)):
            node_ids = item.get('nodes', [])
            if len(node_ids) < 2 or any(n not in nodes for n in node_ids):
                skipped += 1
                continue
            geom = {'type': 'LineString', 'coordinates': [nodes[n] for n in node_ids]}
        elif kind == 'node' and tags.get('railway') == 'switch':
            if item['id'] not in nodes:
                skipped += 1
                continue
            geom = {'type': 'Point', 'coordinates': nodes[item['id']]}
        else:
            continue
        features.append({'type': 'Feature', 'id': f"{kind}/{item['id']}",
                         'properties': {'osm_id': item['id'], 'osm_type': kind, **tags},
                         'geometry': geom})
    return features, skipped


def write_geojson_kml(output, stem, features, title):
    (output / f'{stem}.geojson').write_text(
        json.dumps({'type': 'FeatureCollection', 'features': features}, indent=2),
        encoding='utf-8')
    namespace = 'http://www.opengis.net/kml/2.2'
    ET.register_namespace('', namespace)
    def tag(name):
        return f'{{{namespace}}}{name}'
    root = ET.Element(tag('kml'))
    document = ET.SubElement(root, tag('Document'))
    ET.SubElement(document, tag('name')).text = title
    for feature in features:
        mark = ET.SubElement(document, tag('Placemark'))
        ET.SubElement(mark, tag('name')).text = feature['id']
        ext = ET.SubElement(mark, tag('ExtendedData'))
        for key, value in feature['properties'].items():
            entry = ET.SubElement(ext, tag('Data'), name=str(key))
            ET.SubElement(entry, tag('value')).text = str(value)
        geom = feature['geometry']
        if geom['type'] == 'LineString':
            line = ET.SubElement(mark, tag('LineString'))
            ET.SubElement(line, tag('tessellate')).text = '1'
            ET.SubElement(line, tag('coordinates')).text = ' '.join(
                f'{lon},{lat}' for lon, lat in geom['coordinates'])
        else:
            point = ET.SubElement(mark, tag('Point'))
            lon, lat = geom['coordinates']
            ET.SubElement(point, tag('coordinates')).text = f'{lon},{lat}'
    ET.ElementTree(root).write(output / f'{stem}.kml', encoding='utf-8', xml_declaration=True)


def export_data(data, output):
    output.mkdir(parents=True, exist_ok=True)
    elements = data.get('elements', [])
    rail_ways = [e for e in elements if e.get('type') == 'way'
                 and (e.get('tags') or {}).get('railway') == 'rail']
    switch_nodes = [e for e in elements if e.get('type') == 'node'
                    and (e.get('tags') or {}).get('railway') == 'switch']
    rail_node_ids = {nid for way in rail_ways for nid in way.get('nodes', [])}
    rail_node_ids.update(e['id'] for e in switch_nodes)
    rail_nodes = [e for e in elements if e.get('type') == 'node' and e['id'] in rail_node_ids]
    rail_data = {k: v for k, v in data.items() if k != 'elements'}
    rail_data['elements'] = rail_nodes + rail_ways
    (output / 'infrastructure_raw_osm.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    (output / 'railway_raw_osm.json').write_text(json.dumps(rail_data, indent=2), encoding='utf-8')
    rail_features, rail_skipped = make_features(rail_data['elements'], include_roads=False)
    infrastructure_features, infra_skipped = make_features(elements, include_roads=True)
    write_geojson_kml(output, 'railway', rail_features, 'OSM railway tracks and switches')
    write_geojson_kml(output, 'infrastructure', infrastructure_features, 'OSM railway, roads and switches')
    roads = sum(e.get('type') == 'way' and 'highway' in (e.get('tags') or {}) for e in elements)
    print(f'OSM elements: {len(elements):,}; railways: {len(rail_ways):,}; roads: {roads:,}; switches: {len(switch_nodes):,}')
    print(f'Rail features: {len(rail_features):,} (skipped {rail_skipped}); infrastructure features: {len(infrastructure_features):,} (skipped {infra_skipped})')
    print(f'Saved six files to {output.resolve()}')


def main():
    parser = argparse.ArgumentParser(description='Download OSM railways and road infrastructure for a GeoJSON boundary')
    parser.add_argument('--boundary', type=Path, default=Path(__file__).resolve().with_name('boundary.geojson'))
    parser.add_argument('--output', type=Path, default=Path('osm_output'))
    args = parser.parse_args()
    polygon = load_polygon(args.boundary)
    poly_string = ' '.join(f'{lat} {lon}' for lon, lat, *_ in polygon)
    query = f'''[out:json][timeout:180];
(
  way["railway"="rail"](poly:"{poly_string}");
  way["highway"](poly:"{poly_string}");
  node["railway"="switch"](poly:"{poly_string}");
);
out body;
>;
out skel qt;
'''
    headers = {
        'User-Agent': 'RailInfrastructureExtractor/1.0 (contact: currybuscher@gmail.com)',
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip, deflate',
    }
    response = requests.post('https://overpass-api.de/api/interpreter',
                             data={'data': query}, headers=headers, timeout=240)
    print('HTTP status:', response.status_code)
    if not response.ok:
        print('Server response:', response.text[:3000])
        response.raise_for_status()
    data = response.json()
    if not isinstance(data.get('elements'), list):
        raise ValueError('Overpass response is missing elements')
    export_data(data, args.output)


if __name__ == '__main__':
    main()
