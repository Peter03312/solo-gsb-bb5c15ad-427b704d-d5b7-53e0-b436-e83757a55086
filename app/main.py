"""FastAPI 应用入口。

启动：``uvicorn app.main:app --host 0.0.0.0 --port 8000``
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.database import init_db
from app.routers import router
from app.services import BlockedError, BusinessError, ConflictError

settings = get_settings()


def _json_safe(obj):
    """把校验错误回显中不可 JSON 序列化的值转为可序列化形式。

    覆盖：非有限浮点（NaN/Infinity）、ctx 中附带的原始异常对象等。
    """
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else f"<non-finite:{obj!s}>"
    if isinstance(obj, BaseException):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
    description=(
        "集装箱船配载方案核算服务。录入槽位资料（堆栈/层/承重/纵向力臂/"
        "相邻关系/力矩区间）与箱清单（箱号/重量/危险类别/卸港先后），"
        "核算满足全部约束的最优完整配载方案，并支持修订核对与并发安全确认；"
        "资料修订号按类别在写锁内唯一递增分配、与整份资料原子落库"
        "（并发录入不同号、失败不留半份），历史库同号多份时启动报出冲突并中止；"
        "方案确认后可按岸桥数量与每箱吊装时长/时间窗生成两阶段优选的装船时间表，"
        "并按该排程逐箱登记实际开工/完工事件（事件可晚到、按实际分钟重放，"
        "首次登记原子冻结排程基线用于偏差核算，方案被取代后实绩只读），"
        "或按各卸港允许剩余总力矩区间生成全程箱号序列字典序最小的逐港卸箱顺序；"
        "现场可逐箱登记实际卸箱（首次登记冻结箱重、槽位力臂、堆栈与原定港区间，"
        "可偏离预案但须满足当前港、栈顶与港区间并前瞻可行）；执行过程中港口收紧"
        "剩余力矩安全区间时，可对已开始且未完结的执行凭全局唯一申请号、期望卸箱"
        "进度版本与期望区间版本提交调整申请（只可收紧当前港至后续未完成港、未列"
        "港沿用、不得改已完成港；新区间有限非倒置且包含于原生效区间并包含当前剩余"
        "力矩，按冻结箱重/力臂/堆栈验证未卸箱仍能按港序、栈顶与逐步新区间卸完才"
        "生效并原子递增区间版本；原冻结区间、已卸步骤与原接口字段保留，后续登记与"
        "推荐使用生效区间，同申请号同内容重试返回首次结果）；"
        "码头临时封锁槽位时，可基于当前已确认方案、最新资料修订生成应急改配草稿"
        "（先最少换槽箱数，再最低最高占用层，最后箱号升序槽位序列字典序最小）；"
        "装船完结交接时可凭交接号、截止分钟、期望实绩版本、有效流水号集合与最大"
        "完工延误阈值申请不可变交接凭证（每箱截止前完工、截止后无有效事件、按冻结"
        "基线逐箱核算延误；凭证签发后实绩变化不改内容，仅标示是否仍对应当前实绩）。"
        "已完成装船交接的当前方案离港前，可按方案编号登记覆盖全部箱且封签号互异的"
        "预期封签清单（冻结后不可改写，同内容重试幂等、异内容冲突），现场按箱号、"
        "实见封签号、检查分钟与全局唯一扫描号逐箱登记封签扫描（不符留痕但不算通过，"
        "同箱以检查分钟较晚者为准、同分钟以登记顺序较晚者为准）；封签损坏时可凭"
        "方案、箱号、当前预期原号、新号、换签分钟、原因、全局唯一申请号与期望换签"
        "版本（方案级初值 0）登记只追加的逐箱换签（原清单与历史扫描不可改，换签后"
        "该箱须有检查分钟不早于换签分钟的新登记匹配扫描方可通过）；并凭当前交接凭证、"
        "期望扫描版本、截止分钟与唯一申请号申请离港放行：仅当方案仍已确认、交接凭证"
        "仍对应当前实绩、每箱放行有效扫描匹配当前预期且不晚于截止分钟时，原子签发含"
        "逐箱结果的不可变放行凭证（换签、后续扫描、实绩更正或方案取代只使凭证显示"
        "失效，不改历史内容）。"
    ),
)


@app.exception_handler(BusinessError)
def _business_handler(_request: Request, exc: BusinessError) -> JSONResponse:
    content = {"detail": exc.message}
    if getattr(exc, "extra", None):
        content.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=content)


@app.exception_handler(ConflictError)
def _conflict_handler(_request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": exc.message})


@app.exception_handler(BlockedError)
def _blocked_handler(_request: Request, exc: BlockedError) -> JSONResponse:
    # 所选箱合法但新状态无可行后续：422（业务不可处理），进度保持不变
    content = {"detail": exc.message}
    if getattr(exc, "extra", None):
        content.update(exc.extra)
    return JSONResponse(status_code=422, content=content)


@app.exception_handler(RequestValidationError)
def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # 保持 FastAPI 默认 422 结构，仅把回显中不可序列化的输入（NaN/Infinity）
    # 转为安全字符串，避免错误响应自身无法序列化而退化成 500。
    return JSONResponse(
        status_code=422,
        content={"detail": _json_safe(exc.errors())},
    )


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "version": __version__}


app.include_router(router, prefix=settings.api_prefix)
