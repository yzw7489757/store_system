"""以有标签的OSM主干道、地面铁路及真实陆地边界生成独立网格。"""
import hashlib
import json
import heapq
import math

from shapely import union_all, set_precision, STRtree
from shapely.geometry import LineString, mapping, shape
from shapely.ops import polygonize_full, transform

from app.services import bd_zones as z

MAIN_ROADS = frozenset(('motorway', 'trunk', 'primary', 'secondary'))
SURFACE_RAIL = frozenset(('rail', 'light_rail', 'tram'))


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def boundary_kind(tags, *, length_m=None):
    # 道路投影分割与通行连通分开；公路桥只裁取陆地投影，绝不填水。
    try:
        layer = float(tags.get('layer', 0))
    except (ValueError, TypeError):
        return None
    highway = tags.get('highway', '')
    road = highway in MAIN_ROADS or highway.removesuffix('_link') in MAIN_ROADS
    # tertiary是城市连接干道；明确单车道/窄路排除，住宅路仍始终排除。
    if highway in ('tertiary', 'tertiary_link'):
        try:
            lanes = max(float(value) for value in str(tags.get('lanes', '0')).split(';'))
            width = float(str(tags.get('width', '0')).removesuffix(' m'))
        except ValueError:
            lanes = width = 0
        one_way = tags.get('oneway') in ('yes', '1', '-1', 'true')
        road = one_way or ((lanes == 0 or lanes >= 2) and (width == 0 or width >= 8))
    if road:
        underground = tags.get('tunnel', 'no') not in ('no', 'false', '0', '') or layer < 0 or tags.get('location') == 'underground'
        if tags.get('location') == 'underwater' or (underground and (length_m is None or length_m > 150)):
            return None
        return 'road_projection' if layer or tags.get('bridge', 'no') not in ('no', 'false', '0', '') or underground else 'road'
    if (layer != 0 or tags.get('bridge', 'no') not in ('no', 'false', '0', '')
            or tags.get('tunnel', 'no') not in ('no', 'false', '0', '')
            or tags.get('location') in ('underground', 'underwater')):
        return None
    if tags.get('railway') in SURFACE_RAIL:
        return 'rail'
    return None


def prepare(boundary, elements, exclusions, coastlines, *, captured_at, source):
    border = z.projected(boundary)
    land = z.coastline_land(border, coastlines)
    masks = [z.projected(g) for g in exclusions]
    if masks:
        land = land.difference(union_all(masks, grid_size=.001))
    land = set_precision(land, .001)
    segments, retained = [land.boundary], []
    skipped = {'minor_or_other': 0, 'grade_separated_or_unknown': 0}
    unique = {(e['type'], e['id']): e for e in elements}
    for identity in sorted(unique):
        element = unique[identity]
        tags = element.get('tags', {})
        coords = element.get('geometry', [])
        if element['type'] != 'way' or len(coords) < 2:
            continue
        raw_line = transform(z._TO_METERS, LineString([(p['lon'], p['lat']) for p in coords]))
        kind = boundary_kind(tags, length_m=raw_line.length)
        if kind is None:
            key = ('grade_separated_or_unknown' if tags.get('highway') in MAIN_ROADS
                   or tags.get('railway') in SURFACE_RAIL else 'minor_or_other')
            skipped[key] += 1
            continue
        line = raw_line.intersection(land)
        if line.is_empty or line.length <= .001:
            continue
        segments.append(line)
        retained.append({'osm_id': element['id'], 'kind': kind, 'tags': tags})
    if not retained:
        raise z.ZoneDataError('缺少有标签的地面主干道/铁路，不能生成自然网格')
    faces_collection, cuts, dangles, invalid = polygonize_full(union_all(segments, grid_size=.001))
    # polygonize 的面不继承固定精度；沿海节点化外溢按真实陆地裁掉。
    faces = [part for face in faces_collection.geoms if land.covers(face.representative_point())
             for part in z.polygons(set_precision(face, .001).intersection(land))]
    faces.sort(key=lambda face: (face.centroid.x, face.centroid.y, face.area))
    combined = union_all(faces, grid_size=.001)
    missing, outside = land.difference(combined), combined.difference(land)
    overlap = max(0., sum(f.area for f in faces) - combined.area)
    cells = [{'id': 'tokyo23-N-' + hashlib.sha256(face.normalize().wkb).hexdigest()[:16],
              'metric_geometry': mapping(face), 'area_km2': face.area / 1e6,
              'store_count': 0, 'merchant_ids': []} for face in faces]
    audit = {'crs': 'EPSG:6677', 'precision_m': .001, 'main_road_classes': sorted(MAIN_ROADS),
             'junction_links': True, 'tertiary_evidence': 'arterial class; exclude explicit single-lane or width<8m',
             'grade_policy': 'road projection on land; short underpass<=150m; ground rail only; no transit graph',
             'retained_boundaries': retained, 'skipped': skipped,
             'cuts': mapping(cuts), 'dangles': mapping(dangles), 'invalid_rings': mapping(invalid),
             'uncovered': mapping(missing), 'outside': mapping(outside), 'overlap_m2': overlap,
             'uncovered_m2': missing.area, 'outside_m2': outside.area,
             'geometric_pass': bool(faces) and missing.area <= .01 and outside.area <= .01
                               and overlap <= .01 and invalid.is_empty,
             'input_sha256': canonical_sha({'boundary': boundary, 'elements': [unique[key] for key in sorted(unique)],
                                             'exclusions': exclusions, 'coastlines': coastlines})}
    if not audit['geometric_pass']:
        error = z.ZoneDataError(f'自然网格校验失败：未覆盖{missing.area:.3f}m²，外溢{outside.area:.3f}m²，'
                                f'重叠{overlap:.3f}m²，非法环{len(invalid.geoms)}个')
        error.audit = audit
        raise error
    return {'schema_version': z.SCHEMA_VERSION, 'coverage_revision': 2, 'generation_version': 'natural-1',
            'metric_crs': 'EPSG:6677', 'snapshot_id': canonical_sha(cells)[:12],
            'captured_at': captured_at, 'source': source, 'scope_ward_count': 23,
            'boundary': boundary, 'work_area': z.geographic(land), 'road_cells': cells,
            'store_count': 0, 'area_km2': land.area / 1e6, 'grids': [], 'issues': [],
            'natural_audit': audit, 'preview_only': True}


