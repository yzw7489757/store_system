#!/usr/bin/env python3
"""采集东京23区道路/边界快照，供管理端只读试算。

例：python scripts/bd_collect_zones.py --ward 13104 --merchants-csv /path/merchants_min.csv
    python scripts/bd_collect_zones.py --ward all --merchants-csv /path/merchants_min.csv
公开 Overpass 请求只在本命令执行；Web 请求绝不访问外网。已生成快照不覆盖。
"""
import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import polygonize, unary_union
from app.services import bd_zones as zones

ENDPOINT = 'https://overpass-api.de/api/interpreter'
HEADERS = {'Accept': 'application/json', 'User-Agent': 'StoreSystemZonePreview/1.0 (local admin preview)'}


def fetch(query, path):
    if path.exists():
        return json.loads(path.read_text())
    proxy = os.getenv('https_proxy') or os.getenv('HTTPS_PROXY')
    with httpx.Client(proxy=proxy, trust_env=False, timeout=180, headers=HEADERS) as client:
        for attempt, delay in enumerate((0, 3, 8)):
            if delay:
                time.sleep(delay)
            try:
                response = client.post(ENDPOINT, data={'data': query})
                response.raise_for_status()
                doc = response.json()
                break
            except httpx.HTTPStatusError as exc:
                if attempt == 2 or (exc.response.status_code != 429 and exc.response.status_code < 500):
                    raise
            except httpx.TransportError:
                if attempt == 2:
                    raise
    if doc.get('remark'):
        raise RuntimeError('Overpass returned an incomplete result: ' + doc['remark'][:180])
    if not doc.get('elements'):
        raise RuntimeError('Overpass returned no elements; snapshot not saved')
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    return doc


def relation_polygon(element):
    outer, inner = [], []
    for member in element.get('members', []):
        geom = member.get('geometry', [])
        if member.get('type') != 'way' or len(geom) < 2:
            continue
        line = LineString([(p['lon'], p['lat']) for p in geom])
        (inner if member.get('role') == 'inner' else outer).append(line)
    shells = unary_union(list(polygonize(unary_union(outer))))
    holes = unary_union(list(polygonize(unary_union(inner)))) if inner else Polygon()
    return shells.difference(holes)


def exclusions(elements):
    result = []
    for el in elements:
        tags = el.get('tags', {})
        if not (tags.get('natural') in ('water', 'wood', 'wetland', 'scrub', 'bare_rock')
                or tags.get('landuse') in ('forest', 'reservoir')
                or tags.get('waterway') == 'riverbank'):
            continue
        if el['type'] == 'relation':
            geom = relation_polygon(el)
        else:
            points = [(p['lon'], p['lat']) for p in el.get('geometry', [])]
            if len(points) < 4 or points[0] != points[-1]:
                continue
            geom = Polygon(points)
        if not geom.is_empty:
            result.append(mapping(geom))
    return result


def csv_points(path, wards):
    """流式扫描公开商户快照，仅保留目标区坐标；不导入业务表。"""
    geometries = [shape(w['boundary']) for w in wards]
    tree = STRtree(geometries)
    bounds = unary_union(geometries).bounds
    points = [[] for _ in wards]
    seen = set()
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        columns = set(reader.fieldnames or [])
        if not {'place_id', 'lat'} <= columns or not ({'lon', 'lng'} & columns):
            raise ValueError('商户 CSV 必须含 place_id、lat、lon/lng')
        for row in reader:
            try:
                lng, lat = float(row.get('lon') or row.get('lng')), float(row['lat'])
            except (ValueError, TypeError):
                continue
            if not (bounds[0] <= lng <= bounds[2] and bounds[1] <= lat <= bounds[3]):
                continue
            identity = row['place_id']
            if not identity or identity in seen:
                continue
            hits = tree.query(Point(lng, lat), predicate='intersects')
            if len(hits):
                # 边界上的店只归一个区，避免重复计数。
                points[min(int(i) for i in hits)].append((lng, lat))
                seen.add(identity)
    return points


