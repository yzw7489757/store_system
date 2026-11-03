#!/usr/bin/env python3
"""本地OSM原始快照→自然网格候选；默认不写正式库，不覆盖旧快照。"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build(root):
    from shapely.geometry import mapping
    from shapely import union_all
    from app.services import bd_zones as z, bd_natural_grids as natural
    from app.services.bd_zone_merchants import bind_database
    from scripts.bd_collect_zones import exclusions, relation_polygon, ward_relations
    started = time.monotonic()
    from app.db import SessionLocal
    from app.models import BdZone, BdZoneRelease, BdWorkGrid
    from sqlalchemy import inspect, text
    from types import SimpleNamespace
    # 仅冻结正式版本摘要，不改正式表，也不读取尚未迁移的新列。
    with SessionLocal() as db:
        zone_rows = db.query(BdZone).filter(BdZone.state != 'archived').all() if inspect(db.bind).has_table('bd_region') else [
            SimpleNamespace(**dict(row)) for row in db.execute(text(
                "SELECT id,source_id,geometry,area_km2,store_count FROM bd_zone WHERE state<>'archived'")).mappings()]
        baseline = {'releases': [{'id': r.id, 'fingerprint': r.fingerprint} for r in db.query(BdZoneRelease).all()],
                    'grid_count': db.query(BdWorkGrid.id).count(),
                    'zones': [{'id': row.id, 'source_id': row.source_id,
                               'geometry_sha256': natural.canonical_sha(row.geometry),
                               'shape': {key: value if math.isfinite(value) else None for key, value in
                                         z._shape_metrics(z.projected(row.geometry)).items()},
                               'area_km2': row.area_km2, 'stores': row.store_count}
                              for row in zone_rows]}
    relations = ward_relations(json.loads((root / 'boundaries.source.json').read_text())['elements'])
    if any(code not in relations for code, _ in z.WARDS):
        raise z.ZoneDataError('23区行政边界不完整')
    border = union_all([relation_polygon(relations[code]) for code, _ in z.WARDS])
    elements, fingerprints, timestamps, railway_collected = {}, {}, set(), True
    paths = [root / 'boundaries.source.json', root / 'coastlines.source.json']
    for code, _ in z.WARDS:
        stem = code + '-' + str(relations[code]['id'])
        path = root / (stem + '.roads-v2.source.json')
        if not path.exists():
            path = root / (stem + '.roads-v1.source.json')
            railway_collected = False
        paths.append(path)
        raw = json.loads(path.read_text())
        timestamps.add(raw.get('osm3s', {}).get('timestamp_osm_base', 'unknown'))
        for element in raw['elements']:
            elements[(element['type'], element['id'])] = element
    railway_path = root / 'railways.source.json'
    if railway_path.exists():
        paths.append(railway_path)
        railway_raw = json.loads(railway_path.read_text())
        timestamps.add(railway_raw.get('osm3s', {}).get('timestamp_osm_base', 'unknown'))
        for element in railway_raw['elements']:
            elements[(element['type'], element['id'])] = element
        railway_collected = True
    import hashlib
    for path in paths:
        fingerprints[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    coast = json.loads((root / 'coastlines.source.json').read_text())['elements']
    coastlines = [{'type': 'LineString', 'coordinates': [(p['lon'], p['lat']) for p in e['geometry']]}
                  for e in coast if len(e.get('geometry', [])) > 1]
    doc = natural.prepare(mapping(border), list(elements.values()), exclusions(elements.values()), coastlines,
        captured_at=datetime.now(timezone.utc).isoformat(), source={
            'geometry': 'OpenStreetMap / Overpass (ODbL)', 'osm_timestamps': sorted(timestamps),
            'railway_collected': railway_collected, 'input_files_sha256': fingerprints,
            'limitations': ('仅地面主干道、铁路与已标注陆地边界。' if railway_collected else
                           '旧缓存未采集铁路；本候选仅含主干道与已标注陆地边界，铁路验收待补。')})
    geometry_seconds = time.monotonic() - started
    doc = bind_database(doc)
    doc = natural.normalise(doc)
    result = natural.candidate(doc)
    result['baseline'] = baseline
    result['code_sha256'] = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in (
        'app/services/bd_natural_grids.py', 'app/services/bd_zones.py',
        'app/services/bd_zone_merchants.py', 'scripts/bd_generate_natural_grids.py')}
    result['audit']['phase_seconds'] = {'geometry': round(geometry_seconds, 3),
                                      'total': round(time.monotonic() - started, 3)}
    # 耗时是运行元信息，不参与导入内容校验。
    result['timing'] = result['audit'].pop('phase_seconds')
    try:
        result['code_revision'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        result['code_revision'] = 'unknown'
    return result


def main():
    from app.services import bd_zones as z
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=z.data_dir())
    parser.add_argument('--apply', action='store_true', help='校验后直接正式落库，不经过预览确认')
    parser.add_argument('--replace', action='store_true', help='显式清空旧片区和旧网格后重建；拒绝有作业历史的范围')
    parser.add_argument('--backup', type=Path, help='重建前已经核验的数据库备份')
    parser.add_argument('--admin-user')
    args = parser.parse_args()
    if args.replace and (not args.apply or not args.backup or not args.backup.is_file() or args.backup.stat().st_size < 1000):
        parser.error('--replace必须配合--apply和已核验的--backup文件')
    if args.apply and not args.admin_user:
        parser.error('--apply需要--admin-user')
    if not os.environ.get('DATABASE_URL'):
        parser.error('须先加载项目.env并显式设置DATABASE_URL')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = build(args.root)
    except z.ZoneDataError as error:
        if hasattr(error, 'audit'):
            args.output.write_text(json.dumps({'status': 'failed', 'applied': False, 'audit': error.audit},
                                              ensure_ascii=False, separators=(',', ':')))
        raise
    if args.apply:
        if not args.admin_user:
            parser.error('--apply需要--admin-user')
        from app.db import SessionLocal
        from app.models import User
        from app.services.bd_natural_grids import import_candidate
        if not result['audit'].get('quality_pass'):
            result.update(status='quality_failed', applied=False)
            args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
            raise z.ZoneDataError('极端网格检查未通过；旧数据未清空：' +
                json.dumps(result['audit']['normalisation']['violations'][:8], ensure_ascii=False))
        with SessionLocal() as db:
            release = import_candidate(db, db.query(User).filter_by(username=args.admin_user).first(), result, replace=args.replace)
            db.commit()
            result.update(applied=True, status='imported', release_id=release.id)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
    print(json.dumps({'grids': len(result['features']), 'status': result['status'],
                      'area_km2': result['source_document']['area_km2'],
                      'merchant_audit': result['audit']['merchant_audit'],
                      'geometric_pass': result['audit']['geometric_pass'],
                      'quality_pass': result['audit'].get('quality_pass'),
                      'quality': result['audit'].get('normalisation'),
                      'geojson_sha256': result['audit']['geojson_sha256'],
                      'railway_collected': result['source_document']['source']['railway_collected'],
                      'timing': result['timing']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
