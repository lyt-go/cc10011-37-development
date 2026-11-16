"""数据质量分接口：评分规则维护 + 评分快照生成/追溯 + 准入审计查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.quality_gate import GateError
from app.services.quality_snapshot import StageFault, snapshot_service
from app.services.qualityscore import QualityscoreService

router = APIRouter(prefix="/api/qualityscore", tags=["数据质量分"])

service = QualityscoreService()

LIST_FIELDS = [
    "规则编号", "数据集名称", "完整性权重", "准确性权重", "时效性权重",
    "一致性权重", "评分周期", "总分数", "规则版本", "是否生效版本",
]
STATUSES = ["草稿", "规则评审中", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已启用、已停用"),
    dataset: str | None = Query(default=None, description="按数据集名称过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号、状态与数据集过滤评分规则；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, dataset=dataset, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/snapshots", response_model=PageResult[dict])
def list_snapshots(
    dataset: str | None = Query(default=None, description="按数据集名称过滤"),
    period: str | None = Query(default=None, description="按评分周期过滤"),
    valid_only: bool = Query(default=False, description="只看仍有效、可被服务引用的快照"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """评分快照列表：维度得分、权重快照与评分周期都挂在每条快照上，可追溯。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = snapshot_service.list_snapshots(
        dataset=dataset, period=period, valid_only=valid_only, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: int) -> dict:
    """读取单条评分快照明细，含完整阶段与加权明细。"""
    snapshot = snapshot_service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"评分快照 {snapshot_id} 不存在")
    return snapshot


@router.post("/snapshots/generate", response_model=ActionResult)
def generate_snapshot(payload: EntryPayload) -> ActionResult:
    """按完整阶段生成评分快照；同周期重复请求幂等，中断后从最后完整阶段恢复。"""
    dataset = str(payload.values.get("dataset") or "").strip()
    period = str(payload.values.get("period") or "").strip()
    if not dataset or not period:
        return ActionResult(ok=False, message="生成评分快照需要同时提供 dataset 与 period")
    try:
        snapshot, message = snapshot_service.generate_snapshot(dataset, period)
    except StageFault as exc:
        return ActionResult(ok=False, message=f"评分流水中断（可再次调用恢复）：{exc}")
    except GateError as exc:
        return ActionResult(ok=False, message=exc.reason)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.get("/trace/gap")
def trace_gap(
    dataset: str | None = Query(default=None, description="按数据集追溯调用链"),
    service_id: int | None = Query(default=None, description="按数据服务追溯调用链"),
) -> dict[str, Any]:
    """沿 聚合数据集 → 评分规则 → 评分快照 → 数据服务 调用链查找数据缺口。"""
    return snapshot_service.trace_gap(dataset=dataset, service_id=service_id)


@router.get("/audits", response_model=PageResult[dict])
def list_audits(
    dataset: str | None = Query(default=None, description="按数据集过滤准入审计"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """准入审计日志：发布与调用的每一次门槛判定（含阻断原因）都可回放。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = snapshot_service.list_audits(dataset=dataset)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据质量分清单：返回当前全量评分规则。

    注意：本路由必须声明在 GET /{entry_id} 之前，否则 export 会被当作
    整数路径参数解析而返回 422。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "qualityscore", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条评分规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"评分规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条评分规则，缺字段或权重非法时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"字段校验未通过：{'、'.join(missing)}")
    return ActionResult(ok=True, message="评分规则已登记（新版本默认不生效，需启用）", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交规则评审、启用规则（唯一生效版本）、停用规则（联动快照失效）。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
