"""ORM 模型：槽位资料修订、箱清单修订、方案修订与已确认占用。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


from app.database import Base  # noqa: E402

# 资料修订号计数器类别（revision_counters.category）
SLOT_REVISION_CATEGORY = "slots"
MANIFEST_REVISION_CATEGORY = "manifests"


class RevisionCounter(Base):
    """资料修订号计数器（每资料类别一行）。

    录入在事务首个语句即写本表（upsert 递增）：SQLite 写语句立即取得
    写锁，并发录入按实际提交先后串行化，每次成功提交取得唯一且递增的
    修订号；计数器递增与整份资料在同一事务提交/回滚，失败不留
    半份资料、也不烧号。
    """

    __tablename__ = "revision_counters"

    category: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_allocated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )


class SlotRevision(Base):
    __tablename__ = "slot_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    min_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_moment: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    slots: Mapped[list["SlotRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )
    adjacencies: Mapped[list["AdjacencyRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )
    incompatibilities: Mapped[list["IncompatRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("min_moment <= max_moment", name="ck_moment_range"),
        # 修订号唯一：按号查询/核算/确认始终指向同一份资料（兜底，
        # 并发唯一性由计数器在写锁内分配保证）
        UniqueConstraint("revision", name="uq_slot_revision_no"),
    )


class SlotRow(Base):
    __tablename__ = "slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity: Mapped[float] = mapped_column(Float, nullable=False)
    arm: Mapped[float] = mapped_column(Float, nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="slots")

    __table_args__ = (
        UniqueConstraint("revision_id", "code", name="uq_slot_code_in_rev"),
    )


class AdjacencyRow(Base):
    __tablename__ = "adjacencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    slot_a: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_b: Mapped[str] = mapped_column(String(64), nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="adjacencies")

    __table_args__ = (
        UniqueConstraint("revision_id", "slot_a", "slot_b", name="uq_adj_in_rev"),
    )


class IncompatRow(Base):
    __tablename__ = "incompatibilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("slot_revisions.id", ondelete="CASCADE"), index=True
    )
    class_a: Mapped[str] = mapped_column(String(64), nullable=False)
    class_b: Mapped[str] = mapped_column(String(64), nullable=False)

    rev: Mapped[SlotRevision] = relationship(back_populates="incompatibilities")

    __table_args__ = (
        UniqueConstraint("revision_id", "class_a", "class_b", name="uq_incompat_in_rev"),
    )


class ManifestRevision(Base):
    __tablename__ = "manifest_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    containers: Mapped[list["ContainerRow"]] = relationship(
        back_populates="rev", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # 修订号唯一：按号查询/核算/确认始终指向同一份资料（兜底，
        # 并发唯一性由计数器在写锁内分配保证）
        UniqueConstraint("revision", name="uq_manifest_revision_no"),
    )


class ContainerRow(Base):
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("manifest_revisions.id", ondelete="CASCADE"), index=True
    )
    no: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    hazmat: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)

    rev: Mapped[ManifestRevision] = relationship(back_populates="containers")

    __table_args__ = (
        UniqueConstraint("revision_id", "no", name="uq_container_no_in_rev"),
        CheckConstraint("weight >= 0", name="ck_container_weight"),
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # 当前修订所基于的资料
    slots_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    total_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 修订链
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL"), nullable=True
    )
    root_id: Mapped[str] = mapped_column(String(64), nullable=False)

    items: Mapped[list["PlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_plans_root", "root_id"),
        Index("ix_plans_status", "status"),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'superseded')",
            name="ck_plan_status",
        ),
    )


class PlanItem(Base):
    __tablename__ = "plan_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)

    plan: Mapped[Plan] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("plan_id", "container_no", name="uq_item_container_in_plan"),
    )


class ConfirmedSlot(Base):
    """已确认方案对槽位的占用。

    唯一索引保证：并发确认时同一槽位不可能被两个已确认方案占用。
    """

    __tablename__ = "confirmed_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("slot_code", name="uq_confirmed_slot"),
    )


class LoadingSchedule(Base):
    """绑定某个方案修订的装船时间表。

    只允许对已确认方案保存；方案修订被新修订取代（superseded）后，
    其排程仍可查询但状态转为 historical，不再列为当前排程。
    """

    __tablename__ = "loading_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    crane_count: Mapped[int] = mapped_column(Integer, nullable=False)
    makespan: Mapped[int] = mapped_column(Integer, nullable=False)
    # current=绑定方案仍为当前已确认修订；historical=方案修订已被取代
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    items: Mapped[list["LoadingScheduleItem"]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint("crane_count > 0", name="ck_schedule_cranes"),
        CheckConstraint("makespan >= 0", name="ck_schedule_makespan"),
        CheckConstraint(
            "status IN ('current', 'historical')", name="ck_schedule_status"
        ),
    )


class LoadingScheduleItem(Base):
    """装船时间表中单箱安排：开工/完工分钟与岸桥编号（自 1 起）。"""

    __tablename__ = "loading_schedule_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("loading_schedules.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    finish_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    crane_no: Mapped[int] = mapped_column(Integer, nullable=False)

    schedule: Mapped[LoadingSchedule] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("schedule_id", "container_no",
                         name="uq_schedule_item_container"),
    )


class UnloadingSequence(Base):
    """绑定某个方案修订的逐港卸箱顺序。

    只允许对已确认方案保存；方案修订被新修订取代（superseded）后，
    其卸箱顺序仍可查询但状态转为 historical，不再列为当前卸箱顺序。
    """

    __tablename__ = "unloading_sequences"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    initial_moment: Mapped[float] = mapped_column(Float, nullable=False)
    final_moment: Mapped[float] = mapped_column(Float, nullable=False)
    # current=绑定方案仍为当前已确认修订；historical=方案修订已被取代
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    steps: Mapped[list["UnloadingStepRow"]] = relationship(
        back_populates="sequence", cascade="all, delete-orphan"
    )
    ports: Mapped[list["UnloadingPortRow"]] = relationship(
        back_populates="sequence", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint(
            "status IN ('current', 'historical')", name="ck_unloading_status"
        ),
    )


class UnloadingPortRow(Base):
    """请求中各卸港序号的允许剩余总力矩闭区间（随顺序留档）。"""

    __tablename__ = "unloading_ports"

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_sequences.id", ondelete="CASCADE"), index=True
    )
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)
    min_remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)

    sequence: Mapped[UnloadingSequence] = relationship(back_populates="ports")

    __table_args__ = (
        UniqueConstraint("sequence_id", "discharge_order",
                         name="uq_unloading_port_order"),
    )


class UnloadingStepRow(Base):
    """卸箱顺序中的一步：港序、箱号与卸后剩余总力矩（含槽位快照）。"""

    __tablename__ = "unloading_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    sequence_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_sequences.id", ondelete="CASCADE"), index=True
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    arm: Mapped[float] = mapped_column(Float, nullable=False)
    remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)

    sequence: Mapped[UnloadingSequence] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("sequence_id", "step_no", name="uq_unloading_step_no"),
        UniqueConstraint("sequence_id", "container_no",
                         name="uq_unloading_step_container"),
    )


class UnloadingExecution(Base):
    """码头逐港卸箱执行进度（每个卸箱顺序至多一条，即每方案至多一条）。

    首次现场登记（POST .../unloading/execute）时在写锁内创建，并**原子冻结**
    当时的当前卸箱顺序及所用箱重、力臂与各港力矩区间（复制到
    UnloadingFrozenBox/UnloadingFrozenPort）：此后同一方案修订重排原计划
    （unloading_sequences 原子替换）不改已卸记录与后续核算；方案修订被新
    修订取代后进度只读但仍可查询。progress_version 首次为 0，每成功登记
    一箱递增 1，并发登记以 expected_version 原子核对。
    """

    __tablename__ = "unloading_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # 冻结时所绑定卸箱顺序 id，仅作溯源；顺序被重排删除后可能悬空，故不加外键
    frozen_sequence_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 冻结时刻原卸箱计划的全程箱号顺序（JSON 文本，箱号列表）：
    # 现场允许改选其他合法箱，但查询仍给出“原计划下一步”作为对照
    frozen_plan_order: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    initial_moment: Mapped[float] = mapped_column(Float, nullable=False)
    # 当前剩余总力矩（每登记一箱更新）；全部卸完恒为 0
    remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)
    # 进度版本：创建时为 0，每成功登记一箱 +1
    progress_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 区间版本：创建时为 0（=冻结的原定区间），每次区间调整申请生效后 +1
    window_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    boxes: Mapped[list["UnloadingFrozenBox"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    ports: Mapped[list["UnloadingFrozenPort"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    effective_ports: Mapped[list["UnloadingEffectivePort"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    adjustments: Mapped[list["UnloadingWindowAdjustment"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    steps: Mapped[list["UnloadingExecStep"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    operations: Mapped[list["UnloadingExecOperation"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint("progress_version >= 0", name="uq_exec_version_nonneg"),
        CheckConstraint("window_version >= 0", name="uq_exec_window_version_nonneg"),
    )


class UnloadingFrozenBox(Base):
    """首次登记时冻结的全程逐箱快照：箱重、槽位力臂、堆栈/层与卸港序号。"""

    __tablename__ = "unloading_frozen_boxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_executions.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    arm: Mapped[float] = mapped_column(Float, nullable=False)
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)

    execution: Mapped[UnloadingExecution] = relationship(back_populates="boxes")

    __table_args__ = (
        UniqueConstraint("execution_id", "container_no",
                         name="uq_frozen_box_container"),
    )


class UnloadingFrozenPort(Base):
    """首次登记时冻结的各卸港允许剩余总力矩闭区间（原定区间，不再随后续重排改变）。"""

    __tablename__ = "unloading_frozen_ports"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_executions.id", ondelete="CASCADE"), index=True
    )
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)
    min_remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)

    execution: Mapped[UnloadingExecution] = relationship(back_populates="ports")

    __table_args__ = (
        UniqueConstraint("execution_id", "discharge_order",
                         name="uq_frozen_port_order"),
    )


class UnloadingEffectivePort(Base):
    """各卸港当前**生效**的允许剩余总力矩闭区间。

    首次登记时按冻结的原定区间初始化（区间版本 0）；此后每次区间调整申请
    生效即在同一事务内原子替换为调整后区间（区间版本 +1）。已卸步骤的
    力矩核算与原冻结区间（UnloadingFrozenPort）均保留不改；后续卸箱登记
    与剩余推荐顺序一律使用本表的生效区间。
    """

    __tablename__ = "unloading_effective_ports"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_executions.id", ondelete="CASCADE"), index=True
    )
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)
    min_remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)
    max_remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)

    execution: Mapped[UnloadingExecution] = relationship(
        back_populates="effective_ports"
    )

    __table_args__ = (
        UniqueConstraint("execution_id", "discharge_order",
                         name="uq_effective_port_order"),
    )


class UnloadingWindowAdjustment(Base):
    """收紧剩余力矩安全区间的调整申请留档（每次生效一条，构成调整历史）。

    申请号全局唯一：同号同内容重试返回首次结果快照（200），同号异内容冲突
    （409）。期望进度版本/区间版本与当前不一致（过期或并发已推进）、方案已
    被取代、已完成或未开始均拒绝且不生效。
    """

    __tablename__ = "unloading_window_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_executions.id", ondelete="CASCADE"), index=True
    )
    application_no: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    # 申请时期望的进度版本与区间版本（须与当时当前值一致）
    expected_progress_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_window_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 生效后的新区间版本（= 申请时区间版本 + 1）
    new_window_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 申请中列出的调整区间（JSON 文本，按卸港序号升序的 {order,min,max} 列表）
    windows_json: Mapped[str] = mapped_column(Text, nullable=False)
    # 首次生效后的完整执行视图快照（JSON 文本），供同申请号重试幂等返回
    response_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    # 申请内容指纹（application_no+期望版本+区间列表规范 JSON 的 SHA-256）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    execution: Mapped[UnloadingExecution] = relationship(back_populates="adjustments")


class UnloadingExecStep(Base):
    """已卸一步：自 1 起的步序、所属卸港序号、箱号与卸后剩余总力矩（含快照）。"""

    __tablename__ = "unloading_exec_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_executions.id", ondelete="CASCADE"), index=True
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    discharge_order: Mapped[int] = mapped_column(Integer, nullable=False)
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_code: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    arm: Mapped[float] = mapped_column(Float, nullable=False)
    remaining_moment: Mapped[float] = mapped_column(Float, nullable=False)
    unloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    execution: Mapped[UnloadingExecution] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("execution_id", "step_no", name="uq_exec_step_no"),
        UniqueConstraint("execution_id", "container_no",
                         name="uq_exec_step_container"),
    )


class UnloadingExecOperation(Base):
    """现场登记操作台账：操作号全局唯一，用于重试幂等与异内容冲突识别。

    同操作号同内容重试优先返回首次结果（response_snapshot 定格在首次登记后
    的完整执行视图）；同操作号异内容冲突（409）。
    """

    __tablename__ = "unloading_exec_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("unloading_executions.id", ondelete="CASCADE"), index=True
    )
    operation_no: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 请求内容指纹（operation_no+container_no+expected_version 规范 JSON 的 SHA-256）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 首次登记成功后的完整执行视图快照（JSON 文本），供同操作号重试幂等返回
    response_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    execution: Mapped[UnloadingExecution] = relationship(back_populates="operations")


class LoadingActuals(Base):
    """某方案修订的装船实绩登记头（每方案至多一条）。

    首次登记实绩时原子冻结当时绑定的装船排程作为**偏差基线**（行级快照，
    复制到 LoadingActualBaselineItem）；此后排程被替换（同一方案修订重排）
    或方案被新修订取代，均不改变已冻结基线与已登记实绩。
    方案修订被取代后实绩只读：只可查询，不再接受登记。
    """

    __tablename__ = "loading_actuals"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # 冻结时刻所绑定排程的岸桥数量（排程删除后仍保留）
    baseline_crane_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 冻结时排程 id，仅作溯源；排程行被替换删除后可能悬空，故不加外键
    baseline_schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baseline_frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    # 实绩版本：首次登记为 1，每次成功更正递增 1（更正重放非法则不递增）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    events: Mapped[list["LoadingActualEvent"]] = relationship(
        back_populates="actuals", cascade="all, delete-orphan"
    )
    baseline_items: Mapped[list["LoadingActualBaselineItem"]] = relationship(
        back_populates="actuals", cascade="all, delete-orphan"
    )
    corrections: Mapped[list["LoadingActualCorrection"]] = relationship(
        back_populates="actuals", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint("baseline_crane_count > 0", name="ck_actuals_cranes"),
        CheckConstraint("version >= 1", name="ck_actuals_version"),
    )


class LoadingActualBaselineItem(Base):
    """冻结排程的逐箱快照：基线开工/完工分钟与岸桥编号。"""

    __tablename__ = "loading_actual_baseline_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    actuals_id: Mapped[int] = mapped_column(
        ForeignKey("loading_actuals.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    finish_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    crane_no: Mapped[int] = mapped_column(Integer, nullable=False)

    actuals: Mapped[LoadingActuals] = relationship(back_populates="baseline_items")

    __table_args__ = (
        UniqueConstraint("actuals_id", "container_no",
                         name="uq_actual_baseline_container"),
    )


class LoadingActualEvent(Base):
    """逐箱实际开工/完工事件（只追加，事件流水号在方案内唯一）。

    事件允许晚到；服务按 (实际分钟, 同分钟完工先于开工, 到达先后) 重放。
    到达先后由自增 id 体现；任一事件使整条重放链非法则整次登记拒绝，
    已落库事件保持不变。
    """

    __tablename__ = "loading_actual_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    actuals_id: Mapped[int] = mapped_column(
        ForeignKey("loading_actuals.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(8), nullable=False)  # start/finish
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    crane_no: Mapped[int] = mapped_column(Integer, nullable=False)
    minute: Mapped[int] = mapped_column(Integer, nullable=False)
    # 首次登记时的原始分钟/岸桥：原登记接口始终据此识别同流水号重试与冲突；
    # 更正只改 minute/crane_no，这两列保留原值用于留档。未被更正时与现行值相同。
    orig_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orig_crane_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    corrected: Mapped[bool] = mapped_column(
        # SQLite 无原生布尔，以 0/1 存储
        Boolean, nullable=False, default=False
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    actuals: Mapped[LoadingActuals] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("actuals_id", "seq", name="uq_actual_event_seq"),
        CheckConstraint("event_type IN ('start', 'finish')",
                        name="ck_actual_event_type"),
        CheckConstraint("crane_no >= 1", name="ck_actual_event_crane"),
        CheckConstraint("minute >= 0", name="ck_actual_event_minute"),
        CheckConstraint("orig_crane_no >= 1", name="ck_actual_event_orig_crane"),
        CheckConstraint("orig_minute >= 0", name="ck_actual_event_orig_minute"),
    )


class LoadingActualCorrection(Base):
    """一次已成功生效的实绩更正（方案内 request_id 唯一，支持幂等重试）。

    更正只允许改变某条已登记事件的 minute 或 crane_no：替代事件沿用原流水号、
    事件类型与箱号。每次成功更正把实绩 version 加 1，并把首次更正的完整查询
    结果序列化存入 response_snapshot；同 request_id 同内容重试（即便 version
    已被后续更正推进）直接返回该快照、不重复生效。
    """

    __tablename__ = "loading_actual_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    actuals_id: Mapped[int] = mapped_column(
        ForeignKey("loading_actuals.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(8), nullable=False)
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    old_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    new_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    old_crane_no: Mapped[int] = mapped_column(Integer, nullable=False)
    new_crane_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 首次更正成功后的完整 ActualsOut 快照（JSON 文本），供同请求号重试幂等返回
    response_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    actuals: Mapped[LoadingActuals] = relationship(back_populates="corrections")

    __table_args__ = (
        UniqueConstraint("actuals_id", "request_id",
                         name="uq_actual_correction_request"),
    )


class LoadingHandover(Base):
    """装船完结交接时签发的**不可变凭证**头（交接号全局唯一）。

    仅对已确认（confirmed）、已有登记实绩，且每箱均在截止分钟前完工、截止后无
    有效事件、逐箱相对冻结排程基线的完工延误均不超过阈值的方案签发。签发时在
    写事务内复核方案仍已确认、实绩版本与有效事件（指纹）未变；凭证内容（方案
    修订、各箱实际起止与岸桥、冻结基线、偏差、截止分钟、实绩指纹）整体冻结，
    此后实绩更正/补登不改凭证，仅在查询时标示是否仍对应当前实绩。
    """

    __tablename__ = "loading_handovers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    handover_no: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    cutoff_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_actual_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_version: Mapped[int] = mapped_column(Integer, nullable=False)
    max_finish_delay_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_observed_finish_delay: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    container_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 签发时各有效流水号（升序，JSON 文本）
    valid_seqs: Mapped[str] = mapped_column(Text, nullable=False)
    # 签发时实绩指纹（规范 SHA-256），查询时与当前实绩指纹比对
    actuals_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    items: Mapped[list["LoadingHandoverItem"]] = relationship(
        back_populates="handover", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint("cutoff_minute >= 0", name="ck_handover_cutoff"),
        CheckConstraint("expected_actual_version >= 1",
                        name="ck_handover_expected_version"),
        CheckConstraint("actual_version >= 1", name="ck_handover_actual_version"),
        CheckConstraint("max_finish_delay_minutes >= 0",
                        name="ck_handover_max_delay"),
        CheckConstraint("max_observed_finish_delay >= 0",
                        name="ck_handover_observed_delay"),
        CheckConstraint("container_count >= 0", name="ck_handover_container_count"),
        Index("ix_loading_handovers_plan", "plan_id"),
    )


class LoadingHandoverItem(Base):
    """凭证逐箱快照：实际起止与岸桥、冻结基线及完工延误偏差（签发后不可变）。"""

    __tablename__ = "loading_handover_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    handover_id: Mapped[str] = mapped_column(
        ForeignKey("loading_handovers.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    stack: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_finish_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_crane_no: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_finish_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_crane_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_delta_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    finish_delta_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    crane_changed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    handover: Mapped[LoadingHandover] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("handover_id", "container_no",
                         name="uq_handover_item_container"),
    )


class SealManifest(Base):
    """某方案修订的预期封签清单头（每方案至多一份，冻结后不可改写）。

    首次登记时在写锁内原子冻结：清单须覆盖方案全部箱且封签号互异；此后同
    方案重复登记同内容幂等返回原清单，异内容冲突（409），清单内容不再改
    写。scan_version 为运行元数据（不属于冻结内容）：每成功登记一条新封签
    扫描 +1，供放行申请按期望扫描版本原子核对。
    """

    __tablename__ = "seal_manifests"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    container_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 清单内容指纹（逐箱 箱号+预期封签号 规范 JSON 的 SHA-256）：
    # 同内容重试/异内容冲突识别
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 扫描版本：0=尚无扫描；每登记一条新扫描 +1（运行元数据，非清单内容）
    scan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 换签版本（方案级）：0=尚未换签；每成功登记一次换签 +1（运行元数据，非冻结内容）
    seal_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    items: Mapped[list["SealManifestItem"]] = relationship(
        back_populates="manifest", cascade="all, delete-orphan"
    )
    replacements: Mapped[list["SealReplacement"]] = relationship(
        back_populates="manifest", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint("container_count >= 0", name="ck_seal_manifest_count"),
        CheckConstraint("scan_version >= 0", name="ck_seal_manifest_scan_version"),
        CheckConstraint("seal_version >= 0", name="ck_seal_manifest_seal_version"),
    )


class SealManifestItem(Base):
    """预期封签清单条目：箱号 -> 预期封签号（清单内箱号唯一、封签号互异）。"""

    __tablename__ = "seal_manifest_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    manifest_id: Mapped[int] = mapped_column(
        ForeignKey("seal_manifests.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_seal_no: Mapped[str] = mapped_column(String(128), nullable=False)

    manifest: Mapped[SealManifest] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("manifest_id", "container_no",
                         name="uq_seal_item_container"),
        UniqueConstraint("manifest_id", "expected_seal_no",
                         name="uq_seal_item_seal"),
    )


class SealScan(Base):
    """现场封签扫描记录（只追加；扫描号全局唯一用于幂等与冲突识别）。

    封签不符的扫描同样留痕（matched=0）但不计为通过；同箱有效扫描取检查
    分钟较晚者，同分钟取登记顺序（自增 id）较晚者。response_snapshot 定格
    首次登记响应，供同扫描号同内容重试幂等返回。
    """

    __tablename__ = "seal_scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    manifest_id: Mapped[int] = mapped_column(
        ForeignKey("seal_manifests.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    scan_no: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_seal_no: Mapped[str] = mapped_column(String(128), nullable=False)
    # 实见封签号
    seal_no: Mapped[str] = mapped_column(String(128), nullable=False)
    check_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 请求内容指纹（方案+扫描号+箱号+实见封签号+检查分钟 规范 JSON 的 SHA-256）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 首次登记响应快照（JSON 文本），供同扫描号同内容重试幂等返回
    response_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("check_minute >= 0", name="ck_seal_scan_minute"),
        Index("ix_seal_scans_manifest_container", "manifest_id", "container_no"),
    )


class SealRelease(Base):
    """离港放行凭证头（申请号全局唯一；签发后内容不可变）。

    仅当方案仍已确认、所给交接凭证仍对应当前实绩、每箱最新扫描均匹配预期
    且不晚于截止分钟时，在写事务内原子签发。凭证固化：方案修订号、所用交
    接凭证、截止分钟、扫描版本与扫描/实绩指纹、逐箱核对结果。签发后的后
    续扫描、实绩更正或方案取代不改凭证内容，仅在查询时以 is_current /
    invalid_reasons 标示失效。
    """

    __tablename__ = "seal_releases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_no: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # 签发所依据的交接凭证（留档溯源；交接凭证不删除，故不加外键）
    handover_id: Mapped[str] = mapped_column(String(64), nullable=False)
    handover_no: Mapped[str] = mapped_column(String(128), nullable=False)
    cutoff_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_scan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 签发时方案级换签版本（0=签发前未换签）
    seal_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    container_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_check_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    # 签发时扫描状态指纹与实绩指纹（规范 SHA-256），查询时与当前值比对标示失效
    scans_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    actuals_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    items: Mapped[list["SealReleaseItem"]] = relationship(
        back_populates="release", cascade="all, delete-orphan"
    )
    plan: Mapped[Plan] = relationship()

    __table_args__ = (
        CheckConstraint("cutoff_minute >= 0", name="ck_seal_release_cutoff"),
        CheckConstraint("expected_scan_version >= 0",
                        name="ck_seal_release_expected"),
        CheckConstraint("scan_version >= 0", name="ck_seal_release_scan_version"),
        CheckConstraint("seal_version >= 0", name="ck_seal_release_seal_version"),
        CheckConstraint("container_count >= 0", name="ck_seal_release_count"),
        CheckConstraint("max_check_minute >= 0", name="ck_seal_release_max_check"),
        Index("ix_seal_releases_plan", "plan_id"),
    )


class SealReleaseItem(Base):
    """放行凭证逐箱核对结果快照（签发后不可变）。"""

    __tablename__ = "seal_release_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("seal_releases.id", ondelete="CASCADE"), index=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_seal_no: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_seal_no: Mapped[str] = mapped_column(String(128), nullable=False)
    check_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    scan_no: Mapped[str] = mapped_column(String(128), nullable=False)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False)

    release: Mapped[SealRelease] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("release_id", "container_no",
                         name="uq_seal_release_item_container"),
    )


class SealReplacement(Base):
    """封签损坏后的逐箱换签登记（只追加；申请号全局唯一用于幂等与冲突识别）。

    原预期清单与历史扫描不可改：每次换签在此追加一条，形成该箱换签链，该箱
    当前预期封签号取链上最后一条的 new_seal_no。申请须携带期望换签版本
    （方案级，初值 0，每成功换签 +1）在写锁内原子核对。scan_id_at_replace
    记录换签登记瞬间该箱已存在扫描的最大 id（无扫描为 0）：放行时该箱只有
    id 更大且检查分钟不早于换签分钟的新登记扫描才有资格胜出。
    response_snapshot 定格首次登记响应，供同申请号同内容重试幂等返回。
    """

    __tablename__ = "seal_replacements"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    manifest_id: Mapped[int] = mapped_column(
        ForeignKey("seal_manifests.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    replacement_no: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    container_no: Mapped[str] = mapped_column(String(64), nullable=False)
    old_seal_no: Mapped[str] = mapped_column(String(128), nullable=False)
    new_seal_no: Mapped[str] = mapped_column(String(128), nullable=False)
    replace_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    # 申请人提交的期望换签版本（即换签前版本；成功后方案换签版本 +1）
    expected_seal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 本次登记后的方案级换签版本
    seal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 换签登记瞬间该箱已存在扫描的最大 id（无扫描为 0）
    scan_id_at_replace: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 请求内容指纹（方案+申请号+箱号+原号+新号+换签分钟+原因+期望版本）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 首次登记响应快照（JSON 文本），供同申请号同内容重试幂等返回
    response_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    manifest: Mapped[SealManifest] = relationship(back_populates="replacements")

    __table_args__ = (
        CheckConstraint("replace_minute >= 0", name="ck_seal_repl_minute"),
        CheckConstraint("expected_seal_version >= 0",
                        name="ck_seal_repl_expected_version"),
        CheckConstraint("seal_version >= 1", name="ck_seal_repl_version"),
        CheckConstraint("scan_id_at_replace >= 0", name="ck_seal_repl_scan_id"),
        CheckConstraint("old_seal_no <> new_seal_no", name="ck_seal_repl_diff"),
        Index("ix_seal_replacements_container", "manifest_id", "container_no"),
    )
