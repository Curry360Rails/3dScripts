"""Audit existing AnyLogic RailTracks; optionally delete invalid tracks.

Requires a running AnyLogic Design-Time API connector. DRY_RUN is True by
 default. This is intentionally fail-closed: unknown geometry or deletion
 interfaces are reported, never guessed at or silently ignored.

IMPORTANT: Save a backup of the AnyLogic model before setting DRY_RUN=False.
"""
import math
from collections import Counter

TOKEN = 'd68e12ab-4988-447b-80b1-fe92bc82fc77'
MODEL_NAME = 'CEMEX_Victorville'
AGENT_NAME = 'Main'
LEVEL_INDEX = 0
DRY_RUN = True
NAME_PREFIX = 'osmRail'  # Set None to examine ALL tracks on the level
MIN_POINT_SPACING_MODEL = 0.001 * 0.03  # 1 mm * model units per meter
MIN_LENGTH_MODEL = 0.01 * 0.03         # 1 cm * model units per meter
DELETE_ON_DUPLICATE = True  # If False, only degenerate tracks are deleted


def call(obj, method, *args):
    fn = getattr(obj, method, None)
    if not callable(fn):
        raise AttributeError(f'{type(obj).__name__} has no {method}()')
    return fn(*args)


def sequence(value):
    if value is None:
        raise ValueError('Null point list')
    if hasattr(value, 'size') and callable(value.size):
        return [value.get(i) for i in range(value.size())]
    return list(value)


def xyz(point):
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        return tuple(float(v) for v in (list(point[:3]) if len(point) >= 3 else [*point[:2], 0]))
    out = []
    for axis in ('X', 'Y', 'Z'):
        value = None
        for getter in (f'get{axis}', f'get{axis.lower()}'):
            fn = getattr(point, getter, None)
            if callable(fn):
                value = fn()
                break
        if value is None:
            if axis == 'Z':
                value = 0.0  # 2D point representations are permitted
            else:
                raise ValueError(f'Cannot read {axis} from point {point!r}')
        out.append(float(value))
    return tuple(out)


def points_for_track(track):
    # Try public generated accessors. No assumption that point objects are absolute:
    # translation does not change adjacent-point distances or total length.
    failures = []
    for getter in ('getPointList', 'getPoints'):
        fn = getattr(track, getter, None)
        if not callable(fn):
            continue
        try:
            value = fn()
            for accessor in ('getPoints', 'getPointList'):
                nested = getattr(value, accessor, None)
                if callable(nested):
                    value = nested()
                    break
            pts = [xyz(p) for p in sequence(value)]
            return pts
        except Exception as exc:
            failures.append(f'{getter}: {exc}')
    raise RuntimeError('Cannot read track point geometry; ' + '; '.join(failures))


def name_of(track):
    fn = getattr(track, 'getName', None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:
            pass
    return '<unnamed>'


def enumerate_tracks(level):
    # Different Design-Time API releases may expose different collections.
    for getter in ('getRailTracks', 'getRailwayTracks', 'getRailroadTracks', 'getElements', 'getObjects'):
        fn = getattr(level, getter, None)
        if not callable(fn):
            continue
        try:
            values = sequence(fn())
            if getter in ('getElements', 'getObjects'):
                values = [x for x in values if 'RailTrack' in str(type(x)) or
                          (callable(getattr(x, 'getPointList', None)) and
                           callable(getattr(x, 'getSource', None)))]
            return values, getter
        except Exception as exc:
            print(f'Collection {getter} unavailable: {exc}')
    raise RuntimeError('Could not enumerate RailTracks on level. Check the Design-Time API accessors. '
                       'No changes have been made.')


def classify(pts):
    if len(pts) < 2:
        return 'too_few_points', 0.0, []
    if any(not math.isfinite(v) for p in pts for v in p):
        return 'nonfinite', float('nan'), []
    distances = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    duplicates = [i for i, d in enumerate(distances) if d <= MIN_POINT_SPACING_MODEL]
    length = sum(distances)
    if length < MIN_LENGTH_MODEL:
        return 'zero_length', length, duplicates
    if duplicates:
        return 'duplicate_points', length, duplicates
    return 'valid', length, []


def delete_track(track):
    # Explicitly limit deletion to a supported method on the track itself.
    # Do not delete a parent level or agent by mistake.
    for method in ('delete', 'remove'):
        fn = getattr(track, method, None)
        if callable(fn):
            fn()
            return method
    raise RuntimeError('Neither track.delete() nor track.remove() is available; '
                       'track left untouched. Inspect your API version.')


def main():
    if TOKEN == 'PASTE_ANYLOGIC_CONNECTOR_TOKEN':
        raise ValueError('Set TOKEN to your current AnyLogic connector token')
    from anylogic_design_time_api import Workspace
    ws = Workspace(TOKEN)
    level = ws.getModel(MODEL_NAME).getAgent(AGENT_NAME).getLevels()[LEVEL_INDEX]
    tracks, getter = enumerate_tracks(level)
    print(f'Enumerated {len(tracks)} RailTracks via level.{getter}()')
    if NAME_PREFIX is not None:
        tracks = [t for t in tracks if name_of(t).startswith(NAME_PREFIX)]
    print(f'Checking {len(tracks)} tracks (prefix={NAME_PREFIX!r})')
    counts = Counter()
    candidates = []
    for track in tracks:
        name = name_of(track)
        try:
            pts = points_for_track(track)
            status, length, duplicates = classify(pts)
        except Exception as exc:
            counts['unreadable'] += 1
            print(f'UNREADABLE {name}: {exc}')
            continue  # Never delete unknown geometry
        counts[status] += 1
        if status != 'valid':
            print(f'{status.upper():18s} {name:30s} points={len(pts)} '
                  f'length_model={length:.9g} duplicate_segments={duplicates[:20]}')
            if status != 'duplicate_points' or DELETE_ON_DUPLICATE:
                candidates.append((track, name))
    print('Summary:', dict(counts))
    print(f'Deletion candidates: {len(candidates)}')
    if DRY_RUN:
        print('DRY_RUN=True: no tracks deleted. Save a model backup before enabling deletion.')
        return
    if counts['unreadable']:
        raise RuntimeError('Some tracks could not be inspected; refusing destructive run. '
                           'Fix geometry reading first.')
    print('Deleting candidates only; existing switches are not deleted or reconnected.')
    for track, name in candidates:
        try:
            method = delete_track(track)
            print(f'DELETED {name} using {method}()')
        except Exception as exc:
            print(f'FAILED to delete {name}: {exc}')
    print('Cleanup finished. Inspect switches with missing track legs and save the model.')


if __name__ == '__main__':
    main()
