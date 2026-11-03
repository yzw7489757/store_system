"""片区预览的只读 PostgreSQL 商户源；不导入/更新任何业务表，不回退到旧CSV。"""
from __future__ import annotations

import hashlib
import math
from contextlib import contextmanager
from datetime import datetime, timezone

from shapely import STRtree, intersects_xy, points, set_precision
from shapely.geometry import shape
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

SOURCE_TABLE = 'public.kanto_merchants_v20261029'


@contextmanager
def database_rows(bounds, *, engine=None):
    from app.services import bd_zones as z
    explicit_engine = engine is not None
    if engine is None:
        from app.db import get_engine
        engine = get_engine()
    if engine.dialect.name != 'postgresql' and not explicit_engine:
        raise z.ZoneDataError('片区商户源要求连接store_settle中的PostgreSQL商户表；不会使用旧CSV替代。')
    if engine.dialect.name == 'postgresql' and engine.url.database != 'store_settle':
        raise z.ZoneDataError('片区商户源必须连接store_settle数据库。')
    xmin, ymin, xmax, ymax = bounds
    try:
        with engine.connect() as connection:
            if engine.dialect.name == 'postgresql':
                connection = connection.execution_options(isolation_level='REPEATABLE READ')
            with connection.begin():
                if engine.dialect.name == 'postgresql':
                    connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                    connection.exec_driver_sql("SET LOCAL statement_timeout='60s'")
                    connection.exec_driver_sql("SET LOCAL lock_timeout='2s'")
                else:  # 仅显式传入的隔离SQLite测试库。
                    connection.exec_driver_sql('PRAGMA query_only=ON')
                total = connection.execute(text(f'SELECT COUNT(*) FROM {SOURCE_TABLE}')).scalar_one()
                query = text(f'''SELECT place_id, name, lat, lon FROM {SOURCE_TABLE}
                    WHERE (lon BETWEEN :xmin AND :xmax AND lat BETWEEN :ymin AND :ymax)
                       OR lat IS NULL OR lon IS NULL OR place_id IS NULL OR TRIM(place_id)=''
                       OR NOT (lat BETWEEN -90 AND 90) OR NOT (lon BETWEEN -180 AND 180)''')
                result = connection.execution_options(stream_results=True).execute(
                    query, dict(xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax))
                info = {'backend': 'postgresql', 'database': 'store_settle', 'table': SOURCE_TABLE, 'total_count': total,
                        'read_at': datetime.now(timezone.utc).isoformat()}
                yield info, result.mappings()
    except z.ZoneDataError:
        raise
    except (SQLAlchemyError, OSError) as exc:
        # 不把连接串、凭证或底层SQL错误返回给浏览器，也不静默读CSV。
        raise z.ZoneDataError('数据库商户源读取失败，请检查数据库连接和指定商户表的只读权限。') from exc


def bind_rows(doc, rows, source_info):
    """同一批源记录唯一落到既有道路面；几何与排除区域不随商户源改变。"""
    from app.services import bd_zones as z
    cells = doc.get('road_cells')
    if not cells or doc.get('metric_crs') != 'EPSG:6677':
        raise z.ZoneDataError('缺少完整米制道路基础面，不能绑定数据库商户。')
    geoms = [set_precision(shape(c['metric_geometry']), .001) for c in cells]
    tree = STRtree(geoms)
    border = shape(doc['boundary'])
    work = z.projected(doc['work_area'])
    xmin, ymin, xmax, ymax = border.bounds
    bound = [[] for _ in cells]
    seen, reviews = set(), []
    audit = dict(inside_boundary=0, inside_work=0, excluded_unreviewed=0,
                 unlocated_in_work=0, source_duplicates=0, invalid_coordinates=0)
    batch = []

    def consume(batch):
        if not batch:
            return
        inside = intersects_xy(border, [r['lng'] for r in batch], [r['lat'] for r in batch])
        unique = []
        for i, row in enumerate(batch):
            if not inside[i]:
                continue
            if row['store_id'] in seen:
                audit['source_duplicates'] += 1
                continue
            seen.add(row['store_id'])
            unique.append(row)
        audit['inside_boundary'] += len(unique)
        if not unique:
            return
        mx, my = z._TO_METERS([r['lng'] for r in unique], [r['lat'] for r in unique])
        in_work = intersects_xy(work, mx, my)
        lookup, locations = [], []
        for i, row in enumerate(unique):
            if in_work[i]:
                audit['inside_work'] += 1
                lookup.append(i)
                locations.append((mx[i], my[i]))
            else:
                audit['excluded_unreviewed'] += 1
                reviews.append({**row, 'reason': 'excluded_terrain', 'status': 'unreviewed'})
        if not locations:
            return
        owner = {}
        for point_index, face_index in zip(*tree.query(points(locations), predicate='intersects')):
            key, value = int(point_index), int(face_index)
            owner[key] = min(value, owner.get(key, value))
        for j, i in enumerate(lookup):
            row = unique[i]
            if j in owner:
                bound[owner[j]].append(row['store_id'])
            else:
                audit['unlocated_in_work'] += 1
                reviews.append({**row, 'reason': 'no_road_face', 'status': 'unreviewed'})

    for row in rows:
        try:
            lat, lng = float(row['lat']), float(row['lon'])
            identity = str(row['place_id'] or '').strip()
            valid = identity and math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
        except (TypeError, ValueError):
            valid = False
        if not valid:
            audit['invalid_coordinates'] += 1
            reviews.append({'store_id': str(row.get('place_id') or ''), 'name': row.get('name') or '',
                            'lat': None, 'lng': None, 'reason': 'invalid_coordinates', 'status': 'unreviewed'})
            continue
        if not (xmin <= lng <= xmax and ymin <= lat <= ymax):
            continue
        batch.append({'store_id': identity, 'name': row.get('name') or '', 'lat': lat, 'lng': lng})
        if len(batch) >= 20000:
            consume(batch)
            batch = []
    consume(batch)
    digest = hashlib.sha256()
    updated = []
    for cell, ids in zip(cells, bound):
        ids.sort()
        digest.update((cell['id'] + '|' + '|'.join(ids)).encode())
        updated.append({**cell, 'store_count': len(ids), 'merchant_ids': ids})
    audit['mapped_to_faces'] = sum(len(ids) for ids in bound)
    metadata = {**doc.get('source', {}), **source_info,
                'merchants': 'PostgreSQL / ' + SOURCE_TABLE,
                'limitations': '库内已知商户，非完整门店普查；拟排除地形内商户仍需复核。'}
    return {**doc, 'road_cells': updated, 'store_count': audit['inside_work'],
            'merchant_audit': audit, 'review_merchants': reviews, 'source': metadata,
            'snapshot_id': digest.hexdigest()[:12], 'geometry_captured_at': doc['captured_at'],
            'captured_at': source_info.get('read_at', doc['captured_at']), 'coverage_revision': 2}


def bind_database(doc, *, engine=None):
    with database_rows(shape(doc['boundary']).bounds, engine=engine) as (info, rows):
        return bind_rows(doc, rows, info)
