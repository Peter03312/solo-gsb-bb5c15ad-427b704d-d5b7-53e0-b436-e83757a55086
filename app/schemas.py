"""Pydantic 模式与录入校验。"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, Field, field_validator, model_validator

_SLOT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
_STACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


def _require_nonempty_str(v: str, label: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{label}不能为空")
    return v.strip()


def _is_finite(v: float) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


# ---------- 录入：槽位资料 ----------


class SlotIn(BaseModel):
    code: str = Field(..., description="槽位编号，如 S0101")
    stack: str = Field(..., description="堆栈编号")
    tier: int = Field(..., ge=1, description="层编号，自底向上从 1 开始")
    capacity: float = Field(..., description="槽位承重（重量单位）")
    arm: float = Field(..., description="纵向力臂")

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        v = _require_nonempty_str(v, "槽位编号")
        if not _SLOT_CODE_RE.match(v):
            raise ValueError("槽位编号只能含字母、数字、下划线或连字符，且以字母/数字开头")
        return v

    @field_validator("stack")
    @classmethod
    def _stack(cls, v: str) -> str:
        v = _require_nonempty_str(v, "堆栈编号")
        if not _STACK_RE.match(v):
            raise ValueError("堆栈编号只能含字母、数字、下划线或连字符，且以字母/数字开头")
        return v

    @field_validator("capacity")
    @classmethod
    def _capacity(cls, v: float) -> float:
        if v < 0:
            raise ValueError("槽位承重不得为负数（非法承重）")
        return v


class SlotDataIn(BaseModel):
    slots: list[SlotIn] = Field(..., min_length=1)
    adjacency: list[list[str]] = Field(
        default_factory=list, description="相邻槽位编号对（无向），如 [['S0101','S0201']]"
    )
    incompatible_classes: list[list[str]] = Field(
        default_factory=list, description="不相容危险类别对，如 [['1','5']]"
    )
    min_moment: float = Field(..., description="全船允许总力矩下限")
    max_moment: float = Field(..., description="全船允许总力矩上限")

    @model_validator(mode="after")
    def _validate(self) -> "SlotDataIn":
        if self.min_moment > self.max_moment:
            raise ValueError("允许力矩区间下限不得大于上限")

        codes = [s.code for s in self.slots]
        if len(set(codes)) != len(codes):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            raise ValueError(f"槽位编号重复：{', '.join(dup)}")
        code_set = set(codes)

        for pair in self.adjacency:
            if len(pair) != 2:
                raise ValueError("相邻关系必须是两个槽位编号组成的对")
            a, b = pair
            if a == b:
                raise ValueError(f"槽位 {a} 不能与自身相邻")
            missing = [c for c in (a, b) if c not in code_set]
            if missing:
                raise ValueError(
                    f"相邻关系引用了不存在的槽位 {missing[0]}（无效槽位）"
                )

        norm_adj = {tuple(sorted(p)) for p in self.adjacency}
        if len(norm_adj) != len(self.adjacency):
            raise ValueError("相邻关系存在重复对")

        for pair in self.incompatible_classes:
            if len(pair) != 2:
                raise ValueError("不相容危险类别必须是两个类别组成的对")
            a, b = (p.strip() for p in pair)
            if not a or not b:
                raise ValueError("危险类别不能为空")
        norm_inc = {tuple(sorted((p[0].strip(), p[1].strip()))) for p in self.incompatible_classes}
        if len(norm_inc) != len(self.incompatible_classes):
            raise ValueError("不相容危险类别对存在重复")
        return self


# ---------- 录入：箱清单 ----------


class ContainerIn(BaseModel):
    no: str = Field(..., description="箱号")
    weight: float = Field(..., description="重量（重量单位）")
    hazmat: str | None = Field(None, description="危险类别，普通箱为 null")
    discharge_order: int = Field(..., ge=1, description="卸港先后序号，小者先卸")

    @field_validator("no")
    @classmethod
    def _no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @field_validator("weight")
    @classmethod
    def _weight(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("箱重量必须大于 0（非法重量）")
        return v

    @field_validator("hazmat")
    @classmethod
    def _hazmat(cls, v):
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError("危险类别为空字符串时请使用 null")
        return v.strip()


class ManifestIn(BaseModel):
    containers: list[ContainerIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> "ManifestIn":
        nos = [c.no for c in self.containers]
        if len(set(nos)) != len(nos):
            dup = sorted({n for n in nos if nos.count(n) > 1})
            raise ValueError(f"箱号重复：{', '.join(dup)}")
        return self


# ---------- 方案 ----------


class PlanComputeIn(BaseModel):
    slots_revision: int | None = Field(None, description="槽位资料修订号；缺省取最新")
    manifest_revision: int | None = Field(None, description="箱清单修订号；缺省取最新")


class PlanAmendIn(BaseModel):
    slots_revision: int | None = Field(None)
    manifest_revision: int | None = Field(None)


class PlanConfirmIn(BaseModel):
    slots_revision: int = Field(..., description="确认时核对的槽位资料修订号")
    manifest_revision: int = Field(..., description="确认时核对的箱清单修订号")


# ---------- 应急改配 ----------


class RestowCreateIn(BaseModel):
    blocked_slots: list[str] = Field(
        ...,
        min_length=1,
        description="临时封锁、不可占用的槽位编号列表；须均属于当前最新槽位资料修订，"
                    "不得为空或含重复编号",
    )

    @field_validator("blocked_slots")
    @classmethod
    def _blocked_slots(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("不可用槽位编号列表不能为空")
        cleaned: list[str] = []
        for code in v:
            if not isinstance(code, str) or not code.strip():
                raise ValueError("槽位编号不能为空")
            cleaned.append(code.strip())
        if len(set(cleaned)) != len(cleaned):
            dup = sorted({c for c in cleaned if cleaned.count(c) > 1})
            raise ValueError(f"槽位编号重复：{', '.join(dup)}")
        return cleaned


class RestowMoveOut(BaseModel):
    container_no: str
    from_slot_code: str = Field(..., description="原方案槽位编号")
    to_slot_code: str = Field(..., description="改配后槽位编号")
    from_stack: str
    to_stack: str
    from_tier: int
    to_tier: int


class RestowOut(BaseModel):
    plan_id: str = Field(..., description="新应急改配草稿方案 ID")
    revision_no: int = Field(..., description="草稿方案修订号（同方案链递增）")
    status: str
    root_id: str
    parent_id: str
    slots_revision: int = Field(..., description="改配所用最新槽位资料修订号")
    manifest_revision: int = Field(..., description="改配所用最新箱清单修订号")
    blocked_slots: list[str]
    moved_boxes: int = Field(..., description="相对原确认方案换槽的箱数")
    max_occupied_tier: int
    total_moment: float
    assignments: list[AssignmentOut]
    moves: list[RestowMoveOut]
    created_at: str


# ---------- 装船时间表 ----------


class ScheduleEntryIn(BaseModel):
    no: str = Field(..., description="箱号（须与方案箱项一致）")
    duration: int = Field(..., gt=0, description="该箱吊装时长（正整数分钟）")
    release_minute: int = Field(..., ge=0, description="最早开工分钟（非负整数）")
    deadline_minute: int = Field(..., ge=0, description="最迟完工分钟（非负整数，可恰在此时刻结束）")

    @field_validator("no")
    @classmethod
    def _no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @model_validator(mode="after")
    def _window(self) -> "ScheduleEntryIn":
        if self.release_minute > self.deadline_minute:
            raise ValueError(
                f"箱 {self.no} 的最早开工 {self.release_minute} 不得晚于最迟完工 "
                f"{self.deadline_minute}（时间窗倒置）"
            )
        return self


class ScheduleCreateIn(BaseModel):
    crane_count: int = Field(..., gt=0, description="岸桥数量（正整数）")
    entries: list[ScheduleEntryIn] = Field(..., description="每箱的吊装时长与时间窗，方案为空箱时传空列表")

    @model_validator(mode="after")
    def _unique(self) -> "ScheduleCreateIn":
        nos = [e.no for e in self.entries]
        if len(set(nos)) != len(nos):
            dup = sorted({n for n in nos if nos.count(n) > 1})
            raise ValueError(f"提交箱号重复：{', '.join(dup)}")
        return self


class ScheduleEntryOut(BaseModel):
    container_no: str
    start_minute: int
    finish_minute: int
    crane_no: int
    stack: str
    tier: int


class ScheduleOut(BaseModel):
    plan_id: str
    revision_no: int
    status: str = Field(..., description="current=当前排程；historical=方案修订已被取代")
    crane_count: int
    makespan: int
    entries: list[ScheduleEntryOut]
    created_at: str


# ---------- 逐港卸箱顺序 ----------


class UnloadingPortIn(BaseModel):
    discharge_order: int = Field(..., ge=1, description="卸港序号，须与方案箱清单完全一致")
    min_remaining_moment: float = Field(..., description="该港允许剩余总力矩闭区间下限")
    max_remaining_moment: float = Field(..., description="该港允许剩余总力矩闭区间上限")

    @model_validator(mode="after")
    def _window(self) -> "UnloadingPortIn":
        if self.min_remaining_moment > self.max_remaining_moment:
            raise ValueError(
                f"卸港 {self.discharge_order} 的允许剩余总力矩下限 "
                f"{self.min_remaining_moment} 不得高于上限 "
                f"{self.max_remaining_moment}（区间倒置）"
            )
        return self


class UnloadingCreateIn(BaseModel):
    ports: list[UnloadingPortIn] = Field(
        ..., description="每个卸港一条；卸港序号须与方案箱清单完全一致，不得重复"
    )

    @model_validator(mode="after")
    def _unique(self) -> "UnloadingCreateIn":
        orders = [p.discharge_order for p in self.ports]
        if len(set(orders)) != len(orders):
            dup = sorted({d for d in orders if orders.count(d) > 1})
            raise ValueError(f"卸港序号重复提交：{', '.join(map(str, dup))}")
        return self


class UnloadingStepOut(BaseModel):
    step_no: int
    discharge_order: int
    container_no: str
    slot_code: str
    stack: str
    tier: int
    weight: float
    arm: float
    remaining_moment: float


class UnloadingPortOut(BaseModel):
    discharge_order: int
    min_remaining_moment: float
    max_remaining_moment: float


class UnloadingOut(BaseModel):
    plan_id: str
    revision_no: int
    status: str = Field(..., description="current=当前卸箱顺序；historical=方案修订已被取代")
    initial_moment: float
    final_moment: float
    ports: list[UnloadingPortOut]
    steps: list[UnloadingStepOut]
    created_at: str


# ---------- 逐港卸箱执行（现场逐步登记） ----------


class UnloadingExecuteIn(BaseModel):
    """现场卸一箱的登记请求：方案号取路径参数。"""

    container_no: str = Field(..., description="本次实际卸下的箱号（须属当前港且位于同堆栈最上层）")
    operation_no: str = Field(..., description="全局唯一操作号；同号同内容重试返回首次结果，同号异内容冲突")
    expected_version: int = Field(
        ..., ge=0, description="期望进度版本；首次为 0，每次成功登记后 +1；与当前不一致返回 409"
    )

    @field_validator("container_no")
    @classmethod
    def _container_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @field_validator("operation_no")
    @classmethod
    def _operation_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "操作号")


class UnloadingExecStepOut(BaseModel):
    """已卸一步（含方案所用箱重、槽位力臂与卸后剩余总力矩）。"""

    step_no: int
    discharge_order: int
    container_no: str
    slot_code: str
    stack: str
    tier: int
    weight: float
    arm: float
    remaining_moment: float
    unloaded_at: str | None = None


class UnloadingRecommendedStepOut(BaseModel):
    """从当前状态出发、可行的剩余推荐顺序中的一步（全程箱号字典序最小）。"""

    discharge_order: int
    container_no: str
    slot_code: str
    stack: str
    tier: int
    weight: float
    arm: float
    remaining_moment: float


class UnloadingWindowPortIn(BaseModel):
    """区间调整申请中的单个卸港新区间（只可收紧、只可针对当前港及后续未完成港）。"""

    discharge_order: int = Field(..., ge=1, description="卸港序号；不得为已完成港")
    min_remaining_moment: float = Field(..., description="新区间下限；须为有限数值")
    max_remaining_moment: float = Field(..., description="新区间上限；须为有限数值")

    @field_validator("min_remaining_moment", "max_remaining_moment", mode="before")
    @classmethod
    def _finite_before(cls, v):
        # 在字段转为 float 前以原始输入核对有限性：NaN/Infinity 通过类型转换
        # 后，校验错误回显里的 input 仍为非有限浮点，会使错误响应无法序列化。
        if isinstance(v, float) and not _is_finite(v):
            raise ValueError(
                "新区间上下界须为有限数值（不得为 NaN/Infinity/-Infinity）"
            )
        return v

    @model_validator(mode="after")
    def _window(self) -> "UnloadingWindowPortIn":
        if self.min_remaining_moment > self.max_remaining_moment:
            raise ValueError(
                f"卸港 {self.discharge_order} 的新区间下限 "
                f"{self.min_remaining_moment} 不得高于上限 "
                f"{self.max_remaining_moment}（区间倒置）"
            )
        return self


class UnloadingWindowAdjustIn(BaseModel):
    """收紧剩余力矩安全区间的调整申请：方案号取路径参数。"""

    application_no: str = Field(
        ..., description="全局唯一申请号；同号同内容重试返回首次结果，同号异内容冲突"
    )
    expected_progress_version: int = Field(
        ..., ge=0, description="期望卸箱进度版本；与当前不一致返回 409"
    )
    expected_window_version: int = Field(
        ..., ge=0, description="期望区间版本；初始为 0，每次调整生效后 +1；与当前不一致返回 409"
    )
    ports: list[UnloadingWindowPortIn] = Field(
        ..., min_length=1,
        description="至少一个当前港至后续未完成港的新区间；未列港沿用当前生效区间",
    )

    @model_validator(mode="after")
    def _unique(self) -> "UnloadingWindowAdjustIn":
        orders = [p.discharge_order for p in self.ports]
        if len(set(orders)) != len(orders):
            dup = sorted({d for d in orders if orders.count(d) > 1})
            raise ValueError(f"调整区间卸港序号重复提交：{', '.join(map(str, dup))}")
        return self

    @field_validator("application_no")
    @classmethod
    def _application_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "申请号")


class UnloadingEffectivePortOut(BaseModel):
    """某卸港当前生效的剩余总力矩闭区间。"""

    discharge_order: int
    min_remaining_moment: float
    max_remaining_moment: float


class UnloadingWindowAdjustmentOut(BaseModel):
    """单次生效的区间调整历史条目。"""

    application_no: str
    expected_progress_version: int = Field(
        ..., description="申请提交时核对的期望进度版本"
    )
    expected_window_version: int = Field(
        ..., description="申请提交时核对的期望区间版本"
    )
    window_version: int = Field(..., description="本次生效后的区间版本")
    ports: list[UnloadingEffectivePortOut] = Field(
        ..., description="本次申请列出的调整区间（按卸港序号升序）"
    )
    created_at: str


class UnloadingExecutionOut(BaseModel):
    plan_id: str
    revision_no: int
    status: str = Field(
        ..., description="current=方案修订仍当前；historical=方案修订已被新修订取代（只读）"
    )
    read_only: bool = Field(..., description="true=方案已被取代/全部卸完，不再接受登记")
    completed: bool = Field(..., description="true=全部剩余箱已卸完")
    progress_version: int = Field(..., description="进度版本：首次登记前为 0，每登记一箱 +1")
    # 默认值仅为兼容升级前遗留操作快照的同号重试（其快照无下列新字段）；
    # 新请求的视图始终显式携带这些字段。
    window_version: int = Field(
        default=0, description="区间版本：首次冻结为 0（原定区间），每次区间调整生效后 +1"
    )
    initial_moment: float
    remaining_moment: float = Field(..., description="当前剩余总力矩")
    current_port: int | None = Field(
        ..., description="当前港：剩余箱中最小卸港序号；无剩余箱为 null"
    )
    planned_next_container: str | None = Field(
        ..., description="冻结的原计划下一步箱号（允许现场改选其他合法箱）；无剩余为 null"
    )
    frozen: bool = Field(..., description="true=计划及箱重/力臂/港区间已被首次登记冻结")
    frozen_at: str | None = None
    effective_ports: list[UnloadingEffectivePortOut] = Field(
        default_factory=list,
        description="各卸港当前生效的剩余总力矩闭区间（首次冻结为原定区间，调整后按版本更新）"
    )
    window_adjustments: list[UnloadingWindowAdjustmentOut] = Field(
        default_factory=list, description="区间调整历史（按生效先后升序）；从未调整为空"
    )
    steps: list[UnloadingExecStepOut]
    recommendation: list[UnloadingRecommendedStepOut] = Field(
        ..., description="从当前状态出发可行的剩余推荐顺序；全部卸完为空"
    )


# ---------- 装船实绩登记（事件重放） ----------


class ActualEventIn(BaseModel):
    seq: int = Field(..., ge=1, description="事件流水号（正整数），同一方案内唯一")
    event_type: str = Field(
        ..., description="事件类型：start=开工，finish=完工（同分钟先重放完工）"
    )
    container_no: str = Field(..., description="箱号（须在方案中）")
    crane_no: int = Field(..., ge=1, description="岸桥编号（正整数，须在冻结排程岸桥数量内）")
    minute: int = Field(..., ge=0, description="实际发生分钟（非负整数）")

    @field_validator("event_type")
    @classmethod
    def _event_type(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("事件类型不能为空")
        norm = v.strip().lower()
        if norm not in ("start", "finish"):
            raise ValueError("事件类型只能为 start（开工）或 finish（完工）")
        return norm

    @field_validator("container_no")
    @classmethod
    def _container_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")


class ActualEventRegisterIn(BaseModel):
    events: list[ActualEventIn] = Field(
        ...,
        min_length=1,
        description="本次登记的实际开工/完工事件（可一次登记多条；允许晚到，"
                    "服务按实际分钟重放全部事件）",
    )

    @model_validator(mode="after")
    def _unique(self) -> "ActualEventRegisterIn":
        seqs = [e.seq for e in self.events]
        if len(set(seqs)) != len(seqs):
            dup = sorted({s for s in seqs if seqs.count(s) > 1})
            raise ValueError(f"本次提交内事件流水号重复：{', '.join(map(str, dup))}")
        return self


# ---------- 装船实绩更正（只改分钟或岸桥） ----------


class ActualEventContentIn(BaseModel):
    """替代事件的完整内容；流水号/类型/箱号须与原事件一致。"""

    seq: int = Field(..., ge=1, description="事件流水号（须等于被更正事件的流水号）")
    event_type: str = Field(..., description="事件类型：start 或 finish（须与原事件一致）")
    container_no: str = Field(..., description="箱号（须与原事件一致，仍须在方案中）")
    crane_no: int = Field(..., ge=1, description="替代岸桥编号（正整数，须在冻结排程岸桥数量内）")
    minute: int = Field(..., ge=0, description="替代实际分钟（非负整数）")

    @field_validator("event_type")
    @classmethod
    def _event_type(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("事件类型不能为空")
        norm = v.strip().lower()
        if norm not in ("start", "finish"):
            raise ValueError("事件类型只能为 start（开工）或 finish（完工）")
        return norm

    @field_validator("container_no")
    @classmethod
    def _container_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")


class ActualEventCorrectionIn(BaseModel):
    """已登记装船实绩事件的更正请求。

    凭方案编号（路径参数）、原事件流水号、期望实绩版本、更正请求号及替代的完整
    事件内容更正。替代事件沿用原流水号、事件类型与箱号，只允许改 minute 或
    crane_no；其余字段不一致返回 422。
    """

    seq: int = Field(..., ge=1, description="被更正的原事件流水号（正整数）")
    expected_version: int = Field(
        ..., ge=1, description="客户端所依据的当前实绩版本；与现状不符返回 409"
    )
    request_id: str = Field(
        ..., description="更正请求号（同一方案内唯一）；同号同内容重试幂等返回首次结果"
    )
    event: ActualEventContentIn = Field(..., description="替代的完整事件内容")

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, v: str) -> str:
        return _require_nonempty_str(v, "更正请求号")

    @model_validator(mode="after")
    def _seq_matches(self) -> "ActualEventCorrectionIn":
        # 替代事件沿用原流水号：内外流水号必须一致（seq 仍以路径目标为准）
        if self.event.seq != self.seq:
            raise ValueError(
                f"替代事件流水号 {self.event.seq} 与原事件流水号 {self.seq} 不一致："
                "更正沿用原流水号，不得改号"
            )
        return self


class ActualBaselineEntryOut(BaseModel):
    container_no: str
    start_minute: int
    finish_minute: int
    crane_no: int
    stack: str
    tier: int


class ActualBoxOut(BaseModel):
    container_no: str
    status: str = Field(
        ..., description="pending=未开工；in_progress=已开工未完工；finished=已完工"
    )
    actual_start_minute: int | None
    actual_finish_minute: int | None
    actual_crane_no: int | None
    baseline_start_minute: int | None = Field(
        ..., description="冻结排程基线开工分钟；无基线箱（不应出现）为 null"
    )
    baseline_finish_minute: int | None
    baseline_crane_no: int | None
    start_delta_minutes: int | None = Field(
        ..., description="实际开工-基线开工；未开工为 null（正=延后、负=提前）"
    )
    finish_delta_minutes: int | None = Field(
        ..., description="实际完工-基线完工；未完工为 null（正=延后、负=提前）"
    )
    crane_changed: bool | None = Field(
        ..., description="实际开工岸桥是否与基线不同；未开工为 null"
    )
    stack: str
    tier: int


class ActualEventOut(BaseModel):
    seq: int
    event_type: str
    container_no: str
    crane_no: int = Field(..., description="当前生效岸桥编号（更正后为替代值）")
    minute: int = Field(..., description="当前生效分钟（更正后为替代值）")
    orig_crane_no: int = Field(
        ..., description="首次登记的原始岸桥编号（未更正时与 crane_no 相同）"
    )
    orig_minute: int = Field(
        ..., description="首次登记的原始分钟（未更正时与 minute 相同）"
    )
    corrected: bool = Field(..., description="该事件是否曾被更正")
    registered_at: str


class ActualCorrectionOut(BaseModel):
    request_id: str = Field(..., description="更正请求号")
    seq: int = Field(..., description="被更正的原事件流水号")
    expected_version: int = Field(..., description="请求所依据的期望版本")
    from_version: int = Field(..., description="更正前实绩版本")
    to_version: int = Field(..., description="更正后实绩版本")
    event_type: str
    container_no: str
    old_minute: int = Field(..., description="更正前（当时生效）分钟")
    new_minute: int = Field(..., description="更正后分钟")
    old_crane_no: int = Field(..., description="更正前（当时生效）岸桥")
    new_crane_no: int = Field(..., description="更正后岸桥")
    created_at: str


class ActualsOut(BaseModel):
    plan_id: str
    revision_no: int
    version: int = Field(
        ..., description="实绩版本：首次登记为 1，每次成功更正递增 1"
    )
    plan_status: str = Field(
        ..., description="confirmed=可继续登记/更正；superseded=方案已被新修订取代，实绩只读"
    )
    read_only: bool = Field(..., description="true=方案已被取代，不再接受登记或更正")
    baseline_frozen_at: str
    baseline_crane_count: int
    baseline: list[ActualBaselineEntryOut]
    containers: list[ActualBoxOut]
    unfinished_containers: list[str] = Field(
        ..., description="尚未完工箱号（未开工或已开工未完工），按箱号升序"
    )
    replay_order: list[int] = Field(
        ..., description="按实际分钟重放（同分钟完工先于开工）后的事件流水号顺序"
    )
    events: list[ActualEventOut] = Field(..., description="当前生效事件（按流水号升序）")
    corrections: list[ActualCorrectionOut] = Field(
        ..., description="更正历史（按生效先后升序）"
    )
    created_at: str


# ---------- 岸桥停机：剩余作业预测（只读，不落库） ----------


class ForecastActiveEntryIn(BaseModel):
    """进行中箱（实绩状态为已开工未完工）：正整数预计剩余时长。"""

    container_no: str = Field(..., description="箱号（须为截至时刻的进行中箱）")
    remaining_duration: int = Field(
        ..., gt=0, description="预计剩余作业时长（正整数分钟），自截至分钟连续作业"
    )

    @field_validator("container_no")
    @classmethod
    def _container_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")


class ForecastPendingEntryIn(BaseModel):
    """待开工箱（实绩状态为未开工）：时长与自身时间窗。"""

    container_no: str = Field(..., description="箱号（须为截至时刻的待开工箱）")
    duration: int = Field(..., gt=0, description="该箱剩余吊装时长（正整数分钟）")
    release_minute: int = Field(..., ge=0, description="最早开工分钟（非负整数）")
    deadline_minute: int = Field(
        ..., ge=0, description="最迟完工分钟（非负整数，可恰在此时刻结束）"
    )

    @field_validator("container_no")
    @classmethod
    def _container_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @model_validator(mode="after")
    def _window(self) -> "ForecastPendingEntryIn":
        if self.release_minute > self.deadline_minute:
            raise ValueError(
                f"箱 {self.container_no} 的最早开工 {self.release_minute} 不得晚于"
                f"最迟完工 {self.deadline_minute}（时间窗倒置）"
            )
        return self


class ForecastDowntimeIn(BaseModel):
    """冻结基线岸桥的停机半开区间 [start_minute, end_minute)。"""

    crane_no: int = Field(
        ..., ge=1, description="岸桥编号（正整数，须在冻结基线岸桥数量内）"
    )
    start_minute: int = Field(..., ge=0, description="停机起始分钟（非负整数）")
    end_minute: int = Field(..., ge=0, description="停机结束分钟（非负整数，不含）")

    @model_validator(mode="after")
    def _half_open(self) -> "ForecastDowntimeIn":
        if self.start_minute >= self.end_minute:
            raise ValueError(
                f"岸桥 {self.crane_no} 的停机起点 {self.start_minute} 必须严格早于"
                f"停机终点 {self.end_minute}（半开区间，起点不早于终点即拒绝）"
            )
        return self


class RemainingForecastIn(BaseModel):
    """岸桥停机时按已确认实绩预测剩余作业的请求（纯只读，不保存）。"""

    expected_actual_version: int = Field(
        ..., ge=1, description="期望实绩版本；与当前版本不一致返回 409"
    )
    cutoff_minute: int = Field(
        ..., ge=0, description="截至分钟（非负整数）；实绩事件不得晚于该分钟"
    )
    in_progress: list[ForecastActiveEntryIn] = Field(
        default_factory=list, description="各进行中箱的正整数预计剩余时长"
    )
    pending: list[ForecastPendingEntryIn] = Field(
        default_factory=list,
        description="待开工箱的正整数时长及非负最早开工/最迟完工分钟",
    )
    crane_downtimes: list[ForecastDowntimeIn] = Field(
        default_factory=list,
        description="冻结基线岸桥编号对应的非负起止分钟停机半开区间",
    )

    @model_validator(mode="after")
    def _unique(self) -> "RemainingForecastIn":
        active_nos = [e.container_no for e in self.in_progress]
        if len(set(active_nos)) != len(active_nos):
            dup = sorted({n for n in active_nos if active_nos.count(n) > 1})
            raise ValueError(f"进行中箱号重复提交：{', '.join(dup)}")
        pending_nos = [e.container_no for e in self.pending]
        if len(set(pending_nos)) != len(pending_nos):
            dup = sorted({n for n in pending_nos if pending_nos.count(n) > 1})
            raise ValueError(f"待开工箱号重复提交：{', '.join(dup)}")
        overlap = sorted(set(active_nos) & set(pending_nos))
        if overlap:
            raise ValueError(
                f"箱 {', '.join(overlap)} 同时出现在进行中与待开工箱项中"
            )
        return self


class ForecastEntryOut(BaseModel):
    container_no: str
    stack: str
    tier: int
    kind: str = Field(
        ..., description="in_progress=进行中箱（自截至分钟连续作业）；pending=待开工箱"
    )
    start_minute: int = Field(..., description="预计开工分钟（进行中箱即截至分钟）")
    finish_minute: int = Field(..., description="预计完工分钟")
    crane_no: int = Field(..., description="预计作业岸桥（进行中箱保持实际岸桥）")


class RemainingForecastOut(BaseModel):
    plan_id: str
    revision_no: int = Field(..., description="当前方案修订号")
    actual_version: int = Field(..., description="预测所依据的实绩版本")
    cutoff_minute: int
    baseline_crane_count: int = Field(..., description="冻结基线岸桥数量")
    crane_downtimes: list[ForecastDowntimeIn] = Field(
        ..., description="本次采用的停机半开区间（按岸桥、起点升序）"
    )
    entries: list[ForecastEntryOut] = Field(
        ..., description="未完箱逐箱预计开工/完工分钟与岸桥（按箱号升序）"
    )
    latest_finish_minute: int = Field(
        ..., description="全船预计最晚完工分钟（无未完箱时为已完工箱实际最晚完工）"
    )


# ---------- 装船完结交接（不可变凭证） ----------


class HandoverCreateIn(BaseModel):
    """装船完结交接的不可变凭证申请。"""

    handover_no: str = Field(
        ..., description="交接号（非空字符串，全局唯一）；同号同内容重试返回原凭证"
    )
    cutoff_minute: int = Field(
        ..., ge=0, description="装船截止分钟（非负整数）；每箱须在此时刻前（含）完工"
    )
    expected_actual_version: int = Field(
        ..., ge=1, description="期望实绩版本；与当前版本不一致返回 409"
    )
    valid_seqs: list[int] = Field(
        ...,
        min_length=1,
        description="有效流水号集合（正整数，不得重复）；须与当前已登记有效事件"
                    "流水号集合完全一致",
    )
    max_finish_delay_minutes: int = Field(
        ..., ge=0, description="允许的最大完工延误分钟（非负整数）；按冻结排程基线逐箱核算"
    )

    @field_validator("handover_no")
    @classmethod
    def _handover_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "交接号")

    @field_validator("valid_seqs")
    @classmethod
    def _valid_seqs(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("有效流水号集合不能为空")
        if any(not isinstance(s, int) or isinstance(s, bool) or s < 1 for s in v):
            raise ValueError("有效流水号必须均为正整数")
        if len(set(v)) != len(v):
            dup = sorted({s for s in v if v.count(s) > 1})
            raise ValueError(f"有效流水号重复：{', '.join(map(str, dup))}")
        return sorted(v)


class HandoverContainerOut(BaseModel):
    container_no: str
    stack: str
    tier: int
    actual_start_minute: int
    actual_finish_minute: int
    actual_crane_no: int
    baseline_start_minute: int
    baseline_finish_minute: int
    baseline_crane_no: int
    start_delta_minutes: int = Field(..., description="实际开工-基线开工（正=延后、负=提前）")
    finish_delta_minutes: int = Field(..., description="实际完工-基线完工（正=延后、负=提前）")
    crane_changed: bool


class HandoverSummaryOut(BaseModel):
    container_count: int
    cutoff_minute: int
    actual_version: int
    max_finish_delay_minutes: int = Field(..., description="请求允许的最大完工延误分钟")
    max_observed_finish_delay: int = Field(..., description="签发时各箱最大完工延误（不为负）")
    fingerprint_algorithm: str


class HandoverOut(BaseModel):
    handover_id: str = Field(..., description="凭证号（服务端生成，不可变）")
    handover_no: str = Field(..., description="交接号（客户端提供）")
    plan_id: str
    revision_no: int = Field(..., description="签发时固化的方案修订号")
    cutoff_minute: int
    expected_actual_version: int
    actual_version: int = Field(..., description="签发时实绩版本")
    max_finish_delay_minutes: int
    max_observed_finish_delay: int
    valid_seqs: list[int] = Field(..., description="签发时固化的有效流水号集合（升序）")
    actuals_fingerprint: str = Field(..., description="签发时实绩指纹（SHA-256 十六进制）")
    fingerprint_algorithm: str
    containers: list[HandoverContainerOut] = Field(
        ..., description="各箱实际起止、岸桥、冻结基线与偏差（签发时快照，不可变）"
    )
    summary: HandoverSummaryOut
    matches_current_actuals: bool = Field(
        ..., description="凭证指纹是否仍对应当前实绩（后续更正/补登后为 false）"
    )
    current_actual_version: int | None = Field(
        ..., description="当前实绩版本；方案实绩已不存在时为 null"
    )
    current_fingerprint: str | None = Field(
        ..., description="当前实绩指纹；方案实绩已不存在时为 null"
    )
    created_at: str


# ---------- 离港前封签核对与放行凭证 ----------


class SealManifestItemIn(BaseModel):
    container_no: str = Field(..., description="箱号（须为方案内箱）")
    seal_no: str = Field(..., description="该箱预期封签号（清单内互异）")

    @field_validator("container_no")
    @classmethod
    def _container_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @field_validator("seal_no")
    @classmethod
    def _seal_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "封签号")


class SealManifestIn(BaseModel):
    """预期封签清单登记：以方案编号（路径参数）登记，覆盖全部箱且封签号互异。"""

    items: list[SealManifestItemIn] = Field(
        ...,
        description="逐箱预期封签；须覆盖方案全部箱（不多不少），封签号互异",
    )

    @model_validator(mode="after")
    def _unique(self) -> "SealManifestIn":
        nos = [i.container_no for i in self.items]
        if len(set(nos)) != len(nos):
            dup = sorted({n for n in nos if nos.count(n) > 1})
            raise ValueError(f"提交箱号重复：{', '.join(dup)}")
        seals = [i.seal_no for i in self.items]
        if len(set(seals)) != len(seals):
            dup = sorted({s for s in seals if seals.count(s) > 1})
            raise ValueError(f"封签号须互异，重复封签号：{', '.join(dup)}")
        return self


class SealManifestItemOut(BaseModel):
    container_no: str
    expected_seal_no: str


class SealManifestOut(BaseModel):
    plan_id: str
    revision_no: int = Field(..., description="登记时固化的方案修订号")
    container_count: int
    scan_version: int = Field(
        ..., description="当前扫描版本：0=尚无扫描，每登记一条新扫描 +1"
    )
    seal_version: int = Field(
        ..., description="当前换签版本（方案级）：0=尚未换签，每成功换签一次 +1"
    )
    fingerprint: str = Field(..., description="清单内容指纹（SHA-256 十六进制）")
    fingerprint_algorithm: str
    items: list[SealManifestItemOut] = Field(
        ..., description="逐箱预期封签（按箱号升序；冻结后不可改写）"
    )
    created_at: str


class SealScanIn(BaseModel):
    """现场封签扫描登记：箱号、实见封签号、检查分钟与全局唯一扫描号。"""

    scan_no: str = Field(
        ..., description="全局唯一扫描号；同号同内容重试返回原记录，同号异内容冲突"
    )
    container_no: str = Field(..., description="箱号（须在预期封签清单中）")
    seal_no: str = Field(..., description="实见封签号")
    check_minute: int = Field(..., ge=0, description="检查分钟（非负整数）")

    @field_validator("scan_no")
    @classmethod
    def _scan_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "扫描号")

    @field_validator("container_no")
    @classmethod
    def _container_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @field_validator("seal_no")
    @classmethod
    def _seal_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "实见封签号")


class SealScanOut(BaseModel):
    scan_no: str
    plan_id: str
    container_no: str
    expected_seal_no: str = Field(..., description="清单中该箱预期封签号")
    seal_no: str = Field(..., description="实见封签号")
    check_minute: int
    matched: bool = Field(..., description="实见封签号是否与预期一致（不符留痕但不算通过）")
    effective: bool = Field(
        ..., description="登记后该扫描是否为该箱当前有效扫描"
        "（同箱以检查分钟较晚者为准，同分钟以登记顺序较晚者为准）"
    )
    scan_version: int = Field(..., description="本次登记后的扫描版本")
    registered_at: str


class SealScanRecordOut(BaseModel):
    """留痕扫描记录（只追加，内容为登记时结果；含是否为该箱放行有效扫描）。"""

    scan_no: str
    container_no: str
    expected_seal_no: str = Field(
        ..., description="登记时该箱预期封签号（换签不改历史扫描，定格登记时值）"
    )
    seal_no: str
    check_minute: int
    matched: bool = Field(..., description="登记时实见是否与当时预期一致（定格，不改）")
    effective: bool = Field(
        ..., description="按原有效扫描排序规则（检查分钟较晚、同分钟登记顺序较晚）"
        "是否曾为/仍为该箱排序胜出扫描"
    )
    eligible: bool = Field(
        ..., description="是否为该箱当前放行有效扫描：换签后须为换签后新登记"
        "（id 晚于换签点）且检查分钟不早于最近换签分钟，并在合格扫描中排序胜出"
    )
    current_expected_seal_no: str = Field(
        ..., description="该箱当前预期封签号（可能由换签改变）"
    )
    registered_at: str


class SealContainerStatusOut(BaseModel):
    container_no: str
    expected_seal_no: str = Field(..., description="冻结清单原预期封签号")
    current_expected_seal_no: str = Field(
        ..., description="当前预期封签号（换签后为最新新号，否则同原号）"
    )
    replaced: bool = Field(..., description="该箱是否发生过换签")
    last_replace_minute: int | None = Field(
        ..., description="该箱最近一次换签分钟（未换签为 null）"
    )
    scanned: bool = Field(..., description="是否已有放行有效（合格）扫描记录")
    passed: bool = Field(
        ..., description="该箱当前放行有效扫描是否匹配当前预期"
        "（换签箱旧扫描不算通过，须换签后新扫描）"
    )
    latest_scan_no: str | None
    observed_seal_no: str | None
    check_minute: int | None


class SealScanStatusOut(BaseModel):
    plan_id: str
    revision_no: int
    plan_status: str
    read_only: bool = Field(..., description="true=方案已被取代，不再接受扫描/换签登记")
    scan_version: int
    seal_version: int = Field(..., description="当前换签版本（方案级）：0=尚未换签")
    container_count: int
    containers: list[SealContainerStatusOut] = Field(
        ..., description="逐箱放行有效扫描核对状态（按箱号升序）"
    )
    scans: list[SealScanRecordOut] = Field(
        ..., description="全部扫描留痕记录（按登记顺序升序；内容不可改）"
    )
    created_at: str = Field(..., description="预期清单登记时间")


class SealReplacementIn(BaseModel):
    """封签损坏换签登记：箱号、当前预期原号、新封签号、换签分钟、原因、
    全局唯一申请号与期望换签版本。"""

    replacement_no: str = Field(
        ..., description="全局唯一换签申请号；同号同内容重试返回首次快照，异内容冲突"
    )
    container_no: str = Field(..., description="箱号（须在预期封签清单中）")
    old_seal_no: str = Field(..., description="该箱当前预期封签号（须与当前一致）")
    new_seal_no: str = Field(
        ..., description="新封签号（不得等于旧号，不得与其他箱当前预期封签号重复）"
    )
    replace_minute: int = Field(
        ..., ge=0, description="换签分钟（非负；不得早于该箱上次换签分钟）"
    )
    reason: str = Field(..., min_length=1, description="换签原因（非空字符串）")
    expected_seal_version: int = Field(
        ..., ge=0, description="期望换签版本（方案级，初值 0）；与当前不一致返回 409"
    )

    @field_validator("replacement_no")
    @classmethod
    def _replacement_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "换签申请号")

    @field_validator("container_no")
    @classmethod
    def _container_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "箱号")

    @field_validator("old_seal_no")
    @classmethod
    def _old_seal_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "原封签号")

    @field_validator("new_seal_no")
    @classmethod
    def _new_seal_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "新封签号")

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("换签原因不能为空")
        return v


class SealReplacementOut(BaseModel):
    replacement_id: int = Field(..., description="换签记录号（服务端生成，链中顺序）")
    replacement_no: str = Field(..., description="换签申请号（客户端提供，全局唯一）")
    plan_id: str
    revision_no: int = Field(..., description="登记时固化的方案修订号")
    container_no: str
    old_seal_no: str = Field(..., description="换签前该箱当前预期封签号")
    new_seal_no: str = Field(..., description="换签后该箱当前预期封签号")
    replace_minute: int
    reason: str
    expected_seal_version: int = Field(..., description="提交时的期望换签版本")
    seal_version: int = Field(..., description="本次换签后的方案级换签版本")
    scan_id_at_replace: int = Field(
        ..., description="换签瞬间该箱已存在扫描的最大 id（无扫描为 0）；"
                        "放行时仅 id 更大的新登记扫描才有资格"
    )
    previous_replacement_id: int | None = Field(
        ..., description="该箱上一条换签记录 id（首次换签为 null）"
    )
    registered_at: str


class SealReplacementRecordOut(BaseModel):
    """逐箱换签链上的一条只追加记录。"""

    replacement_id: int
    replacement_no: str
    container_no: str
    old_seal_no: str
    new_seal_no: str
    replace_minute: int
    reason: str
    expected_seal_version: int
    seal_version: int
    scan_id_at_replace: int
    previous_replacement_id: int | None
    registered_at: str


class SealReplacementChainOut(BaseModel):
    plan_id: str
    revision_no: int
    plan_status: str
    seal_version: int = Field(..., description="当前换签版本（方案级）")
    container_count: int
    containers: list[SealContainerStatusOut] = Field(
        ..., description="逐箱当前预期封签与最近换签信息（按箱号升序）"
    )
    replacements: list[SealReplacementRecordOut] = Field(
        ..., description="全部换签留痕（按方案内换签先后升序；只追加不可改）"
    )


class SealReleaseIn(BaseModel):
    """离港放行申请：当前交接凭证、期望扫描版本、截止分钟与唯一申请号。"""
    release_no: str = Field(
        ..., description="放行申请号（非空字符串，全局唯一）；同号同内容重试返回原凭证"
    )
    handover_id: str = Field(
        ..., description="当前交接凭证号；须属于本方案且仍对应当前实绩"
    )
    expected_scan_version: int = Field(
        ..., ge=0, description="期望扫描版本；与当前扫描版本不一致返回 409"
    )
    cutoff_minute: int = Field(
        ..., ge=0, description="放行截止分钟（非负整数）；每箱最新扫描检查分钟须不晚于该时刻"
    )

    @field_validator("release_no")
    @classmethod
    def _release_no(cls, v: str) -> str:
        return _require_nonempty_str(v, "放行申请号")

    @field_validator("handover_id")
    @classmethod
    def _handover_id(cls, v: str) -> str:
        return _require_nonempty_str(v, "交接凭证号")


class SealReleaseItemOut(BaseModel):
    container_no: str
    expected_seal_no: str
    observed_seal_no: str
    check_minute: int
    scan_no: str = Field(..., description="签发时该箱当前有效扫描的扫描号")
    matched: bool


class SealReleaseSummaryOut(BaseModel):
    container_count: int
    cutoff_minute: int
    scan_version: int
    seal_version: int = Field(..., description="签发时换签版本（方案级）：0=未换签")
    max_check_minute: int = Field(..., description="签发时各箱最新扫描的最大检查分钟")
    fingerprint_algorithm: str


class SealReleaseOut(BaseModel):
    release_id: str = Field(..., description="放行凭证号（服务端生成，不可变）")
    release_no: str = Field(..., description="放行申请号（客户端提供）")
    plan_id: str
    revision_no: int = Field(..., description="签发时固化的方案修订号")
    handover_id: str = Field(..., description="签发所依据的交接凭证号")
    handover_no: str = Field(..., description="签发所依据的交接号")
    cutoff_minute: int
    expected_scan_version: int
    scan_version: int = Field(..., description="签发时扫描版本")
    seal_version: int = Field(..., description="签发时换签版本（方案级）：0=未换签")
    container_count: int
    max_check_minute: int
    scans_fingerprint: str = Field(..., description="签发时扫描状态指纹（SHA-256 十六进制）")
    actuals_fingerprint: str = Field(..., description="签发时实绩指纹（SHA-256 十六进制）")
    fingerprint_algorithm: str
    containers: list[SealReleaseItemOut] = Field(
        ..., description="逐箱核对结果（签发时快照，不可变）"
    )
    summary: SealReleaseSummaryOut
    is_current: bool = Field(
        ..., description="凭证是否仍然有效（方案仍确认、扫描状态与实绩均未再变化）"
    )
    invalid_reasons: list[str] = Field(
        ..., description="失效原因：plan_not_confirmed=方案已被取代；"
                        "scans_changed=签发后有新扫描（无换签）；"
                        "seals_replaced=签发后发生过封签换签；"
                        "actuals_changed=签发后实绩已更正/补登"
    )
    plan_status: str | None = Field(..., description="方案当前状态")
    current_scan_version: int | None = Field(
        ..., description="当前扫描版本；无预期清单时为 null"
    )
    current_seal_version: int | None = Field(
        ..., description="当前换签版本（方案级）；无预期清单时为 null"
    )
    current_scans_fingerprint: str | None = Field(
        ..., description="当前扫描状态指纹；无预期清单时为 null"
    )
    current_actuals_fingerprint: str | None = Field(
        ..., description="当前实绩指纹；无实绩时为 null"
    )
    created_at: str


class AssignmentOut(BaseModel):
    container_no: str
    slot_code: str
    stack: str
    tier: int


class PlanOut(BaseModel):
    plan_id: str
    revision_no: int
    status: str
    slots_revision: int
    manifest_revision: int
    total_moment: float
    max_occupied_tier: int
    assignments: list[AssignmentOut]
    created_at: str
    confirmed_at: str | None = None
    parent_id: str | None = None
    root_id: str


class SlotRevisionOut(BaseModel):
    slots_revision: int
    slot_count: int
    min_moment: float
    max_moment: float
    created_at: str


class ManifestRevisionOut(BaseModel):
    manifest_revision: int
    container_count: int
    created_at: str


class MessageOut(BaseModel):
    detail: str
