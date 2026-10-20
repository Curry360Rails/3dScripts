import requests
import json

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
    "https://overpass-api.de/api/interpreter",
    data={"data": query},
    timeout=240
)

response.raise_for_status()
data = response.json()

with open("railway_raw_osm.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)