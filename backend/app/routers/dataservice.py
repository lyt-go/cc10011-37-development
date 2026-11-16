"""数据服务接口：服务维护、发布准入绑定与带评分快照的服务调用。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dataservice import dataservice_service

router = APIRouter(prefix="/api/dataservice", tags=["数据服务"])

service = dataservice_service

LIST_FIELDS = [
    "服务编号", "服务名称", "接口路径", "查询参数", "返回字段", "调用频次限制",
    "缓存策略", "服务版本", "数据集名称", "绑定快照编号", "准入结论", "快照状态",
]
STATUSES = ["草稿", "服务评审中", "已上线", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按服务编号检索"),
    status: str | None = Query(default=None, description="草稿、服务评审中、已上线、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按服务编号与状态过滤数据服务列表；列表同时给出质量准入视图。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据服务清单：返回当前过滤条件下的全量数据。

    声明在 GET /{entry_id} 之前，避免 export 被当作整数路径参数解析。
    """
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dataservice", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条服务接口明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"服务接口 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条服务接口（必须声明数据集名称），缺字段时说明原因。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="服务接口已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """服务评审/上线/下线；上线动作走质量准入链，可在 values.snapshot_id 指定快照。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/invoke", response_model=ActionResult)
def invoke_service(entry_id: int) -> ActionResult:
    """调用已上线服务：返回结果记录评分快照；快照失效或未达标时阻断新请求。"""
    result, message = service.invoke(entry_id)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)
