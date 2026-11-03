"""覆盖优先预览：全部使用隔离合成道路面/CSV，绝不调用外网或业务数据库。"""
import copy
import json

import pytest
from shapely.geometry import LineString, Point, box, mapping
from shapely import union_all

from app.services import bd_zones as zones
from app.services import bd_zone_coverage as coverage


def geo(geometry):
    return zones.geographic(geometry)


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv('BD_ZONE_MERCHANT_SOURCE', 'snapshot')
    # 0.3km²商业地块以前会被错误留灰；最终区内仍必须有归属。
    border = box(0, 0, 2000, 1000)
    excluded = box(1400, 400, 1600, 600)
    rows = [('small-shop', '小街区商场', 100, 500),
            ('main-shop', '主街商场', 1000, 600),
            ('review-shop', '园内咖啡', 1500, 500)]
    path = tmp_path / 'synthetic-merchants.csv'
    import csv
    with path.open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['place_id', 'name', 'lat', 'lon'])
        for identity, name, x, y in rows:
            lng, lat = geo(Point(x, y))['coordinates']
            writer.writerow([identity, name, lat, lng])
    return coverage.prepare_source(geo(border), [geo(LineString([(300, -100), (300, 1100)]))],
                                   [geo(excluded)], [], path,
                                   captured_at='isolated-synthetic', source={'merchants': 'synthetic'})


def test_small_commercial_land_has_responsibility_not_grey(source):
    result = coverage.scope_preview(source, 8000, 25)
    audit = result['audit']
    assert audit['geometric_pass']
    assert audit['merchant_pass']
    assert audit['assigned_merchants'] == audit['expected_merchants'] == 2
    assert audit['unassigned_merchants'] == audit['duplicate_merchants'] == 0
    assert result['pending_grids'] == []
    assert sum(f['properties']['store_count'] for f in result['features']) == 2
    assert sum(f['properties']['store_count'] for f in result['grids']) == 2
    assert audit['all_cells_assigned_once'] and audit['all_task_cells_assigned_once']
    work = zones.projected(source['work_area'])
    assigned = union_all([zones.projected(f['geometry']) for f in result['features']], grid_size=.001)
    assert work.symmetric_difference(assigned).area <= coverage.AREA_TOLERANCE_M2
    assert result['summary']['grid_area_conflicts'] > 0
    assert not result['summary']['confirmation_ready']


def test_excluded_businesses_are_explicit_unreviewed_blockers(source):
    result = coverage.scope_preview(source, 8000, 25)
    assert source['merchant_audit']['inside_boundary'] == 3
    assert result['audit']['excluded_unreviewed'] == 1
    assert result['review_merchants'][0]['store_id'] == 'review-shop'
    assert result['review_merchants'][0]['name'] == '园内咖啡'
    assert result['review_merchants'][0]['status'] == 'unreviewed'
    assert any(i['code'] == 'excluded_merchant_review' for i in result['issues'])
    assert not result['summary']['eligible']


def test_missing_geometry_and_unassigned_business_block_confirmation(source):
    broken = copy.deepcopy(source)
    smallest = min(range(len(broken['road_cells'])), key=lambda i: broken['road_cells'][i]['area_km2'])
    broken['road_cells'].pop(smallest)
    broken.pop('topology')
    result = coverage.scope_preview(broken, 8000, 25)
    assert result['audit']['uncovered_m2'] > 100000
    assert result['audit']['unassigned_merchants'] == 1
    assert not result['audit']['geometric_pass']
    assert not result['audit']['merchant_pass']
    assert result['uncovered_features']
    assert not result['summary']['confirmation_ready']


def test_duplicate_faces_cannot_hide_inside_one_merged_region(source):
    broken = copy.deepcopy(source)
    duplicate = copy.deepcopy(broken['road_cells'][0])
    duplicate['id'] += '-duplicate'
    duplicate['merchant_ids'] = []
    duplicate['store_count'] = 0
    broken['road_cells'].append(duplicate)
    broken.pop('topology')
    result = coverage.scope_preview(broken, 8000, 25)
    assert result['audit']['overlap_m2'] > 100000
    assert not result['audit']['geometric_pass']
    assert not result['summary']['confirmation_ready']


def test_duplicate_merchant_identity_cannot_pass_as_unique(source):
    broken = copy.deepcopy(source)
    broken['road_cells'][0]['merchant_ids'].append('main-shop')
    broken['road_cells'][0]['store_count'] += 1
    result = coverage.scope_preview(broken, 8000, 25)
    assert result['audit']['duplicate_merchants'] == 1
    assert not result['audit']['merchant_pass']
    assert not result['summary']['confirmation_ready']


