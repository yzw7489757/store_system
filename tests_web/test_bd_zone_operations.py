"""正式作业闭环：隔离SQLite，覆盖事务、权限、交接、召回及H5。"""
import copy
import json
import re
import uuid
from datetime import date
from html.parser import HTMLParser
import pytest
from sqlalchemy import event
from shapely.geometry import box
import app.db as appdb
from app.auth import hash_password
from app.models import (User,Person,BdTeam,BdTeamMember,BdZoneRelease,BdZone,BdWorkGrid,
                        BdGridStore,BdWorkStore,BdStoreVisitLog,BdLog)
from app.services import bd_zone_operations as ops,bd_zone_coverage as coverage,bd_zones as z
from tests_web.test_bd_zone_coverage import source  # noqa:F401
from tests.helpers import form_token


@pytest.fixture
def seed(client):
    with appdb.SessionLocal() as db:
        users=[]
        for name,role in [('ops-admin','admin'),('captain','leader'),('worker','staff'),('next-worker','staff'),('outsider','staff'),('next-captain','leader')]:
            db.add(Person(code=name,display_name=name))
            u=User(username=name,person_code=name,display_name=name,role=role,password_hash=hash_password('pw123456'),status='active',is_active=True)
            db.add(u);users.append(u)
        team=BdTeam(name='synthetic-team');other=BdTeam(name='synthetic-other');db.add_all([team,other]);db.flush()
        for code,role in [('captain','leader'),('worker','member'),('next-worker','member')]:
            db.add(BdTeamMember(team_id=team.id,person_code=code,role=role,start_date=date.today()))
        db.add(BdTeamMember(team_id=other.id,person_code='outsider',role='member',start_date=date.today()))
        release=BdZoneRelease(fingerprint='synthetic',source={},parameters={},audit={},reviews=[],created_by=users[0].id)
        db.add(release);db.flush()
        geo=z.geographic(box(0,0,2000,1000))
        zone=BdZone(release_id=release.id,source_id='zone-synthetic',name='synthetic',geometry=geo,area_km2=2,store_count=1,review=[],state='ready')
        db.add(zone);db.flush()
        grid=BdWorkGrid(release_id=release.id,zone_id=zone.id,source_id='grid-synthetic',geometry=geo,area_km2=2,store_count=1,state='ready')
        db.add(grid);db.flush()
        db.add(BdGridStore(release_id=release.id,grid_id=grid.id,source_id='shop-synthetic'))
        lon,lat=z.geographic(__import__('shapely').geometry.Point(500,500))['coordinates']
        db.add(BdWorkStore(id='shop-synthetic',name='synthetic-shop',address='synthetic',lat=lat,lon=lon,status='normal'))
        db.commit()
        return dict(users={u.username:u.id for u in users},zone=zone.id,grid=grid.id,team=team.id,other=other.id,lat=lat,lon=lon)


def assigned(db,seed):
    user=lambda n:db.get(User,seed['users'][n])
    zone=ops.assign_zone(db,user('ops-admin'),seed['zone'],seed['team'],0)
    ops.assign_grid(db,user('captain'),seed['grid'],user('worker').id,1)
    db.flush()
    return user,zone,db.get(BdWorkGrid,seed['grid'])


def test_end_to_end_progress_visit_recall_preserves_store_history(seed):
    with appdb.SessionLocal() as db:
        user,zone,grid=assigned(db,seed)
        ops.progress(db,user('worker'),grid.id,grid.revision,40,'visiting')
        assert ops.stores_page(db,grid.id)[0][0]['visited'] is False
        token=str(uuid.uuid4())
        args=(db,user('worker'),grid.id,'shop-synthetic',token,'updated-shop','updated-address',seed['lat'],seed['lon'],'abnormal','visited')
        row=ops.visit(*args,store_revision=0)
        assert row.status=='abnormal' and row.revision==1
        assert ops.stores_page(db,grid.id)[0][0]['visited'] is True
        assert ops.visit(*args,store_revision=0).id==row.id
        db.flush();assert db.query(BdStoreVisitLog).count()==1
        ops.recall_zone(db,user('ops-admin'),zone.id,zone.revision)
        assert grid.assignee_id is None and grid.progress==40
        assert db.query(BdStoreVisitLog).count()==1 and db.get(BdWorkStore,row.id).status=='abnormal'
        with pytest.raises(ops.OperationError):ops.progress(db,user('worker'),grid.id,grid.revision,50,'visiting')
        db.commit()


