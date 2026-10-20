from anylogic_design_time_api import Workspace
from anylogic_design_time_api.generated import RailTrack, RailroadSwitch

ws = Workspace('d68e12ab-4988-447b-80b1-fe92bc82fc77')
level = ws.getModel("CEMEX_Victorville").getAgent("Main").getLevel("level")
network = level.getChild("railwayNetwork1")

targets = {
    "railwaySwitch91",
    "railwaySwitch92",
    "railwaySwitch93"
}

for element in network.getChildren():
    if str(element.item.typeName()) != "RailroadSwitch":
        continue

    sw = RailroadSwitch(element.pyWorkspace, element.item)

    if sw.getName() not in targets:
        continue

    print("\nSWITCH:", sw.getName())
    print("Position:", sw.getX(), sw.getY(), sw.getZ())
    print("Type:", sw.getType())

    for label, getter in (
        ("Incoming", sw.getIncomingConnections),
        ("Outgoing", sw.getOutgoingConnections)
    ):
        try:
            connections = getter()
            print(label, len(connections))

            for c in connections:
                print("  ", c.getName(), c.item.typeName())
        except Exception as exc:
            print(label, "ERROR:", exc)