def test_synthetic_valid_plan_can_pass_without_publishing(tmp_path):
    border = box(0, 0, 2000, 1000)
    geoms = [box(0, 0, 1000, 1000), box(1000, 0, 2000, 1000)]
    doc = {'schema_version': 1, 'coverage_revision': 1, 'scope_ward_count': 23,
           'ward_code': 'tokyo23', 'ward_name': '东京23区整体', 'grids': [],
           'snapshot_id': 'synthetic', 'captured_at': 'synthetic', 'source': {},
           'boundary': geo(border), 'work_area': geo(border), 'area_km2': 2,
           'excluded_area_km2': 0, 'store_count': 2, 'road_count': 1,
           'road_cells': [{'id': str(i), 'geometry': geo(g), 'area_km2': 1,
                           'store_count': 1, 'merchant_ids': [f'store-{i}']} for i,g in enumerate(geoms)],
           'topology': [[1],[0]], 'merchant_audit': {'inside_boundary': 2, 'inside_work': 2,
              'excluded_unreviewed': 0, 'unlocated_in_work': 0, 'source_duplicates': 0,
              'invalid_coordinates': 0}, 'review_merchants': []}
    result = coverage.scope_preview(doc, 8000, 25)
    assert result['audit']['geometric_pass'] and result['audit']['merchant_pass']
    assert result['summary']['confirmation_ready']
    assert result['preview_only']
    assert len(result['features']) == 1
    assert len(result['grids']) == 2
    assert result == coverage.scope_preview(doc, 8000, 25)


def test_no_source_audit_never_claims_plan_ready(source):
    doc = copy.deepcopy(source)
    doc.pop('coverage_revision')
    result = coverage.scope_preview(doc, 8000, 25)
    assert not result['audit']['source_audit_ready']
    assert not result['summary']['eligible']


def test_admin_can_download_all_review_records_but_no_approval_side_effect(admin, source, tmp_path):
    (tmp_path / 'tokyo23.json').write_text(json.dumps(source))
    response = admin.get('/zones/review.csv')
    assert response.status_code == 200
    assert response.content.startswith(b'\xef\xbb\xbf')
    assert 'review-shop' in response.text and '园内咖啡' in response.text
    assert 'unreviewed' in response.text
    page = admin.get('/zones')
    assert '片区管理' in page.text and 'zone-management-config' in page.text
    assert '/zones/formal-generate' not in page.text
    assert admin.get('/zones/preview').status_code == 410


# 重用项目现有管理员/内存数据库fixture，不增加测试框架或本地服务。
from tests_web.test_bd_zones import admin, snapshot  # noqa: E402,F401


def test_long_chain_fallback_materializes_only_bounded_candidates():
    n=4000
    universe=set(range(n))
    graph={i:({i-1,i+1}&universe) for i in range(n)}
    centers=[(float(i),0.) for i in range(n)]
    result=coverage._tree_candidates(graph,set(range(n)),[1.]*n,centers,1,1,.5)
    assert 1<=len(result)<=6
    for _,_,left,right in result:
        assert left and right and not left&right
        assert left|right==set(range(n))
        assert len(coverage._components(graph,left))==1
        assert len(coverage._components(graph,right))==1


def test_native_projection_matches_geographic_coverage(source):
    native=copy.deepcopy(source)
    if 'metric_geometry' not in native['road_cells'][0]:
        native['metric_crs']='EPSG:6677'
        for cell in native['road_cells']:
            cell['metric_geometry']=mapping(zones.projected(cell.pop('geometry')))
    geographic=copy.deepcopy(native)
    for cell in geographic['road_cells']:
        cell['geometry']=geo(__import__('shapely').geometry.shape(cell.pop('metric_geometry')))
    a=coverage.scope_preview(native,8000,25)
    b=coverage.scope_preview(geographic,8000,25)
    assert a['audit']['assigned_merchants']==b['audit']['assigned_merchants']
    assert a['audit']['geometric_pass']==b['audit']['geometric_pass']
    assert a['audit']['merchant_pass']==b['audit']['merchant_pass']
    assert [f['id'] for f in a['features']]==[f['id'] for f in b['features']]


def test_native_cells_restore_same_precision_model_as_target_domain(source,monkeypatch):
    from shapely import get_precision
    real=coverage._partition
    def checked(geoms,*args):
        assert all(get_precision(g)==.001 for g in geoms)
        return real(geoms,*args)
    monkeypatch.setattr(coverage,'_partition',checked)
    result=coverage.scope_preview(source,8000,25)
    assert result['audit']['geometric_pass']


