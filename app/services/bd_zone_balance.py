"""面积/商户非线性负载。面积是推荐尺度而非硬上限；不删除无法满足约束的地块。"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from collections import deque
from shapely import multipoints, union_all


@dataclass(frozen=True)
class BalanceOptions:
    target_area: float = 25.
    target_stores: int = 8000
    area_weight: float = .15
    load_min: float = .75
    load_max: float = 1.35

    def validate(self):
        from app.services.bd_zones import ZoneDataError
        values = (self.target_area, self.target_stores, self.area_weight, self.load_min, self.load_max)
        if (any(isinstance(v, bool) or not math.isfinite(v) for v in values)
                or not 2 <= self.target_area <= 500 or not 1 <= self.target_stores <= 100000
                or not .05 <= self.area_weight <= .95 or not .1 <= self.load_min < self.load_max <= 4):
            raise ZoneDataError('综合负载参数超出范围：权重须在5%～95%，负载下限必须小于上限。')
        return self

    def score(self, area_m2, merchants):
        return (self.area_weight*(area_m2/(self.target_area*1e6))**2
                + (1-self.area_weight)*(merchants/self.target_stores)**2)

    def marginal(self, area_m2, merchants, added_area, added_merchants):
        return self.score(area_m2+added_area, merchants+added_merchants)-self.score(area_m2, merchants)

    def seed_count(self, area_m2, merchants, faces):
        total = self.score(area_m2,merchants)
        if total <= self.load_max:
            return 1  # 已在合理区间的大面积低密度地区，不为固定片数而硬拆。
        # 以单位负载和下限的中间值留出道路边界/密度不均的余量，再由邻区合并收尾。
        preferred = min(1.,(self.load_min+1)/2)
        return min(faces,max(1,math.ceil(math.sqrt(total/preferred))))


def _connected(graph, part, removed=None):
    if removed is None:
        remaining = part
    else:
        remaining = part-{removed}
    if not remaining:
        return False
    seen, todo = set(), [min(remaining)]
    while todo:
        i = todo.pop()
        if i not in seen:
            seen.add(i)
            todo.extend(j for j in graph[i] if j in remaining and j not in seen)
    return len(seen) == len(remaining)



def _removal_connected(graph, part, removed):
    """邻点能在局部绕行是移除安全的充分条件；不能证明时才遍历整个片区。"""
    if len(part)<=1:
        return False
    neighbours = (graph[removed]&part)-{removed}
    if len(neighbours)<=1:
        return True  # 调用前part已连通，叶节点移除不会断开它。
    allowed = set(neighbours)
    for _ in range(2):
        allowed.update(j for i in tuple(allowed) for j in graph[i] if j in part and j!=removed)
    seen, todo = set(), [min(neighbours)]
    while todo:
        i = todo.pop()
        if i not in seen:
            seen.add(i)
            todo.extend(j for j in graph[i] if j in allowed and j not in seen)
    if neighbours<=seen:
        return True
    return _connected(graph,part,removed)

def _grow(graph, part, centers, areas, counts, seeds, options, multipliers=None):
    """固定每轮速度，在真实共边图上竞争；一轮中不因容量变化重新排队拉长触须。"""
    multipliers = multipliers or [1.]*len(seeds)
    owners = {i:k for k,i in enumerate(seeds)}
    groups = [{i} for i in seeds]
    queue = []
    for owner,seed in enumerate(seeds):
        for j in graph[seed]&part:
            if j not in owners:
                d = max(math.dist(centers[seed],centers[j]),.001)
                heapq.heappush(queue,(d*multipliers[owner],d,owner,j))
    while queue:
        _,distance,owner,i = heapq.heappop(queue)
        if i in owners:
            continue
        owners[i] = owner
        groups[owner].add(i)
        for j in graph[i]&part:
            if j not in owners:
                d = distance+max(math.dist(centers[i],centers[j]),.001)
                heapq.heappush(queue,(d*multipliers[owner],d,owner,j))
    return groups


def _seeds(part, centers, areas, counts, number, options):
    indices = sorted(part)
    mass = {i:options.area_weight*areas[i]/(options.target_area*1e6)
            +(1-options.area_weight)*counts[i]/options.target_stores for i in indices}
    total = sum(mass.values())
    if total <= 0:
        return [indices[0]]
    center = tuple(sum(mass[i]*centers[i][axis] for i in indices)/total for axis in (0,1))
    seeds = [min(indices,key=lambda i:(math.dist(centers[i],center),i))]
    distances = {i:math.dist(centers[i],centers[seeds[0]]) for i in indices}
    while len(seeds)<number:
        selected = set(seeds)
        seed = max((i for i in indices if i not in selected),
                   key=lambda i:(distances[i]**2*math.sqrt(max(mass[i],1e-12)),-i))
        seeds.append(seed)
        for i in indices:
            distances[i] = min(distances[i],math.dist(centers[i],centers[seed]))
    return seeds


def _penalty(load, options):
    # 区间内没有追逐1分的收益；低负载只作轻度引导，不能为补面积拉长边界。
    return (8*max(0.,load-options.load_max)**2
            + .5*max(0.,options.load_min-load)**2)


def _rebalance(groups, graph, centers, areas, counts, options, rounds=12, geoms=None):
    """只移动边界面，接收侧共边、移出侧仍连通；先用标量筛选，避免逐面GEOS合并。"""
    groups = [set(g) for g in groups]
    owners = {i:k for k,g in enumerate(groups) for i in g}
    amounts = [sum(areas[i] for i in g) for g in groups]
    merchants = [sum(counts[i] for i in g) for g in groups]
    perimeters = ([union_all([geoms[i] for i in sorted(g)],grid_size=.001).length for g in groups]
                  if geoms is not None else None)
    edge_lengths = {}
    def contact(i,j):
        key = min(i,j),max(i,j)
        if key not in edge_lengths:
            edge_lengths[key] = geoms[i].boundary.intersection(geoms[j].boundary).length
        return edge_lengths[key]
    for _ in range(rounds):
        loads = [options.score(a,n) for a,n in zip(amounts,merchants)]
        if all(options.load_min <= load <= options.load_max for load in loads):
            break  # 目标是允许区间，不追逐每片同分而继续扭曲边界。
        changed = 0
        for i in sorted(owners):
            source = owners[i]
            if len(groups[source])<=1:
                continue
            neighbours = sorted({owners[j] for j in graph[i] if j in owners and owners[j]!=source})
            current = options.score(amounts[source],merchants[source])
            choices = []
            for target in neighbours:
                other = options.score(amounts[target],merchants[target])
                sa, sn = amounts[source]-areas[i], merchants[source]-counts[i]
                ta, tn = amounts[target]+areas[i], merchants[target]+counts[i]
                after_source, after_target = options.score(sa,sn), options.score(ta,tn)
                if max(after_source-options.load_max,0)+max(after_target-options.load_max,0) > max(current-options.load_max,0)+max(other-options.load_max,0)+1e-8:
                    continue
                before = _penalty(current,options)+_penalty(other,options)
                after = _penalty(after_source,options)+_penalty(after_target,options)
                if before-after>1e-5:
                    if perimeters is not None:
                        source_edge = sum(contact(i,j) for j in graph[i] if j in owners and owners[j]==source)
                        target_edge = sum(contact(i,j) for j in graph[i] if j in owners and owners[j]==target)
                        next_source = perimeters[source]-geoms[i].length+2*source_edge
                        next_target = perimeters[target]+geoms[i].length-2*target_edge
                        shape_before = perimeters[source]**2/max(amounts[source],1)+perimeters[target]**2/max(amounts[target],1)
                        shape_after = next_source**2/max(sa,1)+next_target**2/max(ta,1)
                        # 商户均衡的收益不能靠无限增加边界、形成细链来换。
                        after += .04*max(0.,shape_after-shape_before)
                    else:
                        next_source = next_target = None
                    if before-after>1e-5:
                        choices.append((after-before,target,next_source,next_target))
            if choices and _removal_connected(graph,groups[source],i):
                _,target,next_source,next_target = min(choices)
                if perimeters is not None:
                    perimeters[source],perimeters[target] = next_source,next_target
                groups[source].remove(i);groups[target].add(i);owners[i]=target
                amounts[source]-=areas[i];merchants[source]-=counts[i]
                amounts[target]+=areas[i];merchants[target]+=counts[i]
                changed+=1
                if changed>=1500:
                    break
        if not changed:
            break
    return groups


def merge_residuals(groups, graph, areas, counts, options, geoms=None):
    """只合并实际共边的小负载单元。无可行邻区时保留责任与复核原因，不跨空白/水域。"""
    groups = [set(g) for g in groups]
    while True:
        owners = {i:k for k,g in enumerate(groups) for i in g}
        stats = [(sum(areas[i] for i in g),sum(counts[i] for i in g)) for g in groups]
        changed = False
        for source in sorted(range(len(groups)),key=lambda k:options.score(*stats[k])):
            if not groups[source] or options.score(*stats[source])>=options.load_min:
                continue
            adjacent = {owners[j] for i in groups[source] for j in graph[i] if j in owners and owners[j]!=source}
            feasible = []
            for target in adjacent:
                a,n = stats[source][0]+stats[target][0],stats[source][1]+stats[target][1]
                load = options.score(a,n)
                if load<=options.load_max:
                    if geoms is not None:
                        from app.services import bd_zones as z
                        geom = union_all([geoms[i] for i in sorted(groups[source]|groups[target])],grid_size=.001)
                        if not z._acceptable_shape(geom):
                            continue
                    feasible.append((abs(load-1),options.score(*stats[target]),target))
            if feasible:
                _,_,target = min(feasible)
                groups[target].update(groups[source]);groups[source].clear()
                changed=True
                break
        if not changed:
            return [g for g in groups if g]



def repair_pairs(groups, graph, centers, areas, counts, options, geoms, passes=3):
    """相邻片区联合重拼，交换整组街块而非单面拉出长尾；精查候选数量有界。"""
    from app.services import bd_zones as z
    groups = [set(g) for g in groups]
    cache = {}
    def geom(part):
        key = frozenset(part)
        if key not in cache:
            cache[key] = union_all([geoms[i] for i in sorted(part)],grid_size=.001)
        return cache[key]
    def shape_cost(part):
        g = geom(part)
        q = z._shape_metrics(g)
        if not math.isfinite(q['aspect']):
            return 100.
        return (2*int(not z._acceptable_shape(g)) + 12*max(0.,.75-q['solidity'])**2
                + .8*max(0.,q['aspect']-2.2)**2
                + 6*max(0.,.28-q['compactness'])**2 + .8*int(z._has_narrow_neck(g)))
    def cost(part):
        a,n = sum(areas[i] for i in part),sum(counts[i] for i in part)
        return _penalty(options.score(a,n),options)+shape_cost(part)
    for _ in range(passes):
        owners = {i:k for k,g in enumerate(groups) for i in g}
        values = [cost(g) for g in groups]
        troublesome = [k for k,g in enumerate(groups)
                       if options.score(sum(areas[i] for i in g),sum(counts[i] for i in g)) < options.load_min
                       or options.score(sum(areas[i] for i in g),sum(counts[i] for i in g)) > options.load_max
                       or shape_cost(g)>0]
        used, changed = set(),0
        for source in sorted(troublesome,key=lambda k:(-values[k],k)):
            if source in used:
                continue
            adjacent = {owners[j] for i in groups[source] for j in graph[i] if j in owners and owners[j]!=source}
            best = None
            for target in sorted(adjacent,key=lambda k:(-values[k],k))[:3]:
                if target in used:
                    continue
                part = groups[source]|groups[target]
                seeds = []
                for group in (groups[source],groups[target]):
                    mass = {i:options.area_weight*areas[i]/(options.target_area*1e6)
                            +(1-options.area_weight)*counts[i]/options.target_stores for i in group}
                    total = sum(mass.values())
                    center = tuple(sum(mass[i]*centers[i][axis] for i in group)/max(total,1e-12) for axis in (0,1))
                    seeds.append(min(group,key=lambda i:(math.dist(centers[i],center),i)))
                lo,hi = -1.6,1.6
                candidates = []
                for step in range(9):
                    middle = (lo+hi)/2
                    trial = _grow(graph,part,centers,areas,counts,seeds,options,[math.exp(middle),1.])
                    loads = [options.score(sum(areas[i] for i in g),sum(counts[i] for i in g)) for g in trial]
                    proxy = sum(_penalty(load,options) for load in loads)
                    candidates.append((proxy,step,trial))
                    if loads[0]>loads[1]:
                        lo=middle
                    else:
                        hi=middle
                before = values[source]+values[target]
                for _,_,trial in sorted(candidates,key=lambda row:(row[0],row[1]))[:4]:
                    after = sum(cost(g) for g in trial)
                    old_bad = sum(not z._acceptable_shape(geom(g)) for g in (groups[source],groups[target]))
                    new_bad = sum(not z._acceptable_shape(geom(g)) for g in trial)
                    old_excess = sum(max(0.,options.score(sum(areas[i] for i in g),sum(counts[i] for i in g))-options.load_max) for g in (groups[source],groups[target]))
                    new_excess = sum(max(0.,options.score(sum(areas[i] for i in g),sum(counts[i] for i in g))-options.load_max) for g in trial)
                    if new_excess>old_excess+1e-8:
                        continue
                    if new_bad>old_bad:
                        continue  # 不用制造新的深凹/狭颈片区换取负载更好看。
                    if after < before-.01 and (best is None or after-before<best[0]):
                        best = after-before,target,trial
            if best is not None:
                _,target,trial = best
                groups[source],groups[target] = trial
                used.update((source,target));changed+=1
                if changed>=12:
                    break
        if not changed:
            break
    return groups

def partition(geoms, counts, graph, options):
    from app.services.bd_zone_coverage import _components
    options.validate()
    areas = [g.area for g in geoms]
    centers = [(g.centroid.x,g.centroid.y) for g in geoms]
    output = []
    pending = deque(_components(graph))
    while pending:
        part = pending.popleft()
        a,n = sum(areas[i] for i in part),sum(counts[i] for i in part)
        number = options.seed_count(a,n,len(part))
        if number==1:
            from app.services import bd_zones as z
            value = union_all([geoms[i] for i in sorted(part)],grid_size=.001)
            quality = z._shape_metrics(value)
            # 商户数不超限不代表形状合理；较大的狭长/深凹地块允许先分成两片。
            if n>0 and len(part)>1 and a>=2e6 and (quality['aspect']>3 or z._has_narrow_neck(value)):
                number=2
            else:
                output.append(part)
                continue
        if number==len(part):
            output.extend({i} for i in sorted(part))
            continue
        # 只有真实本地数据范围下的数量才执行最远点初始化。
        if number>128:
            from app.services.bd_zone_coverage import _geodesic_candidates
            choices = []
            for left,right in _geodesic_candidates(graph,part,centers):
                la,ln = sum(areas[i] for i in left),sum(counts[i] for i in left)
                ra,rn = a-la,n-ln
                imbalance = abs(math.sqrt(options.score(la,ln))-math.sqrt(options.score(ra,rn)))
                choices.append((imbalance,tuple(sorted(left)),left,right))
            if choices:
                _,_,left,right = min(choices,key=lambda r:(r[0],r[1]))
                pending.extend((left,right))
            else:
                output.append(part)
            continue
        seeds = _seeds(part,centers,areas,counts,number,options)
        multipliers, finalists = [1.]*number, []
        for step in range(40):
            groups = _grow(graph,part,centers,areas,counts,seeds,options,multipliers)
            loads = [options.score(sum(areas[i] for i in g),sum(counts[i] for i in g)) for g in groups]
            concavity = 0.
            for group in groups:
                area = sum(areas[i] for i in group)
                hull = multipoints([centers[i] for i in sorted(group)]).convex_hull.area
                concavity += 6*max(0.,.75-area/max(hull,area,1.))**2
                # 中心点二阶矩只用于候选粗排，最终仍检查实际轮廓。
                xs=[centers[i][0] for i in group]; ys=[centers[i][1] for i in group]
                mx=sum(xs)/len(xs); my=sum(ys)/len(ys)
                vx=sum((x-mx)**2 for x in xs)/len(xs)
                vy=sum((y-my)**2 for y in ys)/len(ys)
                cv=sum((x-mx)*(y-my) for x,y in zip(xs,ys))/len(xs)
                delta=math.hypot(vx-vy,2*cv)
                aspect=math.sqrt(max(vx+vy+delta,1.)/max(vx+vy-delta,1.))
                concavity += .15*max(0.,min(aspect,10.)-2.2)**2
            cost = sum(_penalty(load,options) for load in loads)+2*concavity
            if len(finalists)<3 or cost<finalists[-1][0]:
                finalists.append((cost,step,[set(g) for g in groups]))
                finalists.sort(key=lambda row:(row[0],row[1]))
                finalists=finalists[:3]
            if all(options.load_min<=load<=options.load_max for load in loads) and concavity==0:
                break
            average = sum(math.sqrt(max(load,.0001)) for load in loads)/number
            eta = .18/(1+step/30)
            multipliers = [max(.15,min(6.,v*(math.sqrt(max(load,.001))/average)**eta))
                           for v,load in zip(multipliers,loads)]
            if step%3==2:
                revised = []
                for group in groups:
                    mass = {i:options.area_weight*areas[i]/(options.target_area*1e6)
                            +(1-options.area_weight)*counts[i]/options.target_stores for i in group}
                    total = sum(mass.values())
                    center = tuple(sum(mass[i]*centers[i][axis] for i in group)/max(total,1e-12) for axis in (0,1))
                    revised.append(min(group,key=lambda i:(math.dist(centers[i],center),i)))
                seeds = revised
        from app.services import bd_zones as z
        evaluated=[]
        for cheap,step,trial in finalists:
            bad_shapes = sum(not z._acceptable_shape(union_all([geoms[i] for i in sorted(g)],grid_size=.001)) for g in trial)
            evaluated.append((cheap+2*bad_shapes,step,trial))
        _,_,groups = min(evaluated,key=lambda row:(row[0],row[1]))
        groups = _rebalance(groups,graph,centers,areas,counts,options,geoms=geoms)
        groups = repair_pairs(groups,graph,centers,areas,counts,options,geoms)
        output.extend(merge_residuals(groups,graph,areas,counts,options,geoms=geoms))
    return output
