# MAINTAINER NOTES
# Overpass OSM acquisition. GeoJSON coordinate pairs are longitude/latitude; Overpass polygon
# syntax uses latitude/longitude. API availability and OSM feature completeness vary by area.
# These comments describe the existing implementation; no executable statements were changed.

import requests
import json
from pathlib import Path
from xml.etree import ElementTree as ET

# The project boundary is shared with the 3DEP and NAIP download stages.
import argparse

# PURPOSE: Load boundary geometry and convert coordinate pairs into Overpass polygon query syntax.
# INPUTS: path.
def load_polygon(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        if len(features) != 1:
            raise ValueError("Boundary must contain exactly one polygon feature")
        data = features[0]["geometry"]
    elif data.get("type") == "Feature":
        data = data["geometry"]
    if data.get("type") != "Polygon":
        raise ValueError("Boundary must be a GeoJSON Polygon in WGS84")
    ring = data["coordinates"][0]
    if len(ring) < 4:
        raise ValueError("Boundary polygon requires at least three vertices")
    if ring[0] == ring[-1]:
        ring = ring[:-1]
    for lon, lat, *_ in ring:
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("Invalid longitude/latitude in boundary")
    return ring

parser = argparse.ArgumentParser(description="Download OSM railways for a GeoJSON boundary")
parser.add_argument("--output",type=Path,default=Path("osm_output"))
parser.add_argument("--boundary", type=Path, default=Path(__file__).resolve().with_name("boundary.geojson"))
args = parser.parse_args()
polygon = load_polygon(args.boundary)
poly_string = " ".join(f"{lat} {lon}" for lon, lat, *_ in polygon)

url = "https://overpass-api.de/api/interpreter"

headers = {
    "User-Agent": "VictorvilleRailOSMExtractor/1.0 (contact: currybuscher@gmail.com)",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

query = f"""
[out:json][timeout:180];
(
  way["railway"="rail"](poly:"{poly_string}");
  node["railway"="switch"](poly:"{poly_string}");
);
out body;
>;
out skel qt;
"""

response = requests.post(
    url,
    data={"data": query},
    headers=headers,
    timeout=240
)
print("HTTP status:", response.status_code)

if response.status_code != 200:
    print("Server response:")
    print(response.text[:3000])
    response.raise_for_status()
data = response.json()

output = args.output
output.mkdir(parents=True, exist_ok=True)

# Keep the complete Overpass response, including untagged nodes needed by ways.
(output / "railway_raw_osm.json").write_text(
    json.dumps(data, indent=2), encoding="utf-8"
)

nodes = {
    item["id"]: [item["lon"], item["lat"]]
    for item in data.get("elements", [])
    if item.get("type") == "node" and "lon" in item and "lat" in item
}

features = []
skipped = 0
for item in data.get("elements", []):
    kind = item.get("type")
    tags = item.get("tags", {})
    if kind == "way" and tags.get("railway") == "rail":
        node_ids = item.get("nodes", [])
        if any(node_id not in nodes for node_id in node_ids):
            skipped += 1
            continue
        coordinates = [nodes[node_id] for node_id in node_ids]
        if len(coordinates) < 2:
            skipped += 1
            continue
        geometry = {"type": "LineString", "coordinates": coordinates}
    elif kind == "node" and tags.get("railway") == "switch":
        if item["id"] not in nodes:
            skipped += 1
            continue
        geometry = {"type": "Point", "coordinates": nodes[item["id"]]}
    else:
        continue

    features.append({
        "type": "Feature",
        "id": f"{kind}/{item['id']}",
        "properties": {"osm_id": item["id"], "osm_type": kind, **tags},
        "geometry": geometry,
    })

geojson = {"type": "FeatureCollection", "features": features}
(output / "railway.geojson").write_text(
    json.dumps(geojson, indent=2), encoding="utf-8"
)

# KML requires lon,lat ordering. OSM data has no surveyed Z values.
namespace = "http://www.opengis.net/kml/2.2"
ET.register_namespace("", namespace)
# PURPOSE: Helper for retrieving or formatting an OSM element tag.
# INPUTS: name.
def tag(name):
    return f"{{{namespace}}}{name}"

root = ET.Element(tag("kml"))
document = ET.SubElement(root, tag("Document"))
ET.SubElement(document, tag("name")).text = "OSM railway tracks and switches"
for feature in features:
    mark = ET.SubElement(document, tag("Placemark"))
    ET.SubElement(mark, tag("name")).text = feature["id"]
    props = feature["properties"]
    ext = ET.SubElement(mark, tag("ExtendedData"))
    for key, value in props.items():
        entry = ET.SubElement(ext, tag("Data"), name=str(key))
        ET.SubElement(entry, tag("value")).text = str(value)
    geom = feature["geometry"]
    if geom["type"] == "LineString":
        line = ET.SubElement(mark, tag("LineString"))
        ET.SubElement(line, tag("tessellate")).text = "1"
        ET.SubElement(line, tag("coordinates")).text = " ".join(
            f"{lon},{lat}" for lon, lat in geom["coordinates"]
        )
    else:
        point = ET.SubElement(mark, tag("Point"))
        lon, lat = geom["coordinates"]
        ET.SubElement(point, tag("coordinates")).text = f"{lon},{lat}"

ET.ElementTree(root).write(
    output / "railway.kml", encoding="utf-8", xml_declaration=True
)

print(f"Downloaded {len(data['elements']):,} OSM elements")
print(f"Exported {len(features):,} features; skipped {skipped} incomplete ways/nodes")
print(f"Files saved to {output.resolve()}")