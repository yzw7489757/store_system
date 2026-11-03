"""覆盖优先的23区预览。道路基础面不是任务网格，任何约束失败都不能删地块。

来源准备时固定商户→道路面映射；预览只改变责任单元/网格归属，不调用外网、
不写业务表。确认资格必须同时通过几何覆盖、商户归属、排除复核及业务约束。
"""
from __future__ import annotations

import bisect
import hashlib
import heapq
import math
from collections import deque

from shapely import STRtree, union_all, set_precision, multipoints
from shapely.geometry import mapping, shape

AREA_TOLERANCE_M2 = 1.0
MAX_COMPACT_SEEDS = 128  # 最远点初始化O(n*k)，高配额继续使用有界递归，避免平方级工作量。


def face_graph(geoms):
    """同一次polygonize的共边拓扑：仅匹配真实共边，不跨河/靠近即连接。"""
    owners, graph = {}, {i: set() for i in range(len(geoms))}
    for i, geom in enumerate(geoms):
        for ring in [geom.exterior, *geom.interiors]:
            coords = [(round(x, 3), round(y, 3)) for x, y, *_ in ring.coords]
            for a, b in zip(coords, coords[1:]):
                if a == b:
                    continue
                edge = (a, b) if a < b else (b, a)
                if edge in owners:
                    other = owners[edge]
                    if other != i:
                        graph[i].add(other)
                        graph[other].add(i)
                else:
                    owners[edge] = i
    return graph


def _components(graph, part=None):
    unseen = set(graph) if part is None else set(part)
    output = []
    while unseen:
        todo, group = [min(unseen)], set()
        while todo:
            i = todo.pop()
            if i not in group:
                group.add(i)
                todo.extend((graph[i] & unseen) - group)
        unseen -= group
        output.append(group)
    return output


def _tree_candidates(graph, part, areas, centers, nl, nr, ratio):
    """线性标量子树统计，只物化最多6组候选，不逐节点复制整片区域。"""
    size = sum(areas[i] for i in part)
    root = min(part, key=lambda i: (centers[i][0], centers[i][1]))
    parent, order, todo = {root: None}, [], deque([root])
    while todo:
        i = todo.popleft()
        order.append(i)
        for j in sorted(graph[i] & part):
            if j not in parent:
                parent[j] = i
                todo.append(j)
    # 不为每个节点保留整棵子树或左右大集合，避免O(n²)内存。
    children = {i: [] for i in part}
    sizes = {i: 1 for i in part}
    weights = {i: areas[i] for i in part}
    for i in reversed(order):
        p = parent[i]
        if p is not None:
            children[p].append(i)
            sizes[p] += sizes[i]
            weights[p] += weights[i]
    choices = []
    for i in order[1:]:
        for reverse in (False, True):
            nleft = len(part) - sizes[i] if reverse else sizes[i]
            nright = len(part) - nleft
            if nleft >= nl and nright >= nr:
                amount = size - weights[i] if reverse else weights[i]
                choices.append((abs(amount / size - ratio), i, reverse))
    result = []
    for balance, root, reverse in sorted(choices)[:6]:
        subtree, stack = set(), [root]
        while stack:
            i = stack.pop()
            subtree.add(i)
            stack.extend(children[i])
        left = part - subtree if reverse else subtree
        right = part - left
        result.append((balance, tuple(sorted(left)), left, right))
    return result


def _geodesic_candidates(graph, part, centers):
    """最多8种两端种子。按真实共边图生长，两侧始终连通，不跨河拼接。

    投影直切要求两侧同时连通，容易在弯折道路面上无解；图距离分水岭允许
    切口沿道路拐弯。每个节点仅归属一次，不在每个节点物化完整子树。
    """
    seen = set()
    for direction in range(8):
        angle = direction*math.pi/8
        dx, dy = math.cos(angle), math.sin(angle)
        def key(i):
            x,y = centers[i]
            return x*dx+y*dy,i
        a,b = min(part,key=key),max(part,key=key)
        seeds = tuple(sorted((a,b)))
        if a == b or seeds in seen:
            continue
        seen.add(seeds)
        owned, queue = {}, [(0.,0,a),(0.,1,b)]
        while queue:
            distance,owner,i = heapq.heappop(queue)
            if i in owned:
                continue
            owned[i] = owner
            for j in graph[i] & part:
                if j not in owned:
                    step = max(math.dist(centers[i],centers[j]),.001)
                    heapq.heappush(queue,(distance+step,owner,j))
        left = {i for i,owner in owned.items() if owner == 0}
        right = part-left
        if left and right:
            yield left,right


