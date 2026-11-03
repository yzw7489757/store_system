"""作业域地图试算：隔离的合成地理数据；不访问外网、不读生产业务记录。"""
import re
import json
from datetime import datetime, timezone

import pytest
from shapely.geometry import LineString, Point, box, mapping, shape
from shapely import union_all
from shapely.ops import unary_union

import app.db as appdb
from app.auth import hash_password
from app.config import get_settings
from app.models import User
from app.services import bd_zones as zones


def geo(geometry):
    return zones.geographic(geometry)


def synthetic_snapshot(code='13104', *, disconnected=False, no_roads=False):
    # EPSG:6677 中的独立合成矩形，不对应真实门店或真实作业记录。
    border = box(-10000, -35000, -6000, -31000)
    roads = [] if no_roads else [
        geo(LineString([(x, -35000), (x, -31000)]))
        for x in range(-9500, -6000, 500)
    ] + [geo(LineString([(-10000, y), (-6000, y)]))
         for y in range(-34500, -31000, 500)]
    if disconnected:
        border = unary_union([border, box(-5000, -35000, -4800, -34800)])
    points = [tuple(geo(Point(x, y))['coordinates'])
              for x in range(-9750, -6000, 500) for y in range(-34750, -31000, 500)]
    return zones.prepare_snapshot(code, geo(border), roads, [], points,
                                  captured_at='2026-10-31T00:00:00+00:00',
                                  source={'geometry': 'isolated synthetic fixture',
                                          'merchants': 'synthetic', 'osm_timestamp': ''})


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('BD_ZONE_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('BD_ZONE_MERCHANT_SOURCE', 'snapshot')
    doc = synthetic_snapshot()
    (tmp_path / '13104.json').write_text(json.dumps(doc))
    return doc


def test_23_wards_catalogue_and_path_validation(snapshot):
    rows = zones.catalogue()
    assert len(rows) == 23
    assert len({row['code'] for row in rows}) == 23
    assert sum(row['ready'] for row in rows) == 1
    assert next(row for row in rows if row['code'] == '13104')['ready']
    with pytest.raises(zones.ZoneDataError):
        zones.load_snapshot('../13104')
    with pytest.raises(zones.ZoneDataError):
        zones.load_snapshot('13101')


def test_grid_area_coverage_and_store_conservation(snapshot):
    grids = snapshot['grids']
    assert grids and not snapshot['issues']
    assert all(.5 - 1e-6 <= row['area_km2'] <= 2 + 1e-6 for row in grids)
    assert sum(row['store_count'] for row in grids) == snapshot['store_count'] == 64
    union = unary_union([zones.projected(g['geometry']) for g in grids])
    border = zones.projected(snapshot['work_area'])
    assert union.symmetric_difference(border).area < 5
    assert abs(sum(g['area_km2'] for g in grids) - snapshot['area_km2']) < 1e-5
    assert all(shape(g['geometry']).geom_type == 'Polygon' for g in grids)


def test_deterministic_ids_and_indivisible_grid_membership(snapshot):
    a = zones.preview('13104', 16, 25)
    b = zones.preview('13104', 16, 25)
    assert a == b
    ids = {f['id'] for f in a['features']}
    assert len(ids) == len(a['features'])
    assert {g['id'] for g in a['grids']} == {g['id'] for g in snapshot['grids']}
    assert all(g['properties']['zone_id'] in ids for g in a['grids'])
    assert sum(f['properties']['grid_count'] for f in a['features']) == len(a['grids'])
    assert sum(f['properties']['store_count'] for f in a['features']) == snapshot['store_count']
    assert all(shape(f['geometry']).geom_type == 'Polygon' for f in a['features'])
    assert a['summary']['area_ratio'] <= 2 + 1e-5
    assert a['summary']['eligible']
    assert a['preview_only']


def test_store_target_and_area_cap_affect_zone_count(snapshot):
    fewer = zones.preview('13104', 64, 25)
    more = zones.preview('13104', 16, 25)
    capped = zones.preview('13104', 64, 4)
    assert fewer['summary']['zone_count'] < more['summary']['zone_count']
    assert capped['summary']['zone_count'] >= 4
    assert all(f['properties']['area_km2'] <= 4 + 1e-5 for f in capped['features'])


@pytest.mark.parametrize('stores,area', [(0, 25), (100001, 25), (20, 1), (20, float('nan'))])
def test_invalid_parameters(snapshot, stores, area):
    with pytest.raises(zones.ZoneDataError):
        zones.preview('13104', stores, area)


def test_cannot_invent_roads_to_split_large_block():
    doc = synthetic_snapshot(no_roads=True)
    assert len(doc['grids']) == 1
    assert doc['grids'][0]['area_km2'] > 2
    assert any(issue['code'] == 'grid_area' for issue in doc['issues'])


def test_disconnected_small_island_is_flagged_not_joined():
    doc = synthetic_snapshot(disconnected=True)
    assert any(issue['code'] == 'grid_area' for issue in doc['issues'])
    assert all(shape(g['geometry']).geom_type == 'Polygon' for g in doc['grids'])
    assert abs(sum(g['area_km2'] for g in doc['grids']) - doc['area_km2']) < 1e-5


def test_excluded_water_removed_and_not_counted():
    border = box(-10000, -35000, -8000, -33000)
    water = box(-9200, -34200, -8800, -33800)
    shops = [geo(Point(-9000, -34000))['coordinates'], geo(Point(-9700, -34700))['coordinates']]
    doc = zones.prepare_snapshot('13104', geo(border), [], [geo(water)], shops,
                                 captured_at='synthetic', source={})
    assert doc['excluded_area_km2'] == pytest.approx(.16, abs=1e-4)
    assert doc['store_count'] == 1
    work = zones.projected(doc['work_area'])
    assert work.intersection(water).area < 1


def test_missing_stores_not_reported_as_balanced(snapshot, tmp_path):
    for grid in snapshot['grids']:
        grid['store_count'] = 0
    snapshot['store_count'] = 0
    (tmp_path / '13104.json').write_text(json.dumps(snapshot))
    result = zones.preview('13104', 400, 25)
    assert not result['summary']['eligible']
    assert any(issue['code'] == 'no_stores' for issue in result['issues'])


@pytest.fixture
def admin(client, snapshot, monkeypatch):
    monkeypatch.setenv('GOOGLE_PUBLIC_API_KEY', 'synthetic-browser-key')
    get_settings.cache_clear()
    with appdb.SessionLocal() as db:
        db.add(User(username='zone-admin', display_name='test admin', role='admin',
                    password_hash=hash_password('pw123456'), is_active=True,
                    status='active', must_change_password=False))
        db.commit()
    client.post('/login', data={'username': 'zone-admin', 'password': 'pw123456'})
    yield client
    get_settings.cache_clear()


def test_admin_page_and_read_only_api(admin, monkeypatch):
    monkeypatch.setattr(zones, 'preview', lambda *a, **k: pytest.fail('页面不得重新划分'))
    page = admin.get('/zones')
    assert page.status_code == 200
    assert 'synthetic-browser-key' in page.text
    assert 'zone-map' in page.text and 'zone-drawer' in page.text
    assert '片区管理' in page.text and '尚无片区数据' in page.text
    assert 'name="area_weight"' not in page.text
    assert '/zones/formal-generate' not in page.text
    assert admin.get('/zones/preview?target_stores=8000').status_code == 410


def test_page_without_google_key_has_recovery_copy(admin, monkeypatch):
    monkeypatch.delenv('GOOGLE_PUBLIC_API_KEY')
    get_settings.cache_clear()
    page = admin.get('/zones')
    config = json.loads(re.search(r'<script id="zone-management-config"[^>]*>(.*?)</script>', page.text, re.S).group(1))
    assert config['key'] == ''
    assert '片区选择器' in config['texts']['keyMissing']


def test_japanese_copy_and_ops_nav(admin):
    page = admin.get('/zones?lang=ja')
    assert 'エリア管理' in page.text
    assert 'エリアプレビュー' not in page.text


@pytest.mark.parametrize('role', ['staff', 'leader'])
def test_employee_roles_do_not_get_admin_map(client, snapshot, role):
    with appdb.SessionLocal() as db:
        db.add(User(username='other', display_name='test', role=role,
                    password_hash=hash_password('pw123456'), is_active=True, status='active'))
        db.commit()
    client.post('/login', data={'username': 'other', 'password': 'pw123456'}, follow_redirects=False)
    for path in ('/zones', '/zones/preview?ward=13104'):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in (302, 403)
        assert 'FeatureCollection' not in response.text


def test_anonymous_redirects_to_login(client):
    response = client.get('/zones', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['location'] == '/login'


def test_invalid_islands_remain_visible_but_are_not_made_into_zones(snapshot, tmp_path):
    doc = synthetic_snapshot(disconnected=True)
    (tmp_path / '13104.json').write_text(json.dumps(doc))
    result = zones.preview('13104', 16, 25)
    assert result['summary']['pending_grid_count'] > 0
    assert result['summary']['pending_area_km2'] == pytest.approx(sum(g['properties']['area_km2'] for g in result['pending_grids']))
    assert result['pending_grids']
    assert not result['summary']['eligible']
    represented = {g['id'] for g in result['grids'] + result['pending_grids']}
    assert represented == {g['id'] for g in doc['grids']}
    assert all(.5 - 1e-6 <= g['properties']['area_km2'] <= 2 + 1e-6 for g in result['grids'])


def test_official_codes_prevent_same_name_ward_collision():
    from scripts.bd_collect_zones import ward_relations
    elements = [
        {'type': 'relation', 'id': 1, 'tags': {'name': '中央区', 'ref': '131024', 'admin_level': '7'}},
        {'type': 'relation', 'id': 2, 'tags': {'name': '中央区', 'ref': '111058', 'admin_level': '8'}},
        {'type': 'relation', 'id': 3, 'tags': {'name': '中央区', 'admin_level': '8'}},
    ]
    assert ward_relations(elements) == {'13102': elements[0]}


def test_collection_retries_transient_gateway_failure(tmp_path, monkeypatch):
    import httpx
    from scripts import bd_collect_zones as collector
    attempts = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, data):
            attempts.append(data)
            return httpx.Response(502 if len(attempts) == 1 else 200,
                                  json={'elements': [{'type': 'way', 'id': 1}]},
                                  request=httpx.Request('POST', url))
    monkeypatch.setattr(collector.httpx, 'Client', Client)
    monkeypatch.setattr(collector.time, 'sleep', lambda _: None)
    dest = tmp_path / 'source.json'
    result = collector.fetch('synthetic query', dest)
    assert len(attempts) == 2
    assert result['elements'] and dest.exists()
    collector.fetch('synthetic query', dest)
    assert len(attempts) == 2  # 成功来源缓存不重复访问外网


def test_collection_never_caches_partial_overpass_result(tmp_path, monkeypatch):
    import httpx
    from scripts import bd_collect_zones as collector
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, data):
            return httpx.Response(200, json={'elements': [{'id': 1}], 'remark': 'timeout'},
                                  request=httpx.Request('POST', url))
    monkeypatch.setattr(collector.httpx, 'Client', Client)
    dest = tmp_path / 'partial.json'
    with pytest.raises(RuntimeError, match='incomplete'):
        collector.fetch('synthetic query', dest)
    assert not dest.exists()


