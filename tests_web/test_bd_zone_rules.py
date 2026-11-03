"""新生成口径：数量、包含、水域隔离与零商户排除，全部使用合成几何。"""
import copy
import pytest
from shapely import union_all
from shapely.geometry import box, mapping
from app.services import bd_zone_rules as rules, bd_zone_coverage as coverage, bd_zone_balance as balance, bd_zones as zones
from tests_web.test_bd_zone_coverage import source  # noqa:F401


def test_count_limit_matches_user_example_and_rounds_down():
    assert rules.count_limit(600,25)==72
    assert rules.count_limit(582.102,25)==69
    assert rules.count_limit(24,25)==2


def test_enclosed_commercial_island_joins_container_without_filling_water():
    frame=box(0,0,4000,4000).difference(box(1000,1000,3000,3000))
    island=box(1500,1500,2000,2000)
    geoms=[frame,island]
    groups,excluded,audit=rules.consolidate([{0},{1}],geoms,[10,2],{0:set(),1:set()},
        balance.BalanceOptions(),area_km2=12.25)
    assert groups==[{0,1}] and excluded==set()
    assert audit['containment_merges']==1
    assert union_all([geoms[i] for i in groups[0]]).area==12.25e6
    assert rules.shell(union_all(geoms)).area>union_all(geoms).area


def test_nested_islands_all_join_outermost_container():
    geoms=[box(0,0,10000,10000).difference(box(1000,1000,9000,9000)),
           box(2000,2000,8000,8000).difference(box(3000,3000,7000,7000)),box(4000,4000,5000,5000)]
    groups,excluded,audit=rules.consolidate([{0},{1},{2}],geoms,[10,2,1],{i:set() for i in range(3)},
        balance.BalanceOptions(),area_km2=57)
    assert groups==[{0,1,2}] and audit['containment_merges']==2 and not excluded


def test_zero_merchant_external_region_is_excluded_but_enclosed_shop_kept():
    geoms=[box(0,0,2000,2000),box(4000,0,5000,1000)]
    groups,excluded,audit=rules.consolidate([{0},{1}],geoms,[5,0],{0:set(),1:set()},
        balance.BalanceOptions(),area_km2=5)
    assert groups==[{0}] and excluded=={1}
    assert audit['excluded_area_km2']==1


def test_external_water_gap_never_joins_even_when_near_and_cap_blocks():
    geoms=[box(0,0,1000,1000),box(1001,0,2001,1000)]
    with pytest.raises(zones.ZoneDataError,match='上限为1.*2个.*禁止跨外部水域'):
        rules.consolidate([{0},{1}],geoms,[5,5],{0:set(),1:set()},balance.BalanceOptions(),area_km2=2)


def test_real_neighbours_merge_to_cap_without_losing_stores():
    geoms=[box(i*1000,0,(i+1)*1000,1000) for i in range(3)]
    groups,excluded,audit=rules.consolidate([{0},{1},{2}],geoms,[2,3,4],coverage.face_graph(geoms),
        balance.BalanceOptions(),area_km2=3)
    assert groups==[{0,1,2}] and excluded==set()
    assert audit['max_zone_count']==1 and audit['adjacent_merges']==2
    assert sum([2,3,4][i] for g in groups for i in g)==9


def test_rules_do_not_keep_an_empty_work_region():
    with pytest.raises(zones.ZoneDataError,match='没有商户'):
        rules.consolidate([{0}],[box(0,0,1000,1000)],[0],{0:set()},balance.BalanceOptions(),area_km2=1)


def policy_source(source):
    doc=copy.deepcopy(source)
    geoms=[box(0,0,4000,4000).difference(box(1000,1000,3000,3000)),
           box(1500,1500,2000,2000),box(8000,0,9000,1000)]
    work=union_all(geoms)
    doc.update(boundary=zones.geographic(box(0,0,9000,4000)),work_area=zones.geographic(work),
        area_km2=13.25,store_count=2,topology=[[],[],[]])
    doc['road_cells']=[dict(id=f'cell-{i}',metric_geometry=mapping(g),store_count=int(i<2),
        merchant_ids=[f'shop-{i}'] if i<2 else []) for i,g in enumerate(geoms)]
    doc['merchant_audit'].update(inside_work=2)
    return doc


