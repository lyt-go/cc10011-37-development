"""数据质量分业务规则：评分规则的字段校验、版本管理与状态流转。

规则本身只保存「怎么评」（四维权重、评分周期、准入门槛），具体的评分计算与
快照生成在 quality_snapshot / quality_gate 中完成。规则的启用/停用委托给
QualitySnapshotService，因为「同一数据集只能一个版本生效」与「版本切换后
旧快照失效」是同一条并发约束，必须在同一把锁里完成。
"""
from __future__ import annotations

from typing import Any

from app.services.quality_gate import GateError, parse_ratio, parse_score
from app.services.quality_snapshot import snapshot_service
from app.store import store

MODULE = "qualityscore"
REQUIRED_FIELDS = [
    "规则编号",
    "数据集名称",
    "完整性权重",
    "准确性权重",
    "时效性权重",
    "一致性权重",
    "评分周期",
    "总分数",
]
WEIGHT_FIELDS = ["完整性权重", "准确性权重", "时效性权重", "一致性权重"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已停用"]
ACTION_RULES = {"提交规则评审": "规则评审中", "启用规则": "已启用", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]


class QualityscoreService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        dataset: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if dataset:
            rows = [row for row in rows if row.get("数据集名称") == dataset]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if str(values.get(field) or "").strip() == ""]
        if missing:
            return None, missing

        rows = store.rows(MODULE)
        dataset = str(values.get("数据集名称") or "")
        # 同一数据集的规则按递增版本管理，新版本默认不生效，需显式启用。
        next_version = max(
            (int(row.get("规则版本", 1)) for row in rows if row.get("数据集名称") == dataset),
            default=0,
        ) + 1

        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})

        try:
            for field in WEIGHT_FIELDS:
                entry[field] = parse_ratio(values.get(field))
            entry["总分数"] = parse_score(values.get("总分数"))
        except GateError as exc:
            return None, [exc.reason]

        entry["规则版本"] = next_version
        entry["是否生效版本"] = False
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"评分规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据质量分可执行范围"

        if action == "启用规则":
            try:
                _, message = snapshot_service.activate_rule(entry)
            except GateError as exc:
                return None, exc.reason
            return entry, message
        if action == "停用规则":
            snapshot_service.deactivate_rule(entry)
            return entry, "评分规则已停用，关联评分快照同步失效，新服务请求将被准入链阻断"

        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"评分规则已{action}"
