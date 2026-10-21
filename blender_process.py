"""Run from normal Python (including Git Bash); Blender does the actual mesh work."""
import subprocess
from pathlib import Path

BLENDER_EXE = r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe'
SCRIPT = Path(__file__).with_name('blender_terrain_to_glb_optimized.py')

subprocess.run([
    BLENDER_EXE,
    '--background', '--factory-startup',
    '--python', str(SCRIPT.resolve()), '--',
    '--input', r'terrain_output\terrain.obj',
    '--output', r'terrain_output\terrain_optimized_baked.glb',
    '--ratio', '0.01',
    '--merge-distance', '0.001',
    '--texture-scale', '0.25',
    #Uncomment these three lines to bake the materials into a new atlas:
    '--bake',
    '--atlas-size', '8192',
    '--bake-margin', '4',
], check=True)
