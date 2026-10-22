import numpy as np
from shapely.geometry import box
from group_elevation_solution import solve_group_elevations

class FakeDEM:
    nodata = None
    def sample(self, xy, indexes=1, masked=True):
        for x,y in xy:
            # Outer stable approaches near 0 and 100; nested bad DEM at center
            z=100 + 0.002*x + 0.01*y
            if 25 < x < 75: z-=10
            yield np.ma.array([z])

outer={'id':'outer','profile_xyz':[[0.,0.,100.],[50.,0.,90.],[100.,0.,100.2]]}
nested={'id':'nested','profile_xyz':[[30.,2.,90.],[50.,2.,80.],[70.,2.,90.]]}
group={'id':'group1','members':[outer,nested],'footprint':box(-3,-4,103,6)}
result={'nodes':{'switch':{'xy':[50,1], 'dem_z_m':90.}}}
d=solve_group_elevations([group],result,FakeDEM())
assert d[0]['status']=='solved',d
assert min(p[2] for p in outer['profile_xyz']+nested['profile_xyz'])>99
assert result['nodes']['switch']['corrected_z_m']>99
assert abs(outer['profile_xyz'][0][2]-outer['profile_xyz'][-1][2])>0.1
print('PASS: nested span depression removed; switch corrected; longitudinal grade retained')