def test_scope_audits_exclusions_and_preserves_source(source):
    doc=policy_source(source)
    result=coverage.scope_preview(doc,8000,25,balance_options=balance.BalanceOptions(),
                                  include_memberships=True,generation_rules=True)
    assert len(result['features'])==1
    assert result['audit']['excluded_empty_cell_ids']==['cell-2']
    assert result['audit']['generation_rules']['max_zone_count']==1
    assert result['audit']['geometric_pass'] and result['audit']['merchant_pass']
    assert result['audit']['all_cells_assigned_once'] and result['audit']['all_task_cells_assigned_once']
    assert sum(f['properties']['store_count'] for f in result['features'])==2
    assert zones.projected(result['work_area']).area==pytest.approx(12.25e6,abs=1)
    assert zones.projected(doc['work_area']).area==pytest.approx(13.25e6,abs=1)
    assert all('cell-2' not in f['properties']['cell_ids'] for f in result['grids'])


def test_formal_generation_enforces_exclusions_and_cap(client,source):
    import app.db as appdb
    from app.models import User,BdZone,BdGridStore
    from app.services import bd_zone_operations as ops
    doc=policy_source(source)
    result=coverage.scope_preview(doc,8000,25,balance_options=balance.BalanceOptions(),
                                  include_memberships=True,generation_rules=True)
    with appdb.SessionLocal() as db:
        admin=User(username='rule-admin',role='admin',password_hash='unused',status='active',is_active=True)
        db.add(admin);db.flush()
        tampered=copy.deepcopy(result)
        tampered['audit']['generation_rules']['max_zone_count']=99
        with pytest.raises(ops.OperationError,match='数量'):ops.formal_generate(db,admin,tampered,doc)
        tampered=copy.deepcopy(result)
        tampered['audit'].pop('generation_rules')
        tampered['features'].append(copy.deepcopy(tampered['features'][0]))
        with pytest.raises(ops.OperationError,match='数量'):ops.formal_generate(db,admin,tampered,doc)
        tampered=copy.deepcopy(result)
        tampered['audit']['excluded_empty_cell_ids'].append('cell-0')
        with pytest.raises(ops.OperationError,match='有商户'):ops.formal_generate(db,admin,tampered,doc)
        ops.formal_generate(db,admin,result,doc)
        assert db.query(BdZone).count()==1 and db.query(BdGridStore).count()==2


def test_excluded_empty_neighbour_does_not_break_coverage(source,monkeypatch):
    doc=policy_source(source)
    doc['road_cells'][2]['metric_geometry']=mapping(box(4000,0,5000,1000))
    from shapely.geometry import shape
    geoms=[shape(c['metric_geometry']) for c in doc['road_cells']]
    doc['work_area']=zones.geographic(union_all(geoms))
    doc['topology']=[[2],[],[0]]
    monkeypatch.setattr(balance,'partition',lambda *args:[{0},{1},{2}])
    result=coverage.scope_preview(doc,8000,25,balance_options=balance.BalanceOptions(),generation_rules=True)
    assert result['audit']['excluded_empty_cell_ids']==['cell-2']
    assert result['audit']['geometric_pass'] and result['audit']['merchant_pass']


def test_count_cap_never_justifies_overloaded_region():
    geoms=[box(0,0,1000,1000),box(1000,0,2000,1000)]
    with pytest.raises(zones.ZoneDataError,match='负载上限'):
        rules.consolidate([{0},{1}],geoms,[8000,8000],coverage.face_graph(geoms),
                          balance.BalanceOptions(),area_km2=2)


@pytest.mark.parametrize('width,allowed',[(60,True),(100,True),(100.1,False),(180,False)])
def test_only_known_river_within_local_width_limit_can_join(width,allowed):
    left=box(0,0,1000,1000);right=box(1000+width,0,2000+width,1000)
    rivers=box(1000,-100,1000+width,1100)
    if allowed:
        groups,excluded,audit=rules.consolidate([{0},{1}],[left,right],[5,5],{0:set(),1:set()},
            balance.BalanceOptions(),area_km2=2,rivers=rivers,max_river_width_m=100)
        assert groups==[{0,1}] and not excluded
        assert len(audit['river_merges'])==1
        assert audit['river_merges'][0]['width_m']==width
        assert union_all([left,right]).area==2e6
    else:
        with pytest.raises(zones.ZoneDataError,match='100米'):
            rules.consolidate([{0},{1}],[left,right],[5,5],{0:set(),1:set()},
                balance.BalanceOptions(),area_km2=2,rivers=rivers,max_river_width_m=100)