def test_coastline_excludes_unlabelled_sea_and_sea_stores():
    border = box(-10000, -35000, -8000, -33000)
    # 北向海岸线，左侧西半部为陆地；右侧海面没有natural=water标签。
    coast = geo(LineString([(-9000, -36000), (-9000, -32000)]))
    points = [geo(Point(-9500, -34000))['coordinates'], geo(Point(-8500, -34000))['coordinates']]
    doc = zones.prepare_snapshot('13104', geo(border), [], [], points,
                                 captured_at='synthetic', source={}, coastlines=[coast])
    assert doc['area_km2'] == pytest.approx(2, abs=.001)
    assert doc['excluded_area_km2'] == pytest.approx(2, abs=.001)
    assert doc['store_count'] == 1
    assert zones.projected(doc['work_area']).bounds[2] == pytest.approx(-9000, abs=.2)


def test_inland_ward_is_not_removed_by_distant_coastline():
    border = box(-10000, -35000, -8000, -33000)
    distant = geo(LineString([(-1000, -36000), (-1000, -32000)]))
    assert zones.coastline_land(border, [distant]).equals(border)


def test_area_ratio_constraint_wins_over_store_balance():
    # 面积2/2/5km²，边界店铺密集；不能为了店铺均衡拒绝合法的面积找平。
    geoms = [box(i * 1000, 0, (i + 1) * 1000, 1000) for i in range(9)]
    counts = [0, 0, 1000000, 0, 1000000, 0, 0, 0, 0]
    parts = [{0, 1}, {2, 3}, {4, 5, 6, 7, 8}]
    groups = [{'geom': unary_union([geoms[i] for i in part]), 'members': part,
               'count': sum(counts[i] for i in part)} for part in parts]
    result = zones._rebalance(groups, geoms, counts, 25_000_000)
    areas = [g['geom'].area for g in result]
    assert max(areas) / min(areas) <= 2
    assert sum(g['count'] for g in result) == sum(counts)
    assert set.union(*(g['members'] for g in result)) == set(range(9))