def candidate(doc):
    from app.services.bd_grid_geometry import outline
    cells = doc['road_cells']
    merchants = doc.get('merchant_audit', {})
    features = [{'type': 'Feature', 'id': c['id'], 'geometry': z.geographic(shape(c['metric_geometry'])),
                 'properties': {'id': c['id'], 'level': 'grid', 'zone_id': None,
                                'area_km2': c['area_km2'], 'store_count': c['store_count'],
                                'cell_ids': [c['id']], 'shape_valid': z._acceptable_shape(outline(shape(c['metric_geometry'])))}}
                for c in cells]
    audit = {**doc['natural_audit'], 'merchant_pass':
             merchants.get('unlocated_in_work') == 0 and merchants.get('source_duplicates') == 0
             and merchants.get('mapped_to_faces') == merchants.get('inside_work'),
             'merchant_audit': merchants, 'geojson_sha256': canonical_sha(features)}
    return {'type': 'FeatureCollection', 'features': features, 'audit': audit,
            'source_document': doc, 'status': 'candidate', 'applied': False}


def normalise(doc, *, min_area=.05, max_area=2., max_natural_area=5.):
    """按完整自然面聚合碎片；不切原子面、不补线、不吞掉有商户的孤岛。"""
    from app.services.bd_zone_coverage import face_graph
    geoms = [set_precision(shape(c['metric_geometry']), .001) for c in doc['road_cells']]
    counts = [len(c['merchant_ids']) for c in doc['road_cells']]
    graph = face_graph(geoms)
    # 裁剪可在共边一侧增加中间点；补查真实线交集，避免完整边哈希漏邻接。
    tree = STRtree(geoms)
    boundaries = [g.boundary for g in geoms]
    for i,geom in enumerate(geoms):
        for raw_j in tree.query(geom, predicate='intersects'):
            j = int(raw_j)
            if j > i and j not in graph[i] and boundaries[i].intersection(boundaries[j]).length > .001:
                graph[i].add(j); graph[j].add(i)
    land_parts = z.polygons(z.projected(doc['work_area']))
    land_tree = STRtree(land_parts)
    component_members = {i:set() for i in range(len(land_parts))}
    for i,geom in enumerate(geoms):
        hits = land_tree.query(geom.representative_point(), predicate='intersects')
        if not len(hits): raise z.ZoneDataError('自然面未归属真实陆地分量')
        component_members[min(int(value) for value in hits)].add(i)
    components = [part for part in component_members.values() if part]
    admin_boundary = z.projected(doc['boundary']).boundary
    protected_islands = set()
    for land_index, component in component_members.items():
        geom = land_parts[land_index]
        stores = sum(counts[i] for i in component)
        if not 5e6 < geom.area <= 15e6 or not 0 < stores <= 1000: continue
        metrics = z._shape_metrics(geom)
        share = geom.boundary.intersection(admin_boundary).length / max(geom.length, 1)
        if share <= .05 and metrics['aspect'] <= 3 and metrics['solidity'] >= .65:
            protected_islands |= component
    excluded = {i for part in components if sum(counts[i] for i in part) == 0 for i in part}
    # 机场运行区等完整自然大块若没有任何已知商户，不为补面积纳入任务。
    # 仅排除完整原子块且必须零商户；正常无商户街块仍参与相邻归并。
    empty_large = {i for i,geom in enumerate(geoms) if i not in protected_islands
                   and counts[i] == 0 and geom.area > max_natural_area * 1e6}
    excluded |= empty_large
    retained = [i for i in range(len(geoms)) if i not in excluded]
    if not retained:
        raise z.ZoneDataError('有效陆地中没有可归属商户')
    kept_geoms, kept_counts = [geoms[i] for i in retained], [counts[i] for i in retained]
    indices = {identity:i for i,identity in enumerate(retained)}
    kept_graph = {i:{indices[j] for j in graph[identity] if j in indices} for i,identity in enumerate(retained)}
    def acceptable_merge(geom):
        metrics = z._shape_metrics(geom)
        return all(math.isfinite(v) for v in metrics.values()) and metrics['aspect'] <= 12 and metrics['solidity'] >= .1
    groups = z._group(kept_geoms, kept_counts,
                      max(1, round(sum(g.area for g in kept_geoms) / 500_000)), max_area * 1e6,
                      adjacency=kept_graph, merge_guard=acceptable_merge)
    # 目标数量不是停止条件：继续沿共边吸收小尾片，双方必须仍为单一合法面。
    groups = dict(enumerate(groups))
    owner = {atom:identity for identity,group in groups.items() for atom in group['members']}
    topology = {identity:{owner[j] for atom in group['members'] for j in kept_graph[atom]
                          if owner[j] != identity} for identity,group in groups.items()}
    versions = {identity:0 for identity in groups}
    queue = []
    def extreme(geom):
        metrics = z._shape_metrics(geom)
        return not all(math.isfinite(v) for v in metrics.values()) or metrics['aspect'] > 12 or metrics['solidity'] < .1
    def enqueue(identity):
        if groups[identity]['geom'].area < min_area * 1e6 or extreme(groups[identity]['geom']):
            heapq.heappush(queue,(groups[identity]['geom'].area,identity,versions[identity]))
    for identity in groups: enqueue(identity)
    while queue:
        _, i, version = heapq.heappop(queue)
        if i not in groups or version != versions[i]: continue
        choices = []
        for j in sorted(topology[i]):
            geom = union_all([groups[i]['geom'], groups[j]['geom']], grid_size=.001)
            # 完整自然块不能强切；仅允许吸收极小尾片，不能扩成无界大网格。
            largest_atom = max(kept_geoms[k].area for k in groups[j]['members'])
            limit = min(max_natural_area * 1e6, max(max_area * 1e6, largest_atom + min_area * 1e6))
            if geom.geom_type != 'Polygon' or geom.area > limit: continue
            metrics = z._shape_metrics(geom)
            if not acceptable_merge(geom): continue
            choices.append((metrics['aspect'], -metrics['solidity'], j, geom))
        if not choices: continue
        _, _, j, geom = min(choices, key=lambda row: row[:3])
        groups[j]['geom'] = geom
        groups[j]['members'] |= groups[i]['members']
        groups[j]['count'] += groups[i]['count']
        adjacent = (topology[i] | topology[j]) - {i,j}
        del groups[i]
        for k in adjacent:
            topology[k].discard(i); topology[k].discard(j); topology[k].add(j)
        topology[j] = adjacent; del topology[i]
        versions[j] += 1
        enqueue(j)
        # 邻区形状变化后，先前无法吸收的尾片必须重新评估。
        # 只重排当前受影响邻居，不能仅把合并结果本身放回队列。
        for k in sorted(adjacent): enqueue(k)
    # 完整近岸岛屿不按城区目标任意切开。必须独立于行政裁剪边、负载低且紧凑，
    # 上限单独固定；既不是把所有大块当岛，也不是用阈值放过未闭合城区。
    admin_boundary = z.projected(doc['boundary']).boundary
    coastal_islands = {}
    for component in components:
        if component & excluded: continue
        geom = union_all([geoms[i] for i in sorted(component)], grid_size=.001)
        stores = sum(counts[i] for i in component)
        if not 5e6 < geom.area <= 15e6 or stores > 1000 or geom.geom_type != 'Polygon': continue
        metrics = z._shape_metrics(geom)
        admin_share = geom.boundary.intersection(admin_boundary).length / max(geom.length, 1)
        if admin_share > .05 or metrics['aspect'] > 3 or metrics['solidity'] < .65: continue
        candidates = [identity for identity,g in groups.items()
                      if {retained[i] for i in g['members']} <= component]
        if {retained[i] for identity in candidates for i in groups[identity]['members']} != component: continue
        members = set().union(*(groups[identity]['members'] for identity in candidates))
        identity = max(groups) + 1
        for old in candidates: del groups[old]
        groups[identity] = {'geom':geom, 'members':members, 'count':stores}
        coastal_islands[frozenset(component)] = {'max_area_km2':15, 'max_merchants':1000,
                                                'admin_boundary_share':admin_share}
    from app.services.bd_grid_geometry import consolidate, outline
    groups = consolidate(groups.values())
    cells, violations, exceptions, natural_exceptions = [], [], [], []
    for group in sorted(groups, key=lambda g: (g['geom'].centroid.x, g['geom'].centroid.y)):
        geom = group['geom']
        indices = sorted(retained[i] for i in group['members'])
        ids = sorted(identity for i in indices for identity in doc['road_cells'][i]['merchant_ids'])
        component = next(part for part in components if indices[0] in part)
        whole_island = set(indices) == set(component)
        coastal = coastal_islands.get(frozenset(indices))
        metrics = z._shape_metrics(outline(geom))
        identity = 'tokyo23-G-' + hashlib.sha256(geom.normalize().wkb).hexdigest()[:16]
        reasons = []
        largest_atom = max(geoms[i].area for i in indices)
        natural_block = (largest_atom > max_area * 1e6 and geom.area <= max_natural_area * 1e6
                         and geom.area - largest_atom <= min_area * 1e6)
        if geom.area > max_area * 1e6 + .01 and not natural_block and not coastal: reasons.append('oversized')
        if not whole_island and geom.area < min_area * 1e6 - .01: reasons.append('undersized')
        if not all(math.isfinite(value) for value in metrics.values()): reasons.append('invalid_shape_metrics')
        if not whole_island and (metrics['aspect'] > 12 or metrics['solidity'] < .1): reasons.append('extreme_shape')
        if len(ids) > 5000: reasons.append('merchant_overload')
        if reasons: violations.append({'id': identity, 'area_km2': geom.area / 1e6, 'stores': len(ids), 'reasons': reasons})
        if whole_island and geom.area < min_area * 1e6:
            exceptions.append({'id': identity, 'reason': 'complete_inhabited_island', 'area_km2': geom.area / 1e6})
        if coastal:
            exceptions.append({'id':identity, 'reason':'complete_coastal_island',
                               'area_km2':geom.area/1e6, 'stores':len(ids), **coastal})
        if natural_block:
            natural_exceptions.append({'id': identity, 'reason': 'indivisible_natural_block', 'area_km2':geom.area/1e6})
        cells.append({'id': identity, 'metric_geometry': mapping(geom), 'area_km2': geom.area / 1e6,
                      'merchant_ids': ids, 'store_count': len(ids),
                      'natural_cell_ids': [doc['road_cells'][i]['id'] for i in indices]})
    work = union_all([shape(c['metric_geometry']) for c in cells], grid_size=.001)
    excluded_area = sum(geoms[i].area for i in excluded)
    expected = z.projected(doc['work_area']).difference(union_all([geoms[i] for i in sorted(excluded)], grid_size=.001))
    audit = {**doc['natural_audit'], 'raw_cell_count': len(geoms),
             'excluded_empty_cell_ids': [doc['road_cells'][i]['id'] for i in sorted(excluded)],
             'excluded_empty_area_km2': excluded_area / 1e6,
             'excluded_large_empty_cell_ids': [doc['road_cells'][i]['id'] for i in sorted(empty_large)],
             'excluded_large_empty_area_km2': sum(geoms[i].area for i in empty_large)/1e6,
             'normalisation': {'min_area_km2': min_area, 'max_area_km2': max_area,
                               'max_natural_area_km2':max_natural_area, 'natural_block_exceptions':natural_exceptions,
                               'max_merchants': 5000, 'violations': violations, 'island_exceptions': exceptions},
             'quality_pass': not violations,
             'geometric_pass': expected.symmetric_difference(work).area <= .01
                               and abs(sum(c['area_km2'] for c in cells) * 1e6 - work.area) <= .01}
    return {**doc, 'road_cells': cells, 'work_area': z.geographic(work), 'area_km2': work.area / 1e6,
            'natural_audit': audit, 'generation_version': 'natural-2', 'snapshot_id': canonical_sha(cells)[:12]}