def test_nearby_land_or_partial_water_evidence_does_not_allow_crossing():
    left=box(0,0,1000,1000);right=box(1060,0,2060,1000)
    for rivers in (None,box(1000,-100,1030,1100),box(3000,0,3060,1000)):
        assert rules.river_crossing(left,right,rivers,100) is None
    assert rules.river_crossing(left,right,box(1000,-100,1060,1100),0) is None


def test_river_width_is_configurable_and_cannot_override_load_limit():
    left=box(0,0,1000,1000);right=box(1120,0,2120,1000);rivers=box(1000,-100,1120,1100)
    groups,_,audit=rules.consolidate([{0},{1}],[left,right],[5,5],{0:set(),1:set()},
        balance.BalanceOptions(),area_km2=2,rivers=rivers,max_river_width_m=150)
    assert groups==[{0,1}] and audit['max_river_width_m']==150
    with pytest.raises(zones.ZoneDataError,match='负载上限'):
        rules.consolidate([{0},{1}],[left,right],[8000,8000],{0:set(),1:set()},
            balance.BalanceOptions(),area_km2=2,rivers=rivers,max_river_width_m=150)
    with pytest.raises(zones.ZoneDataError,match='河宽上限'):
        rules.consolidate([{0},{1}],[left,right],[5,5],{0:set(),1:set()},
            balance.BalanceOptions(),area_km2=2,rivers=rivers,max_river_width_m=float('nan'))


def test_river_loader_ignores_lakes_forest_and_deduplicates_sources(tmp_path):
    import json
    from scripts.bd_generate_zones import load_river_geometry
    def element(identity,tags,x):
        geo=zones.geographic(box(x,0,x+60,1000))
        return dict(type='way',id=identity,tags=tags,geometry=[{'lon':p[0],'lat':p[1]} for p in geo['coordinates'][0]])
    river=element(1,{'natural':'water','water':'river'},0)
    lake=element(2,{'natural':'water','water':'lake'},2000)
    forest=element(3,{'landuse':'forest'},4000)
    for n in (1,2):
        (tmp_path/f'{n}.roads-v1.source.json').write_text(json.dumps({'elements':[river,lake,forest]}))
    geometry=load_river_geometry(tmp_path)
    assert geometry.area==pytest.approx(60000,abs=1)
    assert geometry.bounds[2]<61


def test_scope_river_merging_preserves_area_memberships_and_merchant_audit(source):
    doc=policy_source(source)
    left=box(0,0,1000,1000);right=box(1060,0,2060,1000)
    doc.update(work_area=zones.geographic(union_all([left,right])),area_km2=2,topology=[[],[]])
    doc['road_cells']=[dict(id=f'bank-{i}',metric_geometry=mapping(g),store_count=1,merchant_ids=[f'shop-{i}'])
                       for i,g in enumerate((left,right))]
    result=coverage.scope_preview(doc,8000,25,balance_options=balance.BalanceOptions(),
        generation_rules=True,include_memberships=True,river_geometry=box(1000,-100,1060,1100))
    assert len(result['features'])==1 and len(result['grids'])==2
    assert result['audit']['generation_rules']['river_merges'][0]['width_m']==60
    assert result['audit']['merchant_pass'] and result['audit']['geometric_pass']
    assert result['features'][0]['properties']['area_km2']==pytest.approx(2)
    assert sum(f['properties']['store_count'] for f in result['grids'])==2


