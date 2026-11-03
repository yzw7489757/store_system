"""作业域管理员片区管理入口（只展示脚本生成的正式划分）。"""
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import User
from app.routers.auth_r import require_login
from app.services import bd_zones
from app.templating import get_templates

router = APIRouter()
templates = get_templates()


def _guard(user):
    if user is None:
        return RedirectResponse('/login', status_code=302)
    if user.role != 'admin':
        return JSONResponse({'detail': '仅管理员可查看片区管理'}, status_code=403)
    return None


@router.get('/zones', response_class=HTMLResponse)
def page(request: Request, zone: Optional[int] = None, user: Optional[User] = Depends(require_login),
         db: Session = Depends(get_db)):
    denied = _guard(user)
    if denied is not None:
        return denied
    from app.routers.bd_zone_ops_r import manage
    return manage(request, zone, user, db)


@router.get('/zones/preview')
def generate(user: Optional[User] = Depends(require_login)):
    denied = _guard(user)
    if denied is not None:
        return denied
    return JSONResponse({'detail': '片区和网格划分已迁至脚本生成'}, status_code=410)


@router.get('/zones/review.csv')
def merchant_review(user: Optional[User] = Depends(require_login)):
    """只读下载全部待复核商户；不提交或自动批准任何排除。"""
    import csv
    import io
    from fastapi.responses import Response
    denied = _guard(user)
    if denied is not None:
        return denied
    try:
        doc = bd_zones.load_scope_source()
    except bd_zones.ZoneDataError as exc:
        return JSONResponse({'detail': str(exc)}, status_code=422)
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['store_id','name','lat','lng','reason','status'])
    for row in doc.get('review_merchants', []):
        values = [row.get(key,'') for key in ('store_id','name','lat','lng','reason','status')]
        # CSV是数据，不允许店名被电子表格解释为公式。
        writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in values])
    return Response(('\ufeff'+out.getvalue()).encode('utf-8'), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename=tokyo23-merchant-review.csv', 'Cache-Control':'no-store'})
