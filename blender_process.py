import subprocess
import sys
from pathlib import Path

BLENDER_EXE = r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"

subprocess.run([
    BLENDER_EXE,
    "--background",
    "--factory-startup",
    "--python", str(Path("blender_terrain_to_glb.py").resolve()),
    "--",
    "--input", r"testObjs\terrain.obj",
    "--output", r"testObjs\terrain.glb",
    "--ratio", "0.5",
    "--merge-distance", "0.0001",
], check=True)