def import_candidate(db, user, result, *, replace=False):
    """首个版本导入；不覆盖任何已有正式版本、不自动生成或分配片区。"""
    from sqlalchemy import text
    from app.models import BdZoneRelease, BdWorkGrid, BdGridStore, BdZone, BdStoreVisitLog
    from app.services import bd_zone_operations as ops
    ops.require_admin(user)
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_advisory_xact_lock(70422301)'))
    if db.query(BdZoneRelease.id).first() and not replace:
        raise ops.OperationError('已有正式版本；自然网格仅保留候选，须单独完成备份及版本切换')
    doc, audit = result['source_document'], result['audit']
    expected = candidate(doc)
    if (not audit.get('geometric_pass') or not audit.get('merchant_pass')
            or canonical_sha(result['features']) != expected['audit']['geojson_sha256']
            or audit != expected['audit']):
        raise ops.OperationError('候选内容或审计指纹不一致')
    cells = doc['road_cells']
    geometries = [shape(c['metric_geometry']) for c in cells]
    land = z.projected(doc['work_area'])
    combined = union_all(geometries, grid_size=.001)
    ids = [identity for c in cells for identity in c['merchant_ids']]
    from app.services.bd_grid_geometry import outline
    if (any(not g.is_valid or outline(g).geom_type != 'Polygon' or g.is_empty for g in geometries)
            or land.symmetric_difference(combined).area > .01
            or sum(g.area for g in geometries) - combined.area > .01
            or len({c['id'] for c in cells}) != len(cells)
            or len(ids) != len(set(ids)) or len(ids) != doc['store_count']):
        raise ops.OperationError('网格覆盖或商户唯一归属校验失败')
    if replace and not audit.get('quality_pass'):
        raise ops.OperationError('网格极端面积、形状或商户负载检查未通过，旧数据未清空')
    previous = []
    if replace:
        if db.bind.dialect.name == 'postgresql':
            db.execute(text('LOCK TABLE bd_zone_release, bd_region, bd_work_grid, bd_grid_store, bd_store_visit_log IN EXCLUSIVE MODE'))
        if db.query(BdStoreVisitLog.id).first() or db.query(BdWorkGrid.id).filter(
                (BdWorkGrid.assignee_id.isnot(None)) | (BdWorkGrid.progress != 0)
                | (BdWorkGrid.task_state != 'unvisited')).first():
            raise ops.OperationError('存在网格作业或巡店历史，不能直接清空重建')
        previous = [{'id': r.id, 'team_id': r.team_id, 'leader_id': r.leader_id} for r in db.query(BdZone).all()]
        db.query(BdGridStore).delete(synchronize_session=False)
        db.query(BdWorkGrid).delete(synchronize_session=False)
        db.query(BdZone).delete(synchronize_session=False)
        from sqlalchemy import inspect
        if inspect(db.connection()).has_table('bd_zone'):
            db.execute(text('DELETE FROM bd_zone'))
    release = BdZoneRelease(fingerprint=canonical_sha(result['features']), source=doc['source'],
                            parameters={'generation': doc.get('generation_version', 'natural-1')}, audit=audit,
                            reviews=doc.get('review_merchants', []), created_by=user.id)
    db.add(release)
    db.flush()
    for cell, geom in zip(cells, geometries):
        grid = BdWorkGrid(release_id=release.id, zone_id=None, source_id=cell['id'],
                          geometry=z.geographic(geom), area_km2=geom.area / 1e6,
                          store_count=len(cell['merchant_ids']), state='ready')
        db.add(grid)
        db.flush()
        db.add_all([BdGridStore(release_id=release.id, grid_id=grid.id, source_id=i)
                    for i in cell['merchant_ids']])
    ops.audit_log(db, user, 'zone', 'import_natural_grids', release, {'cleared_regions': previous},
                  {'grids': len(cells), 'fingerprint': release.fingerprint})
    return release
