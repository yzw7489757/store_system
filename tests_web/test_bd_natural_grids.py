import copy
import json
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString, Point, box, shape
from shapely import union_all

import app.db as appdb
from app.models import BdZone, BdZoneRelease, BdWorkGrid, BdGridStore, BdStoreVisitLog
from app.services import bd_zones as z, bd_natural_grids as natural, bd_grid_regions as regions
from app.services import bd_zone_operations as ops
from app.services.bd_zone_merchants import bind_rows
from tests_web.test_bd_zone_operations import seed, login, csrf  # noqa:F401
from tests.helpers import form_token


def osm_way(identity, coordinates, **tags):
    coords = z.geographic(LineString(coordinates))['coordinates']
    return {'type': 'way', 'id': identity, 'tags': tags,
            'geometry': [{'lon': lon, 'lat': lat} for lon, lat in coords]}


def natural_source(elements, excluded=()):
    return natural.prepare(z.geographic(box(0, 0, 2000, 1000)), elements,
                           [z.geographic(g) for g in excluded], [],
                           captured_at='synthetic', source={'geometry': 'synthetic'})


def test_road_projection_and_rail_grade_input_order_is_stable():
    roads = [osm_way(1, [(1000, -100), (1000, 1100)], highway='primary'),
             osm_way(2, [(-100, 500), (2100, 500)], highway='residential'),
             osm_way(3, [(500, -100), (500, 1100)], highway='secondary', bridge='yes'),
             osm_way(4, [(700, -100), (700, 1100)], highway='trunk', tunnel='yes'),
             osm_way(5, [(1500, -100), (1500, 1100)], railway='rail', layer='-1')]
    doc = natural_source(roads)
    assert len(doc['road_cells']) == 3
    assert doc['natural_audit']['retained_boundaries'][0]['osm_id'] == 1
    assert doc['snapshot_id'] == natural_source(list(reversed(roads)))['snapshot_id']
    assert doc['natural_audit']['geometric_pass']


def test_ground_rail_divides_land_and_dangles_are_reported():
    doc = natural_source([osm_way(1, [(1000, -100), (1000, 1100)], railway='rail'),
                          osm_way(2, [(500, 200), (500, 400)], highway='secondary')])
    assert len(doc['road_cells']) == 2
    assert not shape(doc['natural_audit']['dangles']).is_empty


def test_water_holes_and_islands_remain_in_true_geometry():
    water = [box(900, -100, 1100, 1100), box(200, 200, 400, 400)]
    doc = natural_source([osm_way(1, [(-100, 500), (2100, 500)], highway='primary')], water)
    geoms = [shape(c['metric_geometry']) for c in doc['road_cells']]
    assert len(geoms) == 4
    assert union_all(geoms).area == pytest.approx(1_760_000, abs=1)
    assert not union_all(geoms).covers(Point(300, 300))
    assert natural.boundary_kind({'highway': 'tertiary'}) == 'road'


def selected(grid, zone=None):
    return {'id': grid.id, 'revision': grid.revision, 'zone_id': grid.zone_id,
            'zone_revision': zone.revision if zone else None}


def add_grid(db, release_id, identity, geom, zone_id=None):
    row = BdWorkGrid(release_id=release_id, source_id=identity, geometry=z.geographic(geom),
                     area_km2=geom.area / 1e6, store_count=0, zone_id=zone_id)
    db.add(row); db.flush()
    return row