def test_replacement_keeps_team_assignment_and_rolls_back_on_failure(client,source):
    import app.db as appdb
    from app.models import User,BdZone,BdWorkGrid,BdTeam,BdZoneRelease
    from app.services import bd_zone_operations as ops
    doc=policy_source(source)
    result=coverage.scope_preview(doc,8000,25,balance_options=balance.BalanceOptions(),
                                  include_memberships=True,generation_rules=True)
    with appdb.SessionLocal() as db:
        admin=User(username='replace-admin',role='admin',password_hash='unused',status='active',is_active=True)
        db.add(admin);db.flush()
        ops.formal_generate(db,admin,result,doc)
        team=BdTeam(name='carry-team');db.add(team);db.flush()
        zone=db.query(BdZone).one();zone.team_id=team.id;zone.leader_id=admin.id
        db.commit()
        ops.replace_unused_zone_release(db,admin,result,doc);db.commit()
        assert db.query(BdZone).one().team_id==team.id
        assert db.query(BdZoneRelease).count()==1
        grid=db.query(BdWorkGrid).first();grid.progress=1;db.commit()
        with pytest.raises(ops.OperationError,match='进展'):ops.replace_unused_zone_release(db,admin,result,doc)
        db.rollback()
        assert db.query(BdZone).one().team_id==team.id
        grid=db.query(BdWorkGrid).first();grid.progress=0;db.commit()
        broken=copy.deepcopy(result);broken['audit']['geometric_pass']=False
        with pytest.raises(ops.OperationError):ops.replace_unused_zone_release(db,admin,broken,doc)
        db.rollback()
        assert db.query(BdZone).one().team_id==team.id and db.query(BdZoneRelease).count()==1


def test_count_merging_does_not_recreate_long_thin_region():
    geoms=[box(0,0,2000,2000),box(2000,0,8000,2000)]
    with pytest.raises(zones.ZoneDataError):
        rules.consolidate([{0},{1}],geoms,[100,100],coverage.face_graph(geoms),
                          balance.BalanceOptions(),area_km2=8)


def test_replacement_carries_assignment_to_each_split_zone(client,source):
    import app.db as appdb
    from app.models import User,BdZone,BdTeam
    from app.services import bd_zone_operations as ops
    doc=policy_source(source)
    doc['work_area']=zones.geographic(box(0,0,20000,20000))
    result=coverage.scope_preview(policy_source(source),8000,25,balance_options=balance.BalanceOptions(),
                                  include_memberships=True,generation_rules=True)
    # 单独测试拆分承接：原片区的两个商户分别由两个新片区承接。
    split=copy.deepcopy(result)
    first,second=copy.deepcopy(split['features'][0]),copy.deepcopy(split['features'][0])
    first['id']='first';first['properties']['id']='first'
    second['id']='second';second['properties']['id']='second'
    split['features']=[first,second]
    for i,grid in enumerate(split['grids']):grid['properties']['zone_id']='first' if i==0 else 'second'
    split['audit']['generation_rules']['max_zone_count']=rules.count_limit(400,25)
    result['audit']['generation_rules']['max_zone_count']=rules.count_limit(400,25)
    with appdb.SessionLocal() as db:
        admin=User(username='split-admin',role='admin',password_hash='unused',status='active',is_active=True)
        db.add(admin);db.flush()
        ops.formal_generate(db,admin,result,doc)
        team=BdTeam(name='split-team');db.add(team);db.flush()
        zone=db.query(BdZone).one();zone.team_id=team.id;zone.leader_id=admin.id
        db.commit()
        ops.replace_unused_zone_release(db,admin,split,doc);db.flush()
        assert db.query(BdZone).count()==2
        assert all(zone.team_id==team.id for zone in db.query(BdZone).all())


def test_narrow_crossing_cannot_attach_necked_mainland():
    # 两个大片由细走廊相连：最近过河距离虽小，主体仍不能作为可跨河合并方案。
    necked=union_all([box(0,0,1000,1000),box(2000,0,3000,1000),box(1000,450,2000,550)])
    other=box(3020,0,4020,1000)
    assert not rules.river_merge_shape_valid(union_all([necked,other]))
    with pytest.raises(zones.ZoneDataError):
        rules.consolidate([{0},{1}],[necked,other],[100,100],{0:set(),1:set()},
                          balance.BalanceOptions(),area_km2=3.1,
                          rivers=box(3000,-100,3020,1100),max_river_width_m=100)


def test_river_merge_checks_whole_span_not_only_individual_parts():
    parts=[box(0,0,1000,1000),box(8000,0,9000,1000)]
    assert not rules.river_merge_shape_valid(union_all(parts))
    assert rules.river_merge_shape_valid(union_all([parts[0],box(1060,0,2060,1000)]))
