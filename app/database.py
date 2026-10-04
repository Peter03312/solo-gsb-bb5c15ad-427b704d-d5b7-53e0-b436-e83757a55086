"""数据库引擎与会话。"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

# SQLite: 默认 DEFERRED 事务，确认方案时需要 IMMEDIATE 立即拿写锁，
# 以保证并发确认不会产生冲突的已确认槽位占用。
if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # 并发确认时第二个写连接等待写锁（毫秒），避免立刻 SQLITE_BUSY
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations() -> None:
    """对已存在的 SQLite 库幂等补齐后续版本新增列（不做破坏性变更）。

    新库由 ``create_all`` 直接建出最新结构；只有历史库需要 ALTER TABLE 补列。
    每次更正的版本链与原始事件留档列在此补齐。
    """
    if not settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    inspector = inspect(engine)

    def columns(table: str) -> set[str]:
        if table not in inspector.get_table_names():
            return set()
        return {c["name"] for c in inspector.get_columns(table)}

    added_actuals = columns("loading_actuals")
    with engine.begin() as conn:
        if added_actuals and "version" not in added_actuals:
            # 历史库：已有实绩均视为第 1 版（尚未发生过更正）
            conn.execute(
                text("ALTER TABLE loading_actuals ADD COLUMN version INTEGER "
                     "NOT NULL DEFAULT 1")
            )

    added_events = columns("loading_actual_events")
    with engine.begin() as conn:
        if added_events:
            if "orig_minute" not in added_events:
                conn.execute(
                    text("ALTER TABLE loading_actual_events ADD COLUMN orig_minute "
                         "INTEGER NOT NULL DEFAULT 0")
                )
                # 已登记事件的原始分钟即其现行分钟
                conn.execute(
                    text("UPDATE loading_actual_events SET orig_minute = minute")
                )
            if "orig_crane_no" not in added_events:
                conn.execute(
                    text("ALTER TABLE loading_actual_events ADD COLUMN orig_crane_no "
                         "INTEGER NOT NULL DEFAULT 1")
                )
                conn.execute(
                    text("UPDATE loading_actual_events SET orig_crane_no = crane_no")
                )
            if "corrected" not in added_events:
                conn.execute(
                    text("ALTER TABLE loading_actual_events ADD COLUMN corrected "
                         "BOOLEAN NOT NULL DEFAULT 0")
                )

    # 卸箱执行：区间版本列与生效区间表为后续版本新增。
    # 新表由 create_all 直接建出；历史库只需补列并把冻结原定区间回填为
    # 区间版本 0 的生效区间（尚未开始的执行无冻结区间，无需回填）。
    added_exec = columns("unloading_executions")
    with engine.begin() as conn:
        if added_exec and "window_version" not in added_exec:
            conn.execute(
                text("ALTER TABLE unloading_executions ADD COLUMN window_version "
                     "INTEGER NOT NULL DEFAULT 0")
            )
    if added_exec and "unloading_effective_ports" in inspector.get_table_names():
        # 历史库的生效区间表刚由 create_all 建出且为空：把每个已开始执行的
        # 冻结原定区间回填为区间版本 0 的生效区间。
        with engine.begin() as conn:
            has_any = conn.execute(
                text("SELECT 1 FROM unloading_effective_ports LIMIT 1")
            ).first()
            if has_any is None:
                conn.execute(
                    text(
                        "INSERT INTO unloading_effective_ports "
                        "(execution_id, discharge_order, min_remaining_moment, "
                        "max_remaining_moment) "
                        "SELECT f.execution_id, f.discharge_order, "
                        "f.min_remaining_moment, f.max_remaining_moment "
                        "FROM unloading_frozen_ports f"
                    )
                )

    # 封签换签：方案级换签版本列为后续版本新增。
    # 新库由 create_all 直接建出；历史库补列，既有清单/凭证换签版本均为 0
    # （尚未发生过换签）。seal_replacements 新表同样由 create_all 建出。
    added_manifest = columns("seal_manifests")
    with engine.begin() as conn:
        if added_manifest and "seal_version" not in added_manifest:
            conn.execute(
                text("ALTER TABLE seal_manifests ADD COLUMN seal_version "
                     "INTEGER NOT NULL DEFAULT 0")
            )
    added_releases = columns("seal_releases")
    with engine.begin() as conn:
        if added_releases and "seal_version" not in added_releases:
            conn.execute(
                text("ALTER TABLE seal_releases ADD COLUMN seal_version "
                     "INTEGER NOT NULL DEFAULT 0")
            )