def test_single_assignee_switch_revokes_old_member_and_continues_progress(seed):
    with appdb.SessionLocal() as db:
        user,zone,grid=assigned(db,seed)
        ops.progress(db,user('worker'),grid.id,grid.revision,30,'visiting')
        ops.assign_grid(db,user('captain'),grid.id,user('next-worker').id,grid.revision)
        with pytest.raises(ops.OperationError):ops.progress(db,user('worker'),grid.id,grid.revision,50,'visiting')
        ops.progress(db,user('next-worker'),grid.id,grid.revision,60,'visiting')
        assert grid.progress==60


def test_no_self_dispatch_cross_team_or_member_privilege(seed):
    with appdb.SessionLocal() as db:
        user,zone,grid=assigned(db,seed)
        for who,target in [('captain','captain'),('captain','outsider'),('worker','next-worker'),('ops-admin','worker')]:
            with pytest.raises(ops.OperationError):ops.assign_grid(db,user(who),grid.id,user(target).id,grid.revision)
        with pytest.raises(ops.OperationError):ops.assign_zone(db,user('worker'),zone.id,seed['team'],zone.revision)
        with pytest.raises(ops.OperationError):ops.executable(db,user('captain'),grid.id)


def test_captain_handover_keeps_team_and_grids_but_old_leader_loses_access(seed):
    with appdb.SessionLocal() as db:
        user,zone,grid=assigned(db,seed)
        db.query(BdTeamMember).filter_by(team_id=zone.team_id,role='leader').one().end_date=date.today()
        db.add(BdTeamMember(team_id=zone.team_id,person_code='next-captain',role='leader',start_date=date.today()));db.flush()
        with pytest.raises(ops.OperationError):ops.assign_grid(db,user('captain'),grid.id,None,grid.revision)
        ops.assign_zone(db,user('ops-admin'),zone.id,seed['team'],zone.revision)
        assert zone.leader_id==user('next-captain').id and zone.team_id==seed['team']
        assert grid.assignee_id==user('worker').id
        ops.assign_grid(db,user('next-captain'),grid.id,None,grid.revision)


def test_cross_team_transfer_clears_grid_assignee(seed):
    with appdb.SessionLocal() as db:
        user,zone,grid=assigned(db,seed)
        db.add(BdTeamMember(team_id=seed['other'],person_code='next-captain',role='leader',start_date=date.today()));db.flush()
        ops.assign_zone(db,user('ops-admin'),zone.id,seed['other'],zone.revision)
        assert grid.assignee_id is None and zone.team_id==seed['other']


def test_stale_revision_blocks_but_historic_review_does_not_block_dispatch(seed):
    with appdb.SessionLocal() as db:
        user,zone,grid=assigned(db,seed)
        with pytest.raises(ops.OperationError):ops.assign_grid(db,user('captain'),grid.id,None,grid.revision-1)
        zone.state='review'
        grid.state='review'
        ops.assign_zone(db,user('ops-admin'),zone.id,seed['team'],zone.revision)
        ops.assign_grid(db,user('captain'),grid.id,user('worker').id,grid.revision)
        ops.progress(db,user('worker'),grid.id,grid.revision,20,'visiting')
        assert grid.progress==20


def test_lock_refreshes_previously_loaded_revision(seed):
    from sqlalchemy import update
    with appdb.SessionLocal() as db:
        grid=db.get(BdWorkGrid,seed['grid'])
        db.execute(update(BdWorkGrid).where(BdWorkGrid.id==grid.id).values(revision=7),
                   execution_options={'synchronize_session':False})
        assert grid.revision==0
        assert ops.locked(db,BdWorkGrid,grid.id).revision==7
        with pytest.raises(ops.OperationError):ops.revision_ok(grid,0)


def test_add_store_geofence_and_repeated_visits(seed):
    with appdb.SessionLocal() as db:
        user,zone,grid=assigned(db,seed)
        row=ops.visit(db,user('worker'),grid.id,'',str(uuid.uuid4()),'new-shop','',seed['lat'],seed['lon'],'normal','',create=True)
        assert row.id.startswith('manual:') and grid.store_count==2 and zone.store_count==2
        ops.visit(db,user('worker'),grid.id,row.id,str(uuid.uuid4()),'new-shop','',seed['lat'],seed['lon'],'closed','again',store_revision=1)
        assert row.status=='closed'
        with pytest.raises(ops.OperationError):ops.visit(db,user('worker'),grid.id,'',str(uuid.uuid4()),'bad-shop','',0.,0.,'normal','',create=True)
        db.flush();assert db.query(BdStoreVisitLog).count()==2