def _compact_groups(graph, part, centers, areas, number):
    """确定性的多中心道路分水岭；最多8轮面积加权中心更新，不先切出边缘余料。

    种子最远点初始化保持空间分散，随后沿真实共边图扩展；只有从已归属
    节点才能继续扩展，所以每个中心的责任范围始终连通。面积只用于移动
    中心，不以容量截停生长，避免为凑面积留下蛇形走廊。
    """
    number=min(number,len(part)); indices=sorted(part)
    if number == len(indices):
        return [{i} for i in indices]
    area=sum(areas[i] for i in indices)
    center=(sum(areas[i]*centers[i][0] for i in indices)/area,
            sum(areas[i]*centers[i][1] for i in indices)/area)
    first=min(indices,key=lambda i:(math.dist(centers[i],center),i))
    seeds=[first]; distances={i:math.dist(centers[i],centers[first]) for i in indices}
    while len(seeds)<number:
        selected=set(seeds)
        seed=max((i for i in indices if i not in selected),key=lambda i:(distances[i],-i))
        seeds.append(seed)
        for i in indices:
            distances[i]=min(distances[i],math.dist(centers[i],centers[seed]))
    for _ in range(8):
        owned={}; queue=[(0.,owner,i) for owner,i in enumerate(seeds)]; heapq.heapify(queue)
        while queue:
            distance,owner,i=heapq.heappop(queue)
            if i in owned: continue
            owned[i]=owner
            for j in graph[i]&part:
                if j not in owned:
                    heapq.heappush(queue,(distance+max(math.dist(centers[i],centers[j]),.001),owner,j))
        groups=[set() for _ in seeds]
        for i,owner in owned.items(): groups[owner].add(i)
        revised=[]
        for group in groups:
            weight=sum(areas[i] for i in group)
            center=(sum(areas[i]*centers[i][0] for i in group)/weight,
                    sum(areas[i]*centers[i][1] for i in group)/weight)
            revised.append(min(group,key=lambda i:(math.dist(centers[i],center),i)))
        if revised==seeds: break
        seeds=revised
    return groups