@pytest.mark.parametrize('angle', [0, 37, 83])
def test_partition_prefers_compact_children_over_equal_area_strips(angle):
    from shapely.affinity import rotate
    from shapely import set_precision
    geoms = [set_precision(rotate(box(x*250, y*250, (x+1)*250, (y+1)*250), angle,
                    origin=(0, 0)), .001) for x in range(24) for y in range(4)]
    graph = {x*4+y: {xx*4+yy for xx, yy in ((x-1,y),(x+1,y),(x,y-1),(x,y+1))
                       if 0 <= xx < 24 and 0 <= yy < 4}
             for x in range(24) for y in range(4)}
    regions, tasks = coverage._partition(geoms, [1]*len(geoms), graph, 24, 25e6)
    assert len(regions) == 4
    # 道路切口不保证每块都是最优矩形；验证明显低于业务3:1上限，且面积均衡。
    assert all(zones._acceptable_shape(r['geom']) for r in regions)
    assert max(zones._shape_metrics(r['geom'])['aspect'] for r in regions) <= 2.5
    assert max(r['geom'].area for r in regions)/min(r['geom'].area for r in regions) <= 2+1e-5
    for units in (regions, tasks):
        members = [i for r in units for i in r['members']]
        assert sorted(members) == list(range(len(geoms)))
        assert all(len(coverage._components(graph, r['members'])) == 1 for r in units)
        assert all(r['geom'].area >= zones.MIN_GRID-1 for r in units)
    original = union_all(geoms)
    assert original.symmetric_difference(union_all([r['geom'] for r in regions])).area < 1


def test_terminal_elongated_region_is_split_without_dropping_land():
    # 不存在精确面积对半的道路切口，仍应允许合理补切。
    geoms = [box(x*500, y*500, 5980 if x == 11 else (x+1)*500, (y+1)*500)
             for x in range(12) for y in range(2)]
    graph = coverage.face_graph(geoms)
    regions, _ = coverage._partition(geoms, [1]*24, graph, 8000, 25e6)
    assert len(regions) == 2
    assert max(zones._shape_metrics(r['geom'])['aspect'] for r in regions) <= 3
    assert sorted(i for r in regions for i in r['members']) == list(range(24))


def test_indivisible_narrow_road_face_is_not_cut_or_hidden():
    geom = box(0, 0, 8000, 100)
    regions, tasks = coverage._partition([geom], [3], {0:set()}, 8000, 25e6)
    assert len(regions) == len(tasks) == 1
    assert regions[0]['geom'].equals(geom)
    assert regions[0]['count'] == 3
    assert not zones._acceptable_shape(regions[0]['geom'])


def test_full_face_quota_handles_star_graph_without_requiring_balanced_cut():
    geoms = [box(0,0,1000,1000), box(-1000,0,0,1000),
             box(1000,0,2000,1000), box(0,1000,1000,2000)]
    regions, tasks = coverage._partition(geoms, [1]*4, coverage.face_graph(geoms), 8000, 25e6)
    assert len(regions) == 1
    assert len(tasks) == 4
    assert sorted(i for t in tasks for i in t['members']) == list(range(4))


@pytest.mark.parametrize('angle', [0, 37])
@pytest.mark.parametrize('target_stores', [8000, 28])
def test_folded_c_region_must_not_pass_on_bounding_aspect_alone(angle, target_stores):
    from shapely.affinity import rotate
    from shapely import set_precision
    cells = sorted({(0,y) for y in range(6)} | {(x,y) for x in range(1,6) for y in (0,5)})
    index = {xy:i for i,xy in enumerate(cells)}
    geoms = [set_precision(rotate(box(x*1000,y*1000,(x+1)*1000,(y+1)*1000),
                                 angle,origin=(0,0)),.001) for x,y in cells]
    graph = {i:{index[xy] for xy in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)) if xy in index}
             for i,(x,y) in enumerate(cells)}
    original = union_all(geoms)
    assert zones._shape_metrics(original)['aspect'] < 3
    assert zones._shape_metrics(original)['solidity'] < .65
    regions, tasks = coverage._partition(geoms, [7]*len(cells), graph, target_stores, 30e6)
    assert len(regions) > 1
    assert all(zones._acceptable_shape(r['geom']) for r in regions)
    assert max(r['geom'].area for r in regions)/min(r['geom'].area for r in regions) <= 2+1e-5
    for units in (regions,tasks):
        assert sorted(i for r in units for i in r['members']) == list(range(len(cells)))
        assert all(len(coverage._components(graph,r['members'])) == 1 for r in units)
        assert sum(r['count'] for r in units) == 7*len(cells)
    assert original.symmetric_difference(union_all([r['geom'] for r in regions])).area < 1


