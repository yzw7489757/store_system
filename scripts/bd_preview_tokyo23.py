#!/usr/bin/env python3
"""仅构建23区整体试算快照；不是确认/发布/派发命令。复用本地23区原始数据。"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shapely.geometry import mapping
from shapely.ops import unary_union
from app.services import bd_zones as zones
from scripts.bd_collect_zones import exclusions, relation_polygon, ward_relations
from app.services.bd_zone_coverage import prepare_source


def build(merchants_csv):
    root = zones.data_dir()
    boundaries = ward_relations(json.loads((root / 'boundaries.source.json').read_text())['elements'])
    missing = [code for code, _ in zones.WARDS if code not in boundaries]
    if missing:
        raise ValueError('23区边界不完整: ' + ','.join(missing))
    border = unary_union([relation_polygon(boundaries[code]) for code, _ in zones.WARDS])
    print('合并23区行政边界；读取原始道路/排除范围…', flush=True)
    unique = {}
    for code, _ in zones.WARDS:
        path = root / (code + '-' + str(boundaries[code]['id']) + '.roads-v1.source.json')
        for element in json.loads(path.read_text())['elements']:
            unique[(element['type'], element['id'])] = element
    roads = [{'type': 'LineString', 'coordinates': [(p['lon'], p['lat']) for p in e['geometry']]}
             for e in unique.values() if e['type'] == 'way' and e.get('tags', {}).get('highway')
             and len(e.get('geometry', [])) > 1]
    coast = json.loads((root / 'coastlines.source.json').read_text())['elements']
    coastlines = [{'type': 'LineString', 'coordinates': [(p['lon'], p['lat']) for p in e['geometry']]}
                  for e in coast if e.get('geometry')]
    print(f'覆盖优先预览：{len(roads)}条道路；保留全部基础地块并逐店核对归属。', flush=True)
    doc = prepare_source(mapping(border), roads, exclusions(unique.values()), coastlines, merchants_csv,
                         captured_at=datetime.now(timezone.utc).isoformat(),
                         source={'geometry': 'OpenStreetMap / Overpass (ODbL)',
                                 'merchants': '本地商户快照 ' + merchants_csv.parent.name,
                                 'osm_timestamp': '2026-10-31',
                                 'limitations': '覆盖优先预览；排除地形内商户必须复核，非真实商户普查。'})
    path = root / 'tokyo23.json'
    temp = root / 'tokyo23.json.tmp'
    temp.write_text(json.dumps(doc, ensure_ascii=False, separators=(',', ':')))
    backup = root / 'tokyo23.before-coverage.json'
    if path.exists() and not backup.exists():
        backup.write_text(path.read_text())
    temp.replace(path)
    print('整体预览快照完成', {'road_cells': len(doc['road_cells']), 'stores': doc['store_count'],
                                 'area_km2': round(doc['area_km2'], 2), 'merchant_audit': doc['merchant_audit']}, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--merchants-csv', type=Path, required=True)
    args = parser.parse_args()
    build(args.merchants_csv)
