"""正式划分的片区规则：包含归并、零商户排除、数量硬上限。

外部地块沿真实共边或经验证的窄河道归并；填平外环内洞仅用于判断包含关系，
真实面积/网格仍使用扣除水域的原始几何。无法满足数量上限时拒绝生成。
"""
from __future__ import annotations

import math
from shapely import STRtree, union_all, shortest_line
from shapely.geometry import Polygon


def count_limit(area_km2, target_area_km2):
    from app.services.bd_zones import ZoneDataError
    if not math.isfinite(area_km2) or area_km2 <= 0 or not math.isfinite(target_area_km2) or target_area_km2 <= 0:
        raise ZoneDataError('片区面积参数必须为正数')
    return max(1, math.floor(area_km2 / target_area_km2 * 3))


def shell(geometry):
    """仅用于包含判断，不作为面积、覆盖审计或执行围栏。"""
    if geometry.geom_type == 'Polygon':
        return Polygon(geometry.exterior)
    return union_all([Polygon(part.exterior) for part in geometry.geoms if part.geom_type == 'Polygon'])


def river_crossing(left, right, rivers, max_width_m):
    """局部两岸最短连线必须完全穿过已知河道，距离小不等同河窄。

    河宽指本次归并连接位置的两岸间距，不是整条河的最大宽度。
    1厘米仅容忍投影/边界舍入；不向土地、普通空白或海湾扩张。
    """
    if rivers is None or rivers.is_empty or max_width_m <= 0:
        return None
    crossing = shortest_line(left, right)
    width = crossing.length
    if width <= .01 or width > max_width_m + .001:
        return None
    if not rivers.covers(crossing) and not rivers.buffer(.01).covers(crossing):
        return None
    return crossing


def river_merge_shape_valid(geometry):
    """窄河只是准入条件；整片须紧凑，主要陆块不得有狭颈/长条。

    保留真实水域，不用填洞后的单面指标掩盖多个主体间的大范围空白。
    """
    from app.services import bd_zones as z
    parts = z.polygons(geometry)
    if not parts:
        return False
    total = sum(part.area for part in parts)
    substantial = [part for part in parts if part.area >= total*.04]
    if any(not z._acceptable_shape(part) for part in substantial):
        return False
    exterior = shell(geometry)
    hull = exterior.convex_hull
    if exterior.area / max(hull.area,1.) < .65:
        return False
    rectangle = hull.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)
    sides = [math.dist(coords[i],coords[i+1]) for i in range(4)]
    return max(sides)/max(min(sides),.001) <= 3


