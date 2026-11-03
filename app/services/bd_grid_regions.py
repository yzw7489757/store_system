"""完整网格归组；只写作业域，所有验证先于修改，调用者提交事务。"""
import hashlib
import uuid
from datetime import datetime

from sqlalchemy import func
from shapely import union_all
from shapely.geometry import shape

from app.models import BdZone, BdZoneRelease, BdWorkGrid, BdGridStore, BdStoreVisitLog
from app.services import bd_zones as z, bd_zone_operations as ops
from app.services.bd_grid_geometry import outline, display_geometry


def merged_geometry(grids):
    """毫米精度米制并集；不 buffer 补缝、不填水洞、不连接岛屿或角点。"""
    geometries = []
    for grid in grids:
        raw = shape(grid.geometry)
        if raw.geom_type not in ('Polygon', 'MultiPolygon') or not raw.is_valid or raw.is_empty:
            raise ops.OperationError('网格边界无效，请重新生成')
        geometries.append(z.projected(grid.geometry))
    combined = union_all(geometries, grid_size=.001)
    if outline(combined).geom_type != 'Polygon' or not combined.is_valid or combined.is_empty:
        raise ops.OperationError('网格必须共边连续；不能跨水域、飞地或仅连接角点')
    if sum(g.area for g in geometries) - combined.area > .01:
        raise ops.OperationError('网格存在面积重叠，不能合并')
    return combined


def selection_plan(db, user, selections, *, for_update=False):
    ops.require_admin(user)
    try:
        if not isinstance(selections, list) or not selections or len(selections) > 5000:
            raise ValueError()
        expected = {}
        for item in selections:
            if not isinstance(item, dict) or any(type(item.get(k)) is not int for k in ('id', 'revision')):
                raise ValueError()
            if item['id'] in expected:
                raise ValueError()
            if item.get('zone_id') is not None and (type(item['zone_id']) is not int or type(item.get('zone_revision')) is not int):
                raise ValueError()
            expected[item['id']] = item
    except (TypeError, KeyError, ValueError):
        raise ops.OperationError('请选择不重复的网格并刷新版本信息') from None
    snapshots = db.query(BdWorkGrid).filter(BdWorkGrid.id.in_(expected)).all()
    if len(snapshots) != len(expected):
        raise ops.OperationError('网格不存在，请刷新后重试')
    releases = {g.release_id for g in snapshots}
    if len(releases) != 1:
        raise ops.OperationError('不同版本的网格不能合并')
    release_id = next(iter(releases))
    # 与派发一致：先锁片区，再锁网格；版本锁串行化本版本的归组/导入。
    if for_update:
        ops.locked(db, BdZoneRelease, release_id)
    zone_ids = sorted({item['zone_id'] for item in expected.values() if item.get('zone_id') is not None})
    zones = {}
    for identity in zone_ids:
        zone = ops.locked(db, BdZone, identity) if for_update else db.get(BdZone, identity)
        if zone is None or zone.state == 'archived' or zone.release_id != release_id:
            raise ops.OperationError('片区已变更，请刷新后重试')
        zones[identity] = zone
    query = db.query(BdWorkGrid).filter(
        (BdWorkGrid.id.in_(expected)) | (BdWorkGrid.zone_id.in_(zone_ids))).order_by(BdWorkGrid.id)
    if for_update:
        query = query.populate_existing().with_for_update()
    affected = query.all()
    selected = [g for g in affected if g.id in expected]
    for grid in selected:
        item = expected[grid.id]
        ops.revision_ok(grid, item['revision'])
        if grid.zone_id != item.get('zone_id'):
            raise ops.OperationError('网格归属已变更，请刷新后重试')
        if grid.zone_id is not None:
            ops.revision_ok(zones[grid.zone_id], item['zone_revision'])
    if any(zone.team_id is not None or zone.leader_id is not None for zone in zones.values()):
        raise ops.OperationError('请先召回片区，再调整网格归属')
    if any(g.assignee_id is not None or g.progress or g.task_state != 'unvisited' for g in affected):
        raise ops.OperationError('涉及已派发或已开展的网格，不能调整片区')
    if db.query(BdStoreVisitLog.id).filter(BdStoreVisitLog.grid_id.in_([g.id for g in affected])).first():
        raise ops.OperationError('涉及已有巡店历史的网格，不能调整片区')
    geometry = merged_geometry(selected)
    remaining = {}
    for identity in zone_ids:
        members = [g for g in affected if g.zone_id == identity and g.id not in expected]
        remaining[identity] = (members, merged_geometry(members) if members else None)
    counts = dict(db.query(BdGridStore.grid_id, func.count(BdGridStore.id))
                  .filter(BdGridStore.grid_id.in_([g.id for g in affected]))
                  .group_by(BdGridStore.grid_id).all())
    return selected, zones, remaining, geometry, counts


