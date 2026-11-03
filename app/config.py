# -*- coding: utf-8 -*-
"""Web 应用配置：从环境变量读取（default_factory 保证每次构造读最新 env），
默认适合本地开发。"""
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "sqlite:///./store_settle.db"))
    secret_key: str = field(
        default_factory=lambda: os.environ.get(
            "SECRET_KEY", "dev-secret-change-me"))
    session_ttl_hours: int = field(
        default_factory=lambda: _env_int("SESSION_TTL_HOURS", 12))
    upload_dir: str = field(
        default_factory=lambda: os.environ.get(
            "UPLOAD_DIR", "./data/uploads"))
    max_upload_mb: int = field(
        default_factory=lambda: _env_int("MAX_UPLOAD_MB", 50))
    admin_username: str = field(
        default_factory=lambda: os.environ.get("ADMIN_USERNAME", "admin"))
    admin_password: str = field(
        default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))
    default_staff_password: str = field(
        default_factory=lambda: os.environ.get(
            "DEFAULT_STAFF_PASSWORD", "demo123"))
    ai_api_key: str = field(
        default_factory=lambda: os.environ.get("AI_API_KEY", ""))
    ai_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "AI_BASE_URL", "https://api.openai.com/v1"))
    ai_model: str = field(
        default_factory=lambda: os.environ.get("AI_MODEL", "gpt-4o-mini"))
    # 奖金规则（可配）：每满 bonus_group 点奖 bonus_amount 円（整月滚动、不跨月）
    bonus_group: int = field(default_factory=lambda: _env_int("BONUS_GROUP", 68))
    bonus_amount: int = field(
        default_factory=lambda: _env_int("BONUS_AMOUNT", 3000))
    # 奖额按月调整（形如 "2026-09=1250"；未列出的月份用 bonus_amount）
    bonus_amount_schedule: str = field(
        default_factory=lambda: os.environ.get(
            "BONUS_AMOUNT_SCHEDULE", "2026-09=1250"))
    # 按月生效的门槛调整（形如 "2026-09=75,2027-01=80"；未列出的月份用 bonus_group）
    # 例：2026-09 起门槛由 68 改为 75，8 月及以前保持 68（历史月份封账不受影响）
    # 解析默认口径（无规则说明 sheet 时套用；配置在 .env，非代码硬编码）
    #   形如 DEFAULT_VISIBLE_MAP="AUDIT_SUCCESS=candidate, AUDIT_FAILED=candidate, ~=blank"
    #        DEFAULT_POINT_RULES="OTHER|AUDIT_SUCCESS&YES=2, OTHER|AUDIT_SUCCESS&NO|~=1, ..."
    default_visible_map: str = field(
        default_factory=lambda: os.environ.get(
            "DEFAULT_VISIBLE_MAP",
            "AUDIT_SUCCESS=candidate, AUDIT_FAILED=candidate, "
            "OTHER=candidate, YES=candidate, NO=candidate, "
            "NOT_REQUEST=blank"))
    default_deploy_map: str = field(
        default_factory=lambda: os.environ.get("DEFAULT_DEPLOY_MAP", ""))
    default_point_rules: str = field(
        default_factory=lambda: os.environ.get(
            "DEFAULT_POINT_RULES",
            "*&YES=2, OTHER|AUDIT_SUCCESS|YES|NO&*=1"))
    bonus_group_schedule: str = field(
        default_factory=lambda: os.environ.get(
            "BONUS_GROUP_SCHEDULE", "2026-09=75"))
    # 员工可见起始月（YYYY-MM）：员工端只显示该月及之后的数据；
    # SysConfig 表配置优先（/config 页可改），未配置时用本环境变量默认值
    staff_visible_from: str = field(
        default_factory=lambda: os.environ.get(
            "STAFF_VISIBLE_FROM", "2026-10"))
    # Google Maps 浏览器密钥：需在 Google Console 限制 HTTP referrer。
    google_public_api_key: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_PUBLIC_API_KEY", ""))

    # MCP OAuth（SSO）：对外 issuer（默认取 VISIT_MCP_PUBLIC_HOST 的 https 地址）；
    # 0 关闭（authorize/token 返回 404，/my/token 自助签发不受影响）
    visit_oauth_issuer: str = field(
        default_factory=lambda: os.environ.get("VISIT_OAUTH_ISSUER", ""))
    # 受保护资源标识（MCP 服务地址，须与客户端连接的 URL 同源；空=回退 issuer）
    visit_oauth_resource: str = field(
        default_factory=lambda: os.environ.get("VISIT_OAUTH_RESOURCE", ""))
    visit_oauth_enabled: bool = field(
        default_factory=lambda: os.environ.get("VISIT_OAUTH_ENABLED", "1") != "0")
    visit_oauth_access_hours: int = field(
        default_factory=lambda: _env_int("VISIT_OAUTH_ACCESS_HOURS", 24))
    visit_oauth_refresh_days: int = field(
        default_factory=lambda: _env_int("VISIT_OAUTH_REFRESH_DAYS", 90))

    # 员工每日填报 + 对比分析报告（规格 v7 §6.4）
    report_ai_enabled: int = field(
        default_factory=lambda: _env_int("VISIT_REPORT_AI", 1))
    report_ai_langs: str = field(
        default_factory=lambda: os.environ.get("VISIT_REPORT_LANGS", "zh,ja"))
    report_ai_max_tokens: int = field(
        default_factory=lambda: _env_int("VISIT_REPORT_AI_MAX_TOKENS", 12000))
    report_ai_timeout: int = field(
        default_factory=lambda: _env_int("VISIT_REPORT_AI_TIMEOUT", 300))
    report_ai_top_n: int = field(
        default_factory=lambda: _env_int("VISIT_REPORT_AI_TOP_N", 15))

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