def test_formal_generation_atomic_idempotent_and_audits(source,client,monkeypatch):
    with appdb.SessionLocal() as db:
        admin=User(username='formal-admin',role='admin',password_hash='unused',status='active',is_active=True);db.add(admin);db.flush()
        result=coverage.scope_preview(source,8000,25,include_memberships=True)
        # 极薄面的代表点可能舍入到面外；正式生成不得重新猜测空间归属。
        from shapely.geometry.base import BaseGeometry
        def no_point_rematching(*args,**kwargs):
            raise AssertionError('must retain generated cell membership')
        monkeypatch.setattr(BaseGeometry,'representative_point',no_point_rematching)
        r=ops.formal_generate(db,admin,result,source);db.flush()
        assert ops.formal_generate(db,admin,result,source).id==r.id
        assert db.query(BdGridStore).count()==source['store_count']
        assert db.query(BdZone).filter_by(state='review').count()==0
        assert not db.query(BdWorkGrid).filter(BdWorkGrid.assignee_id.isnot(None)).count()
        assert r.reviews==source['review_merchants']
        broken=copy.deepcopy(result);broken['audit']['merchant_pass']=False
        with pytest.raises(ops.OperationError):ops.formal_generate(db,admin,broken,source)
        db.rollback();assert db.query(BdZoneRelease).count()==0


@pytest.mark.parametrize('corruption',['missing','duplicate','unknown'])
def test_formal_generation_rejects_invalid_memberships(source,client,corruption):
    result=coverage.scope_preview(source,8000,25,include_memberships=True)
    cells=result['grids'][0]['properties']['cell_ids']
    if corruption=='missing':cells.pop()
    elif corruption=='duplicate':cells.append(cells[0])
    else:cells.append('unknown-cell')
    with appdb.SessionLocal() as db:
        admin=User(username='formal-invalid',role='admin',password_hash='unused',status='active',is_active=True)
        db.add(admin);db.flush()
        with pytest.raises(ops.OperationError):ops.formal_generate(db,admin,result,source)
        assert db.query(BdZoneRelease).count()==0


def login(client,name):
    r=client.post('/login',data={'username':name,'password':'pw123456'},follow_redirects=False)
    assert r.status_code==302


def csrf(page):return re.search('name="csrf_token" value="([^"]+)"',page).group(1)


def test_h5_pages_isolation_and_server_forms(client,seed):
    with appdb.SessionLocal() as db:
        assigned(db,seed);db.commit()
    login(client,'worker')
    page=client.get(f"/my/work/grids/{seed['grid']}")
    assert page.status_code==200 and 'synthetic-shop' in page.text
    assert 'name="_ft"' in page.text and 'bd_work.css' in page.text
    assert 'name="target_id"' not in page.text
    assert 'id="store-shop-synthetic"' in page.text and '未巡店' in page.text
    config=json.loads(re.search(r'<script id="work-map-config"[^>]*>(.*?)</script>',page.text,re.S).group(1))
    assert config['stores'][0]['id']=='shop-synthetic' and config['stores'][0]['visited'] is False
    r=client.post(f"/my/work/grids/{seed['grid']}/progress",data={'_ft':form_token(client),'csrf_token':csrf(page.text),'revision':2,'pct':100,'task_state':'visited'},follow_redirects=False)
    assert r.status_code==303
    login(client,'outsider')
    assert client.get(f"/my/work/grids/{seed['grid']}").status_code==403
    assert client.get('/zones/manage',follow_redirects=False).status_code in (302,403)


