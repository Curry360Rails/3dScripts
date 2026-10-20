import math
from anylogic_design_time_api import Workspace
from anylogic_design_time_api.generated import RailTrack

TOKEN = 'd68e12ab-4988-447b-80b1-fe92bc82fc77'
MODEL_NAME = "CEMEX_Victorville"

ws = Workspace(TOKEN)
agent = ws.getModel(MODEL_NAME).getAgent("Main")
level = agent.getLevel("level")
yard = level.getChild("railwayNetwork")

TARGETS = {"osmRail0161", "osmRail0163"}

for network in level.getChildren():

    if str(network.item.typeName()) != "Railyard":
        continue

    print(f"\nNetwork: {network.getName()}")

    for element in network.getChildren():

        if str(element.item.typeName()) != "RailTrack":
            continue

        track = RailTrack(element.pyWorkspace, element.item)
        name = track.getName()

        if name not in TARGETS:
            continue

        print(f"\nFOUND: {name}")
        print("Network:", network.getName())
        print("Position:", track.getX(), track.getY(), track.getZ())

        raw = track.getPointList()
        pts = [
            tuple(raw[i:i+3])
            for i in range(0, len(raw), 3)
        ]

        print("Point count:", len(pts))

        for i, p in enumerate(pts):
            if i == 0:
                print(i, p)
            else:
                print(
                    i, p,
                    "XY length:",
                    math.dist(pts[i-1][:2], p[:2]),
                    "3D length:",
                    math.dist(pts[i-1], p)
                )

        for label, getter in (
            ("Source", track.getSource),
            ("Target", track.getTarget)
        ):
            try:
                connected = getter()
                print(
                    label,
                    connected.getName() if connected else None
                )
            except Exception as e:
                print(label, "ERROR:", e)