def test_suggested_count_yields_to_hard_area_constraints(tmp_path, monkeypatch):
    monkeypatch.setenv('BD_ZONE_DATA_DIR', str(tmp_path))
    geoms = [box(-10000 + i * 1000, -35000, -9000 + i * 1000, -34000) for i in range(9)]
    doc = {'schema_version': 1, 'ward_code': '13104', 'ward_name': '新宿区',
           'snapshot_id': 'synthetic', 'captured_at': 'synthetic', 'source': {},
           'boundary': geo(unary_union(geoms)), 'work_area': geo(unary_union(geoms)),
           'store_count': 90, 'area_km2': 9, 'excluded_area_km2': 0, 'road_count': 10,
           'issues': [], 'grids': [{'id': str(i), 'geometry': geo(g), 'area_km2': 1,
                                    'store_count': 10} for i, g in enumerate(geoms)]}
    (tmp_path / '13104.json').write_text(json.dumps(doc))
    def grouping(shapes, counts, number, max_area):
        parts = [{0,1},{2,3},{4,5,6,7,8}] if number == 3 else [{0,1},{2,3},{4,5},{6,7,8}]
        return [{'geom': unary_union([shapes[i] for i in part]), 'members': part,
                 'count': sum(counts[i] for i in part)} for part in parts]
    monkeypatch.setattr(zones, '_group', grouping)
    monkeypatch.setattr(zones, '_rebalance', lambda groups, *args: groups)
    result = zones.preview('13104', 30, 25)
    assert result['summary']['zone_count'] == 4
    assert result['summary']['area_ratio'] <= 2
    assert result['summary']['eligible']
    assert result['issues'] == [{'code': 'zone_count', 'requested': 3, 'actual': 4, 'severity': 'info'}]


