"""Pure geometry and DSU checks; never connect to the AnyLogic API."""
import buildAnylogicRailNetwork as network


def test_dsu_groups_connected_items():
    d=network.DSU(5)
    d.union(0,1);d.union(1,2)
    assert d.find(0)==d.find(2)
    assert d.find(3)!=d.find(0)


def test_clean_points_preserves_endpoints_and_ids():
    pts=[(0.,0.,0.),(0.01,0.,0.),(2.,0.,0.)]
    coords,ids=network.clean_points(pts,[100,None,200],tolerance=.1)
    assert coords==[pts[0],pts[-1]]
    assert ids==[100,200]


def test_validate_polyline_flags_invalid_geometry():
    assert network.validate_polyline([(0,0,0),(1,0,0)]) is None
    assert network.validate_polyline([(0,0,0)]) is not None
    assert network.validate_polyline([(0,0,0),(0.001,0,0)]) is not None
    assert network.validate_polyline([(0,0,float('nan')),(1,0,0)]) is not None


def test_anylogic_coordinate_conversion_uses_division():
    """Extract the nested cv() without invoking Workspace or mutating AnyLogic."""
    import ast
    import inspect
    tree=ast.parse(inspect.getsource(network))
    build=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    cv=next(n for n in ast.walk(build) if isinstance(n,ast.FunctionDef) and n.name=='cv')
    mod=ast.fix_missing_locations(ast.Module(body=[cv],type_ignores=[]))
    scope={'ox':100.0,'oy':200.0,'base':1000.0,'MODEL_UNITS_PER_METER':4.0,'FLIP_Y':True}
    exec(compile(mod,'<cv-under-test>','exec'),scope)
    assert scope['cv']((500.0,100.0,1020.0))==(100.0,25.0,5.0)