def _partition(geoms, counts, graph, target_stores, max_area, *, region_parts=None):
    """先生成片区，再在每个片区内生成0.5～2km²任务网格；均完整保留道路面。"""
    from app.services import bd_zones as z
    areas = [g.area for g in geoms]
    # 性能剖析发现排序中反复跨GEOS读取x/y约占19秒；固定标量只取一次。
    centers = [(g.centroid.x, g.centroid.y) for g in geoms]
    # 面积加权二阶矩估计主轴，避免对每个候选反复union完整道路几何。
    # 坐标平移避免大投影坐标相减损失精度；基础面的内部方差防止单行街块退化。
    ox, oy = centers[0]
    moments = []
    for g, (cx, cy), area in zip(geoms, centers, areas):
        x, y = cx - ox, cy - oy
        x0, y0, x1, y1 = g.bounds
        moments.append((area*x, area*y, area*(x*x+(x1-x0)**2/12),
                        area*x*y, area*(y*y+(y1-y0)**2/12)))
    joined = {}

    def stats(part):
        weight = sx = sy = sxx = sxy = syy = 0.
        for i in part:
            a, b, c, d, e = moments[i]
            weight += areas[i]
            sx += a; sy += b; sxx += c; sxy += d; syy += e
        weight = max(weight, 1e-12)
        vx, vy = max(0., sxx/weight-(sx/weight)**2), max(0., syy/weight-(sy/weight)**2)
        cov = sxy/weight-sx*sy/weight**2
        delta = math.hypot(vx-vy, 2*cov)
        aspect = math.sqrt(max(vx+vy+delta, 1e-9)/max(vx+vy-delta, 1e-9))
        return aspect, .5*math.atan2(2*cov, vx-vy)

    def concavity_proxy(part, area):
        # 只用中心点凸包作排序代理，不union道路点列，更不能作为输出边界。
        # 轴比正常的C/U形仍有大片凸包空缺，不能只依靠二阶矩识别。
        hull_area = multipoints([centers[i] for i in sorted(part)]).convex_hull.area
        return max(0., 1-area/max(hull_area, area, 1.))**2

    def shape_defect(geom):
        quality = z._shape_metrics(geom)
        if not math.isfinite(quality['aspect']):
            return float('inf')
        return (max(0., quality['aspect']/(3+z.SHAPE_TOLERANCE)-1)
                + max(0., (.65-z.SHAPE_TOLERANCE)/max(quality['solidity'], 1e-9)-1)
                + int(z._has_narrow_neck(geom)))

    def geometry(part):
        key = frozenset(part)
        if key not in joined:
            joined[key] = union_all([geoms[i] for i in sorted(part)], grid_size=.001)
        return joined[key]

    def connected(part):
        if not part:
            return False
        todo, seen = [min(part)], set()
        while todo:
            i = todo.pop()
            if i not in seen:
                seen.add(i)
                todo.extend((graph[i] & part) - seen)
        return len(seen) == len(part)

    def cut_score(left, right, size, total_stores, ratio):
        amount = sum(areas[i] for i in left)
        separation = abs(amount/size-ratio)
        shops = abs(sum(counts[i] for i in left)/total_stores-ratio)
        crossing = sum(1 for i in left for j in graph[i] if j in right)
        la, _ = stats(left)
        ra, _ = stats(right)
        elongation = .18*math.log(max(la,ra)) + .04*math.log(la*ra)
        concavity = concavity_proxy(left,amount) + concavity_proxy(right,size-amount)
        score = (separation+elongation+2.5*concavity+.025*shops
                 + .00015*crossing/math.sqrt(max(len(left)+len(right),1)))
        return score,tuple(sorted(left)),set(left),right

    def split(part, number, minimum):
        size = sum(areas[i] for i in part)
        number = min(len(part), max(1, number))
        if len(part) == 1:
            return [part]
        if number == len(part):
            # 每个完整基础面恰好一份时，无需强求存在二等分（例如星形邻接）。
            return [{i} for i in sorted(part)]
        refining = number == 1
        parent_defect = None
        if refining:
            # 数量目标不是形状验收。深凹/狭颈也必须参与补切，不能只检查包围盒轴比。
            if size < 2*minimum:
                return [part]
            parent_defect = shape_defect(geometry(part))
            if parent_defect == 0:
                return [part]
            number = 2
        nl, nr = number // 2, number - number // 2
        ratio = nl / number
        total_stores = max(sum(counts[i] for i in part), 1)
        candidates, seen = [], set()
        # 常规切分保留面积分位均衡；形状修正增加偏移，才能切开不在正中的狭颈。
        offsets = (-.25, -.17, -.1, -.06, -.03, 0, .03, .06, .1, .17, .25) if refining else (-.06, -.03, 0, .03, .06)
        _, principal_angle = stats(part)
        angles = [principal_angle] + [direction*math.pi/8 for direction in range(8)]
        for angle in angles:
            dx, dy = math.cos(angle), math.sin(angle)
            order = sorted(part, key=lambda i: (centers[i][0] * dx + centers[i][1] * dy, i))
            running, cumulative = 0., []
            for i in order:
                running += areas[i]
                cumulative.append(running)
            for offset in offsets:
                pos = min(len(order) - nr, max(nl, bisect.bisect_left(cumulative, size * (ratio + offset)) + 1))
                left = frozenset(order[:pos])
                if left in seen or len(left) < nl or len(part - left) < nr:
                    continue
                seen.add(left)
                right = part - left
                if not connected(left) or not connected(right):
                    continue
                amount = sum(areas[i] for i in left)
                if refining and min(amount, size-amount) < minimum:
                    continue
                # 方向只决定归属；最终仍按完整道路面落界，不生成直线边界。
                candidates.append(cut_score(left,right,size,total_stores,ratio))
        if refining:
            # C形/多叶形不一定存在连通的投影直切，不能因此把余料原样留下。
            for left,right in _geodesic_candidates(graph,part,centers):
                frozen = frozenset(left)
                if frozen in seen or min(sum(areas[i] for i in left),sum(areas[i] for i in right)) < minimum:
                    continue
                seen.add(frozen)
                candidates.append(cut_score(left,right,size,total_stores,ratio))
        if not candidates:
            # 凹形海岸无合适投影切分时，用BFS子树保证两侧连通，不拿飞地凑数。
            candidates = _tree_candidates(graph, part, areas, centers, nl, nr, ratio)
        if not candidates:
            return [part]  # 仍保留完整责任范围，由审计提示约束冲突。
        if refining:
            # 仅最终补切时复核少量真实轮廓；不把无法改善的天然窄地越切越碎。
            improved = []
            for candidate in sorted(candidates, key=lambda c: (c[0], c[1]))[:8]:
                _, _, left, right = candidate
                if min(sum(areas[i] for i in left), sum(areas[i] for i in right)) < minimum:
                    continue
                ld, rd = shape_defect(geometry(left)), shape_defect(geometry(right))
                worst = max(ld,rd)
                weight = sum(areas[i] for i in left)/size
                defect = weight*ld + (1-weight)*rd
                # 多个叶瓣时，先剥离一个合格块，余下的狭颈才有机会继续分。
                # 不能因最坏子块仍有一个狭颈而拒绝实质进展，也不允许制造更坏的子块。
                if math.isfinite(worst) and worst <= parent_defect+z.SHAPE_TOLERANCE and defect < parent_defect*.9:
                    improved.append((defect, candidate))
            if not improved:
                return [part]
            _, candidate = min(improved, key=lambda c: (c[0], c[1][0], c[1][1]))
        else:
            candidate = min(candidates, key=lambda c: (c[0], c[1]))
        _, _, left, right = candidate
        return split(left, nl, minimum) + split(right, nr, minimum)

    if region_parts is None:
        region_parts = []
        for component in _components(graph):
            size = sum(areas[i] for i in component)
            shops = sum(counts[i] for i in component)
            number = max(1, math.ceil(size/max_area), math.ceil(shops/target_stores))
            use_centers = 1 < number <= MAX_COMPACT_SEEDS
            initial = _compact_groups(graph,component,centers,areas,number) if use_centers else [component]
            for candidate in initial:
                remaining = (max(1,math.ceil(sum(areas[i] for i in candidate)/max_area))
                             if use_centers else number)
                region_parts.extend(split(candidate,remaining,z.MIN_GRID))
    regions, tasks = [], []
    for part in region_parts:
        region_index = len(regions)
        regions.append({'members': part, 'geom': geometry(part), 'count': sum(counts[i] for i in part)})
        for component in _components(graph, part):
            grid_number = max(1,round(sum(areas[i] for i in component)/1_000_000))
            for grid in split(component,grid_number,z.MIN_GRID):
                tasks.append({'region_index': region_index, 'members': grid,
                              'geom': geometry(grid), 'count': sum(counts[i] for i in grid)})
    return regions, tasks


