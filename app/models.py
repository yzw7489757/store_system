# -*- coding: utf-8 -*-
"""ORM 模型（spec v0.4 §4.1–4.8）。JSON 一律 sqlalchemy.JSON（SQLite/PG 兼容）。

阶段二对账仅空表骨架（recon_* / self_report_*），逻辑见对账设计文档。
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float, ForeignKey,
                        Integer, String, Text, UniqueConstraint, Index, text)
from sqlalchemy.orm import relationship

from app.db import Base


def _now():
    return datetime.utcnow()


# ---------- §4.1 ----------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(64), nullable=False, default="")
    role = Column(String(16), nullable=False, default="admin")  # admin / staff
    person_code = Column(String(32), nullable=True)  # staff 绑定人员编号
    is_active = Column(Boolean, nullable=False, default=True)
    # 员工状态：active 在岗 / leave 请假 / disabled 停用 / resigned 离职
    # 停用、离职 → is_active=False（禁登录）；数据与历史记录永不删除
    status = Column(String(16), nullable=False, default="active")
    # 首次登录/口令被管理员重置后必须改密（True=未改，除改密/退出外一律拦截）
    must_change_password = Column(Boolean, nullable=False, default=False)
    # 界面语言：zh / ja / 空=自动（URL→cookie→浏览器语言）；员工可账号级指定
    lang = Column(String(8), nullable=False, default="", server_default="")
    created_at = Column(DateTime, nullable=False, default=_now)

    @property
    def can_login(self) -> bool:
        """停用/离职不可登录；在岗/请假可登录。"""
        return self.status in ("active", "leave")


# ---------- §4.2 ----------
class ImportFile(Base):
    __tablename__ = "imports"
    id = Column(Integer, primary_key=True)
    file_name = Column(String(255), nullable=False)
    file_sha256 = Column(String(64), unique=True, nullable=False)
    file_size = Column(Integer, nullable=False, default=0)
    stored_path = Column(String(512), nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=_now)
    month_label = Column(String(16), nullable=True)
    format = Column(String(16), nullable=True)
    header_row = Column(Integer, nullable=True)
    data_start_row = Column(Integer, nullable=True)
    # 解析布局（AI 识别 + 人工可纠正）：
    # {"header_row": 2, "cols": {...}, "value_map": {"visible": {...}, "deploy": {...}},
    #  "source": "ai"|"manual"|"rule"}
    layout = Column(JSON, nullable=True)
    parsed_sheets = Column(JSON, nullable=False, default=list)
    ignored_sheets = Column(JSON, nullable=False, default=list)
    total_rows = Column(Integer, nullable=False, default=0)
    parsed_rows = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="uploaded")  # uploaded/parsed/failed
    warnings = Column(JSON, nullable=False, default=list)
    errors = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=_now)


# ---------- §4.3 ----------
class RawRecord(Base):
    __tablename__ = "raw_records"
    __table_args__ = (
        UniqueConstraint("import_id", "sheet_name", "excel_row",
                         name="uq_raw_import_sheet_row"),
    )
    id = Column(Integer, primary_key=True)
    import_id = Column(Integer, ForeignKey("imports.id"), nullable=False)
    sheet_name = Column(String(128), nullable=False)
    excel_row = Column(Integer, nullable=False)
    store_id_raw = Column(Text, nullable=False, default="")
    store_name_local_raw = Column(Text, nullable=False, default="")
    store_name_en_raw = Column(Text, nullable=False, default="")
    modified_raw = Column(Text, nullable=False, default="")
    submitter_raw = Column(Text, nullable=False, default="")
    submitter_code = Column(String(32), ForeignKey("persons.code"), nullable=True)
    record_id_raw = Column(Text, nullable=False, default="")
    # ⚠️ 不要改回 String(8)：Visible 列真实值有 "AUDIT_SUCCESS"（13 字符，线上 7.4 万行）
    # 与 AUDIT_FAILED / NOT_REQUEST。线上 PG 这两列**本来就是 text**（历史库不是按迁移建的）
    # 所以一直没暴露；但按迁移新建的库会得到 varchar(8) → 一导入就整批失败/截断
    # （2026-10-17 修，迁移 e4f5a6b7c8d9）。deploy 同列宽风险，一并 Text。
    visible_raw = Column(Text, nullable=True)           # YES/NO/空/AUDIT_SUCCESS/…
    deploy_raw = Column(Text, nullable=True)            # YES/NO/空
    original_row = Column(JSON, nullable=False, default=list)
    # 追溯主锚（由全局去重回填）：判定状态 + 被哪条有效记录挤掉
    clean_status = Column(String(16), nullable=True)
    # final/dup_by_id/dup_by_name/visible_blank/manual_void
    filtered_by_raw_id = Column(Integer, nullable=True)
    # V3 过滤原因枚举：master_late/from_sub/cross_file_dup/no_ref（空=有效/空白）
    filter_reason = Column(String(20), nullable=True)
    # 员工确认：pending/approved/disputed/auto_ok（跨文件重复自动认可，不进任务）
    confirm_state = Column(String(24), nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False, default=_now)


# ---------- §4.4 ----------
class Person(Base):
    __tablename__ = "persons"
    code = Column(String(32), primary_key=True)
    display_name = Column(String(64), nullable=False, default="")
    first_seen_import_id = Column(Integer, ForeignKey("imports.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


# ---------- §4.5 ----------
class DashMetric(Base):
    """看板统计物化表：月份-统计项-数值（person 可空=全公司），生成后读表零聚合。"""
    __tablename__ = "dash_metrics"
    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False, index=True)
    metric = Column(String(64), nullable=False)
    value = Column(Float, nullable=False, default=0)
    payload = Column(Text, nullable=True, default=None)   # 列表型指标(JSON字符串)
    person = Column(String(32), nullable=True, default=None)
    updated_at = Column(DateTime, nullable=False, default=_now)
    __table_args__ = (UniqueConstraint("month", "metric", "person",
                                       name="uq_dash_metric"),)


class StaffAnalysis(Base):
    """员工月度分析（算完工资后自动生成，存表供看板读取）。"""
    __tablename__ = "staff_analyses"
    id = Column(Integer, primary_key=True)
    person_code = Column(String(32), nullable=False, index=True)
    month = Column(String(7), nullable=False, index=True)
    content = Column(Text, nullable=False, default="")
    fingerprint = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now,
                        onupdate=_now)
    __table_args__ = (UniqueConstraint("person_code", "month",
                                       name="uq_staff_analysis_month"),)


class SysConfig(Base):
    """系统配置（每点金额/达标点数/达标奖金）：全局单值，最新一条生效（id 最大）。"""
    __tablename__ = "sys_configs"
    id = Column(Integer, primary_key=True)
    config_month = Column(String(7), nullable=False, default="",
                            server_default="")  # 空=无月份语义（仅记录，不参与匹配）
    per_point = Column(Integer, nullable=False, default=250)
    bonus_group = Column(Integer, nullable=False, default=68)
    bonus_amount = Column(Integer, nullable=False, default=3000)
    # 员工可见起始月（YYYY-MM）：员工端只显示该月及之后的数据；空=不限制
    staff_visible_from = Column(String(7), nullable=False, default="",
                                server_default="")
    updated_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now)


class ReconTask(Base):
    __tablename__ = "recon_tasks"
    id = Column(Integer, primary_key=True)
    kind = Column(String(16), nullable=False)   # person_points / daily_records
    source_import_id = Column(Integer, ForeignKey("imports.id"), nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)
    finished_at = Column(DateTime, nullable=True)
    params = Column(JSON, nullable=False, default=dict)
    summary = Column(JSON, nullable=False, default=dict)


class ReconResult(Base):
    __tablename__ = "recon_results"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("recon_tasks.id"), nullable=False)
    japan_date = Column(Date, nullable=True)
    store_id = Column(Text, nullable=True)
    submitter_code = Column(String(32), nullable=True)
    system_value = Column(Integer, nullable=True)
    report_value = Column(Integer, nullable=True)
    diff = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False)
    family = Column(String(16), nullable=True)   # B-1/B-2/B-3
    confirmed = Column(Boolean, nullable=False, default=False)
    note = Column(Text, nullable=True)


class AiRun(Base):
    """B组 AI 批处理运行记录（供页面展示进度/结果）。"""
    __tablename__ = "ai_runs"
    id = Column(Integer, primary_key=True)
    total_pairs = Column(Integer, nullable=False, default=0)
    status = Column(String(12), nullable=False, default="running")  # running/done
    summary = Column(JSON, nullable=False, default=dict)
    started_at = Column(DateTime, nullable=False, default=_now)
    finished_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class StoreEntity(Base):
    """店铺实体档案：一个 store_id_raw 一行；master_id 自指=主档，否则指主档。

    V3：主档 = 一家真实店铺的代表实体（master_id 自指）；
    从档 = 同店不同编号，master_id 指向主档实体。
    raw_data_id = 该 store_id 在原始数据中最早出现的那条 raw；
    从档冗余 master_store_id / master_raw_data_id（少一次查询）。
    """
    __tablename__ = "store_entities"
    id = Column(Integer, primary_key=True)
    store_id_raw = Column(String(64), unique=True, nullable=False)
    name_local = Column(Text, nullable=False, default="")       # 最早出现写法
    name_norm = Column(Text, nullable=False, default="")        # 归一化（NFKC去空白小写）
    city = Column(String(64), nullable=True)
    address_local = Column(Text, nullable=True)   # 宽表 Store Address-Local（可空）
    first_seen_import_id = Column(Integer, ForeignKey("imports.id"), nullable=True)
    master_id = Column(Integer, ForeignKey("store_entities.id"), nullable=False)
    # V3：归属主档的 store_id（主档时等于自己）；从档冗余
    master_store_id = Column(String(64), nullable=True)
    # 该店最早出现的原始记录 id（主档/从档都维护各自 raw_data_id）
    raw_data_id = Column(Integer, ForeignKey("raw_records.id"), nullable=True)
    # 从档冗余：主档实体最早原始记录 id
    master_raw_data_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)
    note = Column(Text, nullable=True)


class StorePair(Base):
    """判重候选对：exact=归一化全等（程序必同）；fuzzy=高相似（人工/AI）。"""
    __tablename__ = "store_pairs"
    __table_args__ = (UniqueConstraint("entity_a", "entity_b",
                                       name="uq_storepair_ab"),)
    id = Column(Integer, primary_key=True)
    entity_a = Column(Integer, ForeignKey("store_entities.id"), nullable=False)
    entity_b = Column(Integer, ForeignKey("store_entities.id"), nullable=False)
    kind = Column(String(8), nullable=False)                    # exact / fuzzy
    sim = Column(Integer, nullable=True)                        # 相似度 x100
    status = Column(String(10), nullable=False, default="pending")  # pending/merged/skip
    note = Column(Text, nullable=True)
    ai_decision = Column(String(8), nullable=True)             # same/diff/unknown
    ai_confidence = Column(String(8), nullable=True)           # high/medium/low
    ai_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class StoreMergeLog(Base):
    """主档合并留痕：basis ∈ program_exact / manual / ai（预留）。"""
    __tablename__ = "store_merge_logs"
    id = Column(Integer, primary_key=True)
    entity_id = Column(Integer, ForeignKey("store_entities.id"), nullable=False)
    from_master = Column(Integer, nullable=False)
    to_master = Column(Integer, nullable=False)
    basis = Column(String(16), nullable=False)
    note = Column(Text, nullable=True)
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class FormalRecord(Base):
    """V3 正式数据：文件判定后入此表（算绩效的唯一依据）。"""
    __tablename__ = "formal_records"
    # min/max(japan_date) 出现在每个报告/对比/导出请求里 → 必须有索引，
    # 否则 3 万行的正式表每次全表扫描（评审实测 SCAN formal_records）。
    __table_args__ = (Index("ix_formal_japan_date", "japan_date"),)
    id = Column(Integer, primary_key=True)
    import_id = Column(Integer, ForeignKey("imports.id"), nullable=False)
    raw_record_id = Column(Integer, ForeignKey("raw_records.id"),
                           nullable=False, unique=True)
    person_code = Column(String(32), ForeignKey("persons.code"), nullable=True)
    store_id_raw = Column(Text, nullable=False, default="")
    japan_date = Column(Date, nullable=True)
    points = Column(Integer, nullable=False, default=0)   # 1 或 2
    created_at = Column(DateTime, nullable=False, default=_now)


class AppealRecord(Base):
    """V3 绩效申诉：员工对被滤(master_late 等)记录逐条申诉，管理员处理。

    - 仅 master_late（同店更早、跨日疑似重复）可申诉——员工主张这条应算有效。
    - status: pending(待处理) / approved(申诉成立→改判有效) /
      rejected(申诉驳回→维持被滤)。
    - approve 后由 finalize 重算并入正式表；reject 后员工不可再申诉该条。
    """
    __tablename__ = "appeals"
    __table_args__ = (UniqueConstraint("raw_record_id",
                                       name="uq_appeal_raw"),)
    id = Column(Integer, primary_key=True)
    raw_record_id = Column(Integer, ForeignKey("raw_records.id"),
                           nullable=False, unique=True)
    import_id = Column(Integer, ForeignKey("imports.id"), nullable=False)
    submitter_code = Column(String(32), ForeignKey("persons.code"),
                            nullable=False)
    reason = Column(Text, nullable=False, default="")   # 员工申诉理由
    status = Column(String(12), nullable=False, default="pending")
    # pending / approved / rejected
    created_at = Column(DateTime, nullable=False, default=_now)
    handled_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    handled_at = Column(DateTime, nullable=True)


class AdjustRecord(Base):
    """V3 找平：对账确认的差异 → 下月工资加/减（留痕）。

    一个对账任务 × 一名员工最多一条确认（确认可取消=删除记录）。
    amount 为円：正=下月补发，负=下月扣回；applied_to_month = month 的下一月。
    """
    __tablename__ = "adjust_records"
    __table_args__ = (UniqueConstraint("source_task_id", "person_code",
                                       name="uq_adjust_task_person"),)
    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False)            # 差异所属月（被对账月）
    applied_to_month = Column(String(7), nullable=False)  # 生效月 = 下一月
    person_code = Column(String(32), nullable=False)
    amount = Column(Integer, nullable=False, default=0)   # 点数（正补/负扣，参考）
    per_point = Column(Integer, nullable=False, default=250)  # 发生时单价(円/点)，纠偏按此价
    amount_adj = Column(Integer, nullable=False, default=0)  # 金额增量(円，含奖金)：确认时写入薪资找平的金额
    reason = Column(Text, nullable=False, default="")
    source_task_id = Column(Integer, nullable=False)      # 来源对账任务
    source_points_diff = Column(Integer, nullable=False, default=0)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class ReconDayRow(Base):
    """V3 日级对账：按 (员工 × 日) 的比对明细（只存"问题行"）。

    问题行 = 本地点数与对账点数不一致，或只有一侧有记录。
    diff = 本地点数 - 对账点数；side: both / local_only / report_only。
    """
    __tablename__ = "recon_day_rows"
    __table_args__ = (UniqueConstraint("task_id", "ref_date", "person_code",
                                       name="uq_recon_day_task_date_person"),
                      Index("ix_recon_day_task", "task_id"))
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, nullable=False)
    ref_date = Column(Date, nullable=False)              # 巡店日期（日本日期）
    person_code = Column(String(32), nullable=False)
    sys_points = Column(Integer, nullable=False, default=0)   # 本地（系统）
    rep_points = Column(Integer, nullable=False, default=0)   # 对账文件
    sys_count = Column(Integer, nullable=False, default=0)    # 点数来源行数
    rep_count = Column(Integer, nullable=False, default=0)
    diff = Column(Integer, nullable=False, default=0)
    side = Column(String(12), nullable=False, default="both")
    note = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=_now)


class PersonDailyStat(Base):
    """本地绩效统计表（人 × 日）：由正式表刷写，供月度对账任务直接查询。"""
    __tablename__ = "person_daily_stats"
    __table_args__ = (UniqueConstraint("person_code", "ref_date",
                                       name="uq_pdstat_person_date"),
                      Index("ix_pdstat_date", "ref_date"))
    id = Column(Integer, primary_key=True)
    person_code = Column(String(32), nullable=False)
    ref_date = Column(Date, nullable=False)
    records = Column(Integer, nullable=False, default=0)   # 当日有效店数
    p1 = Column(Integer, nullable=False, default=0)
    p2 = Column(Integer, nullable=False, default=0)
    points = Column(Integer, nullable=False, default=0)    # p1 + p2*2


class ReconDataRow(Base):
    """对账数据表：外部对账文件解析后按 (任务 × 日期 × 员工) 全量落库。"""
    __tablename__ = "recon_data_rows"
    __table_args__ = (UniqueConstraint("task_id", "ref_date", "person_code",
                                       name="uq_recon_data_task_date_person"),
                      Index("ix_recon_data_task", "task_id"))
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, nullable=False)
    ref_date = Column(Date, nullable=False)
    person_code = Column(String(32), nullable=False)
    person_name = Column(String(64), nullable=False, default="")
    points = Column(Integer, nullable=False, default=0)
    cnt = Column(Integer, nullable=False, default=0)       # 对账侧行数（店数）
    created_at = Column(DateTime, nullable=False, default=_now)


class PayrollPeriodRow(Base):
    """月度分期对账偏差表：月 × 员工。

    上半月(1-15)/下半月(16-月末) = 系统正式表该段点数（已分期发给现场）；
    对账点数 = 该月对账文件点数（期末终算）；上月修正 = 上月该员工对账偏差；
    本月对账偏差 = 对账 − (上半月+下半月) + 上月修正（偏差>0=少付需补，<0=多付需扣；
    生成后管理员可手改，手改值在重新生成时保留）。
    """
    __tablename__ = "payroll_period_rows"
    __table_args__ = (UniqueConstraint("month", "person_code",
                                        name="uq_period_month_code"),)
    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False)          # YYYY-MM
    person_code = Column(String(32), nullable=False)
    half1_points = Column(Integer, nullable=False, default=0)   # 1-15 点数
    half2_points = Column(Integer, nullable=False, default=0)   # 16-月末 点数
    # 分期店数快照（同步时间点固化；发薪单据不随人×日表后续变化漂移）
    half1_records = Column(Integer, nullable=False, default=0)  # 上半月有效店
    half1_p1 = Column(Integer, nullable=False, default=0)       # 上半月1点店
    half1_p2 = Column(Integer, nullable=False, default=0)       # 上半月2点店
    half2_records = Column(Integer, nullable=False, default=0)  # 下半月有效店
    half2_p1 = Column(Integer, nullable=False, default=0)       # 下半月1点店
    half2_p2 = Column(Integer, nullable=False, default=0)       # 下半月2点店
    settle_points = Column(Integer, nullable=False, default=0)  # 对账点数
    prev_adjust_points = Column(Integer, nullable=False, default=0)  # 上月修正点数(递延上月找平)
    diff_points = Column(Integer, nullable=False, default=0)    # 对账偏差(点,系统参考,自动刷)
    # 金额(円)
    half1_bonus = Column(Integer, nullable=False, default=0)   # 上半月奖金(满68=3000)
    half2_bonus = Column(Integer, nullable=False, default=0)   # 下半月奖金(满68=3000)
    half1_amount = Column(Integer, nullable=False, default=0)   # 上半月实发=点数×250+奖金
    half2_amount = Column(Integer, nullable=False, default=0)   # 下半月实发=点数×250+奖金
    settle_amount = Column(Integer, nullable=False, default=0)  # 对账金额(自动=工资规则×对账点数)
    prev_adjust_amount = Column(Integer, nullable=False, default=0)  # 上月修正金额(递延)
    diff_amount = Column(Integer, nullable=False, default=0)    # 对账偏差金额(系统参考,自动刷)
    adjust_points = Column(Integer, nullable=False, default=0)  # 找平点数(人工执行,默认0)
    adjust_amount = Column(Integer, nullable=False, default=0)  # 找平金额(=点数×该月单价)
    updated_by = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now)


class MonthPerfRecord(Base):
    """月绩效工资记录（入统计表/重算时物化，每员工每月一条）。

    打开绩效工资页只查本表，不再实时聚合正式表；
    明细经页面「详细」按钮查正式表/统计表。
    """
    __tablename__ = "month_perf_records"
    __table_args__ = (UniqueConstraint("month", "person_code",
                                        name="uq_mpf_month_code"),)
    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False)
    person_code = Column(String(32), nullable=False)
    records = Column(Integer, nullable=False, default=0)   # 有效店数
    p1 = Column(Integer, nullable=False, default=0)
    p2 = Column(Integer, nullable=False, default=0)
    points = Column(Integer, nullable=False, default=0)    # p1 + p2*2
    salary = Column(Integer, nullable=False, default=0)    # 工资 = 点数×单价 + 满68奖3000
    per_point = Column(Integer, nullable=False, default=250)  # 该月单价(円/点)=当前点数金额
    # 月度对账完成后更新（来自薪资找平表）
    settle_points = Column(Integer, nullable=False, default=0)   # 对账点数
    settle_amount = Column(Integer, nullable=False, default=0)   # 对账金额(円)
    diff_points = Column(Integer, nullable=False, default=0)     # 本月对账偏差(点)
    diff_amount = Column(Integer, nullable=False, default=0)     # 本月对账偏差金额(円)
    rate37 = Column(Float, nullable=False, default=0.0)
    pass37 = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_now)


# ---------------------------------------------------------------------------
# P1（WorkBuddy 接入）：Token / 审计 / 封账 / 重算快照
# 设计见 docs/MCP对接手册.md §4.6（工具场景化）与 §5（身份/授权）
# ---------------------------------------------------------------------------

class ApiToken(Base):
    """MCP Access Token（绑定到人，可吊销）。spec §5.1。"""
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False, default="")
    token_prefix = Column(String(16), nullable=False, unique=True, index=True)
    token_hash = Column(String(64), nullable=False)          # sha256 十六进制
    scopes = Column(String(64), nullable=False, default="read")   # read / read,write
    created_at = Column(DateTime, nullable=False, default=_now)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    # 有效期：默认签发 90 天；管理员可签永久（NULL=永久）。spec §三
    expires_at = Column(DateTime, nullable=True)


class McpAuditLog(Base):
    """MCP 调用审计（两阶段写入：先插行 ok=NULL，返回前回填）。spec §8。"""
    __tablename__ = "mcp_audit_log"

    id = Column(Integer, primary_key=True)
    token_id = Column(Integer, nullable=True)
    user_id = Column(Integer, nullable=True)
    tool = Column(String(64), nullable=True)
    params_json = Column(Text, nullable=True)                # 脱敏，不含 Token 明文
    ok = Column(Boolean, nullable=True)                      # 执行中为 NULL
    error_code = Column(String(32), nullable=True)
    detail = Column(Text, nullable=True)
    client_info = Column(String(255), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class OAuthClient(Base):
    """MCP OAuth 动态注册客户端（见 docs/MCP对接手册.md §3）。

    `client_secret_hash` 为 NULL = 公共客户端（PKCE 强制，WorkBuddy 即此类）；
    库内只存哈希，明文只在注册响应中出现一次。
    """
    __tablename__ = "oauth_clients"

    id = Column(Integer, primary_key=True)
    client_id = Column(String(64), unique=True, nullable=False, index=True)
    client_secret_hash = Column(String(64), nullable=True)
    client_name = Column(String(128), nullable=False, default="")
    redirect_uris = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=_now)
    last_used_at = Column(DateTime, nullable=True)


class OAuthCode(Base):
    """授权码（5 分钟有效，一次性）。

    `access_token_id`：该 code 换出的 access token 行 id —— code 被重放时据此
    吊销已换出的 token（见 docs/MCP对接手册.md §3）。
    """
    __tablename__ = "oauth_codes"

    id = Column(Integer, primary_key=True)
    code_hash = Column(String(64), unique=True, nullable=False, index=True)
    client_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    redirect_uri = Column(String(512), nullable=False)
    code_challenge = Column(String(128), nullable=False)
    code_challenge_method = Column(String(16), nullable=False, default="S256")
    scope = Column(String(64), nullable=False, default="read")
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    access_token_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class OAuthRefreshToken(Base):
    """Refresh token（90 天，每次刷新轮换：旧的置 revoked_at）。"""
    __tablename__ = "oauth_refresh_tokens"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    client_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    scope = Column(String(64), nullable=False, default="read")
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    last_used_at = Column(DateTime, nullable=True)


class SealedMonth(Base):
    """封账月份：表内月份一律禁止写入（闸门 3）。spec §7。"""
    __tablename__ = "sealed_months"

    month = Column(String(7), primary_key=True)              # YYYY-MM
    note = Column(String(255), nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class RebuildSnapshot(Base):
    """重算前正式表全量快照（只能回灌正式表，非整月可逆）。spec §9 改动 B。"""
    __tablename__ = "rebuild_snapshots"

    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False, index=True)
    audit_id = Column(Integer, nullable=True)                # 不加 FK：HTML 路径无审计行
    payload_json = Column(Text, nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)


class PayrollPaidMark(Base):
    """发薪标记：某月某期（上半月/下半月）是否已实际发薪。

    为什么需要：找平的「上月结转」要在本月两期工资里扣/补，但**已发薪的期改不了**。
    系统没有发薪动作（只导出发薪表、线下发），所以必须由管理员显式标记；
    `period.sync_period_table` 只把**未标记的期**算作可吸收额度，
    否则会出现「上半月已发、下半月为 0 → 结转被判定已吸收，实际漏扣」。
    """
    __tablename__ = "payroll_paid_marks"
    __table_args__ = (UniqueConstraint("month", "half", name="uq_paid_mark"),)

    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False, index=True)     # YYYY-MM
    half = Column(Integer, nullable=False)                    # 1=上半月 2=下半月
    marked_by = Column(Integer, nullable=True)
    marked_at = Column(DateTime, nullable=False, default=_now)


class PayrollPayment(Base):
    """**发放台账（不可变历史事实）**：某月某人第 seq 期实际发了多少。

    为什么要独立成表：`payroll_period_rows` 是**计算结果**（会随数据/对账/规则重算），
    而"已经发了 375,000"是**历史事实**，不能再被重算改写。两者混在一张表里会导致：
    8月对账晚上传 → 9月整行重算 → 已发的上半月数字也被改；且吸收逻辑不知道哪期已发
    → 高估吸收能力 → **漏扣**（实测 -59,000 被误判为已吸收）。

    - seq 不固定：一月发 2 次就 seq=1,2；发 3 次就 1,2,3（不写死两期）
    - amount 是**发放时快照**，之后重算不影响它
    - adjust_applied 记该期实际抵扣/补发的找平额（负=扣）
    """
    __tablename__ = "payroll_payments"
    __table_args__ = (UniqueConstraint("month", "person_code", "seq",
                                       name="uq_payment_inst"),)

    id = Column(Integer, primary_key=True)
    month = Column(String(7), nullable=False, index=True)      # 归属结算月 YYYY-MM
    person_code = Column(String(32), nullable=False, index=True)
    seq = Column(Integer, nullable=False)                      # 第几期（1,2,3…）
    points = Column(Integer, nullable=False, default=0)        # 该期点数（快照）
    amount = Column(Integer, nullable=False, default=0)        # 实发金额 円（快照）
    bonus = Column(Integer, nullable=False, default=0)         # 其中奖金 円
    adjust_applied = Column(Integer, nullable=False, default=0)  # 该期实际抵扣/补发 円
    note = Column(String(255), nullable=True)
    paid_at = Column(DateTime, nullable=False, default=_now)
    paid_by = Column(Integer, nullable=True)
    # ---- 找平抵扣溯源（否则只有一个金额 = 空穴来风，出 bug 没法查）----
    adjust_source_type = Column(String(12), nullable=True)     # carry | diff
    adjust_source_month = Column(String(7), nullable=True)     # 产生结转的月份（上月）
    adjust_source_row_id = Column(Integer, nullable=True)      # 上月 payroll_period_rows.id
    adjust_source_task_id = Column(Integer, nullable=True)     # 上月当前对账任务 id（可空）
    adjust_leftover = Column(Integer, nullable=True)           # 扣完后仍需递延的金额（负）


class PayrollAdjust(Base):
    """**找平表**：一行 = 一笔找平（某月差异 × 某人），进度是**存下来的事实**。

    为什么独立成表（用户要求）：
    - 找平进度（已找平/剩余/是否结清）应是可直接查询的一等数据，
      而不是每次查询靠找平链（prev_adjust_amount）反推；
    - 多月差异交错时链条反推会失真（旧实现只能给"近似回收额"）；
    - 可记录"结清时间/结清于哪一期"，便于对账与 bug 排查；
    - 这张表就是找平的账：谁欠谁、欠多少、还了多少、何时还清。

    约定：
    - adjust_amount：原始找平金额（负=应扣/正=应补，含奖金口径）
    - settled_amount：已找平金额（逐步累加，与 adjust_amount 同号）
    - remaining：剩余（= adjust_amount − settled_amount；0 = 已结清）
    - 回收顺序：**FIFO**（先欠的先还），一笔发放吸收的金额优先冲最早的未结清找平
    """
    __tablename__ = "payroll_adjusts"
    __table_args__ = (UniqueConstraint("source_month", "person_code",
                                       name="uq_adjust_person"),)

    id = Column(Integer, primary_key=True)
    source_month = Column(String(7), nullable=False, index=True)   # 产生差异的月份
    person_code = Column(String(32), nullable=False, index=True)
    source_task_id = Column(Integer, nullable=True)                # 对账任务 id
    source_row_id = Column(Integer, nullable=True)                 # 该月找平行 id
    adjust_amount = Column(Integer, nullable=False, default=0)     # 原始找平金额
    settled_amount = Column(Integer, nullable=False, default=0)    # 已找平
    remaining = Column(Integer, nullable=False, default=0)         # 剩余
    status = Column(String(12), nullable=False, default="in_progress")
    settled_at = Column(DateTime, nullable=True)                   # 结清时间
    updated_at = Column(DateTime, nullable=False, default=_now)


class PayrollSettlementLink(Base):
    """**找平回收明细**：发放（payroll_payments）↔ 找平（payroll_adjusts）多对多。

    用户要求双向可查：
    - 一笔薪资（某月某期）→ 冲了哪几笔找平（FIFO 可能同时冲多个月）
    - 一笔找平 → 被哪几期薪资回收的（可能跨多期/多月）
    单一来源字段（payroll_payments.adjust_source_*）只能表达一对一，故独立成表。
    """
    __tablename__ = "payroll_settlement_links"
    __table_args__ = (UniqueConstraint("adjust_id", "payment_id",
                                       name="uq_link_adjust_payment"),)

    id = Column(Integer, primary_key=True)
    adjust_id = Column(Integer, ForeignKey("payroll_adjusts.id"),
                       nullable=False, index=True)      # 哪笔找平
    payment_id = Column(Integer, ForeignKey("payroll_payments.id"),
                        nullable=False, index=True)     # 哪笔发放冲的
    month = Column(String(7), nullable=False, index=True)   # 发放月
    person_code = Column(String(32), nullable=False, index=True)
    amount = Column(Integer, nullable=False, default=0)     # 本次冲抵（负=扣回）
    created_at = Column(DateTime, nullable=False, default=_now)


# ---------- 员工每日填报 + 对比分析报告 ----------
class StaffDailyReport(Base):
    """员工每天提交一次：担当区域 + 1点店铺数 + 2点店铺数。

    填报数据**不参与工资计算**，只用于与文件跑出的 person_daily_stats 对比（规格 D1–D7）。
    一天一条：`(person_code, report_date)` 唯一；`report_date` 为 JST 业务日。
    """
    __tablename__ = "staff_daily_reports"
    __table_args__ = (UniqueConstraint("person_code", "report_date",
                                       name="uq_sdr_person_date"),
                      Index("ix_sdr_date", "report_date"))
    id = Column(Integer, primary_key=True)
    person_code = Column(String(32), ForeignKey("persons.code"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    report_date = Column(Date, nullable=False)              # JST 业务日
    area = Column(String(64), nullable=False, default="", server_default="")
    p1_cnt = Column(Integer, nullable=False, default=0)     # 1 点店铺数
    p2_cnt = Column(Integer, nullable=False, default=0)     # 2 点店铺数
    total_cnt = Column(Integer, nullable=False, default=0)  # = p1_cnt + p2_cnt
    submitted_at = Column(DateTime, nullable=False, default=_now)
    client_ts = Column(String(40), nullable=False, default="", server_default="")
    source = Column(String(16), nullable=False, default="web", server_default="web")
    created_at = Column(DateTime, nullable=False, default=_now)


class StaffReportAnalysis(Base):
    """区间对比分析报告。

    `summary` = **程序算出的**对比结果（数字唯一可信来源）；
    `payload` = **模型产出的**结构化评语（`by_lang.zh/ja`，逐人段落）。
    管理端渲染全部；员工端只渲染 `per_person[自己]`（服务端裁剪，规格 D13/D14/D15）。
    """
    __tablename__ = "staff_report_analyses"
    __table_args__ = (Index("ix_sra_period", "period_start", "period_end"),)
    id = Column(Integer, primary_key=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    status = Column(String(12), nullable=False, default="pending",
                    server_default="pending")        # pending/running/done/failed
    summary = Column(JSON, nullable=False, default=dict)
    payload = Column(JSON, nullable=False, default=dict)
    ai_model = Column(String(64), nullable=False, default="", server_default="")
    ai_tokens = Column(Integer, nullable=False, default=0)
    ai_error = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    finished_at = Column(DateTime, nullable=True)


# ---------- 核对结果物化（员工端只读；报告生成时落表） ----------
class StaffReportComparePerson(Base):
    """对比结果·人×区间汇总（物化）。

    报告生成时写一次；**员工端核对页直接读本表**，不再实时跑 compare()。
    `acc/acc1/acc2` 存 0..1 的准确率（NULL=没有可对照的日子）。
    """
    __tablename__ = "staff_report_compare_person"
    __table_args__ = (UniqueConstraint("analysis_id", "person_code",
                                       name="uq_srcp_analysis_person"),
                      Index("ix_srcp_person", "person_code"))
    id = Column(Integer, primary_key=True)
    analysis_id = Column(Integer, ForeignKey("staff_report_analyses.id"),
                         nullable=False)
    person_code = Column(String(32), nullable=False)
    name = Column(String(64), nullable=False, default="", server_default="")
    sys_p1 = Column(Integer, nullable=False, default=0)
    sys_p2 = Column(Integer, nullable=False, default=0)
    sys_total = Column(Integer, nullable=False, default=0)
    rep_p1 = Column(Integer, nullable=False, default=0)
    rep_p2 = Column(Integer, nullable=False, default=0)
    rep_total = Column(Integer, nullable=False, default=0)
    d1 = Column(Integer, nullable=False, default=0)
    d2 = Column(Integer, nullable=False, default=0)
    d_total = Column(Integer, nullable=False, default=0)
    acc = Column(Float, nullable=True)      # 0..1；NULL=没有可对照的日子
    days_filled = Column(Integer, nullable=False, default=0)
    days_system = Column(Integer, nullable=False, default=0)
    days_both = Column(Integer, nullable=False, default=0)
    gaps = Column(Integer, nullable=False, default=0)      # 应填未填
    abs_dt = Column(Integer, nullable=False, default=0)    # Σ|Δ|（准确率分子）
    created_at = Column(DateTime, nullable=False, default=_now)


class StaffReportCompareDay(Base):
    """对比结果·人×日明细（物化）。`kind` ∈ both / missing_report / missing_system。"""
    __tablename__ = "staff_report_compare_day"
    __table_args__ = (UniqueConstraint("analysis_id", "person_code", "ref_date",
                                       name="uq_srcd_analysis_person_date"),
                      Index("ix_srcd_person", "person_code"))
    id = Column(Integer, primary_key=True)
    analysis_id = Column(Integer, ForeignKey("staff_report_analyses.id"),
                         nullable=False)
    person_code = Column(String(32), nullable=False)
    ref_date = Column(Date, nullable=False)
    kind = Column(String(16), nullable=False, default="", server_default="")
    sys_p1 = Column(Integer, nullable=True)
    sys_p2 = Column(Integer, nullable=True)
    sys_total = Column(Integer, nullable=True)
    rep_p1 = Column(Integer, nullable=True)
    rep_p2 = Column(Integer, nullable=True)
    rep_total = Column(Integer, nullable=True)
    d1 = Column(Integer, nullable=True)
    d2 = Column(Integer, nullable=True)
    dt = Column(Integer, nullable=True)
    acc = Column(Float, nullable=True)      # 单日准确率（仅 both 日有值）
    created_at = Column(DateTime, nullable=False, default=_now)


class StaffDatePlan(Base):
    """**日期计划（半月出勤登记）**：一行 = 一个人一天，**整格状态都在这一行里**。

    规格 `docs/记录-日期计划.md`：

    - 自然半月：上半月 1–15（H1，截止 3 号）/ 下半月 16–月末（H2，截止 18 号）；
    - **一天一条**（`(person_code, plan_date)` 唯一），默认 `available=True`（默认每天都出勤），
      员工只勾"不出勤"的那些天；
    - **`reported` = 该日已自报出勤**（用户 2026-10-01 口径："员工自报了就直接改这个计划表里的状态"）：
      每日填报提交/修改/删除时**写透**到本表（`date_plan.mark_reported`），
      所以管理端矩阵**只读这一张表即可渲染三态，不需要关联 `staff_daily_reports`**
      （= 不做 join、不做第二条查询）；
    - 单元格状态 = `cell_state(available, reported, ...)`（同一行内计算）：
      `reported` → □ 已出勤 / 过去且亲手登记可出勤却没自报 → △ /
      `available` → ○ 可出勤 / 否则 × 不出勤；没有行 = 未登记；
    - 计划只是**预报**，`reported` 才是事实（实际出勤以自报为准）；
    - **`source`**：`web` = 员工登记的整期计划行；`report` = 由每日填报写入的行（不算"已登记"）；
      `admin` = 管理员写入。**"该半月是否已登记" = 该期存在 `source != 'report'` 的行**
      （否则只自报没登记的人会被误判成登记过）；
    - 不存 period_key：半月归属由 `plan_date` 现算，规则调整不会让历史数据产生歧义。
    """
    __tablename__ = "staff_date_plans"
    __table_args__ = (UniqueConstraint("person_code", "plan_date",
                                       name="uq_sdp_person_date"),
                      Index("ix_sdp_date", "plan_date"))
    id = Column(Integer, primary_key=True)
    person_code = Column(String(32), ForeignKey("persons.code"), nullable=False)
    plan_date = Column(Date, nullable=False)                # JST 业务日
    available = Column(Boolean, nullable=False, default=True)   # True=可出勤 / False=不出勤
    reported = Column(Boolean, nullable=False, default=False)   # True=当天已自报出勤（□）
    source = Column(String(16), nullable=False, default="web", server_default="web")
    #: 该行是**假期模式自动标的"不出勤"**（用户 2026-10-03"可以标"）：
    #: 指向 `bd_staff_leave.id`；结束/替换休假时按它**精确撤销**，
    #: 这样"员工自己点的 ×"和"休假带的 ×"分得清，不会互相抹掉。
    leave_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class FormToken(Base):
    """**一次性提交令牌**（防重复提交）。

    渲染表单时发放（`{{ form_token() }}`），提交时校验并**立即作废**：
    双击、返回再提交、网络重试、脚本重放都会在第二次被拒。
    `user_id` 为空 = 未登录时发放（登录页豁免，用不到）。
    """
    __tablename__ = "form_tokens"
    token = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=_now, index=True)
    used_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# BD 作业域（bd_*）：行政区划基底 / 门店宇宙
# 设计见 docs/记录-作业域.md（车站三层资产）
#
# ⚠️ 硬边界：本域**只新增表**，绝不向结算域表（formal_records /
#    person_daily_stats / month_perf_records / payroll_*）加列；
#    结算域永不读取本域数据。
# ---------------------------------------------------------------------------

class BdArea(Base):
    """行政区划基底（作業域）：都道府県 / 市区町村 / 町丁目。

    官方编码，**只读同步**（来源：総務省 全国地方公共団体コード；
    町丁目层将来可用 e-Stat 小地域补全）。不自造坐标系 ——
    片区（bd_zone，P2 建）由本表的丁目集合构成。

    - `level`：pref / city / town
    - `code`：pref=2 位、city=6 位（全国地方公共団体コード）、town=11 位
    - `parent_code`：上级 code（city → pref、town → city）
    """
    __tablename__ = "bd_area"
    __table_args__ = (
        UniqueConstraint("level", "code", name="uq_bd_area_level_code"),
        Index("ix_bd_area_parent", "parent_code"),
    )
    id = Column(Integer, primary_key=True)
    level = Column(String(8), nullable=False)                # pref/city/town
    code = Column(String(16), nullable=False)
    name = Column(String(128), nullable=False, default="")
    name_kana = Column(String(128), nullable=False, default="",
                       server_default="")
    parent_code = Column(String(16), nullable=True)
    pref_code = Column(String(4), nullable=False, default="",
                       server_default="")
    city_code = Column(String(8), nullable=False, default="",
                       server_default="")
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdStore(Base):
    """门店宇宙（作業域）：回流累积的门店主档。

    ⭐ `store_key` **优先取既有 `raw_records.store_id_raw`** ——
    实测它是稳定的门店主键（一个 id → 一个店名，基本 1:1；
    格式 `010104709` + 注册日 + 序号）。**不必依赖 Google place_id。**

    - `area_code`：所属町丁目 code（靠地址地理编码回填，可空）
    - `last_visit_date`：**60 天冷却的判定依据**
    - `zone_id`：所属片区（冗余；片区表 P2 再建，此处不加 FK）
    """
    __tablename__ = "bd_store"
    __table_args__ = (
        UniqueConstraint("store_key", name="uq_bd_store_key"),
        Index("ix_bd_store_area", "area_code"),
        Index("ix_bd_store_zone", "zone_id"),
    )
    id = Column(Integer, primary_key=True)
    store_key = Column(String(64), nullable=False)
    name_raw = Column(Text, nullable=False, default="")
    name_norm = Column(Text, nullable=False, default="")
    address = Column(Text, nullable=False, default="", server_default="")
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    area_code = Column(String(16), nullable=True)
    zone_id = Column(Integer, nullable=True)
    place_id = Column(String(64), nullable=True)
    gyotai = Column(String(64), nullable=False, default="", server_default="")
    first_seen_person_code = Column(String(32), nullable=True)
    first_seen_date = Column(Date, nullable=True)
    last_visit_date = Column(Date, nullable=True)
    visit_count = Column(Integer, nullable=False, default=0)
    source = Column(String(16), nullable=False, default="import",
                    server_default="import")
    status = Column(String(16), nullable=False, default="active",
                    server_default="active")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


# ---------------------------------------------------------------------------
# 团队 + 车站任务（feat/team-task · 见 docs/记录-作业域.md）
#
# ⚠️ 硬边界：本域**只新增 bd_* 表**，绝不向结算域表（formal_records /
#    person_daily_stats / month_perf_records / payroll_*）加列；
#    结算域永不读取本域数据。
# ---------------------------------------------------------------------------

class BdTeam(Base):
    """团队（一层队；队名由管理员起，如 `小川队`）。"""
    __tablename__ = "bd_team"
    __table_args__ = (
        UniqueConstraint("name", name="uq_bd_team_name"),
        UniqueConstraint("code", name="uq_bd_team_code"),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    code = Column(String(32), nullable=True)
    note = Column(Text, nullable=False, default="", server_default="")
    status = Column(String(16), nullable=False, default="active",
                    server_default="active")          # active / closed
    created_by = Column(String(64), nullable=False, default="", server_default="")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdTeamMember(Base):
    """团队成员（含历史：人走了写 end_date，永不删行）。

    - `role`：leader 队长 / member 队员
    - ⚠️ **死规定（用户 2026-10-03）**：**一个人同时只能在一个队**；跨队没有"借调"，
      只有**先转出、再转入**（原队 `end_date` 收口 + 新队开一行）。
      所以 `person_code` 在 `end_date IS NULL` 的行上**唯一** —— 部分唯一索引
      `uq_bd_team_member_active_person`（SQLite/PG 都支持）。服务层判重只是第一层，
      这条索引让任何代码路径 / 手工 SQL 都破不了。
    """
    __tablename__ = "bd_team_member"
    __table_args__ = (
        UniqueConstraint("team_id", "person_code", "start_date",
                         name="uq_bd_team_member"),
        Index("ix_bd_team_member_person", "person_code"),
        Index("uq_bd_team_member_active_person", "person_code", unique=True,
              sqlite_where=text("end_date IS NULL"),
              postgresql_where=text("end_date IS NULL")),
    )
    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("bd_team.id"), nullable=False)
    person_code = Column(String(32), ForeignKey("persons.code"), nullable=False)
    role = Column(String(16), nullable=False, default="member",
                  server_default="member")           # leader / member
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)           # NULL = 现役
    created_at = Column(DateTime, nullable=False, default=_now)


class BdLine(Base):
    """铁道线路主档（车站的上级）。

    数据来源：**国土数値情報 N02（鉄道）** —— `scripts/bd_fetch_rail.py` 抓取并生成
    `scripts/bd_kanto_rail.json`，`scripts/bd_import_rail.py` 导入。一都三県 ≈ 131 线。

    ⚠️ `name` 是 MLIT 的**官方线路名**，单看会重名（「本線」有京急/京成/西武…，
    都営是「12号線大江戸線」）→ **界面一律显示 `operator_short + name`**（如「京急 本線」）。
    `kind` 是粗分类（jr / shinkansen / private / public / third / monorail / agt / tram / cable）。
    """
    __tablename__ = "bd_line"
    __table_args__ = (
        UniqueConstraint("operator", "name", name="uq_bd_line_op_name"),
        Index("ix_bd_line_kind", "kind"),
        Index("ix_bd_line_name_norm", "name_norm"),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    name_norm = Column(String(64), nullable=False)          # NFKC + 去空白
    operator = Column(String(64), nullable=False)           # MLIT 运营公司全名
    operator_short = Column(String(32), nullable=False, default="", server_default="")
    kind = Column(String(16), nullable=False, default="", server_default="")
    prefs = Column(String(32), nullable=False, default="", server_default="")
    n_station = Column(Integer, nullable=False, default=0, server_default="0")
    source = Column(String(16), nullable=False, default="mlit", server_default="mlit")
    note = Column(Text, nullable=False, default="", server_default="")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdStationPlace(Base):
    """**物理车站（场所）**：车站数据资产的**核心层**（2026-10-05 用户："车站数据可以当成我们的数据资产。

    也是任务的输入源之一"）。

    - 一条 = 一个真实存在的车站（人真正走到的那个地方）
    - `bd_station` = "**某条线路上的**这个站"（站×线隶属），一个 place 下 1~N 条
    - 分层键 = **N02_005g 駅グループコード**（MLIT 官方"同一车站"分组；**不用猜**）
      实测：1,920 条站×线 → **1,568 个物理车站**，237 个跨线站（渋谷/新宿/横浜/大宮/池袋 最多 7 条线），
      同组站名 100% 一致、坐标 100% 在 1km 内（0 异常）
    - ⚠️ **任务仍挂在 `bd_station`（站×线）上**，本层**不改任务行为**：将来要"一个物理车站一个任务"
      时，把任务指到 place 即可（届时定合并规则）；`lines_text`/`n_line` 让界面先能显示"N 条线经过"
    - 未来**片区（zone）**也从这一层长出来（一片区域 = 若干物理车站）
    """
    __tablename__ = "bd_station_place"
    __table_args__ = (
        # group_code 是 N02 的分组码；手工建的站没有 → 用部分唯一索引（只约束非空）
        Index("uq_bd_place_group", "group_code", unique=True,
              sqlite_where=text("group_code != ''"),
              postgresql_where=text("group_code != ''")),
        Index("ix_bd_place_pref", "pref"),
        Index("ix_bd_place_name_norm", "name_norm"),
        Index("ix_bd_place_city", "city"),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)              # 规范站名（同组取一致写法）
    name_norm = Column(String(64), nullable=False)         # NFKC + 去空白 + ケ/ヶ 统一
    pref = Column(String(8), nullable=False, default="", server_default="")
    city = Column(String(64), nullable=False, default="", server_default="")  # 市区町村（待补）
    lon = Column(Float, nullable=True)                     # 组内坐标均值
    lat = Column(Float, nullable=True)
    group_code = Column(String(16), nullable=False, default="", server_default="")
    n_line = Column(Integer, nullable=False, default=0, server_default="0")
    n_operator = Column(Integer, nullable=False, default=0, server_default="0")
    operators = Column(String(128), nullable=False, default="", server_default="")   # 冗余可读
    lines_text = Column(Text, nullable=False, default="", server_default="")        # 冗余可读
    source = Column(String(16), nullable=False, default="mlit", server_default="mlit")
    note = Column(Text, nullable=False, default="", server_default="")
    status = Column(String(16), nullable=False, default="active",
                    server_default="active")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdStation(Base):
    """车站主数据（一个站 = 一个站前商圈 = 用户口中的"一边区域"）。

    只存主数据；任务/担当/进展在 `bd_task*` 上。
    `name_norm` 是站名判重键（NFKC + 去空白 + **ケ/ヶ 统一**）。

    ⚠️ **一线一站**（用户 2026-10-03 口径）：唯一键 = `(line_id, name_norm)`。
    同一个物理车站跨多条线路时**现在按线路各存一行**（东京站 ×山手線/中央線/…），
    将来再定合并规则 —— `group_code`（N02_005g，MLIT 的同一车站分组码）已存下来，
    合并时用它 + 坐标即可（用户："知道有这个坑就行，先不处理"）。
    `line_id` 为空的历史/手工行，用 `uq_bd_station_noline_name` 保证站名不重复。
    """
    __tablename__ = "bd_station"
    __table_args__ = (
        Index("uq_bd_station_line_name", "line_id", "name_norm", unique=True,
              sqlite_where=text("line_id IS NOT NULL"),
              postgresql_where=text("line_id IS NOT NULL")),
        Index("uq_bd_station_noline_name", "name_norm", unique=True,
              sqlite_where=text("line_id IS NULL"),
              postgresql_where=text("line_id IS NULL")),
        Index("ix_bd_station_line", "line"),
        Index("ix_bd_station_line_id", "line_id"),
        Index("ix_bd_station_pref", "pref"),
        Index("ix_bd_station_group_code", "group_code"),
        Index("ix_bd_station_place_id", "place_id"),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    name_norm = Column(String(64), nullable=False)
    line = Column(String(64), nullable=False, default="", server_default="")
    #: 线路主档（N02 导入的行都有；手工建且没填线路的行可能为空）
    line_id = Column(Integer, ForeignKey("bd_line.id"), nullable=True)
    #: **物理车站**（资产核心层）：同一 place 下的多行 = 多条线路经过同一个车站
    place_id = Column(Integer, ForeignKey("bd_station_place.id"), nullable=True)
    #: MLIT 运营公司全名（冗余，列表页不用 join）
    operator = Column(String(64), nullable=False, default="", server_default="")
    #: JIS 都道府県码（13=東京都 / 11=埼玉 / 12=千葉 / 14=神奈川）
    pref = Column(String(8), nullable=False, default="", server_default="")
    lon = Column(Float, nullable=True)
    lat = Column(Float, nullable=True)
    #: N02_005c 駅コード / N02_005g 同一駅グループコード（将来の合并用）
    ekicode = Column(String(16), nullable=False, default="", server_default="")
    group_code = Column(String(16), nullable=False, default="", server_default="")
    #: **沿线顺序**（1 起）——「按线路选站」时用来排序（用户 2026-10-05：
    #: "直接在现有的车站表里加一列，后面我们查的时候就拿这列做 order 排序"）
    #: ⚠️ 来源是 **OSM 的"运行系统线路"**（山手線 = 30 站一圈），不是 N02 官方线路口径
    #:   （N02 把山手环拆成 山手線17 + 東北線 + 東海道線）→ 所以同一 N02 线路内的 seq
    #:   **可能不连续，但相对顺序是对的**（排序只看相对大小，不看是否 1..N 连号）
    seq = Column(Integer, nullable=True)
    #: 沿线里程（km，从该运行系统线路的起点站算）—— 判断"这一站离市中心多远"
    along_km = Column(Float, nullable=True)
    #: 顺序来源标记（如 `osm:JR山手線`），空 = 没取到顺序
    seq_src = Column(String(64), nullable=False, default="", server_default="")
    source = Column(String(16), nullable=False, default="manual",
                    server_default="manual")           # manual / mlit
    note = Column(Text, nullable=False, default="", server_default="")
    status = Column(String(16), nullable=False, default="active",
                    server_default="active")          # active / closed
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdTask(Base):
    """任务 = 一件要做的活；**来源可以是车站，将来也可以是片区**。

    - **任务单位 = 物理车站**（用户 2026-10-05 定稿）：`UNIQUE(place_id)`，
      「1 个车站 = 1 个任务，各自独立状态」。用户原话："我分给 A 队的 10 个站，
      **并不是他这 10 个站作为一个整体任务跑完我再分新的**，而是在剩下几个站的时候，
      我就可以再派发新的一组任务给他" → **派活是滚动的**，所以任务必须细到站、状态独立。
      （京葉線 10 站给 A 队 + 8 站给 B 队 = **18 个任务**，不是 2 个）
    - `station_id` 保留（老数据/站×线口径仍可用），**新任务只填 `place_id`**；
      `UNIQUE(station_id)` 对 NULL 不生效（SQLite/PG 里 NULL 互不相等），所以两者能共存
    - `source_type`：任务来源判别（今天恒为 `station`）。将来以**片区**当任务时：
      加 `zone_id`（可空）+ `place_id` 改可空 + `source_type='zone'`，任务表别的都不动
    - `assign_date`：**分配日期**（管理员把任务派给团队的日期；**默认今天**，
      用户："一个任务是要好几天才能执行完的"）→ 管理端按区间查询
    - `state`：unassigned 未分配 / doing 进行中 / done 已完成
      （由担当 + 进度推导，服务层统一维护）
    - `pct`：当前进展 0–100（最近一次提交的值，冗余在此便于列表直读）
    """
    __tablename__ = "bd_task"
    __table_args__ = (
        UniqueConstraint("station_id", name="uq_bd_task_station"),
        Index("uq_bd_task_place", "place_id", unique=True,
              sqlite_where=text("place_id IS NOT NULL"),
              postgresql_where=text("place_id IS NOT NULL")),
        Index("ix_bd_task_team", "team_id"),
        Index("ix_bd_task_assign_date", "assign_date"),
        Index("ix_bd_task_state", "state"),
    )
    id = Column(Integer, primary_key=True)
    #: 任务来源判别：station（现在）/ zone（将来片区）—— 车站与片区都是任务的**输入源**
    source_type = Column(String(16), nullable=False, default="station",
                         server_default="station")
    #: 老口径（站×线）；新任务只填 `place_id`，此列留空
    station_id = Column(Integer, ForeignKey("bd_station.id"), nullable=True)
    #: **物理车站**（任务单位）：1 个车站 1 个任务（跨线站也只 1 个）
    place_id = Column(Integer, ForeignKey("bd_station_place.id"), nullable=True)
    team_id = Column(Integer, ForeignKey("bd_team.id"), nullable=True)
    assign_date = Column(Date, nullable=True)        # 分配日期（派给团队那天）
    state = Column(String(16), nullable=False, default="unassigned",
                   server_default="unassigned")      # unassigned / doing / done
    pct = Column(Integer, nullable=False, default=0, server_default="0")
    #: **店铺数**（用户 2026-10-06）：队员把任务做到 100% 时必填（**允许 0**）；
    #:  队长批量补录可留空（NULL）。⚠️ 系统算不出来 —— `bd_store` 3 万家店的
    #:  `place_id`/经纬度实测全空（0/30681），只能手填。
    store_count = Column(Integer, nullable=True)
    # 开始日 / 完成日：对应用户 Excel 的那两列，**自动写**
    # （首次提交进展 = 开始日；pct 到 100 = 完成日）
    start_date = Column(Date, nullable=True)
    done_date = Column(Date, nullable=True)
    note = Column(Text, nullable=False, default="", server_default="")
    created_by = Column(String(64), nullable=False, default="", server_default="")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdTaskAssign(Base):
    """任务担当：一个任务分给 1~2 名队员（上限在服务层强制）。"""
    __tablename__ = "bd_task_assign"
    __table_args__ = (
        UniqueConstraint("task_id", "person_code", name="uq_bd_task_assign"),
        Index("ix_bd_task_assign_person", "person_code"),
    )
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("bd_task.id"), nullable=False)
    person_code = Column(String(32), ForeignKey("persons.code"), nullable=False)
    assigned_by = Column(String(64), nullable=False, default="", server_default="")
    assigned_at = Column(DateTime, nullable=False, default=_now)
    #: **当天派工日期**（JST）。队长"每天派工"用：今天派的 ∪ 未完成的（自动延续）就是队员当天要做的。
    #: NULL = 早于本字段的历史行（按"未完成即延续"处理）
    dispatch_date = Column(Date, nullable=True, index=True)


class BdTaskProgress(Base):
    """每日任务进展提交（一天一条，当天可改；历史留痕）。

    - `pct` 0–100（滑动条）= **当前生效值**（队长调整后就是调整后的值）
    - `progress_date`：业务日（JST），UNIQUE(task_id, progress_date)
    - **审核（用户 2026-10-03 要求：员工先上报 → 队长确认或调整）**：
      · `reported_pct`/`reported_by`：**员工上报的原值**（队长调整也不动，用于对比展示）
      · `review_status`：`pending`（等确认）/ `confirmed`（认可，值与上报一致）
        / `adjusted`（队长改了值）
      · `reviewed_by`/`reviewed_at`/`review_note`：谁什么时候处理的
    """
    __tablename__ = "bd_task_progress"
    __table_args__ = (
        UniqueConstraint("task_id", "progress_date", name="uq_bd_task_progress"),
        Index("ix_bd_task_progress_date", "progress_date"),
    )
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("bd_task.id"), nullable=False)
    progress_date = Column(Date, nullable=False)
    pct = Column(Integer, nullable=False, default=0, server_default="0")
    note = Column(Text, nullable=False, default="", server_default="")
    submitted_by = Column(String(32), nullable=False, default="",
                          server_default="")
    # ---- 上报 / 审核（员工先报、队长确认或调整）----
    reported_pct = Column(Integer, nullable=True)
    reported_by = Column(String(32), nullable=False, default="",
                         server_default="")
    review_status = Column(String(16), nullable=False, default="pending",
                           server_default="pending")
    reviewed_by = Column(String(32), nullable=False, default="",
                         server_default="")
    reviewed_at = Column(DateTime, nullable=True)
    review_note = Column(Text, nullable=False, default="", server_default="")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdLog(Base):
    """作业域变更日志（**追加型：只写不改不删**）。

    用户 2026-10-03 要求：「每个任务的变化日志；团队变化、团队成员变化、队长变化也要记」。
    设计取舍：**一张通用表**（`domain` 区分），写入只有一处、时间线一次查询出全。

    - `domain`：task / team / member / station
    - `ref_id`：对应行 id；`ref_label` 冗余可读（车站名/队名/人名），日志永远看得懂
    - `action`：create / update / dispatch / assign / unassign / role / state
      / progress / remove / status
    - `field` + `old_value` + `new_value`：改了什么（"担当：汤静 → 甘子杰"）
    - `actor`：操作人（登录名或 person_code）；`actor_name` 冗余显示名
    """
    __tablename__ = "bd_log"
    __table_args__ = (
        Index("ix_bd_log_ref", "domain", "ref_id"),
        Index("ix_bd_log_created", "created_at"),
    )
    id = Column(Integer, primary_key=True)
    domain = Column(String(16), nullable=False)
    ref_id = Column(Integer, nullable=True)
    ref_label = Column(String(128), nullable=False, default="", server_default="")
    action = Column(String(24), nullable=False)
    field = Column(String(32), nullable=False, default="", server_default="")
    old_value = Column(Text, nullable=False, default="", server_default="")
    new_value = Column(Text, nullable=False, default="", server_default="")
    actor = Column(String(64), nullable=False, default="", server_default="")
    actor_name = Column(String(64), nullable=False, default="", server_default="")
    note = Column(Text, nullable=False, default="", server_default="")
    created_at = Column(DateTime, nullable=False, default=_now)


class BdRoleCap(Base):
    """角色能力表（**权限口径的单一来源**，用户 2026-10-03 选 (a) 方案）。

    `(role, capability) → allowed`；判权（服务层）与页面按钮（模板）共用这一份。
    能力清单见 `app/services/bd_perm.py::CAPABILITIES`（保持小而实用）。
    """
    __tablename__ = "bd_role_cap"
    __table_args__ = (
        UniqueConstraint("role", "capability", name="uq_bd_role_cap"),
    )
    id = Column(Integer, primary_key=True)
    role = Column(String(16), nullable=False)
    capability = Column(String(32), nullable=False)
    allowed = Column(Boolean, nullable=False, default=False,
                      server_default="0")
    note = Column(Text, nullable=False, default="", server_default="")
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdAiSummary(Base):
    """**任务域 AI 总结的缓存**（一天一条；用户 2026-10-06：生成一次、点开即看）。

    ⚠️ 为什么不复用结算域的 `ai_runs`：铁律「作业域只写 `bd_*` 表」。
    """
    __tablename__ = "bd_ai_summary"
    __table_args__ = (UniqueConstraint("summary_date", name="uq_bd_ai_summary_day"),)
    id = Column(Integer, primary_key=True)
    #: 业务日（JST）—— 一天一条，当天再点直接看缓存
    summary_date = Column(Date, nullable=False)
    days = Column(Integer, nullable=False, default=7, server_default="7")
    #: 送进模型的指标（JSON 文本；回头核对"总结是不是基于这些数"就靠它）
    metrics_json = Column(Text, nullable=False, default="", server_default="")
    #: 模型产出的总结（JSON 文本：headline / bullets / risks）
    summary_json = Column(Text, nullable=False, default="", server_default="")
    model = Column(String(64), nullable=False, default="", server_default="")
    tokens = Column(Integer, nullable=False, default=0, server_default="0")
    created_by = Column(String(64), nullable=False, default="", server_default="")
    created_at = Column(DateTime, nullable=False, default=_now)


class BdStaffLeave(Base):
    """员工自己的**假期模式**（休假期，用户 2026-10-03 要求）。

    - 员工在员工端 `/my/plan` 上自己开启/结束；管理员可代改（服务层支持）
    - `end_date` 为空 = **未定结束日**（长期休假）
    - **一人同时只有一条 `status='active'` 的休假期**（新开一条 → 旧的自动结束）
    - 派工只看"目标日期是否落在休假期内" → **提醒，不强制约束**（用户明确）
    """
    __tablename__ = "bd_staff_leave"
    __table_args__ = (
        Index("ix_bd_staff_leave_person", "person_code", "start_date"),
    )
    id = Column(Integer, primary_key=True)
    person_code = Column(String(64), nullable=False, index=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)          # 空 = 未定
    reason = Column(Text, nullable=False, default="", server_default="")
    status = Column(String(16), nullable=False, default="active",
                    server_default="active")        # active / ended
    created_by = Column(String(64), nullable=False, default="", server_default="")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdMessage(Base):
    """站内消息（**头信息**，一条消息一行）。

    用户 2026-10-03：「消息模块就是类似其它网站或者系统的消息模块」——
    所以走标准设计：**一条消息 + N 个收件人**（`BdMessageRecipient`），已读是**按人**的。

    - `sender_kind`：`system`（任务确认/调整等自动通知）/ `admin` / `leader`
    - `scope`：`manual`（手动发）/ `task_review`（任务进展确认/调整）/ `announce`（公告）
    - `url`：点开消息跳哪里（如任务详情 `/tasks/12`）
    - `ref_type`/`ref_id`：关联对象，便于按任务查消息
    """
    __tablename__ = "bd_message"
    __table_args__ = (Index("ix_bd_message_created", "created_at"),)
    id = Column(Integer, primary_key=True)
    sender_kind = Column(String(16), nullable=False, default="system",
                         server_default="system")
    sender = Column(String(64), nullable=False, default="", server_default="")
    sender_name = Column(String(64), nullable=False, default="",
                         server_default="")
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False, default="", server_default="")
    url = Column(String(255), nullable=False, default="", server_default="")
    scope = Column(String(24), nullable=False, default="manual",
                   server_default="manual")
    ref_type = Column(String(24), nullable=False, default="", server_default="")
    ref_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class BdMessageRecipient(Base):
    """消息收件人（**每个收件人一行**，已读各自独立）。"""
    __tablename__ = "bd_message_recipient"
    __table_args__ = (
        UniqueConstraint("message_id", "person_code",
                         name="uq_bd_message_recipient"),
        Index("ix_bd_message_recipient_person", "person_code", "read_at"),
    )
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("bd_message.id", ondelete="CASCADE"),
                        nullable=False)
    person_code = Column(String(64), nullable=False)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class BdZoneRelease(Base):
    __tablename__ = 'bd_zone_release'
    id = Column(Integer, primary_key=True)
    fingerprint = Column(String(64), nullable=False, unique=True)
    source = Column(JSON, nullable=False)
    parameters = Column(JSON, nullable=False)
    audit = Column(JSON, nullable=False)
    reviews = Column(JSON, nullable=False)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)


class BdRegion(Base):
    __tablename__ = 'bd_region'
    __table_args__ = (UniqueConstraint('release_id','source_id',name='uq_bd_region_release_source'),)
    id = Column(Integer, primary_key=True)
    release_id = Column(Integer, ForeignKey('bd_zone_release.id'), nullable=False, index=True)
    source_id = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    geometry = Column(JSON, nullable=False)
    merge_boundaries = Column(Boolean, nullable=False, default=True, server_default=text('true'))
    area_km2 = Column(Float, nullable=False)
    store_count = Column(Integer, nullable=False)
    workload = Column(Float)
    review = Column(JSON, nullable=False)
    state = Column(String(16), nullable=False, default='ready')
    team_id = Column(Integer, ForeignKey('bd_team.id'), index=True)
    leader_id = Column(Integer, ForeignKey('users.id'))
    revision = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=_now)


# 保持原作业端接口兼容；新片区持久化仅使用bd_region表。
BdZone = BdRegion


class BdWorkGrid(Base):
    __tablename__ = 'bd_work_grid'
    __table_args__ = (UniqueConstraint('zone_id','source_id',name='uq_bd_grid_source'),
                     UniqueConstraint('release_id','source_id',name='uq_bd_grid_release_source'))
    id = Column(Integer, primary_key=True)
    release_id = Column(Integer, ForeignKey('bd_zone_release.id'), nullable=False, index=True)
    zone_id = Column(Integer, ForeignKey('bd_region.id'), nullable=True, index=True)
    source_id = Column(String(64), nullable=False)
    geometry = Column(JSON, nullable=False)
    area_km2 = Column(Float, nullable=False)
    store_count = Column(Integer, nullable=False)
    state = Column(String(16), nullable=False, default='ready')
    assignee_id = Column(Integer, ForeignKey('users.id'), index=True)
    progress = Column(Integer, nullable=False, default=0)
    task_state = Column(String(16), nullable=False, default='unvisited')
    revision = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdGridStore(Base):
    __tablename__ = 'bd_grid_store'
    __table_args__ = (UniqueConstraint('release_id','source_id',name='uq_bd_grid_store_release_source'),)
    id = Column(Integer, primary_key=True)
    release_id = Column(Integer, ForeignKey('bd_zone_release.id'), nullable=False)
    grid_id = Column(Integer, ForeignKey('bd_work_grid.id'), nullable=False, index=True)
    source_id = Column(String(128), nullable=False)


class BdWorkStore(Base):
    __tablename__ = 'bd_work_store'
    id = Column(String(128), primary_key=True)
    name = Column(Text, nullable=False)
    address = Column(Text, nullable=False, default='')
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    status = Column(String(16), nullable=False, default='normal')
    revision = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=_now)


class BdStoreVisitLog(Base):
    __tablename__ = 'bd_store_visit_log'
    id = Column(Integer, primary_key=True)
    request_id = Column(String(64), nullable=False, unique=True)
    store_id = Column(String(128), ForeignKey('bd_work_store.id'), nullable=False, index=True)
    grid_id = Column(Integer, ForeignKey('bd_work_grid.id'), nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    operation = Column(String(32), nullable=False)
    before = Column(JSON, nullable=False)
    after = Column(JSON, nullable=False)
    note = Column(Text, nullable=False, default='')
    created_at = Column(DateTime, nullable=False, default=_now)