def test_csrf_and_leader_h5_dispatch(client,seed):
    with appdb.SessionLocal() as db:
        zone=ops.assign_zone(db,db.get(User,seed['users']['ops-admin']),seed['zone'],seed['team'],0);db.commit()
    login(client,'captain')
    page=client.get(f"/my/work/zones/{seed['zone']}")
    assert page.status_code==200 and 'name="target_id"' in page.text
    bad=client.post(f"/my/work/grids/{seed['grid']}/assign",data={'_ft':form_token(client),'csrf_token':'bad','revision':1,'target_id':seed['users']['worker']})
    assert bad.status_code==409
    page=client.get(f"/my/work/zones/{seed['zone']}")
    ok=client.post(f"/my/work/grids/{seed['grid']}/assign",data={'_ft':form_token(client),'csrf_token':csrf(page.text),'revision':1,'target_id':seed['users']['worker']},follow_redirects=False)
    assert ok.status_code==303


def test_work_routes_redirect_expired_session_to_login(client):
    for url in ('/zones/manage','/my/work','/my/work/zones/1','/my/work/grids/1'):
        r=client.get(url,follow_redirects=False)
        assert r.status_code==302 and r.headers['location']=='/login'


def test_admin_saved_audits_and_review_details(client,seed):
    with appdb.SessionLocal() as db:
        r=db.query(BdZoneRelease).one()
        r.audit={'geometric_pass':True,'merchant_pass':True,'assigned_merchants':1}
        r.reviews=[{'store_id':'review-synthetic','name':'review shop','lat':35.6,'lng':139.7,'status':'unreviewed'}]
        db.commit()
    login(client,'ops-admin')
    page=client.get(f"/zones/manage?zone={seed['zone']}")
    assert page.status_code==200 and 'review-synthetic' not in page.text and 'geometric_pass' not in page.text
    assert 'synthetic-team</option>' in page.text
    assert 'synthetic-other</option>' not in page.text  # 无有效唯一队长的团队不能供指派。


def test_operations_write_only_bd_tables(seed):
    statements=[]
    def capture(conn,cursor,statement,parameters,context,many):
        if statement.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')):statements.append(statement)
    engine=appdb._ENGINE;event.listen(engine,'before_cursor_execute',capture)
    try:
        with appdb.SessionLocal() as db:
            user,zone,grid=assigned(db,seed)
            ops.progress(db,user('worker'),grid.id,grid.revision,10,'visiting')
            ops.visit(db,user('worker'),grid.id,'shop-synthetic',str(uuid.uuid4()),'shop','',seed['lat'],seed['lon'],'normal','')
            db.commit()
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert statements
    assert all(re.search(r'(?:INSERT INTO|UPDATE|DELETE FROM)\s+bd_',s,re.I) for s in statements)


class WorkFormParser(HTMLParser):
    """读取实际页面表单，沿用页面发出的令牌、版本号和门店请求ID。"""
    def __init__(self,html):
        super().__init__();self.forms=[];self.current=None;self.feed(html)

    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='form':
            self.current={'action':a.get('action',''),'data':{}}
            self.forms.append(self.current)
        elif self.current is not None and tag in ('input','select','textarea') and a.get('name'):
            self.current['data'][a['name']]=a.get('value','')

    def handle_endtag(self,tag):
        if tag=='form':self.current=None


def page_form(client,url,action,**match):
    if url == '/zones/manage' and re.match(r'/zones/\d+/(assign|recall)$', action):
        url += '?zone=' + action.split('/')[2]
    response=client.get(url)
    assert response.status_code==200,response.text
    forms=[f['data'] for f in WorkFormParser(response.text).forms if f['action']==action
           and all(f['data'].get(k)==v for k,v in match.items())]
    assert len(forms)==1,(action,match)
    return forms[0]


