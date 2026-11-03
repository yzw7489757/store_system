"""正式片区管理与H5网格执行；角色/团队/归属/CSRF均在服务端校验。"""
import uuid
import json
from urllib.parse import quote
from typing import Optional
from fastapi import APIRouter,Depends,Request,Form,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse,JSONResponse
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import User,BdZone,BdWorkGrid,BdTeam,BdTeamMember,BdZoneRelease,BdStoreVisitLog
from app.routers.auth_r import require_login,csrf_ok
from app.forms import require_form_token
from app.config import get_settings
from app.services import bd_zone_operations as ops,bd_zones
from app.services import bd_grid_regions
from app.templating import get_templates

router=APIRouter(dependencies=[Depends(require_form_token)])
templates=get_templates()


def work_login(user:Optional[User]=Depends(require_login)):
    if user is None:raise HTTPException(status_code=302,headers={'Location':'/login'})
    return user


def error(exc,status=403):return HTMLResponse(str(exc),status_code=status)


def render(request,user,template,**data):
    return templates.TemplateResponse(template,{'request':request,'current_user':user,
        'maps_key':get_settings().google_public_api_key,**data})


def post_guard(request,user,csrf):
    ops.require_active(user)
    if not csrf_ok(request,csrf):raise ops.OperationError('CSRF校验失败')


