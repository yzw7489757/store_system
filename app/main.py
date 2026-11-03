# -*- coding: utf-8 -*-
"""FastAPI 应用工厂与入口。"""
import re as _re
from sqlalchemy import text as _text
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


_TASK_DETAIL_RE = _re.compile(r"^/tasks/\d+/?$")


def _staff_path_ok(path: str, method: str, allowed) -> bool:
    """员工/队长能不能访问这个路径。

    特例：`/tasks/<id>` **只有 GET** 放行（任务详情页，路由内还有 can_report 数据隔离）；
    同前缀的写端点一律走管理端守卫。
    """
    if path.startswith("/tasks/"):
        if method != "GET":
            return False
        return bool(_TASK_DETAIL_RE.match(path))
    return path.startswith(allowed)


def create_app() -> FastAPI:
    app = FastAPI(title="巡店结算系统", version="0.2.0")
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    from app.routers import (auth_r, files_r, perf_r,
                        stores_r, accounts_r, info_r, settle_r, tokens_r,
                        oauth_r, report_r, plan_r)
    app.include_router(auth_r.router)
    app.include_router(files_r.router)
    app.include_router(perf_r.router)
    app.include_router(stores_r.router)
    app.include_router(accounts_r.router)
    app.include_router(info_r.router)
    app.include_router(settle_r.router)
    app.include_router(tokens_r.router)
    app.include_router(oauth_r.router)
    app.include_router(report_r.router)
    app.include_router(plan_r.router)
    from app.routers import bd_r
    app.include_router(bd_r.router)
    from app.routers import bd_zones_r
    app.include_router(bd_zones_r.router)
    from app.routers import bd_zone_ops_r
    app.include_router(bd_zone_ops_r.router)
    from app.routers import msg_r
    app.include_router(msg_r.router)

    # 多语言：模板全局函数已在 app/templating.get_templates() 统一注册（t/LANG_NAMES/lang_url）

    from fastapi import Depends
    from fastapi.responses import JSONResponse
    from app.db import get_db
    from sqlalchemy.orm import Session

    STAFF_ALLOWED = ("/my/password", "/static", "/healthz",
                     "/login", "/logout", "/product", "/my/confirm",
                     "/my/appeal", "/my/perf", "/my/report", "/my/plan",
                     # 假期模式（员工自己开/结束休假；派工时只提醒不阻断）
                     "/my/leave",
                     # 站内消息（员工只读收件箱；队长/管理员可发）
                     "/messages",
                     # 车站任务：队员看"分给我的车站"，队长在**同一页**分派 + 提交每日进展
                     # （两处写端点 `/my/tasks/assign`、`/my/tasks/progress` 由路由内做角色校验）
                     "/my/tasks", "/my/work",
                     # 员工每日自报（点数 + 任务进度**一次提交**）——用户 2026-10-06 口径
                     "/my/self-report",
                     # 任务详情页（进展历史 + 变更日志）——**驳回原因/谁改的只有这里能看**。
                     # ⚠️ 白名单是**前缀匹配且不看方法**，所以这里只放行 `GET /tasks/<id>`；
                     # 同前缀下的写端点（/tasks/<id>/progress、/tasks/assign、/tasks/new）
                     # 仍归管理端，由路由守卫拒绝（放行整段会让它们先撞令牌校验 → 400）
                     "/tasks/",
                     # MCP OAuth：授权确认页（浏览器）+ token/register（机器端）都是公开端点
                     "/oauth/", "/.well-known/")

    # 队长端在**员工端**里（规格 §6.2），所以白名单与员工一致；
    # 单独列出是为了"以后队长若多出专属页面"有唯一落点。
    LEADER_ALLOWED = STAFF_ALLOWED

    from fastapi.responses import RedirectResponse as _RR

    from app.forms import reset_ctx as _reset_form_ctx

    def _auth_snapshot(request):
        """当前请求的身份快照（只取列值，**每请求最多查一次**）。

        中间件与依赖共用：语言中间件要 `lang`，员工隔离中间件要
        `role/status/must_change_password/person_code`，route 里再由 `require_login`
        取一次 ORM 对象（那是唯一需要真对象的地方——改密码/改语言要写回）。
        ⚠️ 不要跨 session 传递 ORM 对象（detach 后写入静默丢失）。
        """
        snap = getattr(request.state, "auth_snap", None)
        if snap is not None:
            return snap          # 可能是 None（未登录）或 SimpleNamespace
        from types import SimpleNamespace as _NS
        snap = None
        token = request.cookies.get("ss")
        if token:
            from app.auth import read_session_token as _read
            from app.db import SessionLocal as _SL
            data = _read(token)
            if data:
                s = _SL()
                try:
                    row = s.execute(_text(
                        "SELECT id, role, lang, person_code, status, "
                        "must_change_password FROM users WHERE id = :i"),
                        {"i": data["uid"]}).first()
                    if row is not None:
                        # 极简对象：`landing_home/staff_home` 只读 role/person_code
                        # （dict 不行——它们用属性访问）
                        snap = _NS(uid=row[0], role=row[1] or "", lang=row[2] or "",
                                   person_code=row[3], status=row[4] or "",
                                   must_change_password=bool(row[5]),
                                   can_login=(row[4] or "") not in
                                   ("resigned", "disabled"))
                finally:
                    s.close()
        request.state.auth_snap = snap
        return snap

    @app.middleware("http")
    async def lang_middleware(request, call_next):
        """解析当前语言（URL→cookie→账号级→浏览器→默认）并注入 contextvar。

        模板全局 t() 读取该值渲染界面文案；同时回写 lang cookie 保持选择。
        """
        from app.auth import read_session_token as _read
        from app.db import SessionLocal as _SL
        from app.models import User as _User
        from app.i18n import CURRENT_LANG as _LANG
        from app.i18n import resolve_lang as _resolve
        user_lang = ""
        token = request.cookies.get("ss")
        if token:
            data = _read(token)
            if data:
                user_lang = getattr(_auth_snapshot(request), "lang", "") or ""
        lang = _resolve(query=request.query_params.get("lang", ""),
                        cookie=request.cookies.get("lang", ""),
                        user_lang=user_lang,
                        accept=request.headers.get("accept-language", ""))
        tok = _LANG.set(lang)
        try:
            from app.auth import SESSION_COOKIE as _SC, read_session_token as _rst
            _reset_form_ctx((_rst(request.cookies.get(_SC)) or {}).get("uid"))
            response = await call_next(request)
            # 页面禁止缓存：保存/修改后必须看到最新数据（浏览器复用旧页面会让人以为没刷新）
            if response.headers.get("content-type", "").startswith("text/html"):
                response.headers["Cache-Control"] = "no-store, must-revalidate"
            # 仅显式 ?lang= 切换时才持久化 cookie；否则保留现有 cookie，
            # 避免登录时的默认语言覆盖账号级/浏览器级选择
            if request.query_params.get("lang", "").strip().lower() in ("zh", "ja"):
                response.set_cookie("lang", lang, httponly=False,
                                    samesite="lax", max_age=3600 * 24 * 365)
            return response
        finally:
            _LANG.reset(tok)

    @app.middleware("http")
    async def staff_isolation(request, call_next):
        from app.auth import read_session_token as _read
        from app.db import SessionLocal as _SL
        from app.models import User as _User
        path = request.url.path
        # 模板统一读 request.state.plan_pending（员工端"待填报出勤计划"弹窗），默认置空
        request.state.plan_pending = None
        token = request.cookies.get("ss")
        if token and not path.startswith("/static"):
            data = _read(token)
            snap = _auth_snapshot(request) if data else {}
            if snap:
                s = _SL()
                try:
                    # 这里只读"待填报计划/未读消息"，用户对象由 require_login 按需取
                    if getattr(snap, "role", "") in ("staff", "leader"):
                        # 已登录员工/队长且「待改密」（首登/口令被重置）：
                        # 除改密页与登出外一律拦到改密页（队长同一规则，别漏）
                        if getattr(snap, "must_change_password", False):
                            if not (path.startswith("/my/password")
                                    or path == "/logout"):
                                return _RR("/my/password?must=1", status_code=302)
                        # 待填报出勤计划 → 员工端提示；越权访问非白名单页 → 回各角色首页
                        from app.services import date_plan as _dp
                        request.state.plan_pending = _dp.needs_plan(
                            s, getattr(snap, "person_code", None)) or None
                        # 未读消息数（底栏红点；一次 COUNT，模板直接读）
                        try:
                            from app.services import bd_msg as _bm
                            request.state.unread_messages = _bm.unread_count(
                                s, getattr(snap, "person_code", None))
                        except Exception:        # 表还没建也不该 500
                            request.state.unread_messages = 0
                        allowed = (LEADER_ALLOWED
                                   if getattr(snap, "role", "") == "leader"
                                   else STAFF_ALLOWED)
                        if not _staff_path_ok(path, request.method, allowed):
                            from app.services import home as _home
                            return _RR(_home.landing_home(s, snap), status_code=302)
                finally:
                    s.close()
        return await call_next(request)

    # ⚠️ 一次性表单令牌失效（返回键 / 双开 / 超时）→ 回跳原页并给一句**人话**，
    #    不要把整页变成一行 JSON（`{"detail":"表单已提交过或已过期…"}`）
    #    —— 2026-10-06 审计：员工弱网/双开时看到的就是这串 JSON，像"页面坏了"。
    from starlette.exceptions import HTTPException as _StarletteHTTP
    from fastapi.exception_handlers import (
        http_exception_handler as _default_http_handler)

    @app.exception_handler(_StarletteHTTP)
    async def _friendly_http_error(request, exc):
        detail = str(getattr(exc, "detail", "") or "")
        if exc.status_code == 400 and "表单" in detail:
            from urllib.parse import quote as _quote
            from app.i18n import render_msg as _rm
            from fastapi.responses import HTMLResponse as _HR
            from fastapi.responses import RedirectResponse as _RD
            msg = _rm("表单已过期，请重试")
            if request.headers.get("hx-request"):
                return _HR('<div class="msg err">%s</div>' % msg, status_code=200)
            ref = request.headers.get("referer", "")
            if ref.startswith("/") and not ref.startswith("//"):
                sep = "&" if "?" in ref else "?"
                return _RD("%s%serr=%s" % (ref, sep, _quote(msg)), status_code=303)
            return _HR("<p>%s</p><p><a href='/'>←</a></p>" % msg, status_code=400)
        return await _default_http_handler(request, exc)

    @app.get("/healthz")
    def healthz(db: Session = Depends(get_db)):
        try:
            db.execute(_text("SELECT 1"))
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    return app


app = create_app()