def test_coastline_nodes_against_slanted_boundary_without_clipping_gaps():
    from shapely.affinity import rotate
    border = rotate(box(-10000, -35000, -8000, -33000), 17)
    coast = geo(LineString([(-9000, -37000), (-9000, -31000)]))
    land = zones.coastline_land(border, [coast])
    assert land.area == pytest.approx(border.area / 2, abs=100)
    assert land.is_valid


def test_csv_store_counts_dedupe_ids_and_ignore_invalid_coordinates(tmp_path):
    from scripts.bd_collect_zones import csv_points
    csv_file = tmp_path / 'synthetic.csv'
    csv_file.write_text('place_id,lat,lon\na,35.5,139.5\na,35.6,139.6\nb,35.7,139.7\nc,0,0\nd,invalid,139.8\n')
    wards = [{'boundary': mapping(box(139, 35, 140, 36))}]
    points = csv_points(csv_file, wards)
    assert points == [[(139.5, 35.5), (139.7, 35.7)]]


def test_disconnected_land_components_set_feasible_zone_count(tmp_path, monkeypatch):
    monkeypatch.setenv('BD_ZONE_DATA_DIR', str(tmp_path))
    geoms = [box(-10000 + i*1000, -35000, -9000+i*1000, -34000) for i in range(4)]
    geoms.append(box(-4000, -35000, -3400, -34000))  # 独立0.6km²陆块，非待处理小碎片。
    doc = {'schema_version': 1, 'ward_code': '13104', 'ward_name': '新宿区',
           'snapshot_id': 'synthetic', 'captured_at': 'synthetic', 'source': {},
           'boundary': geo(unary_union(geoms)), 'work_area': geo(unary_union(geoms)),
           'store_count': 50, 'area_km2': 4.6, 'excluded_area_km2': 0, 'road_count': 10,
           'issues': [], 'grids': [{'id': str(i), 'geometry': geo(g), 'area_km2': g.area/1e6,
                                    'store_count': 10} for i,g in enumerate(geoms)]}
    (tmp_path / '13104.json').write_text(json.dumps(doc))
    result = zones.preview('13104',8000,25)
    assert result['summary']['area_ratio'] <= 2
    assert result['summary']['eligible']
    assert result['summary']['zone_count'] == 5