@router.get('/zones/manage',response_class=HTMLResponse)
def manage(request:Request,zone:Optional[int]=None,user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    mode=request.query_params.get('mode','assign')
    if mode not in ('assign','manage'):return error('模式不存在',404)
    region=request.query_params.get('region','tokyo23')
    regions=[('tokyo23','东京23区'),('tokyo26','东京26市'),('kanagawa','神奈川'),('saitama','埼玉'),('chiba','千叶')]
    if region not in dict(regions):return error('地区不存在',404)
    try:
        context=ops.management_context(db,user)
        if region!='tokyo23':
            context.update(zones=[],geometry={'type':'FeatureCollection','features':[]},candidate_count=0,residual_count=0)
            zone=None
        # 重建后旧书签中的片区ID已清空，仍应直接展示新网格。
        selected=ops.management_detail(db,user,zone) if mode=='assign' and zone is not None and any(
            row.id==zone for row in context['zones']) else None
    except ops.OperationError as e:return error(e,404 if user.role=='admin' else 403)
    return render(request,user,'bd_zone_manage.html',**context,selected=selected,region=region,regions=regions,mode=mode,
                  grid_rows=bd_grid_regions.management_grids(db,user) if region=='tokyo23' else [],
                  releases=db.query(BdZoneRelease).order_by(BdZoneRelease.id.desc()).all() if region=='tokyo23' else [])


@router.post('/zones/grid-merge-preview')
async def grid_merge_preview(request:Request,user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        payload=await request.json()
        if not isinstance(payload,dict):raise ValueError()
        post_guard(request,user,payload.get('csrf_token',''))
        return JSONResponse(bd_grid_regions.preview(db,user,payload.get('selections'),payload.get('merge_boundaries',True)))
    except (ValueError,ops.OperationError) as exc:return JSONResponse({'error':str(exc) or '合并参数无效'},status_code=409)


@router.post('/zones/merge-grids')
def grid_merge(request:Request,name:str=Form(...),selections:list[str]=Form(...),csrf_token:str=Form(''),
               merge_boundaries:bool=Form(False),
               user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        post_guard(request,user,csrf_token)
        zone=bd_grid_regions.merge(db,user,[json.loads(item) for item in selections],name,merge_boundaries)
        db.commit()
    except (ValueError,ops.OperationError) as exc:
        db.rollback();return error(exc,409)
    return RedirectResponse(f'/zones?zone={zone.id}',status_code=303)


@router.get('/zones/{zone_id}/panel',response_class=HTMLResponse)
def zone_panel(zone_id:int,request:Request,user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        context=ops.management_context(db,user,with_geometry=False)
        detail=ops.management_detail(db,user,zone_id)
    except ops.OperationError as e:return error(e,404 if user.role=='admin' else 403)
    return render(request,user,'bd_zone_panel.html',**context,**detail)


@router.post('/zones/formal-generate')
def generate(request:Request,csrf_token:str=Form(''),user:Optional[User]=Depends(work_login)):
    try:post_guard(request,user,csrf_token);ops.require_admin(user)
    except ops.OperationError as e:return error(e)
    return error('片区和网格划分已迁至脚本生成',410)


@router.post('/zones/{zone_id}/assign')
def zone_assign(zone_id:int,request:Request,team_id:int=Form(...),revision:int=Form(...),csrf_token:str=Form(''),
                user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        post_guard(request,user,csrf_token);ops.assign_zone(db,user,zone_id,team_id,revision);db.commit()
    except ops.OperationError as e:db.rollback();return error(e,409)
    return RedirectResponse(f'/zones?zone={zone_id}',status_code=303)


@router.post('/zones/{zone_id}/recall')
def zone_recall(zone_id:int,request:Request,revision:int=Form(...),csrf_token:str=Form(''),
                user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        post_guard(request,user,csrf_token);ops.recall_zone(db,user,zone_id,revision);db.commit()
    except ops.OperationError as e:db.rollback();return error(e,409)
    return RedirectResponse(f'/zones?zone={zone_id}',status_code=303)


@router.get('/my/work',response_class=HTMLResponse)
def work(request:Request,user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:zones=ops.visible_zones(db,user)
    except ops.OperationError as e:return error(e)
    return render(request,user,'bd_work_home.html',zones=zones)


@router.get('/my/work/zones/{zone_id}',response_class=HTMLResponse)
def work_zone(zone_id:int,request:Request,user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    zone=db.get(BdZone,zone_id)
    if not zone:return error('片区不存在',404)
    try:ops.can_view(db,user,zone)
    except ops.OperationError as e:return error(e)
    leader=user.role=='admin' or zone.leader_id==user.id
    query=db.query(BdWorkGrid).filter_by(zone_id=zone_id)
    if not leader:query=query.filter_by(assignee_id=user.id)
    grids=query.order_by(BdWorkGrid.id).all()
    members=db.query(User).join(BdTeamMember,BdTeamMember.person_code==User.person_code).filter(
        BdTeamMember.team_id==zone.team_id,BdTeamMember.role=='member',BdTeamMember.end_date.is_(None),
        User.is_active.is_(True),User.status=='active',User.id!=zone.leader_id).all() if leader else []
    names={u.id:u.display_name or u.username for u in db.query(User).filter(User.id.in_(
        {g.assignee_id for g in grids if g.assignee_id})).all()}
    geometry={'type':'FeatureCollection','features':[{'type':'Feature','id':g.id,'geometry':g.geometry,
        'properties':{'id':g.id,'assigned':g.assignee_id is not None,'review':g.state=='review'}} for g in grids]}
    return render(request,user,'bd_work_zone.html',zone=zone,grids=grids,members=members,names=names,
                  leader=zone.leader_id==user.id,geometry=geometry)


@router.post('/my/work/grids/{grid_id}/assign')
def grid_assign(grid_id:int,request:Request,target_id:Optional[int]=Form(None),revision:int=Form(...),csrf_token:str=Form(''),
                user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        post_guard(request,user,csrf_token);ops.assign_grid(db,user,grid_id,target_id,revision);db.commit()
        zone_id=db.get(BdWorkGrid,grid_id).zone_id
    except ops.OperationError as e:db.rollback();return error(e,409)
    return RedirectResponse(f'/my/work/zones/{zone_id}',status_code=303)


@router.get('/my/work/grids/{grid_id}',response_class=HTMLResponse)
def grid_page(grid_id:int,request:Request,page:int=1,kw:str='',user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        zone,grid=ops.executable(db,user,grid_id)
        stores,more=ops.stores_page(db,grid_id,page,kw)
        logs=db.query(BdStoreVisitLog).filter_by(grid_id=grid_id).order_by(BdStoreVisitLog.id.desc()).limit(20).all()
        geometry={'type':'FeatureCollection','features':[{'type':'Feature','geometry':grid.geometry,'properties':{'id':grid.id}}]}
        return render(request,user,'bd_work_grid.html',grid=grid,zone=zone,stores=stores,more=more,
            page=max(1,page),kw=kw,logs=logs,geometry=geometry,nonce=str(uuid.uuid4()),
            nonce_for=lambda identity:str(uuid.uuid4()))
    except ops.OperationError as e:return error(e)


@router.post('/my/work/grids/{grid_id}/progress')
def grid_progress(grid_id:int,request:Request,revision:int=Form(...),pct:int=Form(...),task_state:str=Form(...),csrf_token:str=Form(''),
                  user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        post_guard(request,user,csrf_token);ops.progress(db,user,grid_id,revision,pct,task_state);db.commit()
    except ops.OperationError as e:db.rollback();return error(e,409)
    return RedirectResponse(f'/my/work/grids/{grid_id}',status_code=303)


@router.post('/my/work/grids/{grid_id}/visit')
def visit(grid_id:int,request:Request,request_id:str=Form(...),store_id:str=Form(''),name:str=Form(...),address:str=Form(''),
          lat:float=Form(...),lon:float=Form(...),status:str=Form(...),note:str=Form(''),create:bool=Form(False),
          store_revision:Optional[int]=Form(None),csrf_token:str=Form(''),
          user:Optional[User]=Depends(work_login),db:Session=Depends(get_db)):
    try:
        post_guard(request,user,csrf_token)
        ops.visit(db,user,grid_id,store_id,request_id,name,address,lat,lon,status,note,create=create,store_revision=store_revision)
        db.commit()
    except ops.OperationError as e:db.rollback();return error(e,409)
    return RedirectResponse(f'/my/work/grids/{grid_id}',status_code=303)