def test_merge_independent_grids_preserves_hole_and_unique_store_mapping(seed):
    with appdb.SessionLocal() as db:
        from app.models import User
        user = db.get(User, seed['users']['ops-admin'])
        release = db.query(BdZoneRelease).one()
        left = box(3000, 0, 4000, 1000).difference(box(3200, 200, 3400, 400))
        a = add_grid(db, release.id, 'a', left)
        b = add_grid(db, release.id, 'b', box(4000, 0, 5000, 1000))
        db.add(BdGridStore(release_id=release.id, grid_id=a.id, source_id='second-shop')); db.flush()
        selections = [selected(a), selected(b)]
        preview = regions.preview(db, user, selections)
        zone = regions.merge(db, user, selections, '两块合并')
        db.flush()
        assert a.zone_id == b.zone_id == zone.id
        assert zone.store_count == 1 and zone.team_id is None and zone.leader_id is None
        merged = z.projected(zone.geometry)
        assert len(merged.interiors) == 1 and merged.area == pytest.approx(1_960_000, abs=1)
        assert z.projected(preview['geometry']).equals(merged)
        assert db.query(BdGridStore).filter_by(grid_id=a.id, source_id='second-shop').count() == 1
        with pytest.raises(ops.OperationError, match='已变更'):
            regions.merge(db, user, selections, '重复提交')


@pytest.mark.parametrize('other', [box(1000, 1000, 2000, 2000), box(1100, 0, 2100, 1000), box(500, 0, 1500, 1000)])
def test_union_rejects_corner_gap_and_overlap(other):
    with pytest.raises(ops.OperationError):
        regions.merged_geometry([SimpleNamespace(geometry=z.geographic(g)) for g in (box(0, 0, 1000, 1000), other)])


def test_regroup_updates_remainder_archives_empty_and_rejects_disconnected_remainder(seed):
    with appdb.SessionLocal() as db:
        from app.models import User
        user = db.get(User, seed['users']['ops-admin'])
        zone = db.get(BdZone, seed['zone'])
        a = db.get(BdWorkGrid, seed['grid'])
        a.geometry = z.geographic(box(0, 0, 1000, 1000))
        middle = add_grid(db, zone.release_id, 'middle', box(1000, 0, 2000, 1000), zone.id)
        end = add_grid(db, zone.release_id, 'end', box(2000, 0, 3000, 1000), zone.id)
        with pytest.raises(ops.OperationError, match='共边连续'):
            regions.merge(db, user, [selected(middle, zone)], '不能切断旧片区')
        assert a.zone_id == middle.zone_id == end.zone_id == zone.id
        result = regions.merge(db, user, [selected(end, zone)], '东端')
        assert result.area_km2 == pytest.approx(1)
        assert zone.area_km2 == pytest.approx(2)
        new = regions.merge(db, user, [selected(a, zone), selected(middle, zone)], '剩余网格')
        db.flush()
        assert zone.state == 'archived' and a.zone_id == middle.zone_id == new.id
        assert zone.id not in [row.id for row in ops.visible_zones(db, user)]
        with pytest.raises(ops.OperationError, match='归档'):
            ops.assign_zone(db, user, zone.id, seed['team'], zone.revision)


def test_merge_permissions_cross_release_revision_assignment_and_history(seed):
    with appdb.SessionLocal() as db:
        from app.models import User
        admin = db.get(User, seed['users']['ops-admin'])
        grid, zone = db.get(BdWorkGrid, seed['grid']), db.get(BdZone, seed['zone'])
        with pytest.raises(ops.OperationError, match='管理员'):
            regions.merge(db, db.get(User, seed['users']['captain']), [selected(grid, zone)], 'x')
        with pytest.raises(ops.OperationError, match='不重复'):
            regions.merge(db, admin, [selected(grid, zone)] * 2, 'x')
        stale = selected(grid, zone); stale['zone_revision'] -= 1
        with pytest.raises(ops.OperationError, match='已变更'):
            regions.merge(db, admin, [stale], 'x')
        release = BdZoneRelease(fingerprint='other', source={}, parameters={}, audit={}, reviews=[], created_by=admin.id)
        db.add(release); db.flush()
        other = add_grid(db, release.id, 'other', box(2000, 0, 3000, 1000))
        with pytest.raises(ops.OperationError, match='不同版本'):
            regions.merge(db, admin, [selected(grid, zone), selected(other)], 'x')
        ops.assign_zone(db, admin, zone.id, seed['team'], zone.revision)
        with pytest.raises(ops.OperationError, match='召回'):
            regions.merge(db, admin, [selected(grid, zone)], 'x')
        ops.recall_zone(db, admin, zone.id, zone.revision)
        db.add(BdStoreVisitLog(request_id='prior', store_id='shop-synthetic', grid_id=grid.id,
                              actor_id=admin.id, operation='visit', before={}, after={}, note=''))
        db.flush()
        with pytest.raises(ops.OperationError, match='巡店历史'):
            regions.merge(db, admin, [selected(grid, zone)], 'x')