def test_tokyo23_is_one_scope_and_can_merge_across_ward_borders(snapshot, tmp_path):
    snapshot['ward_code'] = 'tokyo23'
    snapshot['ward_name'] = '东京23区整体'
    snapshot['scope_ward_count'] = 23
    (tmp_path / 'tokyo23.json').write_text(json.dumps(snapshot))
    result = zones.preview('tokyo23', 100000, 25)
    assert result['meta']['ward_code'] == 'tokyo23'
    assert result['summary']['zone_count'] == 1
    assert result['features'][0]['properties']['grid_count'] == len(snapshot['grids'])
    assert result['preview_only']


def test_tokyo23_incomplete_snapshot_is_rejected(snapshot, tmp_path):
    snapshot.update(ward_code='tokyo23', scope_ward_count=22)
    (tmp_path / 'tokyo23.json').write_text(json.dumps(snapshot))
    with pytest.raises(zones.ZoneDataError, match='不完整'):
        zones.preview('tokyo23', 8000, 25)


def test_admin_default_scope_has_no_ward_picker_or_automatic_dispatch(admin, snapshot, tmp_path):
    page = admin.get('/zones')
    assert 'id="zone-ward"' not in page.text
    assert 'id="zone-select"' in page.text
    assert '/zones/preview' not in page.text
    assert admin.get('/zones/preview').status_code == 410


def test_projected_roundtrip_does_not_round_individual_shared_edges():
    from shapely.geometry import Polygon
    left=Polygon([(0,0),(1000.034,0),(1000.047,1000),(0,1000)])
    right=Polygon([(1000.034,0),(2000,0),(2000,1000),(1000.047,1000)])
    converted=[zones.projected(geo(g)) for g in (left,right)]
    assert zones.neighbours(converted)=={0:{1},1:{0}}


def test_map_outlines_only_external_rings():
    from pathlib import Path
    source = Path('app/static/bd_zones.js').read_text()
    start = source.index('  function exteriorBoundary(')
    end = source.index('\n  function renderMap()', start)
    function = source[start:end]
    assert "type: 'MultiLineString'" in function
    assert 'polygons.map((rings) => rings[0])' in function
    assert "strokeWeight: 0" in source
    assert "el('zone-grid-layer').checked && selected" in source


def test_tokyo23_disconnected_commercial_land_keeps_responsibility(snapshot,tmp_path):
    island=box(-3000,-35000,-2400,-34000)
    snapshot.update(ward_code='tokyo23',scope_ward_count=23)
    snapshot['grids'].append({'id':'island','geometry':geo(island),'area_km2':.6,'store_count':1})
    snapshot['store_count']+=1
    complete=union_all([zones.projected(g['geometry']) for g in snapshot['grids']],grid_size=.001)
    snapshot['boundary']=snapshot['work_area']=geo(complete)
    (tmp_path/'tokyo23.json').write_text(json.dumps(snapshot))
    result=zones.preview('tokyo23',100000,25)
    assert result['pending_grids']==[]
    assert len(result['features'])>=2
    assert sum(f['properties']['store_count'] for f in result['features'])==snapshot['store_count']
    assert result['audit']['geometric_pass']
    assert not result['summary']['confirmation_ready']



