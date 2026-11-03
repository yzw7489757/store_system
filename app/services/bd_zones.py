"""东京23区作业片区试算：只读地理快照，不读写结算域或正式任务。

坐标统一投影到日本平面直角 IX (EPSG:6677)，面积按剔除水域/林地后的
实际多边形计算。网格由道路街块合并，绝不为了凑面积制造直线/飞地。
不满足硬约束的结果仍可预览，但必须标为不可发布的草案。
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
from pathlib import Path

from pyproj import Transformer
from shapely import STRtree, make_valid, set_precision, union_all
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import polygonize, transform, unary_union

WARDS = tuple(zip(
    (str(13101 + i) for i in range(23)),
    ('千代田区', '中央区', '港区', '新宿区', '文京区', '台東区', '墨田区',
     '江東区', '品川区', '目黒区', '大田区', '世田谷区', '渋谷区', '中野区',
     '杉並区', '豊島区', '北区', '荒川区', '板橋区', '練馬区', '足立区', '葛飾区', '江戸川区'),
))
_TO_METERS = Transformer.from_crs(4326, 6677, always_xy=True).transform
_TO_LONLAT = Transformer.from_crs(6677, 4326, always_xy=True).transform
MIN_GRID = 500_000.0
MAX_GRID = 2_000_000.0
SHAPE_TOLERANCE = 1e-5  # 毫米精度旋转造成的无量纲形状舍入误差，不是业务放宽。
SCHEMA_VERSION = 1


class ZoneDataError(ValueError):
    pass


def data_dir() -> Path:
    return Path(os.environ.get('BD_ZONE_DATA_DIR', 'data/bd_zones'))


SCOPE_CODE = 'tokyo23'
SCOPE_NAME = '东京23区整体'


def ward_name(code: str) -> str:
    if code == SCOPE_CODE:
        return SCOPE_NAME
    if code not in dict(WARDS):
        raise ZoneDataError('请选择东京23区之一')
    return dict(WARDS)[code]


def load_snapshot(code: str) -> dict:
    ward_name(code)  # 校验后才拼接路径
    path = data_dir() / (code + '.json')
    if not path.exists():
        raise ZoneDataError('该区尚无地理快照，请先运行 scripts/bd_collect_zones.py 采集')
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        if doc.get('schema_version') != SCHEMA_VERSION or doc.get('ward_code') != code:
            raise ValueError('snapshot version')
        return doc
    except (ValueError, KeyError) as exc:
        raise ZoneDataError('地理快照无效，请重新采集') from exc



def merchant_source_mode():
    mode = os.environ.get('BD_ZONE_MERCHANT_SOURCE', 'postgresql')
    if mode not in ('postgresql', 'snapshot'):
        raise ZoneDataError('商户源配置无效，不能静默回退到旧CSV。')
    return mode


def load_scope_source():
    doc = load_snapshot(SCOPE_CODE)
    if doc.get('scope_ward_count') != 23:
        raise ZoneDataError('东京23区整体快照不完整，不能按单区结果拼接代替')
    if merchant_source_mode() == 'postgresql':
        from app.services.bd_zone_merchants import bind_database
        return bind_database(doc)
    return doc  # 仅显式离线/隔离测试模式，不是数据库失败时的回退。

def catalogue() -> list[dict]:
    rows = []
    for code, name in WARDS:
        row = {'code': code, 'name': name, 'ready': False}
        try:
            doc = load_snapshot(code)
            row.update(ready=True, grid_count=len(doc['grids']),
                       store_count=doc['store_count'], captured_at=doc['captured_at'],
                       boundary=doc['boundary'], issues=doc.get('issues', []))
        except ZoneDataError:
            pass
        rows.append(row)
    return rows


def overview():
    rows = catalogue()
    ready = all(row['ready'] for row in rows)
    boundary = geographic(unary_union([projected(row['boundary']) for row in rows if row['ready']])) if any(row['ready'] for row in rows) else None
    try:
        doc = load_snapshot(SCOPE_CODE)
    except ZoneDataError:
        doc = None
    return {'code': SCOPE_CODE, 'name': SCOPE_NAME, 'ready': ready and doc is not None,
            'ward_count': sum(row['ready'] for row in rows), 'boundary': boundary,
            'store_count': doc['store_count'] if doc else sum(row.get('store_count', 0) for row in rows),
            'grid_count': len(doc.get('road_cells') or doc['grids']) if doc else 0,
            'merchant_source': 'public.kanto_merchants_v20261029' if merchant_source_mode() == 'postgresql' else 'snapshot',
            'store_count_pending': merchant_source_mode() == 'postgresql',
            'coverage_source': bool(doc and doc.get('coverage_revision') in (1, 2)),
            'captured_at': doc['captured_at'] if doc else ''}


def polygons(geom):
    """只保留有效面，不把 make_valid 产生的线当作作业面积。"""
    if geom.is_empty:
        return []
    if geom.geom_type == 'Polygon':
        return [geom]
    return [p for g in getattr(geom, 'geoms', ()) for p in polygons(g)]


def projected(geojson):
    # 每个面分别舍入会在原本共边的网格之间制造裂缝和假孤岛。
    return set_precision(make_valid(transform(_TO_METERS, shape(geojson))), .001)


def geographic(geom):
    return mapping(transform(_TO_LONLAT, geom))


def neighbours(geoms):
    """共边邻接；只容忍投影往返的微米误差，不连接角点或真实分离陆块。"""
    tree = STRtree(geoms)
    graph = {i: set() for i in range(len(geoms))}
    for i, geom in enumerate(geoms):
        for raw_j in tree.query(geom.buffer(.002)):
            j = int(raw_j)
            if j > i and geom.boundary.intersection(geoms[j].boundary.buffer(.002)).length > .2:
                graph[i].add(j)
                graph[j].add(i)
    return graph


def _group(geoms, counts, target_count, max_area, *, adjacency=None, merge_guard=None):
    """有界、确定性的邻接合并。面积优先，店铺均衡次之，兼顾紧凑形状。"""
    graph = neighbours(geoms) if adjacency is None else {i:set(values) for i,values in adjacency.items()}
    groups = {i: {'geom': g, 'members': {i}, 'count': counts[i], 'version': 0}
              for i, g in enumerate(geoms)}
    target_area = sum(g.area for g in geoms) / max(target_count, 1)
    target_stores = max(sum(counts) / max(target_count, 1), 1)
    queue = []
    has_stores = any(counts)

    def push(i, j):
        if i > j:
            i, j = j, i
        a, b = groups[i], groups[j]
        if a['geom'].area + b['geom'].area > max_area + 1:
            return
        # Ward 增量：避免先生成超大片再剩下一堆小片。
        area_cost = 2 * a['geom'].area * b['geom'].area / max(target_area ** 2, 1)
        store_cost = 2 * a['count'] * b['count'] / target_stores ** 2
        shared = a['geom'].boundary.intersection(
            b['geom'].boundary.buffer(.002) if adjacency is None else b['geom'].boundary).length
        # 内部排除地形的孔洞不应左右片区外轮廓的紧凑度。
        perimeter = a['geom'].exterior.length + b['geom'].exterior.length - 2 * shared
        compactness = perimeter ** 2 / (4 * math.pi * (a['geom'].area + b['geom'].area))
        contact = shared / max(math.sqrt(min(a['geom'].area, b['geom'].area)), 1)
        score = (area_cost + 0.35 * store_cost
                 + (0.8 if has_stores else .04) * compactness
                 + (.25 / max(contact, .01) if has_stores else 0))
        heapq.heappush(queue, (score, i, j, a['version'], b['version']))

    for i, adjacent in graph.items():
        for j in adjacent:
            if j > i:
                push(i, j)
    while len(groups) > target_count and queue:
        _, i, j, vi, vj = heapq.heappop(queue)
        if i not in groups or j not in groups:
            continue
        a, b = groups[i], groups[j]
        if (vi, vj) != (a['version'], b['version']):
            continue
        merged = union_all([a['geom'], b['geom']], grid_size=.001)
        if merged.geom_type != 'Polygon':
            continue
        if merge_guard is not None and not merge_guard(merged):
            continue
        a.update(geom=merged, members=a['members'] | b['members'],
                 count=a['count'] + b['count'], version=a['version'] + 1)
        adjacent = (graph[i] | graph[j]) - {i, j}
        del groups[j]
        for k in adjacent:
            graph[k].discard(j)
            graph[k].discard(i)
            graph[k].add(i)
        graph[i] = adjacent
        del graph[j]
        for k in adjacent:
            push(i, k)
    return list(groups.values())


def _rebalance(groups, geoms, counts, max_area):
    """只移动完整网格；移动前验证原片区仍连续，绝不切断任务网格。"""
    if len(groups) < 2:
        return groups
    graph = neighbours(geoms)
    members = [set(g['members']) for g in groups]
    mean_area = sum(g.area for g in geoms) / len(groups)
    mean_count = max(sum(counts) / len(groups), 1)
    areas = [sum(geoms[i].area for i in part) for part in members]
    stores = [sum(counts[i] for i in part) for part in members]

    def connected(part):
        if not part:
            return False
        seen, pending = set(), [min(part)]
        while pending:
            i = pending.pop()
            if i not in seen:
                seen.add(i)
                pending.extend((graph[i] & part) - seen)
        return len(seen) == len(part)

    for _ in range(min(len(geoms) * 2, 300)):
        owner = {i: k for k, part in enumerate(members) for i in part}
        best = None
        for a, part in enumerate(members):
            if len(part) <= 1:
                continue
            for i in sorted(part):
                for b in sorted({owner[j] for j in graph[i]} - {a}):
                    size, count = geoms[i].area, counts[i]
                    if areas[b] + size > max_area + 1 or not connected(part - {i}):
                        continue
                    delta = (2 * size * (areas[b] - areas[a] + size) / mean_area ** 2
                             + .15 * 2 * count * (stores[b] - stores[a] + count) / mean_count ** 2)
                    # 面积2倍是硬约束，不可被门店均衡的软权重抵消。
                    before = max(0, max(areas) / min(areas) - 2)
                    updated = list(areas)
                    updated[a] -= size
                    updated[b] += size
                    after = max(0, max(updated) / min(updated) - 2)
                    hard_delta = after - before
                    improves = (hard_delta < -1e-8 or
                                (abs(hard_delta) <= 1e-8 and delta < -1e-8))
                    candidate = (hard_delta, delta, i, a, b)
                    if improves and (best is None or candidate < best):
                        best = candidate
        if best is None:
            break
        _, _, i, a, b = best
        members[a].remove(i)
        members[b].add(i)
        areas[a] -= geoms[i].area
        areas[b] += geoms[i].area
        stores[a] -= counts[i]
        stores[b] += counts[i]
    return [{'geom': union_all([geoms[i] for i in sorted(part)], grid_size=.001),
             'members': part, 'count': stores[k]} for k, part in enumerate(members)]


def _shape_metrics(geom):
    """评价真实外轮廓；水域/林地孔洞不作为形状凹陷，也不填进作业区。"""
    if geom.geom_type != 'Polygon' or geom.is_empty:
        return {'solidity': 0., 'aspect': float('inf'), 'compactness': 0.}
    shell = Polygon(geom.exterior)
    hull = shell.convex_hull
    rectangle = hull.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)
    sides = [math.dist(coords[i], coords[i + 1]) for i in range(4)]
    return {'solidity': shell.area / max(hull.area, 1),
            'aspect': max(sides) / max(min(sides), .001),
            'compactness': 4 * math.pi * shell.area / max(shell.length ** 2, 1)}


def _has_narrow_neck(geom):
    if geom.geom_type != 'Polygon':
        return True
    shell = Polygon(geom.exterior)
    erosion = min(150., math.sqrt(shell.area) * .06)
    substantial = [p for p in polygons(shell.buffer(-erosion)) if p.area > shell.area * .04]
    return len(substantial) > 1


def _acceptable_shape(geom):
    quality = _shape_metrics(geom)
    return (quality['solidity'] >= .65 - SHAPE_TOLERANCE
            and quality['aspect'] <= 3 + SHAPE_TOLERANCE
            and not _has_narrow_neck(geom))


def _compact_partition(geoms, counts, target_count, max_area):
    """空间递归分块，只分配完整道路网格，不画直线、不凸包填水域。

    切分候选由多个空间方向产生，必须保证两侧都连通。片区凹陷、细长比
    和共享边界长度优先于店铺软均衡；禁止再用忽略形状的旧找平器二次扭曲。
    """
    graph = neighbours(geoms)
    areas = [g.area for g in geoms]
    centers = [g.centroid for g in geoms]
    merged_cache, score_cache = {}, {}
    # 只在评分时简化轮廓。性能基线中90%以上耗时来自反复合并完整道路点列。
    # 最终输出和连通判断仍使用原始网格，绝不把这些评分代理当作实际边界。
    score_geoms = [Polygon(g.exterior).simplify(10, preserve_topology=True)
                   .buffer(12, join_style=2) for g in geoms]

    def score_joined(part):
        key = frozenset(part)
        if key not in score_cache:
            score_cache[key] = union_all([score_geoms[i] for i in sorted(part)], grid_size=.1)
        return score_cache[key]

    def joined(part):
        key = frozenset(part)
        if key not in merged_cache:
            merged_cache[key] = union_all([geoms[i] for i in sorted(part)], grid_size=.001)
        return merged_cache[key]

    def connected(part):
        if not part:
            return False
        seen, todo = set(), [min(part)]
        while todo:
            i = todo.pop()
            if i not in seen:
                seen.add(i)
                todo.extend((graph[i] & part) - seen)
        return seen == part

    def split(part, number):
        total = sum(areas[i] for i in part)
        number = min(len(part), max(number, math.ceil(total / max_area)))
        if len(part) == 1:
            return [part]
        if number <= 1:
            if _acceptable_shape(joined(part)):
                return [part]
            number = 2  # 细长/深凹/细颈片区不能因配额已用完就直接作为结果。
        left_number = number // 2
        right_number = number - left_number
        fraction = left_number / number
        total_stores = max(sum(counts[i] for i in part), 1)
        candidates, signatures = [], set()
        # 方向只用于决定网格归属；最终边界仍完全由已有道路网格组成。
        for direction in range(8):
            angle = direction * math.pi / 8
            dx, dy = math.cos(angle), math.sin(angle)
            ordered = sorted(part, key=lambda i: (centers[i].x * dx + centers[i].y * dy, i))
            prefix, amount, stores = set(), 0., 0
            for pos, i in enumerate(ordered[:-1], 1):
                prefix.add(i)
                amount += areas[i]
                stores += counts[i]
                if pos < left_number or len(part) - pos < right_number:
                    continue
                # 每层只容忍少量面积偏差，避免多层递归累积成悬殊片区。
                if abs(amount / total - fraction) > .09:
                    continue
                if amount / left_number > max_area + 1 or (total - amount) / right_number > max_area + 1:
                    continue
                key = frozenset(prefix)
                if key in signatures:
                    continue
                signatures.add(key)
                other = part - prefix
                if connected(prefix) and connected(other):
                    cheap = abs(amount / total - fraction) + .025 * abs(stores / total_stores - fraction)
                    candidates.append((cheap, key))
        if not candidates:
            # 凹形海岸等可能没有两侧都连通的投影切分。空间BFS树的子树切分
            # 保证两侧连通，仍只移动完整网格，绝不拿飞地凑数。
            from collections import deque
            for root in sorted(part, key=lambda i: (centers[i].x, centers[i].y))[:1] + sorted(part, key=lambda i: (-centers[i].x, -centers[i].y))[:1]:
                parent, order, todo = {root: None}, [], deque([root])
                while todo:
                    i = todo.popleft()
                    order.append(i)
                    for j in sorted(graph[i] & part):
                        if j not in parent:
                            parent[j] = i
                            todo.append(j)
                subtrees = {i: {i} for i in part}
                for i in reversed(order):
                    if parent[i] is not None:
                        subtrees[parent[i]].update(subtrees[i])
                    for child in (subtrees[i], part - subtrees[i]):
                        if len(child) < left_number or len(part - child) < right_number:
                            continue
                        amount = sum(areas[j] for j in child)
                        if abs(amount / total - fraction) <= .2:
                            candidates.append((abs(amount / total - fraction), frozenset(child)))
        if not candidates:
            return [part]  # 明确留下不可满足的草案，不能递归丢失网格。
        scored = []
        for cheap, frozen in sorted(candidates, key=lambda c: (c[0], tuple(sorted(c[1]))))[:28]:
            left = set(frozen)
            right = part - left
            a, b = score_joined(left), score_joined(right)
            if a.geom_type != 'Polygon' or b.geom_type != 'Polygon':
                continue
            qa, qb = _shape_metrics(a), _shape_metrics(b)
            # 两个子块外周越短、外包填充率越高，切出来就越接近块状。
            concavity = (1 - qa['solidity']) ** 2 + (1 - qb['solidity']) ** 2
            elongated = max(0, qa['aspect'] - 2.2) ** 2 + max(0, qb['aspect'] - 2.2) ** 2
            perimeter = (Polygon(a.exterior).length + Polygon(b.exterior).length) / math.sqrt(total)
            score = 5 * cheap + 3 * concavity + .25 * elongated + .045 * perimeter
            scored.append((score, tuple(sorted(left)), left, right))
        if not scored:
            return [part]
        _, _, left, right = min(scored, key=lambda row: (row[0], row[1]))
        return split(left, left_number) + split(right, right_number)

    unseen, components = set(range(len(geoms))), []
    while unseen:
        todo, part = [min(unseen)], set()
        while todo:
            i = todo.pop()
            if i not in part:
                part.add(i)
                todo.extend(graph[i] - part)
        unseen -= part
        components.append(part)
    total = sum(areas)
    allocations = [max(1, math.ceil(sum(areas[i] for i in part) / max_area),
                       round(target_count * sum(areas[i] for i in part) / total)) for part in components]
    while sum(allocations) < target_count:
        choices = [k for k, part in enumerate(components) if allocations[k] < len(part)]
        if not choices:
            break
        best = max(choices, key=lambda k: sum(areas[i] for i in components[k]) / allocations[k])
        allocations[best] += 1
    groups = []
    for part, number in zip(components, allocations):
        for members in split(part, number):
            groups.append({'geom': joined(members), 'members': members,
                           'count': sum(counts[i] for i in members)})
    return groups


def coastline_land(border, coastlines):
    """OSM海岸线的陆地在行进方向左侧；海面不一定有natural=water面。

    用真实海岸线分割行政边界，按共边的陆/海侧判定保留面；不靠商户点
    猜测陆地，也不把整个沿海行政区直接当作可作业范围。
    """
    # 裁剪/节点化使用双精度；逐点舍入会让海岸线端点离开边界几厘米，
    # 导致原本闭合的岸线不能polygonize。
    border = set_precision(border, 0)
    lines = [transform(_TO_METERS, shape(g)) for g in coastlines]
    lines = [line for line in lines if line.intersects(border)]
    if not lines:
        return border
    left = unary_union([line.buffer(1, single_sided=True) for line in lines])
    right = unary_union([line.buffer(-1, single_sided=True) for line in lines])
    # 在同一次节点化中处理完整岸线和边界，避免先裁剪造成浮点端点裂缝。
    faces = [face for face in polygonize(unary_union([border.boundary, *lines]))
             if border.covers(face.representative_point())]
    kept = [face for face in faces if face.intersection(left).area > face.intersection(right).area]
    if not kept:
        raise ZoneDataError('海岸线无法确认有效陆地范围，停止生成')
    return set_precision(unary_union(kept), .1)


def prepare_snapshot(code, boundary, roads, excluded, merchants, *, captured_at, source, coastlines=()):
    """从实际道路生成可复用的网格快照；外部采集只在 CLI 执行。"""
    name = ward_name(code)
    border = projected(boundary)
    masks = [projected(g) for g in excluded]
    land = coastline_land(border, coastlines)
    land = land.difference(unary_union(masks)) if masks else land
    land = set_precision(make_valid(land), 0)
    segments = [land.boundary]
    for road in roads:
        line = transform(_TO_METERS, shape(road))
        if not line.is_empty and line.intersects(land):
            segments.append(line)
    # 纳入所有面（含无店铺的街块），不是只围住商户点。
    atoms = [p for p in polygonize(union_all(segments, grid_size=.001))
             if p.area > .01 and land.covers(p.representative_point())]
    atoms.sort(key=lambda p: (p.centroid.x, p.centroid.y, p.area))
    if not atoms:
        raise ZoneDataError('行政边界内没有可用道路街块')
    tree = STRtree(atoms)
    counts = [0] * len(atoms)
    located = 0
    for lng, lat in merchants:
        point = transform(_TO_METERS, Point(lng, lat))
        hits = tree.query(point, predicate='intersects')
        if len(hits):
            counts[min(int(i) for i in hits)] += 1
            located += 1
    # 在网格生成阶段只按面积，不用门店数扭曲已确认的1km²规则。
    # 河流/林地会产生真实离散地块。每个连通分量分别计入所需网格数，
    # 不能让大量小岛迫使主城区的网格一路合并到2km²上限。
    atom_graph = neighbours(atoms)
    unseen = set(atom_graph)
    n = 0
    while unseen:
        pending, component = [min(unseen)], set()
        while pending:
            i = pending.pop()
            if i in component:
                continue
            component.add(i)
            pending.extend(atom_graph[i] - component)
        unseen -= component
        n += max(1, round(sum(atoms[i].area for i in component) / 1_000_000))
    groups = _group(atoms, [0] * len(atoms), n, MAX_GRID)
    # 小于0.5km²的尾片只与共边邻居合并，不连接飞地。
    for _ in range(len(groups)):
        small = [i for i, g in enumerate(groups) if g['geom'].area < MIN_GRID - 1]
        if not small:
            break
        adjacent = neighbours([g['geom'] for g in groups])
        merged = False
        for i in small:
            choices = [j for j in adjacent[i] if groups[i]['geom'].area + groups[j]['geom'].area <= MAX_GRID + 1]
            if not choices:
                continue
            j = min(choices, key=lambda k: (groups[k]['geom'].area, k))
            groups[j]['geom'] = unary_union([groups[j]['geom'], groups[i]['geom']])
            groups[j]['members'] |= groups[i]['members']
            groups.pop(i)
            merged = True
            break
        if not merged:
            break
    groups.sort(key=lambda g: (g['geom'].centroid.x, g['geom'].centroid.y))
    grids = []
    for g in groups:
        geom = g['geom']
        digest = hashlib.sha256(geom.normalize().wkb).hexdigest()[:10]
        grids.append({'id': code + '-G-' + digest, 'geometry': geographic(geom),
                      'area_km2': geom.area / 1_000_000,
                      'store_count': sum(counts[i] for i in g['members'])})
    issues = []
    invalid = sum(not MIN_GRID - 1 <= g['area_km2'] * 1_000_000 <= MAX_GRID + 1 for g in grids)
    if invalid:
        issues.append({'code': 'grid_area', 'count': invalid})
    missing = land.area - sum(g['geom'].area for g in groups)
    if abs(missing) > max(10, land.area * .0001):
        issues.append({'code': 'coverage', 'area_km2': missing / 1_000_000})
    version = hashlib.sha256(json.dumps(grids, sort_keys=True).encode()).hexdigest()[:12]
    return {'schema_version': SCHEMA_VERSION, 'generation_version': 3, 'ward_code': code, 'ward_name': name,
            'snapshot_id': version, 'captured_at': captured_at, 'source': source,
            'coastline_applied': True,
            'boundary': geographic(border), 'work_area': geographic(land),
            'area_km2': land.area / 1_000_000, 'excluded_area_km2': (border.area - land.area) / 1_000_000,
            'store_count': located, 'grids': grids, 'issues': issues,
            'road_count': len(roads)}


def preview(code, target_stores, max_area_km2, *, area_weight=.25, load_min=.75, load_max=1.35):
    if (isinstance(target_stores, bool) or not 1 <= target_stores <= 100_000
            or not math.isfinite(max_area_km2) or not 2 <= max_area_km2 <= 500):
        raise ZoneDataError('试算参数超出范围')
    doc = load_snapshot(code)
    if code == SCOPE_CODE and doc.get('scope_ward_count') != 23:
        raise ZoneDataError('东京23区整体快照不完整，不能按单区结果拼接代替')
    if code == SCOPE_CODE:
        from app.services.bd_zone_coverage import scope_preview
        from app.services.bd_zone_balance import BalanceOptions
        options = BalanceOptions(max_area_km2, target_stores, area_weight, load_min, load_max).validate()
        doc = load_scope_source()
        return scope_preview(doc, target_stores, max_area_km2, balance_options=options)
    if not doc['grids']:
        raise ZoneDataError('该区没有可用任务网格')
    pending_grids = [g for g in doc['grids'] if not .5 - 1e-6 <= g['area_km2'] <= 2 + 1e-6]
    grids = [g for g in doc['grids'] if .5 - 1e-6 <= g['area_km2'] <= 2 + 1e-6]
    if not grids:
        raise ZoneDataError('没有满足0.5～2km²面积约束的网格；请补充道路数据后重新采集')
    geoms = [projected(g['geometry']) for g in grids]
    isolated_count = 0
    if code == SCOPE_CODE:
        initial_target = max(1, math.ceil(sum(g['store_count'] for g in grids) / target_stores),
                             math.ceil(sum(g.area for g in geoms) / (max_area_km2 * 1_000_000)))
        minimum_zone = sum(g.area for g in geoms) / initial_target / 2
        graph0, unseen, components = neighbours(geoms), set(range(len(geoms))), []
        while unseen:
            part, todo = set(), [min(unseen)]
            while todo:
                i = todo.pop()
                if i not in part:
                    part.add(i)
                    todo.extend(graph0[i] - part)
            unseen -= part
            components.append(part)
        small = set.union(*(part for part in components if sum(geoms[i].area for i in part) < minimum_zone)) if any(sum(geoms[i].area for i in part) < minimum_zone for part in components) else set()
        if small and len(small) < len(grids):
            isolated_count = len(small)
            pending_grids.extend(g for i,g in enumerate(grids) if i in small)
            grids = [g for i,g in enumerate(grids) if i not in small]
            geoms = [g for i,g in enumerate(geoms) if i not in small]
    counts = [g['store_count'] for g in grids]
    total_area = sum(g.area for g in geoms)
    requested = max(1, math.ceil(sum(counts) / target_stores),
                    math.ceil(total_area / (max_area_km2 * 1_000_000)))
    graph = neighbours(geoms)
    unseen, component_areas = set(graph), []
    while unseen:
        pending, part = [min(unseen)], set()
        while pending:
            i = pending.pop()
            if i not in part:
                part.add(i)
                pending.extend(graph[i] - part)
        unseen -= part
        component_areas.append(sum(geoms[i].area for i in part))
    # 离散陆块必须各自连续。最小陆块面积S意味着任何合法片区都不能超过
    # 2S；否则无论怎样调整店铺目标，该区的面积比也不可能达标。
    ratio_cap = 2 * min(component_areas)
    impossible_ratio = max(g.area for g in geoms) > ratio_cap + 1
    # 无解的小岛不能迫使所有大陆网格退化为单独片区。保留草案告警，
    # 大陆继续按店铺目标和正常面积上限分组。
    cap = max_area_km2 * 1_000_000 if impossible_ratio else min(max_area_km2 * 1_000_000, ratio_cap)
    desired = min(max(requested, len(component_areas), math.ceil(total_area / cap)), len(grids))
    def partition(number):
        if code == SCOPE_CODE:
            return _compact_partition(geoms, counts, number, cap)
        parts = _group(geoms, counts, number, cap)
        return _rebalance(parts, geoms, counts, cap)

    def area_ratio(parts):
        sizes = [p['geom'].area for p in parts]
        return max(sizes) / min(sizes)

    groups = partition(desired)
    # 店铺目标推算的是建议数量，不是硬配额。优先寻找附近可满足2倍面积的
    # 数量，而不是把一个其实能合法划分的区直接判为失败。
    if code == SCOPE_CODE and not impossible_ratio and area_ratio(groups) > 2 + 1e-5:
        for _ in range(2):
            next_cap = min(cap, 2 * min(g['geom'].area for g in groups))
            next_number = max(desired, math.ceil(total_area / next_cap))
            if next_number > desired * 2 or next_cap < max(g.area for g in geoms):
                break
            candidate = _compact_partition(geoms, counts, next_number, next_cap)
            if area_ratio(candidate) >= area_ratio(groups) - 1e-5:
                break
            groups = candidate
            if area_ratio(groups) <= 2 + 1e-5:
                break
    if code != SCOPE_CODE and not impossible_ratio and area_ratio(groups) > 2 + 1e-5:
        minimum = max(len(component_areas), math.ceil(total_area / cap))
        candidates = []
        for offset in range(1, 7):
            candidates.extend(k for k in (desired + offset, desired - offset)
                              if minimum <= k <= len(grids))
        for number in candidates:
            candidate = partition(number)
            if area_ratio(candidate) < area_ratio(groups):
                groups = candidate
            if area_ratio(groups) <= 2 + 1e-5:
                break
    groups.sort(key=lambda g: (g['geom'].centroid.x, g['geom'].centroid.y))
    issues = list(doc.get('issues', []))
    if isolated_count:
        issues.append({'code': 'isolated_land', 'count': isolated_count})
    areas = [g['geom'].area / 1_000_000 for g in groups]
    ratio = max(areas) / min(areas)
    if ratio > 2 + 1e-5:
        issues.append({'code': 'ratio_infeasible' if impossible_ratio else 'zone_ratio', 'ratio': round(ratio, 3)})
    shape_failures = sum(not _acceptable_shape(g['geom']) for g in groups) if code == SCOPE_CODE else 0
    if shape_failures:
        issues.append({'code': 'zone_shape', 'count': shape_failures})
    if max(areas) > max_area_km2 + 1e-5:
        issues.append({'code': 'zone_area'})
    if len(groups) != requested:
        issues.append({'code': 'zone_count', 'requested': requested, 'actual': len(groups), 'severity': 'info'})
    if sum(counts) == 0:
        issues.append({'code': 'no_stores'})
    features, owner = [], {}
    for group in groups:
        ids = sorted(grids[i]['id'] for i in group['members'])
        digest = hashlib.sha256('|'.join(ids).encode()).hexdigest()[:10]
        zone_id = code + '-Z-' + digest
        features.append({'type': 'Feature', 'id': zone_id, 'geometry': geographic(group['geom']),
                         'properties': {'id': zone_id, 'level': 'zone', 'ward_code': code,
                                        'grid_count': len(ids), 'store_count': group['count'],
                                        'shape': _shape_metrics(group['geom']) if code == SCOPE_CODE else None,
                                        'area_km2': group['geom'].area / 1_000_000}})
        owner.update({i: zone_id for i in group['members']})
    grid_features = [{'type': 'Feature', 'id': g['id'], 'geometry': g['geometry'],
                      'properties': {'id': g['id'], 'level': 'grid', 'zone_id': owner[i],
                                     'area_km2': g['area_km2'], 'store_count': g['store_count']}}
                     for i, g in enumerate(grids)]
    return {'type': 'FeatureCollection', 'features': features, 'grids': grid_features,
            'boundary': doc['boundary'], 'work_area': doc['work_area'],
            'pending_grids': [{'type': 'Feature', 'id': g['id'], 'geometry': g['geometry'],
                               'properties': {'id': g['id'], 'level': 'pending',
                                              'area_km2': g['area_km2'], 'store_count': g['store_count']}}
                              for g in pending_grids],
            'meta': {k: doc[k] for k in ('ward_code', 'ward_name', 'snapshot_id', 'captured_at',
                                       'source', 'store_count', 'area_km2', 'excluded_area_km2', 'road_count')},
            'summary': {'zone_count': len(groups), 'grid_count': len(grids),
                        'area_ratio': ratio, 'min_area_km2': min(areas), 'max_area_km2': max(areas),
                        'target_stores': target_stores, 'max_area_km2_limit': max_area_km2,
                        'eligible': not any(i.get('severity') != 'info' for i in issues), 'pending_grid_count': len(pending_grids),
                        'pending_store_count': sum(g['store_count'] for g in pending_grids),
                        'pending_area_km2': sum(g['area_km2'] for g in pending_grids)}, 'issues': issues,
            'preview_only': True}