def scope_preview(doc, target_stores, max_area_km2, *, balance_options=None, include_memberships=False, generation_rules=False, river_geometry=None, max_river_width_m=100.):
    from app.services import bd_zones as z
    cells = doc.get('road_cells') or doc.get('grids', [])
    if not cells:
        raise z.ZoneDataError('缺少完整道路基础面，无法进行覆盖优先预览')
    from shapely.geometry import shape
    if any('metric_geometry' in c for c in cells) and doc.get('metric_crs') != 'EPSG:6677':
        raise z.ZoneDataError('道路基础面的投影声明不正确，不能预览')
    # 原生坐标也要恢复同一毫米精度模型，不能与目标域混用浮点布尔运算。
    geoms = [set_precision(shape(c['metric_geometry']), .001) if 'metric_geometry' in c else z.projected(c['geometry']) for c in cells]
    counts = [c['store_count'] for c in cells]
    if doc.get('topology') is not None and len(doc['topology']) == len(cells):
        graph = {i: set(adjacent) for i, adjacent in enumerate(doc['topology'])}
    else:
        graph = z.neighbours(geoms)
    excluded_cells, policy = set(), None
    if balance_options is not None:
        from app.services.bd_zone_balance import partition as balanced_partition
        parts = balanced_partition(geoms, counts, graph, balance_options)
        if generation_rules:
            from app.services.bd_zone_rules import consolidate
            parts, excluded_cells, policy = consolidate(parts, geoms, counts, graph, balance_options,
                area_km2=z.projected(doc['work_area']).area / 1e6,
                rivers=river_geometry, max_river_width_m=max_river_width_m)
            if any(cells[i].get('merchant_ids') for i in excluded_cells):
                raise z.ZoneDataError('无商户排除范围仍有商户绑定，拒绝生成')
        groups, tasks = _partition(geoms,counts,graph,target_stores,max_area_km2*1_000_000,region_parts=parts)
    else:
        groups, tasks = _partition(geoms,counts,graph,target_stores,max_area_km2*1_000_000)
    owner_indices = {i:k for k,g in enumerate(groups) for i in g['members']}
    for k,g in enumerate(groups):
        load = balance_options.score(g['geom'].area,g['count']) if balance_options else None
        has_neighbour = any(j in owner_indices and owner_indices[j] != k for i in g['members'] for j in graph[i])
        g['workload'] = load
        g['load_valid'] = balance_options is None or balance_options.load_min <= load <= balance_options.load_max
        g['residual'] = (load < balance_options.load_min and not has_neighbour) if balance_options else g['geom'].area < z.MIN_GRID-1
        g['shape_valid'] = z._acceptable_shape(g['geom'])
    features, grid_features, cell_owners, ids = [], [], {}, []
    for k, group in enumerate(groups):
        identity = '|'.join(sorted(cells[i]['id'] for i in group['members']))
        rid = 'tokyo23-Z-' + hashlib.sha256(identity.encode()).hexdigest()[:10]
        ids.append(rid)
        cell_owners.update({i: rid for i in group['members']})
        features.append({'type': 'Feature', 'id': rid, 'geometry': z.geographic(group['geom']),
                         'properties': {'id': rid, 'level': 'zone', 'ward_code': z.SCOPE_CODE,
                                        'unit_kind': ('residual_land' if balance_options else 'independent_land') if group['residual'] else 'region_candidate',
                                        'needs_review': group['residual'] or not group['load_valid'] or not group['shape_valid'],
                                        'workload': group['workload'], 'load_valid': group['load_valid'], 'shape_valid': group['shape_valid'],
                                        'review_reason': ('no_shared_edge_neighbour' if group['residual'] else
                                                          'workload_outside_band' if not group['load_valid'] else
                                                          'shape_conflict' if not group['shape_valid'] else ''),
                                        'grid_count': sum(t['region_index'] == k for t in tasks),
                                        'store_count': group['count'], 'area_km2': group['geom'].area / 1_000_000}})
    grid_conflicts = 0
    for task in tasks:
        gid = 'tokyo23-G-' + hashlib.sha256('|'.join(sorted(cells[i]['id'] for i in task['members'])).encode()).hexdigest()[:10]
        valid = z.MIN_GRID - 1 <= task['geom'].area <= z.MAX_GRID + 1
        grid_conflicts += not valid
        grid_features.append({'type': 'Feature', 'id': gid, 'geometry': z.geographic(task['geom']),
                              'properties': {'id': gid, 'level': 'grid', 'zone_id': ids[task['region_index']],
                                             'store_count': task['count'], 'area_km2': task['geom'].area / 1_000_000,
                                             'area_valid': valid}})
        if include_memberships:
            grid_features[-1]['properties']['cell_ids'] = [cells[i]['id'] for i in sorted(task['members'])]
    original_work = z.projected(doc['work_area'])
    excluded_geometry = union_all([geoms[i] for i in sorted(excluded_cells)], grid_size=.001)
    work = original_work.difference(excluded_geometry) if excluded_cells else original_work
    combined = union_all([g['geom'] for g in groups], grid_size=.001)
    uncovered = work.difference(combined)
    outside = combined.difference(work)
    overlap = max(0., sum(g['geom'].area for g in groups) - combined.area,
                  sum(g.area for i,g in enumerate(geoms) if i not in excluded_cells) - combined.area)
    source = doc.get('merchant_audit', {})
    bound_ids = [identity for cell in cells for identity in cell.get('merchant_ids', [])]
    unique_ids = set(bound_ids)
    source_ready = doc.get('coverage_revision') in (1, 2) and bool(source)
    expected = source.get('inside_work', doc['store_count'])
    mapped = len(unique_ids) if source_ready else sum(counts)
    duplicate_ids = len(bound_ids) - len(unique_ids) if source_ready else 0
    unmatched = max(0, expected - mapped)
    reviews = doc.get('review_merchants', [])
    excluded_unreviewed = source.get('excluded_unreviewed', len(reviews))
    active_cells = set(range(len(cells))) - excluded_cells
    assigned_once = set(cell_owners) == active_cells and sum(len(g['members']) for g in groups) == len(active_cells)
    task_members = [i for task in tasks for i in task['members']]
    tasks_once = len(task_members) == len(active_cells) and set(task_members) == active_cells
    bindings_mismatch = sum(abs(c['store_count'] - len(c.get('merchant_ids', []))) for c in cells) if source_ready else 0
    audit = {'tolerance_m2': AREA_TOLERANCE_M2, 'work_area_km2': work.area / 1_000_000,
             'uncovered_m2': uncovered.area, 'overlap_m2': overlap, 'outside_m2': outside.area,
             'coverage_percent': max(0, 100 * (1 - uncovered.area / max(work.area, 1))),
             'source_merchant_count': source.get('inside_boundary', doc['store_count']),
             'expected_merchants': expected, 'assigned_merchants': mapped,
             'unassigned_merchants': unmatched, 'duplicate_merchants': duplicate_ids,
             'excluded_unreviewed': excluded_unreviewed,
             'unlocated_source_merchants': source.get('unlocated_in_work', 0),
             'source_duplicates': source.get('source_duplicates', 0),
             'duplicate_cell_ids': len(cells) - len({c['id'] for c in cells}),
             'duplicate_region_ids': len(features) - len({f['id'] for f in features}),
             'duplicate_grid_ids': len(grid_features) - len({f['id'] for f in grid_features}),
             'all_cells_assigned_once': assigned_once, 'all_task_cells_assigned_once': tasks_once,
             'binding_count_mismatch': bindings_mismatch,
             'invalid_source_coordinates': source.get('invalid_coordinates', 0), 'source_audit_ready': source_ready}
    if policy:
        audit['generation_rules'] = policy
        audit['excluded_empty_cell_ids'] = [cells[i]['id'] for i in sorted(excluded_cells)]
    audit['geometric_pass'] = (uncovered.area <= AREA_TOLERANCE_M2 and overlap <= AREA_TOLERANCE_M2
                               and outside.area <= AREA_TOLERANCE_M2 and assigned_once and tasks_once
                               and audit['duplicate_cell_ids'] == audit['duplicate_region_ids'] == audit['duplicate_grid_ids'] == 0)
    audit['merchant_pass'] = (source_ready and mapped == expected and duplicate_ids == 0
                             and audit['unlocated_source_merchants'] == 0 and audit['source_duplicates'] == 0
                             and audit['invalid_source_coordinates'] == 0 and bindings_mismatch == 0)
    issues = []
    if not audit['geometric_pass']:
        issues.append({'code': 'coverage_failed'})
    if not audit['merchant_pass']:
        issues.append({'code': 'merchant_coverage_failed'})
    if not source_ready:
        issues.append({'code': 'source_audit_missing'})
    if excluded_unreviewed:
        issues.append({'code': 'excluded_merchant_review', 'count': excluded_unreviewed})
    if grid_conflicts:
        issues.append({'code': 'grid_area_conflict', 'count': grid_conflicts})
    sizes = [g['geom'].area / 1_000_000 for g in groups]
    ratio = max(sizes) / max(min(sizes), 1e-12)
    if balance_options is None and ratio > 2 + 1e-5:
        issues.append({'code': 'zone_ratio', 'ratio': ratio})
    if balance_options is None and max(sizes) > max_area_km2 + 1e-6:
        issues.append({'code': 'zone_area'})
    normal = [g for g in groups if not g['residual']]
    workload_conflicts = sum(not g['load_valid'] for g in normal)
    residual_count = sum(g['residual'] for g in groups)
    if balance_options and workload_conflicts:
        issues.append({'code': 'workload_conflict', 'count': workload_conflicts})
    if balance_options and residual_count:
        issues.append({'code': 'residual_land_review', 'count': residual_count})
    shape_count = sum(not g['shape_valid'] for g in (normal if balance_options else groups))
    if shape_count:
        issues.append({'code': 'zone_shape', 'count': shape_count})
    meta = {key: doc[key] for key in ('ward_code', 'ward_name', 'snapshot_id', 'captured_at',
                                     'source', 'store_count', 'area_km2', 'excluded_area_km2', 'road_count')}
    return {'type': 'FeatureCollection', 'features': features, 'grids': grid_features,
            'boundary': doc['boundary'], 'work_area': z.geographic(work), 'meta': meta,
            'audit': audit, 'review_merchants': reviews,
            'uncovered_features': [{'type': 'Feature', 'geometry': z.geographic(p), 'properties': {}}
                                   for p in z.polygons(uncovered) if p.area > AREA_TOLERANCE_M2],
            'pending_grids': [],  # 约束冲突保留责任归属，不再转成灰色未分配地块。
            'summary': {'zone_count': len(groups),
                        'region_candidate_count': len(normal),
                        'independent_unit_count': residual_count,
                        'grid_count': len(tasks), 'area_ratio': ratio,
                        'min_area_km2': min(sizes), 'max_area_km2': max(sizes),
                        'target_stores': target_stores, 'max_area_km2_limit': max_area_km2 if balance_options is None else None,
                        'target_area_km2': max_area_km2,
                        'balance': vars(balance_options) if balance_options else None,
                        'workload_conflicts': workload_conflicts, 'shape_conflicts': shape_count,
                        'residual_area_km2': sum(g['geom'].area for g in groups if g['residual'])/1e6,
                        'residual_merchants': sum(g['count'] for g in groups if g['residual']),
                        'merchant_count_min': min((g['count'] for g in normal),default=0),
                        'merchant_count_max': max((g['count'] for g in normal),default=0),
                        'workload_min': min((g['workload'] for g in normal if g['workload'] is not None),default=0),
                        'workload_max': max((g['workload'] for g in normal if g['workload'] is not None),default=0),
                        'pending_grid_count': 0, 'pending_store_count': 0, 'pending_area_km2': 0,
                        'grid_area_conflicts': grid_conflicts, 'eligible': not issues,
                        'confirmation_ready': not issues},
            'issues': issues, 'preview_only': True}


