import requests
import json
from pathlib import Path
from xml.etree import ElementTree as ET

polygon = [
  {
    "lat": 34.4980686189572,
    "lng": -117.30033874511719
  },
  {
    "lat": 34.496370914707285,
    "lng": -117.25982666015626
  },
  {
    "lat": 34.53433110273239,
    "lng": -117.26428985595705
  },
  {
    "lat": 34.57222098601774,
    "lng": -117.26291656494142
  },
  {
    "lat": 34.60105097018313,
    "lng": -117.18738555908205
  },
  {
    "lat": 34.59596405277047,
    "lng": -117.10292816162111
  },
  {
    "lat": 34.62704590420544,
    "lng": -117.07889556884767
  },
  {
    "lat": 34.6428649215081,
    "lng": -117.1084213256836
  },
  {
    "lat": 34.636650667365934,
    "lng": -117.12902069091797
  },
  {
    "lat": 34.62987094998342,
    "lng": -117.15167999267578
  },
  {
    "lat": 34.63100094136006,
    "lng": -117.18601226806642
  },
  {
    "lat": 34.628175934059875,
    "lng": -117.24163055419923
  },
  {
    "lat": 34.60331172236259,
    "lng": -117.29244232177736
  },
  {
    "lat": 34.593137852926574,
    "lng": -117.30823516845705
  },
  {
    "lat": 34.639475386064426,
    "lng": -117.3274612426758
  },
  {
    "lat": 34.635520752949176,
    "lng": -117.35698699951173
  },
  {
    "lat": 34.58013609530447,
    "lng": -117.34874725341798
  },
  {
    "lat": 34.552429915988604,
    "lng": -117.32677459716798
  }
]

# poly_string = " ".join(
#     f"{lat} {lon}" for lat, lon in polygon
# )

poly_string = " ".join(
    f"{point['lat']} {point['lng']}" for point in polygon
)

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

output = Path("osm_output")
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