def test_broad_lobes_with_narrow_bridge_are_separate_regions():
    geoms = [box(0,0,2000,2000), box(2000,950,2600,1050), box(2600,0,4600,2000)]
    graph = {0:{1},1:{0,2},2:{1}}
    original = union_all(geoms)
    assert zones._shape_metrics(original)['solidity'] > .65
    assert zones._shape_metrics(original)['aspect'] < 3
    assert zones._has_narrow_neck(original)
    regions, _ = coverage._partition(geoms,[3,1,5],graph,8000,30e6)
    assert len(regions) == 2
    assert all(zones._acceptable_shape(r['geom']) for r in regions)
    assert sorted(i for r in regions for i in r['members']) == [0,1,2]
    assert sum(r['count'] for r in regions) == 9
    assert original.symmetric_difference(union_all([r['geom'] for r in regions])).area < 1


def test_internal_water_hole_does_not_force_region_fragmentation():
    from shapely.geometry import Polygon
    geoms = [Polygon(box(0,0,2000,2000).exterior,
                     [box(500,500,1500,1500).exterior.coords]), box(2000,0,4000,2000)]
    regions, _ = coverage._partition(geoms,[3,5],{0:{1},1:{0}},8000,30e6)
    assert len(regions) == 1
    assert len(regions[0]['geom'].interiors) == 1
    assert regions[0]['geom'].area == 7e6



def test_shape_rounding_tolerance_does_not_fragment_borderline_rotated_block():
    from shapely.affinity import rotate
    from shapely import set_precision
    g = set_precision(rotate(box(0,0,3000,1000),37,origin=(0,0)),.001)
    assert zones._acceptable_shape(g)
    assert not zones._acceptable_shape(box(0,0,3001,1000))



def test_geodesic_cuts_are_bounded_connected_and_keep_every_road_face():
    cells = sorted({(0,y) for y in range(8)} | {(x,y) for x in range(1,8) for y in (0,7)})
    index = {xy:i for i,xy in enumerate(cells)}
    graph = {i:{index[xy] for xy in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)) if xy in index}
             for i,(x,y) in enumerate(cells)}
    centers = [(x*1000.,y*1000.) for x,y in cells]
    part = set(graph)
    candidates = list(coverage._geodesic_candidates(graph,part,centers))
    assert 1 <= len(candidates) <= 8
    for left,right in candidates:
        assert left and right and not left&right and left|right == part
        assert len(coverage._components(graph,left)) == 1
        assert len(coverage._components(graph,right)) == 1
    assert candidates == list(coverage._geodesic_candidates(graph,part,centers))



def test_multiple_narrow_necks_can_be_removed_progressively():
    geoms = [box(0,0,2000,2000), box(2000,950,2600,1050),
             box(2600,0,4600,2000), box(950,2000,1050,2600), box(0,2600,2000,4600)]
    graph = {0:{1,3},1:{0,2},2:{1},3:{0,4},4:{3}}
    original = union_all(geoms)
    assert zones._has_narrow_neck(original)
    regions,_ = coverage._partition(geoms,[3]*len(geoms),graph,8000,30e6)
    assert len(regions) == 3
    assert all(zones._acceptable_shape(r['geom']) for r in regions)
    assert sorted(i for r in regions for i in r['members']) == list(range(len(geoms)))
    assert original.symmetric_difference(union_all([r['geom'] for r in regions])).area < 1



def test_multicenter_regions_are_compact_connected_and_deterministic():
    geoms = [box(x*1000,y*1000,(x+1)*1000,(y+1)*1000) for x in range(12) for y in range(12)]
    graph = coverage.face_graph(geoms)
    centers = [(g.centroid.x,g.centroid.y) for g in geoms]
    areas = [g.area for g in geoms]
    groups = coverage._compact_groups(graph,set(graph),centers,areas,9)
    assert len(groups) == 9
    assert sorted(i for part in groups for i in part) == list(range(len(geoms)))
    assert all(len(coverage._components(graph,part)) == 1 for part in groups)
    assert all(zones._acceptable_shape(union_all([geoms[i] for i in part])) for part in groups)
    assert groups == coverage._compact_groups(graph,set(graph),centers,areas,9)


def test_multicenter_coincident_centers_do_not_drop_faces_or_seeds():
    groups = coverage._compact_groups({0:{1},1:{0}}, {0,1}, [(0.,0.),(0.,0.)], [1.,1.], 2)
    assert len(groups) == 2
    assert sorted(i for part in groups for i in part) == [0,1]



def test_high_partition_quota_does_not_run_quadratic_seed_initialization(monkeypatch):
    geoms = [box(x*1000,y*1000,(x+1)*1000,(y+1)*1000) for x in range(20) for y in range(20)]
    def forbidden(*args):
        raise AssertionError('high quota must use the bounded recursive path')
    monkeypatch.setattr(coverage,'_compact_groups',forbidden)
    regions,tasks = coverage._partition(geoms,[1]*400,coverage.face_graph(geoms),2,30e6)
    for units in (regions,tasks):
        assert sorted(i for r in units for i in r['members']) == list(range(400))