def prepare_source(boundary, roads, exclusions, coastlines, merchants_csv, *, captured_at, source):
    """为覆盖优先预览构建基础面及商户绑定，不先生成/筛选最终任务网格。"""
    import csv
    from shapely import intersects_xy, make_valid, points, set_precision
    from shapely.geometry import shape
    from shapely.ops import polygonize, transform
    from app.services import bd_zones as z

    border_geo = shape(boundary)
    border = z.projected(boundary)
    land = z.coastline_land(border, coastlines)
    masks = [z.projected(g) for g in exclusions]
    if masks:
        land = land.difference(union_all(masks))
    land = set_precision(make_valid(land), 0)
    segments = [land.boundary]
    for item in roads:
        line = transform(z._TO_METERS, shape(item))
        if not line.is_empty and line.intersects(land):
            segments.append(line)
    faces = [p for p in polygonize(union_all(segments, grid_size=.001))
             if p.area > 0 and land.covers(p.representative_point())]
    faces.sort(key=lambda p: (p.centroid.x, p.centroid.y, p.area))
    if not faces:
        raise z.ZoneDataError('没有完整道路基础面，停止构建预览')
    print('完整道路基础面:', len(faces), '（未按0.5～2km²过滤）', flush=True)
    graph = face_graph(faces)
    tree = STRtree(faces)
    merchant_ids = [[] for _ in faces]
    reviews, seen = [], set()
    audit = {'inside_boundary': 0, 'inside_work': 0, 'excluded_unreviewed': 0,
             'unlocated_in_work': 0, 'source_duplicates': 0, 'invalid_coordinates': 0}
    bbox = border_geo.bounds
    batch = []

    def consume(rows):
        if not rows:
            return
        xs, ys = [r['lng'] for r in rows], [r['lat'] for r in rows]
        inside = intersects_xy(border_geo, xs, ys)
        unique = []
        for i, row in enumerate(rows):
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
        in_work = intersects_xy(land, mx, my)
        locate, lookup = [], []
        for i, row in enumerate(unique):
            if not in_work[i]:
                reviews.append({**row, 'reason': 'excluded_terrain', 'status': 'unreviewed'})
                audit['excluded_unreviewed'] += 1
            else:
                audit['inside_work'] += 1
                lookup.append(i)
                locate.append((mx[i], my[i]))
        if not locate:
            return
        hits = tree.query(points(locate), predicate='intersects')
        owner = {}
        for point_index, face_index in zip(*hits):
            key, value = int(point_index), int(face_index)
            if key not in owner or value < owner[key]:
                owner[key] = value
        for j, i in enumerate(lookup):
            row = unique[i]
            if j in owner:
                merchant_ids[owner[j]].append(row['store_id'])
            else:
                reviews.append({**row, 'reason': 'no_road_face', 'status': 'unreviewed'})
                audit['unlocated_in_work'] += 1

    with open(merchants_csv, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        columns = set(reader.fieldnames or [])
        if not {'place_id', 'lat'} <= columns or not ({'lon', 'lng'} & columns):
            raise z.ZoneDataError('商户源必须包含place_id、lat、lon/lng')
        for row in reader:
            try:
                lng, lat = float(row.get('lon') or row.get('lng')), float(row['lat'])
            except (ValueError, TypeError):
                audit['invalid_coordinates'] += 1
                continue
            if not (math.isfinite(lng) and math.isfinite(lat)):
                audit['invalid_coordinates'] += 1
                continue
            if not (bbox[0] <= lng <= bbox[2] and bbox[1] <= lat <= bbox[3]):
                continue
            if not row['place_id']:
                audit['invalid_coordinates'] += 1
                continue
            batch.append({'store_id': row['place_id'], 'name': row.get('name', ''), 'lng': lng, 'lat': lat})
            if len(batch) >= 20000:
                consume(batch)
                batch = []
        consume(batch)
    cells = []
    digest = hashlib.sha256()
    for i, geom in enumerate(faces):
        identity = hashlib.sha256(geom.normalize().wkb).hexdigest()[:12]
        ids = sorted(merchant_ids[i])
        digest.update((identity + '|'.join(ids)).encode())
        cells.append({'id': 'tokyo23-R-' + identity, 'metric_geometry': mapping(geom),
                      'area_km2': geom.area / 1_000_000, 'store_count': len(ids), 'merchant_ids': ids})
    audit['mapped_to_faces'] = sum(len(ids) for ids in merchant_ids)
    return {'schema_version': z.SCHEMA_VERSION, 'generation_version': 5, 'coverage_revision': 2, 'metric_crs': 'EPSG:6677',
            'scope_ward_count': 23, 'ward_code': z.SCOPE_CODE, 'ward_name': z.SCOPE_NAME,
            'snapshot_id': digest.hexdigest()[:12], 'captured_at': captured_at, 'source': source,
            'boundary': z.geographic(border), 'work_area': z.geographic(land),
            'area_km2': land.area / 1_000_000, 'excluded_area_km2': (border.area - land.area) / 1_000_000,
            'store_count': audit['inside_work'], 'road_count': len(roads),
            'road_cells': cells, 'topology': [sorted(graph[i]) for i in range(len(faces))],
            'merchant_audit': audit, 'review_merchants': reviews, 'grids': [], 'issues': [],
            'preview_only': True}