def preview(db, user, selections, merge_boundaries=True):
    if type(merge_boundaries) is not bool:
        raise ops.OperationError('合并边界选项无效')
    grids, zones, remaining, geometry, counts = selection_plan(db, user, selections)
    footprint = z.geographic(geometry)
    return {'type': 'Feature', 'geometry': footprint,
            'display_geometry': display_geometry(footprint) if merge_boundaries else {
                'type': 'GeometryCollection', 'geometries': [display_geometry(g.geometry) for g in grids]}, 'properties': {
        'merge_boundaries': merge_boundaries,
        'grid_count': len(grids), 'area_km2': geometry.area / 1e6,
        'store_count': sum(counts.get(g.id, 0) for g in grids),
        'shape_valid': z._acceptable_shape(geometry),
        'archived_zone_ids': [identity for identity, (members, _) in remaining.items() if not members]}}


def merge(db, user, selections, name, merge_boundaries=True):
    if type(merge_boundaries) is not bool:
        raise ops.OperationError('合并边界选项无效')
    name = name.strip()
    if not name or len(name) > 128:
        raise ops.OperationError('片区名称须为1～128个字符')
    grids, zones, remaining, geometry, counts = selection_plan(db, user, selections, for_update=True)
    before = [{'id': g.id, 'zone_id': g.zone_id, 'revision': g.revision} for g in grids]
    zone = BdZone(release_id=grids[0].release_id, source_id='manual-' + uuid.uuid4().hex,
                  name=name, geometry=z.geographic(geometry), area_km2=geometry.area / 1e6,
                  store_count=sum(counts.get(g.id, 0) for g in grids), review=[], state='ready',
                  merge_boundaries=merge_boundaries)
    db.add(zone)
    db.flush()
    now = datetime.utcnow()
    for grid in grids:
        grid.zone_id = zone.id
        grid.revision += 1
        grid.updated_at = now
    for identity, (members, rest) in remaining.items():
        parent = zones[identity]
        old = {'revision': parent.revision, 'state': parent.state, 'store_count': parent.store_count}
        if rest is None:
            parent.state = 'archived'
            parent.store_count = 0
        else:
            parent.geometry = z.geographic(rest)
            parent.area_km2 = rest.area / 1e6
            parent.store_count = sum(counts.get(g.id, 0) for g in members)
        parent.workload = None
        parent.revision += 1
        parent.updated_at = now
        ops.audit_log(db, user, 'zone', 'regroup', parent, old,
                      {'state': parent.state, 'grid_ids': [g.id for g in members], 'new_zone_id': zone.id})
    ops.audit_log(db, user, 'zone', 'merge_grids', zone, {'grids': before}, {
        'name': name, 'merge_boundaries': merge_boundaries, 'grid_ids': [g.id for g in grids],
        'geometry_sha256': hashlib.sha256(geometry.normalize().wkb).hexdigest()})
    return zone


def management_grids(db, user):
    ops.require_admin(user)
    history = db.query(BdStoreVisitLog.grid_id, BdWorkGrid.zone_id).join(
        BdWorkGrid, BdWorkGrid.id == BdStoreVisitLog.grid_id).distinct().all()
    history_grids = {identity for identity, _ in history}
    history_zones = {identity for _, identity in history if identity is not None}
    rows = db.query(BdWorkGrid, BdZone).outerjoin(BdZone, BdZone.id == BdWorkGrid.zone_id)
    return [{'id': g.id, 'revision': g.revision, 'zone_id': g.zone_id,
             'zone_revision': zone.revision if zone else None, 'release_id': g.release_id,
             'name': g.source_id, 'zone_name': zone.name if zone else '',
             'geometry': display_geometry(g.geometry), 'stores': g.store_count, 'area': g.area_km2,
             'locked': bool(g.id in history_grids or g.zone_id in history_zones
                            or g.assignee_id or g.progress or g.task_state != 'unvisited'
                            or (zone and (zone.team_id or zone.leader_id)))}
            for g, zone in rows.order_by(BdWorkGrid.id).all()]