def test_compact_partition_preserves_roads_counts_and_connected_block_shapes():
    # 隔离8×8道路网格。高密度商户不能把片区拉成细长商圈走廊。
    geoms = [box(x*1000, y*1000, (x+1)*1000, (y+1)*1000)
             for x in range(8) for y in range(8)]
    counts = [1000 if (i//8 == 3 or i%8 == 3) else 10 for i in range(64)]
    result = zones._compact_partition(geoms, counts, 8, 12_000_000)
    assert len(result) == 8
    all_ids = [i for part in result for i in part['members']]
    assert sorted(all_ids) == list(range(64))
    assert sum(part['count'] for part in result) == sum(counts)
    assert all(part['geom'].geom_type == 'Polygon' for part in result)
    quality = [zones._shape_metrics(part['geom']) for part in result]
    assert min(q['solidity'] for q in quality) >= .8
    assert max(q['aspect'] for q in quality) <= 2.5
    sizes = [part['geom'].area for part in result]
    assert max(sizes)/min(sizes) <= 2
    assert max(sizes) <= 12_000_000
    assert unary_union([p['geom'] for p in result]).symmetric_difference(unary_union(geoms)).area < .01
    again = zones._compact_partition(geoms, counts, 8, 12_000_000)
    assert [p['members'] for p in again] == [p['members'] for p in result]


def test_shape_metrics_ignore_natural_holes_but_detect_snake():
    from shapely.geometry import Polygon
    block = box(0, 0, 4000, 4000)
    hole = box(1000,1000,3000,3000)
    with_water = block.difference(hole)
    assert zones._shape_metrics(with_water) == zones._shape_metrics(block)
    snake = unary_union([box(0,0,5000,500), box(4500,0,5000,4000),box(0,3500,5000,4000)])
    assert zones._shape_metrics(snake)['solidity'] < .5
    assert zones._shape_metrics(box(0,0,10000,1000))['aspect'] >= 10


def test_tokyo23_does_not_use_shape_blind_rebalance(snapshot,tmp_path,monkeypatch):
    snapshot.update(ward_code='tokyo23',scope_ward_count=23)
    (tmp_path/'tokyo23.json').write_text(json.dumps(snapshot))
    def forbidden(*args):
        raise AssertionError('shape-blind rebalance must not run for tokyo23')
    monkeypatch.setattr(zones,'_rebalance',forbidden)
    result=zones.preview('tokyo23',16,25)
    assert len(result['features']) > 1
    assert sum(f['properties']['grid_count'] for f in result['features']) == len(snapshot['grids'])


def test_shape_refinement_does_not_accept_one_large_u_shaped_territory():
    cells={(0,y) for y in range(6)} | {(5,y) for y in range(6)} | {(x,0) for x in range(1,5)}
    geoms=[box(x*1000,y*1000,(x+1)*1000,(y+1)*1000) for x,y in sorted(cells)]
    result=zones._compact_partition(geoms,[10]*len(geoms),1,25_000_000)
    assert len(result)>1
    assert all(zones._acceptable_shape(p['geom']) for p in result)
    assert sorted(i for p in result for i in p['members'])==list(range(len(geoms)))
    assert unary_union([p['geom'] for p in result]).symmetric_difference(unary_union(geoms)).area<.01


def test_narrow_neck_is_not_accepted_as_compact_block():
    dogbone=unary_union([box(0,0,1000,1000),box(950,450,2050,550),box(2000,0,3000,1000)])
    assert zones._has_narrow_neck(dogbone)
    assert not zones._acceptable_shape(dogbone)
    assert not zones._has_narrow_neck(box(0,0,2000,2000))



def test_retired_preview_does_not_read_database_source(admin, monkeypatch):
    from app.services import bd_zone_merchants as merchants
    monkeypatch.setattr(merchants, 'bind_database', lambda *a, **k: pytest.fail('已停用接口不得读取源商户'))
    assert admin.get('/zones/preview?target_stores=8000').status_code == 410
