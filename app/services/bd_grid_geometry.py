"""闭合外轮廓与真实作业几何分离；包含归并不填水、不连接外部飞地。"""
from shapely import STRtree, union_all
from shapely.geometry import Polygon, mapping, shape


def outline(geometry):
    if geometry.geom_type == 'Polygon':
        return Polygon(geometry.exterior)
    return union_all([outline(g) for g in getattr(geometry, 'geoms', ())
                      if g.geom_type in ('Polygon', 'MultiPolygon')], grid_size=.001)


def display_geometry(geojson):
    """只供地图显示/点击，不得用于面积、门店位置校验或覆盖审计。"""
    return mapping(outline(shape(geojson)))


def containment_groups(geometries):
    """每个内部面归到最小包围面；追溯根节点，一次处理多层包含。"""
    shells = [outline(g) for g in geometries]
    tree = STRtree(shells)
    parent = list(range(len(shells)))
    for i, shell in enumerate(shells):
        containers = [int(j) for j in tree.query(shell, predicate='covered_by')
                      if int(j) != i and shells[int(j)].area > shell.area + .01]
        if containers:
            parent[i] = min(containers, key=lambda j:(shells[j].area,j))
    groups = {}
    for i in range(len(parent)):
        root = i
        while parent[root] != root:
            root = parent[root]
        groups.setdefault(root, []).append(i)
    return groups


def consolidate(groups):
    """生成阶段：按完整自然面成员合并，被包围岛保留真实MultiPolygon。"""
    groups = list(groups)
    families = containment_groups([group['geom'] for group in groups])
    output = []
    for root, members in families.items():
        output.append({**groups[root],
            'geom':union_all([groups[i]['geom'] for i in members],grid_size=.001),
            'members':set().union(*(groups[i]['members'] for i in members)),
            'count':sum(groups[i]['count'] for i in members)})
    return output


def repair_grids(db,user,*,apply=False):
    """存量修复，完整校验后修改；调用方备份并提交。"""
    import hashlib
    from datetime import datetime
    from sqlalchemy import func
    from app.models import BdWorkGrid,BdGridStore,BdStoreVisitLog,BdZoneRelease
    from app.services import bd_zones as z,bd_zone_operations as ops
    ops.require_admin(user)
    if apply:
        # 与片区归组保持相同锁顺序。
        for identity, in db.query(BdZoneRelease.id).order_by(BdZoneRelease.id):
            ops.locked(db,BdZoneRelease,identity)
    query=db.query(BdWorkGrid).order_by(BdWorkGrid.id)
    if apply: query=query.populate_existing().with_for_update()
    rows=query.all()
    geoms=[z.projected(row.geometry) for row in rows]
    planned=[]
    for release_id in sorted({row.release_id for row in rows}):
        indices=[i for i,row in enumerate(rows) if row.release_id==release_id]
        families=containment_groups([geoms[i] for i in indices])
        for root, members in families.items():
            if len(members)<2:continue
            outer=indices[root];inner=[indices[i] for i in members if i!=root]
            involved=[rows[i] for i in [outer,*inner]]
            if any(row.zone_id is not None or row.assignee_id is not None or row.progress
                   or row.task_state!='unvisited' for row in involved):
                raise ops.OperationError('被包围网格已有片区或作业归属，不能自动归并')
            ids=[row.id for row in involved]
            if db.query(BdStoreVisitLog.id).filter(BdStoreVisitLog.grid_id.in_(ids)).first():
                raise ops.OperationError('被包围网格已有巡店记录，不能自动归并')
            geom=union_all([geoms[i] for i in [outer,*inner]],grid_size=.001)
            if (not geom.is_valid or outline(geom).geom_type!='Polygon'
                    or abs(geom.area-sum(geoms[i].area for i in [outer,*inner]))>.01):
                raise ops.OperationError('包含网格并集无效或存在重叠')
            planned.append((outer,inner,geom))
    report={'groups':[], 'removed_grid_count':sum(len(inner) for _,inner,_ in planned)}
    for outer,inner,geom in planned:
        row=rows[outer];removed=[rows[i].id for i in inner]
        count=db.query(func.count(BdGridStore.id)).filter(BdGridStore.grid_id.in_([row.id,*removed])).scalar()
        entry={'outer_id':row.id,'absorbed_ids':removed,'area_km2':geom.area/1e6,
               'store_count':count,'geometry_sha256':hashlib.sha256(geom.normalize().wkb).hexdigest()}
        report['groups'].append(entry)
        if apply:
            before={'source_id':row.source_id,'area_km2':row.area_km2,'store_count':row.store_count,
                    'absorbed':[{'id':rows[i].id,'source_id':rows[i].source_id} for i in inner]}
            db.query(BdGridStore).filter(BdGridStore.grid_id.in_(removed)).update(
                {BdGridStore.grid_id:row.id},synchronize_session=False)
            row.geometry=z.geographic(geom);row.area_km2=geom.area/1e6
            row.store_count=count;row.revision+=1;row.updated_at=datetime.utcnow()
            for i in inner:db.delete(rows[i])
            ops.audit_log(db,user,'grid','absorb_enclosed',row,before,entry)
    return report