def test_import_natural_candidate_creates_only_grids_and_guards_existing_version(client):
    from app.models import User
    with appdb.SessionLocal() as db:
        user = User(username='natural-admin', password_hash='unused', role='admin', status='active', is_active=True)
        db.add(user); db.flush()
        doc = natural_source([osm_way(1, [(1000, -100), (1000, 1100)], highway='primary')])
        doc = bind_rows(doc, [], {'backend': 'synthetic'})
        candidate = natural.candidate(doc)
        forged = copy.deepcopy(candidate); forged['features'][0]['geometry'] = z.geographic(box(0, 0, 10, 10))
        with pytest.raises(ops.OperationError, match='指纹'):
            natural.import_candidate(db, user, forged)
        release = natural.import_candidate(db, user, candidate)
        db.flush()
        assert db.query(BdZone).count() == 0
        grids = db.query(BdWorkGrid).all()
        assert len(grids) == 2 and all(g.zone_id is None and g.release_id == release.id for g in grids)
        with pytest.raises(ops.OperationError, match='已有正式版本'):
            natural.import_candidate(db, user, candidate)


def test_management_merge_preview_does_not_consume_form_token_and_save_is_admin_only(client, seed):
    login(client, 'ops-admin')
    page = client.get('/zones/manage?mode=manage')
    assert page.status_code == 200 and 'grid-region-editor' in page.text
    with appdb.SessionLocal() as db:
        choices = [selected(db.get(BdWorkGrid, seed['grid']), db.get(BdZone, seed['zone']))]
    token = form_token(client)
    preview = client.post('/zones/grid-merge-preview', json={'selections': choices, 'csrf_token': csrf(page.text)})
    assert preview.status_code == 200, preview.text
    denied = client.post('/zones/grid-merge-preview', json={'selections': choices, 'csrf_token': 'wrong'})
    assert denied.status_code == 409


    saved = client.post('/zones/merge-grids', data={'_ft': token, 'csrf_token': csrf(page.text),
        'selections': [json.dumps(choices[0])], 'name': '手动片区'}, follow_redirects=False)
    assert saved.status_code == 303, saved.text
    client.cookies.clear()
    login(client, 'captain')
    denied = client.post('/zones/grid-merge-preview', json={'selections': choices,
        'csrf_token': csrf(client.get('/my/work').text)}, follow_redirects=False)
    assert denied.status_code in (302, 403, 409)