def consolidate(parts, geoms, counts, graph, options, *, area_km2, rivers=None, max_river_width_m=100.):
    from app.services.bd_zones import ZoneDataError
    if not math.isfinite(max_river_width_m) or max_river_width_m < 0:
        raise ZoneDataError('河宽上限必须为非负有限数值')
    limit = count_limit(area_km2, options.target_area)
    groups = [set(part) for part in parts]
    expected = set(range(len(geoms)))
    members = [i for group in groups for i in group]
    if set(members) != expected or len(members) != len(expected):
        raise ZoneDataError('片区基础面必须完整且唯一归属')
    initial = len(groups)
    containment_merges = adjacent_merges = 0
    river_merges = []

    def geometry(group):
        return union_all([geoms[i] for i in sorted(group)], grid_size=.001)

    # 从小到大寻找最小包围片区，避免包含地块仍各自成为一个片区。
    shapes = [geometry(group) for group in groups]
    shells = [shell(value) for value in shapes]
    tree = STRtree(shells)
    parent = list(range(len(groups)))
    for i in sorted(range(len(groups)), key=lambda i: (shells[i].area, i)):
        candidates = [int(j) for j in tree.query(shapes[i]) if int(j) != i
                      and shells[int(j)].area > shells[i].area
                      and shells[int(j)].covers(shapes[i])]
        if candidates:
            parent[i] = min(candidates, key=lambda j: (shells[j].area, j))
    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    consolidated = {}
    for i, group in enumerate(groups):
        consolidated.setdefault(root(i), set()).update(group)
        containment_merges += parent[i] != i
    groups = [consolidated[key] for key in sorted(consolidated)]

    excluded = set().union(*(group for group in groups if sum(counts[i] for i in group) == 0)) if groups else set()
    # 绑定不一致不能通过“无商户”排除掩盖；调用方还会核对merchant_ids。
    groups = [group for group in groups if sum(counts[i] for i in group) > 0]
    if not groups:
        raise ZoneDataError('有效范围内没有商户，不生成作业片区')

    # 仅维护每个当前片区的一份几何；每次合并只计算受影响片区。
    shapes = [geometry(group) for group in groups]
    outlines = [shell(value) for value in shapes]

    def merge(left, right):
        groups[left] |= groups[right]
        shapes[left] = union_all([shapes[left], shapes[right]], grid_size=.001)
        outlines[left] = shell(shapes[left])
        for values in (groups, shapes, outlines):
            values.pop(right)

    def shape_cost(value):
        from app.services import bd_zones as z
        parts = z.polygons(value)
        total = sum(part.area for part in parts)
        score = 0.
        for part in parts:
            quality = z._shape_metrics(part)
            weight = part.area/max(total,1.)
            score += weight*(8*max(0.,.75-quality['solidity'])**2
                + max(0.,quality['aspect']-2.2)**2
                + 4*max(0.,.28-quality['compactness'])**2
                + int(z._has_narrow_neck(part)))
        return score

    # 真实共边优先；允许窄河归并时仍保持真实几何和负载上限。
    while len(groups) > limit:
        owners = {i: k for k, group in enumerate(groups) for i in group}
        pairs = {tuple(sorted((owners[i], owners[j]))) for i in owners for j in graph[i]
                 if j in owners and owners[i] != owners[j]}
        crossings = {}
        if rivers is not None and not rivers.is_empty and max_river_width_m > 0:
            near = STRtree(shapes)
            for left, value in enumerate(shapes):
                for raw_right in near.query(value, predicate='dwithin', distance=max_river_width_m):
                    right = int(raw_right)
                    if right <= left or (left, right) in pairs:
                        continue
                    crossing = river_crossing(value, shapes[right], rivers, max_river_width_m)
                    if crossing is not None:
                        pairs.add((left, right))
                        crossings[left, right] = crossing
        choices = []
        for left, right in sorted(pairs):
            joined = groups[left] | groups[right]
            area = sum(geoms[i].area for i in joined)
            stores = sum(counts[i] for i in joined)
            load = options.score(area, stores)
            if load <= options.load_max:
                merged = union_all([shapes[left],shapes[right]],grid_size=.001)
                if (left,right) in crossings and not river_merge_shape_valid(merged):
                    continue
                before = shape_cost(shapes[left])+shape_cost(shapes[right])
                after = shape_cost(merged)
                # 数量上限不能把刚修好的狭长片区重新合成原来的长条。
                from app.services import bd_zones as z
                acceptable = all(z._acceptable_shape(part) for part in z.polygons(merged) if part.area > merged.area*.04)
                if acceptable or after <= before + .01:
                    choices.append(((left, right) in crossings, after-before, load, left, right))
        if not choices:
            raise ZoneDataError(f'片区数量上限为{limit}，但仍有{len(groups)}个有商户的独立片区；'
                                f'包含归并、空商户排除及不超过{max_river_width_m:g}米的已知河道检查后，'
                                '无法在负载上限和形状不退化约束内继续合并，禁止跨外部水域强行拼接。')
        is_river, _, _, left, right = min(choices)
        if is_river:
            crossing = crossings[left, right]
            river_merges.append(dict(width_m=round(crossing.length, 3),
                crossing_metric=list(crossing.coords),
                merged_merchants=sum(counts[i] for i in groups[left] | groups[right])))
        merge(left, right)
        adjacent_merges += not is_river
        # 新闭合内洞的检查复用当前几何，不重新并集所有基础面。
        changed = True
        while changed:
            changed = False
            tree = STRtree(outlines)
            for inner in sorted(range(len(groups)), key=lambda i: (outlines[i].area, i)):
                containers = [int(outer) for outer in tree.query(shapes[inner]) if int(outer) != inner
                              and outlines[int(outer)].area > outlines[inner].area
                              and outlines[int(outer)].covers(shapes[inner])]
                if containers:
                    outer = min(containers, key=lambda i: (outlines[i].area, i))
                    merge(outer, inner)
                    containment_merges += 1
                    changed = True
                    break
    return groups, excluded, dict(version=3, zone_count_multiplier=3, max_zone_count=limit, original_zone_count=initial,
        containment_merges=containment_merges, adjacent_merges=adjacent_merges,
        max_river_width_m=max_river_width_m, river_merges=river_merges,
        river_geometry_available=rivers is not None and not rivers.is_empty,
        excluded_cell_count=len(excluded), excluded_area_km2=sum(geoms[i].area for i in excluded)/1e6,
        zone_count=len(groups), reference_area_km2=area_km2, target_area_km2=options.target_area)
