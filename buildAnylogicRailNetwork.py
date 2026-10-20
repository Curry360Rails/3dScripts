"""Controlled AnyLogic Design-Time API RailTrack point-list test.

Creates isolated temporary tracks on Main/level (no switches).
Tests direct setPointList versus Utils.createStraightPointsList.
Dry-run by default; edit TOKEN, then set RUN_TESTS=True.
Created tracks are deleted by default in finally, even on errors.
Run on a BACKUP of the model; an internal Java error may disrupt cleanup.
"""
import math
import traceback

from anylogic_design_time_api import Workspace
from anylogic_design_time_api.generated import RailTrack
from anylogic_design_time_api.Utils import Utils

TOKEN = 'd68e12ab-4988-447b-80b1-fe92bc82fc77'
MODEL_NAME = 'CEMEX_Victorville'
AGENT_NAME = 'Main'
LEVEL_NAME = 'level'
RUN_TESTS = True
DELETE_AFTER_TEST = False
TEST_PREFIX = 'apiPointListTest_'
# Test placement in model coordinate units. No switches are connected.
BASE_X, BASE_Y, BASE_Z = 20.0, 20.0, 2.0

# Relative XYZ coordinates, including a modest vertical profile.
POINTS_5 = [
    0.0, 0.0, 0.0,
    0.7, 0.4, 0.01,
    1.3, 0.8, 0.015,
    2.1, 1.0, 0.005,
    3.0, 1.5, 0.02,
]
POINTS_3 = [
    0.0, 0.0, 0.0,
    1.5, 0.75, 0.01,
    3.0, 1.5, 0.02,
]


def summarize_points(label, track, expected):
    actual = list(track.getPointList())
    print(f'  {label}: readback {len(actual)//3} points / {len(actual)} values')
    print(f'  values: {actual}')
    if len(actual) != len(expected):
        print(f'  FAIL: expected {len(expected)} values, got {len(actual)}')
        return False
    max_error = max((abs(float(a) - float(b)) for a, b in zip(actual, expected)), default=0.0)
    print(f'  max coordinate error: {max_error:.12g}')
    return max_error < 1e-7


def set_geometry(track, coords, mode):
    if mode == 'direct':
        prepared = list(coords)
    elif mode == 'utility':
        prepared = Utils.createStraightPointsList(track, list(coords))
        print(f'  utility output length: {len(prepared)}; first values: {prepared[:12]}')
    else:
        raise ValueError(mode)
    track.setPointList(prepared)
    return prepared


def run_case(level, mode, index, created):
    name = f'{TEST_PREFIX}{mode}'
    print(f'\n=== {name} ===', flush=True)
    track = RailTrack.create(level)
    created.append(track)  # register immediately, even if property setting fails
    track.setName(name)
    track.setX(BASE_X)
    track.setY(BASE_Y + index * 5.0)
    track.setZ(BASE_Z)
    print('  created and positioned')

    print('  phase 1: assign five points', flush=True)
    prepared = set_geometry(track, POINTS_5, mode)
    print('  readback matches prepared:', summarize_points('phase 1', track, prepared))
    print('  readback matches original XYZ:', summarize_points('phase 1 original', track, POINTS_5))

    print('  phase 2: replace with three points', flush=True)
    prepared = set_geometry(track, POINTS_3, mode)
    print('  readback matches prepared:', summarize_points('phase 2', track, prepared))

    print('  phase 3: replace with five points again', flush=True)
    prepared = set_geometry(track, POINTS_5, mode)
    print('  readback matches prepared:', summarize_points('phase 3', track, prepared))
    print('  final source:', track.getSource())
    print('  final target:', track.getTarget())
    print('  PASS: API calls completed (visual inspection still required)')


def main():
    if not RUN_TESTS:
        print('DRY RUN: set TOKEN and RUN_TESTS=True to create temporary tracks.')
        print('Tests: direct point list; straight-point-list utility; 5 -> 3 -> 5 point updates.')
        return
    if TOKEN == 'PASTE_ANYLOGIC_CONNECTOR_TOKEN':
        raise ValueError('Set TOKEN before running')
    ws = Workspace(TOKEN)
    level = ws.getModel(MODEL_NAME).getAgent(AGENT_NAME).getLevel(LEVEL_NAME)
    created = []
    try:
        for index, mode in enumerate(('direct', 'utility')):
            try:
                run_case(level, mode, index, created)
            except Exception:
                print(f'  FAIL in {mode}:')
                traceback.print_exc()
    finally:
        if DELETE_AFTER_TEST:
            for track in reversed(created):
                try:
                    name = track.getName()
                    track.delete()
                    print(f'Deleted test track: {name}')
                except Exception as exc:
                    print(f'WARNING: could not delete a test track: {exc}')
        else:
            print('Test tracks retained for editor/3D preview inspection.')
    print('\nIf both tests fail with Path.updateSegments, compare the initial create/setPointList behavior.')
    print('If only direct fails, prefer Utils.createStraightPointsList in the importer.')
    print('If both pass, the failure may depend on switch attachment or a different point-count sequence.')


if __name__ == '__main__':
    main()