def ward_relations(elements):
    """只认东京区代码，防止其他城市同名区覆盖。"""
    return {el.get('tags', {}).get('ref', '')[:5]: el
            for el in elements if el['type'] == 'relation'
            and el.get('tags', {}).get('admin_level') == '7'
            and el.get('tags', {}).get('ref', '')[:5] in dict(zones.WARDS)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ward', default='13104', choices=['all'] + [c for c, _ in zones.WARDS])
    parser.add_argument('--merchants-csv', type=Path)
    args = parser.parse_args()
    dest = zones.data_dir()
    dest.mkdir(parents=True, exist_ok=True)
    selected = [(c, n) for c, n in zones.WARDS if args.ward in ('all', c) and not (dest / (c + '.json')).exists()]
    if not selected:
        print('快照已存在；不覆盖既有边界。')
        return
    # 只取23个区的行政边界，不包含东京其他市、町、村。
    names = '|'.join(n for _, n in zones.WARDS)
    boundaries = fetch('[out:json][timeout:120];rel["boundary"="administrative"]'
                       '["name"~"^(' + names + ')$"](35.45,139.45,35.95,140.05);out geom;',
                       dest / 'boundaries.source.json')
    # 东京与其他城市有同名中央区/北区。以官方JIS代码识别，不按名称覆盖。
    by_code = ward_relations(boundaries['elements'])
    wards = []
    for code, name in selected:
        if code not in by_code:
            raise RuntimeError(name + '行政边界缺失，停止，不用矩形替代')
        element = by_code[code]
        polygon = relation_polygon(element)
        if polygon.is_empty or not polygon.is_valid:
            raise RuntimeError(name + '行政边界无效')
        wards.append({'code': code, 'name': name, 'relation': element['id'], 'boundary': mapping(polygon)})
    coast = fetch('[out:json][timeout:120];way["natural"="coastline"]'
                  '(35.40,139.45,35.95,140.05);out geom;', dest / 'coastlines.source.json')
    coastlines = [{'type': 'LineString', 'coordinates': [(p['lon'], p['lat']) for p in el.get('geometry', [])]}
                  for el in coast['elements'] if el['type'] == 'way' and len(el.get('geometry', [])) > 1]
    local_points = csv_points(args.merchants_csv, wards) if args.merchants_csv else None
    for index, ward in enumerate(wards):
        code = ward['code']
        area = 3_600_000_000 + ward['relation']
        shops = ('' if local_points is not None else
                 'nwr(area.a)["shop"];nwr(area.a)["amenity"~"^(restaurant|cafe|fast_food|bar|pub|food_court)$"];')
        query = ('[out:json][timeout:150];area(' + str(area) + ')->.a;('
                 'way(area.a)["highway"~"^(motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|unclassified|residential|living_street|pedestrian)$"];'
                 'way(area.a)["railway"~"^(rail|light_rail|tram)$"];'
                 'nwr(area.a)["natural"~"^(water|wood|wetland|scrub|bare_rock)$"];'
                 'nwr(area.a)["landuse"~"^(forest|reservoir)$"];'
                 'nwr(area.a)["waterway"="riverbank"];' + shops + ');out geom;')
        print('采集 ' + ward['name'] + ' 道路及排除区…', flush=True)
        raw = fetch(query, dest / (code + '-' + str(ward['relation']) + '.roads-v2.source.json'))
        roads, osm_points = [], []
        for el in raw['elements']:
            tags = el.get('tags', {})
            geom = el.get('geometry', [])
            if el['type'] == 'way' and tags.get('highway') and len(geom) > 1:
                roads.append({'type': 'LineString', 'coordinates': [(p['lon'], p['lat']) for p in geom]})
            if tags.get('shop') or tags.get('amenity') in ('restaurant', 'cafe', 'fast_food', 'bar', 'pub', 'food_court'):
                center = el if 'lat' in el else el.get('center', {})
                if 'lat' in center:
                    osm_points.append((center['lon'], center['lat']))
                elif geom:
                    point = LineString([(p['lon'], p['lat']) for p in geom]).centroid
                    osm_points.append((point.x, point.y))
        if not roads:
            raise RuntimeError(ward['name'] + '道路缺失，停止，不生成伪网格')
        points = local_points[index] if local_points is not None else osm_points
        source = {'geometry': 'OpenStreetMap / Overpass (ODbL)',
                  'merchants': ('本地商户快照 ' + args.merchants_csv.parent.name if local_points is not None
                                else 'OpenStreetMap POI（不完整，仅供试算）'),
                  'osm_timestamp': raw.get('osm3s', {}).get('timestamp_osm_base', ''),
                  'limitations': '商户为来源快照估算；仅排除已标注水域、林地等，未标注地形不自动推断。'}
        doc = zones.prepare_snapshot(code, ward['boundary'], roads, exclusions(raw['elements']), points,
                                     captured_at=datetime.now(timezone.utc).isoformat(), source=source, coastlines=coastlines)
        tmp = dest / (code + '.json.tmp')
        tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        tmp.replace(dest / (code + '.json'))
        print(f"{ward['name']}: {len(doc['grids'])}网格 / {doc['store_count']}店 / {doc['area_km2']:.2f}km² / {len(doc['issues'])}项待检查", flush=True)
        if index + 1 < len(wards):
            time.sleep(3)


if __name__ == '__main__':
    main()
