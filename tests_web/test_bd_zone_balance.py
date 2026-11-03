"""隔离合成街块验证综合负载，不连接业务数据库。"""
import math
import pytest
from shapely import union_all
from shapely.geometry import box
from app.services import bd_zone_balance as b, bd_zone_coverage as c, bd_zones as z


@pytest.mark.parametrize('area,shops',[(20,8000),(30,6000),(50,3500),(50,4000),(50,4500)])
def test_historic_user_examples_keep_original_calibration_band(area,shops):
    options=b.BalanceOptions(area_weight=.25).validate()
    assert options.load_min<=options.score(area*1e6,shops)<=options.load_max


def test_marginal_cost_increases_with_entire_region_not_fixed_face_score():
    o=b.BalanceOptions()
    assert o.marginal(40e6,4000,10e6,0)>o.marginal(20e6,4000,10e6,0)
    assert o.marginal(20e6,12000,0,2000)>o.marginal(20e6,6000,0,2000)
    assert o.score(100e6,100)<o.score(100e6,20000)
    assert o.score(100e6,100)>o.load_max
    assert o.score(5e6,20000)>o.load_max
    assert not math.isclose(o.score(20e6,4000)+o.score(10e6,2000),o.score(30e6,6000))


@pytest.mark.parametrize('kw',[{'area_weight':0},{'area_weight':1},{'load_min':2,'load_max':1},{'target_area':float('nan')}])
def test_bad_options_rejected(kw):
    with pytest.raises(z.ZoneDataError):b.BalanceOptions(**kw).validate()


def test_only_shared_edge_residuals_merge_and_limit_is_checked():
    options=b.BalanceOptions(2,10,.25,.5,1.4)
    groups=[{0},{1},{2}];areas=[.2e6,1e6,1e6];counts=[1,8,8]
    merged=b.merge_residuals(groups,{0:{1},1:{0},2:set()},areas,counts,options)
    assert {0,1} in merged and {2} in merged
    assert b.merge_residuals([{0},{1}],{0:{1},1:{0}},areas,[9,9],options)==[{0},{1}]