def test_grid_editor_runs_preview_and_discards_stale_response():
    import subprocess
    result = subprocess.run(['node', 'tests_web/bd_grid_regions_mock.cjs'], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_removed_region_bookmark_still_opens_grid_management(client,seed):
    login(client,'ops-admin')
    page=client.get('/zones/manage?mode=manage&zone=999999')
    assert page.status_code==200
    assert 'grid-region-editor' in page.text
    assert 'grid-region-merge' in page.text
    assert 'grid-region-preview' not in page.text


def test_shared_url_separates_assignment_and_composition(client,seed):
    import re
    login(client,'ops-admin')
    assignment=client.get(f'/zones?zone={seed["zone"]}')
    assert assignment.status_code==200
    assert 'data-mode="assign"' in assignment.text
    assert f'action="/zones/{seed["zone"]}/assign"' in assignment.text
    assert 'id="grid-region-editor"' not in assignment.text
    management=client.get(f'/zones?mode=manage&zone={seed["zone"]}')
    assert management.status_code==200
    assert 'data-mode="manage"' in management.text
    assert 'id="grid-region-editor"' in management.text
    assert 'id="grid-region-search"' not in management.text
    assert 'id="grid-region-count"' in management.text
    assert 'id="grid-region-area"' in management.text
    assert 'id="grid-region-stores"' in management.text
    assert f'action="/zones/{seed["zone"]}/assign"' not in management.text
    navigation=re.search(r'<nav class="zone-mode-switch".*?</nav>',management.text,re.S).group(0)
    links=re.findall(r'href="([^"]*mode=(?:assign|manage)[^"]*)"',navigation)
    assert len(links)==2 and all('/zones?' in link and '/zones/manage' not in link for link in links)
    assert client.get('/zones?mode=unknown').status_code==404


def test_qualified_arterials_and_junction_links_not_small_roads():
    assert natural.boundary_kind({'highway':'primary_link'}) == 'road'
    assert natural.boundary_kind({'highway':'tertiary','lanes':'2'}) == 'road'
    assert natural.boundary_kind({'highway':'tertiary','width':'8 m'}) == 'road'
    assert natural.boundary_kind({'highway':'tertiary','lanes':'1'}) is None
    assert natural.boundary_kind({'highway':'tertiary_link','lanes':'1','oneway':'yes'}) == 'road'
    assert natural.boundary_kind({'highway':'residential','lanes':'2'}) is None
    assert natural.boundary_kind({'highway':'primary','tunnel':'yes'},length_m=100) == 'road_projection'
    assert natural.boundary_kind({'highway':'primary','tunnel':'yes'},length_m=1000) is None
    assert natural.boundary_kind({'railway':'rail','bridge':'yes'},length_m=100) is None


@pytest.mark.parametrize('combine', [True, False])
def test_optional_boundary_mode_dynamic_geometry_and_persistence(seed, combine):
    from app.models import User, BdRegion
    assert BdRegion.__tablename__ == 'bd_region'
    with appdb.SessionLocal() as db:
        user = db.get(User, seed['users']['ops-admin'])
        release = db.query(BdZoneRelease).one()
        a = add_grid(db, release.id, 'dynamic-a', box(4000,0,5000,1000))
        b = add_grid(db, release.id, 'dynamic-b', box(5000,0,6000,1000))
        choices = [selected(a), selected(b)]
        result = regions.preview(db,user,choices,combine)
        assert result['display_geometry']['type'] == ('Polygon' if combine else 'GeometryCollection')
        assert result['properties']['area_km2'] == pytest.approx(2)
        region = regions.merge(db,user,choices,'可选边界',combine)
        db.flush()
        assert region.merge_boundaries is combine
        features = ops.management_context(db,user)['geometry']['features']
        rendered = next(f for f in features if f['properties']['id'] == region.id)
        assert rendered['geometry']['type'] == ('Polygon' if combine else 'GeometryCollection')
        assert rendered['properties']['preserve_holes'] is True


def test_normalisation_conserves_merchants_and_absorbs_tiny_face():
    doc = natural_source([osm_way(1,[(1,-100),(1,1100)],highway='primary'),
                          osm_way(2,[(1000,-100),(1000,1100)],highway='primary')])
    lon,lat=z.geographic(Point(.5,500))['coordinates']
    doc=bind_rows(doc,[{'place_id':'tiny-shop','name':'tiny','lon':lon,'lat':lat}],{'backend':'synthetic'})
    normal=natural.normalise(doc)
    assert normal['natural_audit']['quality_pass']
    assert normal['natural_audit']['geometric_pass']
    assert sorted(i for c in normal['road_cells'] for i in c['merchant_ids']) == ['tiny-shop']
    assert min(c['area_km2'] for c in normal['road_cells']) >= .05


def test_replacement_clears_old_regions_only_after_quality_validation(seed):
    from app.models import User
    with appdb.SessionLocal() as db:
        user=db.get(User,seed['users']['ops-admin'])
        doc=natural_source([osm_way(1,[(1000,-100),(1000,1100)],highway='primary')])
        lon,lat=z.geographic(Point(500,500))['coordinates']
        doc=natural.normalise(bind_rows(doc,[{'place_id':'shop-synthetic','name':'shop','lon':lon,'lat':lat}],{'backend':'synthetic'}))
        result=natural.candidate(doc)
        invalid=copy.deepcopy(result)
        invalid['source_document']['natural_audit']['quality_pass']=False
        invalid['audit']['quality_pass']=False
        with pytest.raises(ops.OperationError,match='旧数据未清空'):
            natural.import_candidate(db,user,invalid,replace=True)
        assert db.query(BdZone).count()==1
        release=natural.import_candidate(db,user,result,replace=True)
        db.flush()
        assert db.query(BdZone).count()==0
        assert all(g.zone_id is None and g.release_id==release.id for g in db.query(BdWorkGrid).all())
        assert db.query(BdGridStore).count()==1


def test_extreme_area_is_blocked_and_small_inhabited_island_is_explicit():
    mainland=box(0,0,10000,10000)
    island=box(12000,0,12010,10)
    blank=box(13000,0,13010,10)
    doc=natural.prepare(z.geographic(union_all([mainland,island,blank])),
        [osm_way(1,[(0,5000),(100,5000)],highway='primary')],[],[],captured_at='synthetic',source={})
    rows=[]
    for identity,point in [('main-shop',Point(5000,5000)),('island-shop',Point(12005,5))]:
        lon,lat=z.geographic(point)['coordinates']
        rows.append({'place_id':identity,'name':identity,'lon':lon,'lat':lat})
    normal=natural.normalise(bind_rows(doc,rows,{'backend':'synthetic'}))
    audit=normal['natural_audit']
    assert not audit['quality_pass']
    assert any('oversized' in item['reasons'] for item in audit['normalisation']['violations'])
    assert len(audit['normalisation']['island_exceptions'])==1
    assert len(audit['excluded_empty_cell_ids'])==1
    assert sum(len(c['merchant_ids']) for c in normal['road_cells'])==2


@pytest.mark.parametrize('area,accepted', [(2.5,True),(6.,False)])
def test_indivisible_natural_block_exception_has_a_hard_upper_bound(area,accepted):
    doc=natural.prepare(z.geographic(box(0,0,area*1000,1000)),
        [osm_way(1,[(0,500),(100,500)],highway='primary')],[],[],captured_at='synthetic',source={})
    lon,lat=z.geographic(Point(500,500))['coordinates']
    normal=natural.normalise(bind_rows(doc,[{'place_id':'one','name':'one','lon':lon,'lat':lat}],{'backend':'synthetic'}))
    assert normal['natural_audit']['quality_pass'] is accepted
    assert bool(normal['natural_audit']['normalisation']['natural_block_exceptions']) is accepted


def test_empty_indivisible_large_block_is_audited_without_losing_merchants():
    doc=natural.prepare(z.geographic(box(0,0,7000,1000)),
        [osm_way(1,[(6000,-100),(6000,1100)],highway='primary')],[],[],captured_at='synthetic',source={})
    lon,lat=z.geographic(Point(6500,500))['coordinates']
    normal=natural.normalise(bind_rows(doc,[{'place_id':'commercial','name':'shop','lon':lon,'lat':lat}],{'backend':'synthetic'}))
    assert normal['natural_audit']['quality_pass']
    assert normal['natural_audit']['excluded_large_empty_area_km2']==pytest.approx(6)
    assert normal['area_km2']==pytest.approx(1)
    assert sum(len(c['merchant_ids']) for c in normal['road_cells'])==1


def test_coastal_island_is_bounded_and_not_an_administrative_cutout():
    from shapely.geometry import mapping
    admin=box(0,0,10000,10000)
    island=box(1000,1000,4500,4500)
    doc=natural.prepare(z.geographic(admin),
        [osm_way(1,[(2750,500),(2750,5000)],highway='primary')],
        [z.geographic(admin.difference(island))],[],captured_at='synthetic',source={})
    lon,lat=z.geographic(Point(2000,2000))['coordinates']
    result=natural.normalise(bind_rows(doc,[{'place_id':'island','name':'shop','lon':lon,'lat':lat}],{'backend':'synthetic'}))
    assert result['natural_audit']['quality_pass']
    assert len(result['road_cells'])==1
    assert result['area_km2']==pytest.approx(12.25)
    assert result['natural_audit']['normalisation']['island_exceptions'][0]['reason']=='complete_coastal_island'
    # 相同面积若由行政外框裁出，不能伪装成海岸岛屿。
    doc['boundary']=z.geographic(island)
    result=natural.normalise(bind_rows(doc,[{'place_id':'island','name':'shop','lon':lon,'lat':lat}],{'backend':'synthetic'}))
    assert not result['natural_audit']['quality_pass']


def test_group_guard_prevents_creating_extreme_chain_without_changing_default():
    geometries=[box(0,0,1000,100),box(1000,0,2000,100)]
    topology={0:{1},1:{0}}
    plain=z._group(geometries,[0,0],1,2e6,adjacency=topology)
    guarded=z._group(geometries,[0,0],1,2e6,adjacency=topology,
                     merge_guard=lambda geom:z._shape_metrics(geom)['aspect']<=12)
    assert len(plain)==1 and len(guarded)==2
    assert topology=={0:{1},1:{0}}


def test_closed_outline_covers_lake_but_real_geometry_stays_excluded():
    from app.services.bd_grid_geometry import display_geometry,consolidate
    from shapely.geometry import mapping
    outer=box(0,0,1000,1000).difference(box(200,200,800,800))
    island=box(400,400,600,600)
    result=consolidate([{'geom':outer,'members':{0},'count':2},
                        {'geom':island,'members':{1},'count':1}])
    assert len(result)==1
    real=result[0]['geom']
    assert real.geom_type=='MultiPolygon' and real.area==pytest.approx(680000)
    shown=shape(display_geometry(mapping(real)))
    assert shown.geom_type=='Polygon' and len(shown.interiors)==0
    assert shown.area==pytest.approx(1000000) and shown.covers(Point(300,300))
    assert not real.covers(Point(300,300))
    assert result[0]['members']=={0,1} and result[0]['count']==3
    merged=regions.merged_geometry([SimpleNamespace(geometry=z.geographic(real))])
    assert merged.area==pytest.approx(real.area,abs=.01)


def test_containment_does_not_absorb_partial_overlap_or_external_island():
    from app.services.bd_grid_geometry import containment_groups
    outer=box(0,0,1000,1000).difference(box(200,200,800,800))
    inner=box(300,300,400,400)
    crossing=box(900,900,1100,1100)
    external=box(2000,0,2100,100)
    families=containment_groups([outer,inner,crossing,external])
    assert families=={0:[0,1],2:[2],3:[3]}


def test_repair_enclosed_grid_keeps_ids_maps_and_effective_area(seed):
    from app.services.bd_grid_geometry import repair_grids
    from app.models import User
    with appdb.SessionLocal() as db:
        user=db.get(User,seed['users']['ops-admin'])
        release=db.query(BdZoneRelease).one()
        outer=add_grid(db,release.id,'enclosing',box(3000,0,4000,1000).difference(box(3200,200,3800,800)))
        inner=add_grid(db,release.id,'inside-lake',box(3400,400,3600,600))
        db.add(BdGridStore(release_id=release.id,grid_id=inner.id,source_id='island-shop'));db.flush()
        outer_id,inner_id=outer.id,inner.id
        before=repair_grids(db,user)
        assert before['removed_grid_count']==1
        assert db.get(BdWorkGrid,inner_id) is not None
        repaired=repair_grids(db,user,apply=True);db.flush()
        assert repaired['groups'][0]['outer_id']==outer_id
        assert db.get(BdWorkGrid,inner_id) is None
        assert outer.area_km2==pytest.approx(.68)
        assert outer.store_count==1 and outer.revision==1
        assert db.query(BdGridStore).filter_by(source_id='island-shop').one().grid_id==outer_id
        assert z.projected(outer.geometry).geom_type=='MultiPolygon'
        shown=next(g for g in regions.management_grids(db,user) if g['id']==outer_id)['geometry']
        assert shown['type']=='Polygon' and len(shown['coordinates'])==1
        assert repair_grids(db,user,apply=True)['removed_grid_count']==0


def test_repair_enclosed_assigned_grid_fails_before_modifying(seed):
    from app.services.bd_grid_geometry import repair_grids
    from app.models import User
    with appdb.SessionLocal() as db:
        user=db.get(User,seed['users']['ops-admin'])
        release=db.query(BdZoneRelease).one()
        outer=add_grid(db,release.id,'locked-outer',box(3000,0,4000,1000).difference(box(3200,200,3800,800)))
        inner=add_grid(db,release.id,'locked-inner',box(3400,400,3600,600),seed['zone'])
        with pytest.raises(ops.OperationError,match='归属'):
            repair_grids(db,user,apply=True)
        assert db.get(BdWorkGrid,inner.id) is not None and outer.revision==0


def test_tail_is_reconsidered_after_its_neighbour_changes(monkeypatch):
    from shapely.geometry import mapping
    geoms=[box(0,0,100,1),box(100,0,200,10),box(200,0,1000,1000)]
    work=union_all(geoms,grid_size=.001)
    doc={'boundary':z.geographic(work.buffer(100)), 'work_area':z.geographic(work),
         'natural_audit':{}, 'road_cells':[
             {'id':str(i),'metric_geometry':mapping(g),'merchant_ids':['shop'] if i==2 else [],
              'store_count':1 if i==2 else 0} for i,g in enumerate(geoms)]}
    monkeypatch.setattr(z,'_group',lambda gs,counts,*args,**kwargs:[
        {'geom':g,'members':{i},'count':counts[i]} for i,g in enumerate(gs)])
    result=natural.normalise(doc)
    assert result['natural_audit']['quality_pass']
    assert len(result['road_cells'])==1
    assert result['road_cells'][0]['merchant_ids']==['shop']


def test_independent_grid_migration_backfills_and_prevents_unsafe_downgrade():
    import importlib.util
    from sqlalchemy import create_engine, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location('grid_migration', 'migrations/versions/c8e31f209abd_independent_work_grids.py')
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE bd_zone_release (id INTEGER PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE bd_zone (id INTEGER PRIMARY KEY, release_id INTEGER NOT NULL)'))
        connection.execute(text('CREATE TABLE bd_work_grid (id INTEGER PRIMARY KEY, zone_id INTEGER NOT NULL REFERENCES bd_zone(id), source_id VARCHAR(64) NOT NULL)'))
        connection.execute(text('INSERT INTO bd_zone_release VALUES (7)'))
        connection.execute(text('INSERT INTO bd_zone VALUES (3,7)'))
        connection.execute(text("INSERT INTO bd_work_grid VALUES (1,3,'old')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert connection.execute(text('SELECT release_id FROM bd_work_grid WHERE id=1')).scalar() == 7
        assert next(c for c in inspect(connection).get_columns('bd_work_grid') if c['name'] == 'zone_id')['nullable']
        connection.execute(text("INSERT INTO bd_work_grid (id,zone_id,release_id,source_id) VALUES (2,NULL,7,'new')"))
        with pytest.raises(RuntimeError, match='未归片区'):
            migration.downgrade()
        connection.execute(text('DELETE FROM bd_work_grid WHERE id=2'))
        migration.downgrade()
        assert [c['name'] for c in inspect(connection).get_columns('bd_work_grid')] == ['id', 'zone_id', 'source_id']
        assert connection.execute(text('SELECT zone_id FROM bd_work_grid WHERE id=1')).scalar() == 3
