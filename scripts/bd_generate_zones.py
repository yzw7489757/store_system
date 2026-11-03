#!/usr/bin/env python3
"""从既有源快照生成正式片区/网格；默认只试算，--apply入库，不自动派发。"""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_river_geometry(root):
    """读取已有OSM河道面快照；无来源时保守禁止跨河，不联网补造。"""
    from shapely import union_all
    from scripts.bd_collect_zones import exclusions
    from app.services import bd_zones
    elements = {}
    for path in sorted(root.glob('*.roads-v1.source.json')):
        for element in json.loads(path.read_text()).get('elements', []):
            tags = element.get('tags', {})
            if tags.get('water') == 'river' or tags.get('waterway') == 'riverbank':
                elements[(element['type'], element['id'])] = element
    return union_all([bd_zones.projected(geometry) for geometry in exclusions(elements.values())], grid_size=.001)


def generate(db, user, *, apply=False, target_stores=8000, target_area=25.,
             area_weight=.15, load_min=.75, load_max=1.35, max_river_width_m=100., replace_unused=False, output=None):
    from app.services import bd_zone_operations as ops, bd_zones
    from app.services.bd_zone_balance import BalanceOptions
    from app.services.bd_zone_coverage import scope_preview
    ops.require_admin(user)
    options = BalanceOptions(target_area, target_stores, area_weight, load_min, load_max).validate()
    source = bd_zones.load_scope_source()
    rivers = load_river_geometry(bd_zones.data_dir())
    result = scope_preview(source, target_stores, target_area, balance_options=options, include_memberships=True, generation_rules=True,
                           river_geometry=rivers, max_river_width_m=max_river_width_m)
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    summary = dict(zones=len(result['features']), grids=len(result['grids']),
                   audit=result['audit'], applied=False, rules=result['audit'].get('generation_rules'),
                   quality=result['summary'])
    if apply:
        release = (ops.replace_unused_zone_release if replace_unused else ops.formal_generate)(db, user, result, source)
        db.commit()
        summary.update(applied=True, release_id=release.id)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='保存候选GeoJSON及审计供对比，不改变正式版本')
    parser.add_argument('--admin-user', required=True, help='已有在岗管理员账号，用于鉴权和审计')
    parser.add_argument('--max-river-width-m', type=float, default=100.,
                        help='允许跨河的局部两岸间距上限（米），默认100；0禁用跨河')
    parser.add_argument('--replace-unused', action='store_true',
                        help='显式替换未开展的正式划分，保留可唯一映射的团队归属；须先备份，配合--apply')
    parser.add_argument('--apply', action='store_true', help='写入正式版本；已有版本保护仍生效')
    parser.add_argument('--target-stores', type=int, default=8000)
    parser.add_argument('--target-area', type=float, default=25.)
    parser.add_argument('--area-weight', type=float, default=.15)
    parser.add_argument('--load-min', type=float, default=.75)
    parser.add_argument('--load-max', type=float, default=1.35)
    args = parser.parse_args()
    if args.replace_unused and not args.apply:
        parser.error('--replace-unused必须配合--apply，执行前先备份')
    if not os.environ.get('DATABASE_URL'):
        parser.error('请先加载项目.env并显式设置DATABASE_URL，避免连接错误数据库')
    from app.db import SessionLocal
    from app.models import User
    from app.services.bd_zone_operations import OperationError
    from app.services.bd_zones import ZoneDataError
    with SessionLocal() as db:
        try:
            user = db.query(User).filter_by(username=args.admin_user).one_or_none()
            values = vars(args).copy(); values.pop('admin_user')
            print(json.dumps(generate(db, user, **values), ensure_ascii=False, indent=2))
        except (OperationError, ZoneDataError) as exc:
            db.rollback(); parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