def test_balanced_partition_conserves_ids_counts_and_connectivity():
    gs=[box(x*1000,y*1000,(x+1)*1000,(y+1)*1000) for x in range(8) for y in range(8)]
    graph=c.face_graph(gs);counts=[70 if i//8<4 else 10 for i in range(64)]
    options=b.BalanceOptions(10,600,.25,.5,1.5)
    parts=b.partition(gs,counts,graph,options)
    assert sorted(i for p in parts for i in p)==list(range(64))
    assert all(b._connected(graph,p) for p in parts)
    assert sum(counts[i] for p in parts for i in p)==sum(counts)
    assert parts==b.partition(gs,counts,graph,options)
    assert union_all([union_all([gs[i] for i in p]) for p in parts]).equals(union_all(gs))



from tests_web.test_bd_zone_coverage import source  # noqa:E402,F401


def test_fifty_km_with_four_thousand_stores_is_not_rejected_as_area_limit(source):
    import copy
    from shapely.geometry import mapping
    doc=copy.deepcopy(source);g=box(0,0,5000,10000)
    doc.update(boundary=z.geographic(g),work_area=z.geographic(g),area_km2=50,excluded_area_km2=0,
               store_count=4000,review_merchants=[],topology=[[]])
    doc['road_cells']=[dict(id='isolated-fifty',metric_geometry=mapping(g),store_count=4000,
                            merchant_ids=[f'synthetic-{i}' for i in range(4000)])]
    doc['merchant_audit'].update(inside_boundary=4000,inside_work=4000,excluded_unreviewed=0)
    result=c.scope_preview(doc,8000,25,balance_options=b.BalanceOptions())
    assert result['features'][0]['properties']['load_valid']
    assert result['features'][0]['properties']['unit_kind']=='region_candidate'
    assert not any(i['code'] in ('zone_area','zone_ratio') for i in result['issues'])


def test_residual_label_requires_no_adjacent_region_not_only_small_area(source):
    result=c.scope_preview(source,8000,25,balance_options=b.BalanceOptions())
    assert sum(f['properties']['store_count'] for f in result['features'])==2
    assert result['audit']['geometric_pass'] and result['audit']['merchant_pass']
    for f in result['features']:
        if f['properties']['unit_kind']=='residual_land':
            assert f['properties']['review_reason']=='no_shared_edge_neighbour'



def test_local_connectivity_proof_never_accepts_articulation_removal():
    gs=[box(x,y,x+1,y+1) for x in range(5) for y in range(5)]
    graph=c.face_graph(gs);part=set(graph)
    for i in part:
        assert b._removal_connected(graph,part,i)==b._connected(graph,part,i)
    chain={0:{1},1:{0,2},2:{1}}
    assert not b._removal_connected(chain,{0,1,2},1)
    assert b._removal_connected(chain,{0,1,2},0)


def test_already_acceptable_groups_are_not_rebalanced_towards_equal_counts():
    o=b.BalanceOptions();graph={0:{1},1:{0}}
    groups=[{0},{1}]
    assert b._rebalance(groups,graph,[(0,0),(1,0)],[20e6,50e6],[8000,4000],o)==groups



def test_pair_regroup_preserves_real_edges_and_every_store():
    gs=[box(x*1000,y*1000,(x+1)*1000,(y+1)*1000) for x in range(8) for y in range(4)]
    graph=c.face_graph(gs);areas=[g.area for g in gs];centers=[(g.centroid.x,g.centroid.y) for g in gs]
    counts=[100 if i//4<4 else 10 for i in range(32)]
    options=b.BalanceOptions(20,1800,.25,.5,1.5)
    before=[set(range(16)),set(range(16,32))]
    result=b.repair_pairs(before,graph,centers,areas,counts,options,gs)
    assert sorted(i for p in result for i in p)==list(range(32))
    assert all(b._connected(graph,p) for p in result)
    assert sum(counts[i] for p in result for i in p)==sum(counts)
    assert before==[set(range(16)),set(range(16,32))]


def test_all_shapes_and_load_flags_remain_auditable(source):
    result=c.scope_preview(source,8000,25,balance_options=b.BalanceOptions())
    assert result['summary']['confirmation_ready'] is False
    assert any(issue['code']=='residual_land_review' for issue in result['issues'])
    assert result['summary']['independent_unit_count']>0
    assert result['summary']['max_area_km2_limit'] is None
    assert result['audit']['all_cells_assigned_once'] and result['audit']['all_task_cells_assigned_once']



def test_region_count_uses_combined_load_and_does_not_force_fifty_km_split():
    options=b.BalanceOptions()
    assert options.seed_count(50e6,4000,100)==1
    assert options.seed_count(25e6,24000,100)>options.seed_count(25e6,8000,100)
    assert options.seed_count(100e6,1000,100)>options.seed_count(25e6,1000,100)



def test_pair_repair_does_not_trade_good_shapes_for_better_loads():
    gs=[box(x*1000,y*1000,(x+1)*1000,(y+1)*1000) for x in range(8) for y in range(4)]
    graph=c.face_graph(gs);areas=[g.area for g in gs];centers=[(g.centroid.x,g.centroid.y) for g in gs]
    counts=[100 if i//4<4 else 10 for i in range(32)]
    groups=[set(range(16)),set(range(16,32))]
    assert all(z._acceptable_shape(union_all([gs[i] for i in g])) for g in groups)
    result=b.repair_pairs(groups,graph,centers,areas,counts,b.BalanceOptions(20,1800,.25,.5,1.5),gs)
    assert all(z._acceptable_shape(union_all([gs[i] for i in g])) for g in result)


def test_default_area_weight_is_lower_and_valid_band_is_not_equalization_target():
    options=b.BalanceOptions()
    assert options.area_weight==.15
    assert b._penalty(.8,options)==b._penalty(1.2,options)==0
    assert b._penalty(.6,options)<b._penalty(1.5,options)


def test_long_commercial_component_is_split_even_below_load_ceiling():
    gs=[box(x*1000,y*1000,(x+1)*1000,(y+1)*1000) for x in range(8) for y in range(2)]
    graph=c.face_graph(gs);counts=[200]*len(gs);options=b.BalanceOptions()
    assert options.score(16e6,sum(counts))<options.load_max
    parts=b.partition(gs,counts,graph,options)
    assert len(parts)>=2
    assert sorted(i for group in parts for i in group)==list(range(len(gs)))
    assert all(b._connected(graph,group) for group in parts)
    assert max(z._shape_metrics(union_all([gs[i] for i in group]))['aspect'] for group in parts)<4