def test_rendered_forms_complete_admin_captain_h5_visit_recall(client,seed):
    zone_path=f"/my/work/zones/{seed['zone']}"
    grid_path=f"/my/work/grids/{seed['grid']}"
    login(client,'ops-admin')
    action=f"/zones/{seed['zone']}/assign"
    data=page_form(client,'/zones/manage',action)
    data['team_id']=seed['team']
    assert client.post(action,data=data,follow_redirects=False).status_code==303
    with appdb.SessionLocal() as db:
        zone=db.get(BdZone,seed['zone'])
        assert (zone.team_id,zone.leader_id)==(seed['team'],seed['users']['captain'])

    login(client,'captain')
    assert zone_path in client.get('/my/work').text
    action=grid_path+'/assign'
    data=page_form(client,zone_path,action)
    data['target_id']=seed['users']['worker']
    assert client.post(action,data=data,follow_redirects=False).status_code==303

    login(client,'worker')
    assert grid_path in client.get(zone_path).text
    data=page_form(client,grid_path,grid_path+'/visit',store_id='shop-synthetic')
    data.update(name='shop-after-visit',address='new-address',status='abnormal',note='HTTP visit')
    assert client.post(grid_path+'/visit',data=data,follow_redirects=False).status_code==303
    data=page_form(client,grid_path,grid_path+'/visit',create='true')
    data.update(name='new-H5-store',address='new-store-address',lat=seed['lat'],lon=seed['lon'],status='normal',note='HTTP new store')
    assert client.post(grid_path+'/visit',data=data,follow_redirects=False).status_code==303
    data=page_form(client,grid_path,grid_path+'/progress')
    data.update(pct=40,task_state='visiting')
    assert client.post(grid_path+'/progress',data=data,follow_redirects=False).status_code==303
    page=client.get(grid_path)
    assert 'shop-after-visit' in page.text and 'new-H5-store' in page.text and '已巡店' in page.text

    login(client,'captain')
    forms=WorkFormParser(client.get(zone_path).text).forms
    recall=next(f['data'] for f in forms if f['action']==grid_path+'/assign' and 'target_id' not in f['data'])
    assert client.post(grid_path+'/assign',data=recall,follow_redirects=False).status_code==303
    login(client,'worker')
    assert client.get(grid_path).status_code==403
    assert zone_path not in client.get('/my/work').text

    login(client,'ops-admin')
    action=f"/zones/{seed['zone']}/recall"
    data=page_form(client,'/zones/manage',action)
    assert client.post(action,data=data,follow_redirects=False).status_code==303
    login(client,'captain')
    assert client.get(zone_path).status_code==403
    with appdb.SessionLocal() as db:
        zone=db.get(BdZone,seed['zone']);grid=db.get(BdWorkGrid,seed['grid'])
        assert zone.team_id is None and zone.leader_id is None and grid.assignee_id is None
        assert grid.progress==40 and zone.store_count==grid.store_count==2
        assert db.get(BdWorkStore,'shop-synthetic').status=='abnormal'
        logs=db.query(BdStoreVisitLog).order_by(BdStoreVisitLog.id).all()
        assert [(l.operation,l.actor_id) for l in logs]==[
            ('visit_update',seed['users']['worker']),('create_store',seed['users']['worker'])]
        assert all(l.created_at is not None for l in logs)


def test_script_generation_replaces_web_generation(client,source,monkeypatch):
    from scripts.bd_generate_zones import generate
    with appdb.SessionLocal() as db:
        user=User(username='formal-script-admin',role='admin',password_hash=hash_password('pw123456'),
                  status='active',is_active=True)
        db.add(user);db.commit()
        monkeypatch.setattr(z,'load_scope_source',lambda:copy.deepcopy(source))
        dry=generate(db,user)
        assert dry['applied'] is False and db.query(BdZoneRelease).count()==0
        applied=generate(db,user,apply=True)
        assert applied['applied'] and db.query(BdZoneRelease).count()==1
        assert db.query(BdGridStore).count()==source['store_count']
        assert db.query(BdZone).filter(BdZone.team_id.isnot(None)).count()==0
        assert generate(db,user,apply=True)['release_id']==applied['release_id']
    login(client,'formal-script-admin')
    page=client.get('/zones')
    assert '/zones/formal-generate' not in page.text
    response=client.post('/zones/formal-generate',data={'_ft':form_token(client),'csrf_token':csrf(page.text)},follow_redirects=False)
    assert response.status_code==410


