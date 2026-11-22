"""批量查询收益核验接口：计划绑定、实测样本、阈值判断与版本留档。

通用资源路由只负责列表/登记/动作；收益核验需要携带查询标识、输入规模和
计划版本，因此单独成组，避免把核验口径塞进通用动作。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/benefit", tags=["批量查询收益核验"])


class VerifyPayload(BaseModel):
    rule_no: str
    threshold: float | None = Field(default=None, ge=0, le=1)


class RevisePayload(BaseModel):
    version: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)


class ExecutePayload(BaseModel):
    plan_version: str | None = None
    simulated_latencies: dict[str, float] = Field(default_factory=dict)


class RunPayload(BaseModel):
    query_key: str
    plan_version: str
    latency_ms: float | None = None
    status: str = "success"
    input_scale: int | None = None
    query_class: str | None = None
    baseline_version: str | None = None


def _services():
    optimize = service_for("query_optimize")
    batch = service_for("batch_query")
    if optimize is None or batch is None:
        raise HTTPException(status_code=503, detail="优化或批量查询服务未注册")
    return optimize, batch


@router.get("/plans")
def list_plans(api: str | None = None) -> dict[str, Any]:
    """读取每条优化计划的核验结论，判断哪些计划已达阈值。"""
    optimize, _ = _services()
    plans = []
    for row in optimize.list_entries(page=1, size=1_000_000)[0]:
        if api and row.get("关联API") != api:
            continue
        rule_no = str(row.get("规则编号") or "")
        version = str(row.get("规则版本") or "")
        report = optimize.read_verification(rule_no, version)
        plans.append({
            "rule_no": rule_no,
            "api": row.get("关联API"),
            "plan_version": version,
            "estimated_benefit": row.get("优化收益"),
            "measured_benefit": report.get("measured_benefit"),
            "passed": bool(report.get("passed")),
            "threshold": report.get("threshold"),
            "data_scope": report.get("data_scope", {}),
            "verified_at": report.get("verified_at"),
        })
    reached = [plan["rule_no"] for plan in plans if plan["passed"]]
    return {"plans": plans, "reached_threshold": reached}


@router.post("/verify")
def verify(payload: VerifyPayload) -> dict[str, Any]:
    optimize, _ = _services()
    report = optimize.verify_plan(payload.rule_no, payload.threshold)
    if not report.get("ok"):
        raise HTTPException(status_code=404, detail=report.get("message"))
    return report


@router.get("/verifications/{rule_no}/{plan_version}")
def read_verification(rule_no: str, plan_version: str) -> dict[str, Any]:
    optimize, _ = _services()
    report = optimize.read_verification(rule_no, plan_version)
    if not report:
        raise HTTPException(status_code=404, detail="该计划版本尚无核验记录")
    return report


@router.post("/plans/{entry_id}/revise")
def revise_plan(entry_id: int, payload: RevisePayload) -> dict[str, Any]:
    optimize, _ = _services()
    entry, message = optimize.revise_plan(entry_id, payload.version, payload.changes)
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.get("/batches/{batch_id}/preflight")
def preflight(batch_id: int) -> dict[str, Any]:
    """执行前把计划与延迟样本关联，返回将使用的实际版本与阈值判断。"""
    optimize, batch = _services()
    entry = batch.get_entry(batch_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    plan = optimize.get_plan_for_api(entry.get("关联API", ""))
    rule_no = str(plan.get("rule_no") or "")
    version = str(plan.get("plan_version") or "")
    # 读核验样本记录判断是否已达阈值，而不是只看上一次存档的结论。
    report = optimize.verify_plan(rule_no) if rule_no else {}
    if str(report.get("plan_version") or "") != version:
        report = {}
    samples = batch.verification_samples(entry.get("关联API", ""))
    return {
        "batch_id": batch_id,
        "api": entry.get("关联API"),
        "planned_plan_version": version,
        "baseline_plan_version": entry.get("对照计划版本", ""),
        "rule_no": rule_no,
        "sample_count": len(samples),
        "reached_threshold": bool(report.get("passed")),
        "measured_benefit": report.get("measured_benefit"),
        "threshold": report.get("threshold"),
        "data_scope": report.get("data_scope", {}),
        "plan_binding": {
            "rule_no": rule_no,
            "plan_version": version,
            "benefit_basis": "实测核验" if report.get("measured_benefit") is not None else "待核验",
        },
    }


@router.post("/batches/{batch_id}/execute")
def execute(batch_id: int, payload: ExecutePayload) -> dict[str, Any]:
    _, batch = _services()
    result = batch.execute_batch(
        batch_id,
        plan_version=payload.plan_version,
        simulated_latencies=payload.simulated_latencies,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.post("/batches/{batch_id}/runs")
def record_run(batch_id: int, payload: RunPayload) -> dict[str, Any]:
    """补齐实测耗时：超时查询回填对照数据，或为新一轮执行登记样本。"""
    _, batch = _services()
    result = batch.record_run(
        batch_id,
        query_key=payload.query_key,
        plan_version=payload.plan_version,
        latency_ms=payload.latency_ms or 0.0,
        status=payload.status,
        input_scale=payload.input_scale,
        query_class=payload.query_class,
        baseline_version=payload.baseline_version,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.get("/batches/{batch_id}/rounds")
def rounds(batch_id: int) -> dict[str, Any]:
    """核对两轮执行使用的计划版本与各自超时情况。"""
    _, batch = _services()
    if batch.get_entry(batch_id) is None:
        raise HTTPException(status_code=404, detail="批量任务不存在")
    return batch.compare_rounds(batch_id)