def test_management_detail_counts_current_status_once_and_keeps_visit_separate(client,seed):
    with appdb.SessionLocal() as db:
        user,zone,grid=assigned(db,seed)
        ops.progress(db,user('worker'),grid.id,grid.revision,40,'visiting')
        for _ in range(2):
            ops.visit(db,user('worker'),grid.id,'shop-synthetic',str(uuid.uuid4()),'shop','address',
                      seed['lat'],seed['lon'],'abnormal','test')
        db.add(BdGridStore(release_id=zone.release_id,grid_id=grid.id,source_id='not-yet-verified'))
        db.add(BdWorkStore(id='uncertain-shop',name='uncertain',address='',lat=seed['lat'],lon=seed['lon'],status='uncertain'))
        db.add(BdGridStore(release_id=zone.release_id,grid_id=grid.id,source_id='uncertain-shop'))
        db.commit()
        detail=ops.management_detail(db,user('ops-admin'),zone.id)
        assert detail['merchant_total']==3 and detail['visited']==1
        assert detail['status_counts']==dict(normal=0,abnormal=1,uncertain=1,closed=0,unverified=1)
        assert detail['progress']==40 and detail['visiting']==1 and detail['completed']==0
        with pytest.raises(ops.OperationError):ops.management_detail(db,user('worker'),zone.id)
    login(client,'ops-admin')
    page=client.get(f"/zones/{seed['zone']}/panel")
    assert page.status_code==200 and '33.3%' in page.text and '40.0%' in page.text
    assert 'synthetic-team' in page.text and 'captain' in page.text
    assert '未核实' in page.text
    assert 'id="zone-grid-toggle"' in page.text
    geometry=json.loads(re.search(r'<script id="zone-grid-data"[^>]*>(.*?)</script>',page.text,re.S).group(1))
    assert [f['properties']['id'] for f in geometry['features']]==[seed['grid']]
    assert client.get('/zones/999999/panel').status_code==404
    login(client,'outsider')
    assert client.get(f"/zones/{seed['zone']}/panel",follow_redirects=False).status_code in (302,403)


def test_management_map_reads_formal_geometry_without_source_access(client,seed,monkeypatch):
    monkeypatch.setattr(z,'load_scope_source',lambda:pytest.fail('管理页不得读取或生成划分'))
    login(client,'ops-admin')
    page=client.get('/zones')
    data=json.loads(re.search(r'<script id="zone-management-config"[^>]*>(.*?)</script>',page.text,re.S).group(1))
    assert data['geometry']['features'][0]['properties']['id']==seed['zone']
    assert data['geometry']['features'][0]['properties']['stores']==1
    assert 'zone-select' in page.text and 'bd_zone_manage.js' in page.text
    assert 'name="target_stores"' not in page.text
    assert 'zone-heading' not in page.text
    selected=client.get(f"/zones?zone={seed['zone']}")
    assert selected.status_code==200 and 'zone-grid-data' in selected.text


def test_management_distinguishes_candidates_from_residual_land(client,seed):
    with appdb.SessionLocal() as db:
        zone=db.get(BdZone,seed['zone'])
        db.add(BdZone(release_id=zone.release_id,source_id='isolated-land',name='isolated',
                      geometry=zone.geometry,area_km2=.1,store_count=0,
                      state='review',review=['no_shared_edge_neighbour','grid_area_conflict']))
        db.commit()
        context=ops.management_context(db,db.get(User,seed['users']['ops-admin']))
        assert context['candidate_count']==1 and context['residual_count']==1
        assert len(context['geometry']['features'])==2
    login(client,'ops-admin')
    page=client.get('/zones')
    assert '2 个片区' in page.text and '待复核地块' not in page.text


def test_region_switch_scopes_zones_and_formula_uses_release_parameters(client,seed):
    with appdb.SessionLocal() as db:
        zone=db.get(BdZone,seed['zone']);zone.state='review'
        release=db.get(BdZoneRelease,zone.release_id)
        release.parameters=dict(target_area=20.,target_stores=6000,area_weight=.4,load_min=.75,load_max=1.35)
        db.get(BdWorkGrid,seed['grid']).progress=35
        db.commit()
    login(client,'ops-admin')
    page=client.get(f"/zones?zone={seed['zone']}")
    assert '负载计算' in page.text and '0.40 × (2.000 / 20.0)²' in page.text
    assert '/ 6000)²' in page.text and 'S = 0.004' in page.text
    assert f"/zones/{seed['zone']}/assign" in page.text and '待复核' not in page.text
    config=json.loads(re.search(r'<script id="zone-management-config"[^>]*>(.*?)</script>',page.text,re.S).group(1))
    assert config['geometry']['features'][0]['properties']['progress']==35
    for region in ('tokyo26','kanagawa','saitama','chiba'):
        page=client.get('/zones?region='+region)
        assert page.status_code==200 and '尚无片区数据' in page.text
        config=json.loads(re.search(r'<script id="zone-management-config"[^>]*>(.*?)</script>',page.text,re.S).group(1))
        assert config['geometry']['features']==[]
    assert client.get('/zones?region=invalid').status_code==404
