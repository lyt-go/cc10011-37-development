"""数据服务业务规则：状态流转之外，承接质量评分准入链。

发布准入与调用准入的门槛判断不在本文件重写，统一调用 quality_gate 的
admission_decision——数据服务列表看到的准入状态、上线动作的发布校验、
服务调用的运行时校验走的是同一份可审计逻辑，杜绝「各写一遍」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import quality_gate
from app.services.quality_gate import GateError
from app.services.quality_snapshot import snapshot_service
from app.store import store

MODULE = "dataservice"
REQUIRED_FIELDS = ["服务编号", "服务名称", "接口路径", "数据集名称"]
STATUS_ORDER = ["草稿", "服务评审中", "已上线", "已下线"]
ACTION_RULES = {"提交服务评审": "服务评审中", "上线服务": "已上线", "下线服务": "已下线"}
NEGATIVE_ACTIONS = ["下线服务"]


class DataserviceService:
    # ------------------------------------------------------------ 基础读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("服务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        # 列表里的准入状态同样取自 quality_gate 的判定结果，不另起一套口径。
        rows = [self._with_admission_view(dict(row)) for row in rows]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._with_admission_view(dict(entry)) if entry else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["绑定评分快照"] = None
        entry["准入结论"] = "未评估"
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------ 准入解析
    def _rule_of_snapshot(self, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        rule_id = snapshot.get("规则编号")
        for row in store.rows("qualityscore"):
            if row.get("规则编号") == rule_id:
                return row
        return None

    def _resolve_gate_context(
        self, service: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
        """解析服务当前引用的评分快照与对应规则；发布与调用两条链路共用。"""
        dataset = str(service.get("数据集名称") or "")
        bound_id = service.get("绑定评分快照")
        snapshot: dict[str, Any] | None = None
        if bound_id is not None:
            try:
                snapshot = store.find("quality_snapshot", int(bound_id))
            except (TypeError, ValueError):
                snapshot = None
        if snapshot is None:  # 未显式绑定时回落到数据集当前有效快照
            snapshot = snapshot_service.latest_valid_snapshot(dataset)
        return dataset, snapshot, self._rule_of_snapshot(snapshot)

    def _with_admission_view(self, view: dict[str, Any]) -> dict[str, Any]:
        """列表/详情的准入视图：只读取判定所需事实，不产生审计写动作。

        审计写动作集中在真正的发布与调用入口；这里仅复用同一套解析口径，
        避免列表页和详情页各自再实现一份「快照是否有效」的判断。
        """
        dataset, snapshot, rule = self._resolve_gate_context(view)
        view["当前有效快照"] = snapshot.get("快照编号") if snapshot else None
        view["快照状态"] = self._snapshot_state(dataset, snapshot, rule)
        if view.get("status") == "已上线":
            view["准入结论"] = "可调用" if view["快照状态"] == "有效" else "准入阻断"
        return view

    def _snapshot_state(
        self,
        dataset: str,
        snapshot: dict[str, Any] | None,
        rule: dict[str, Any] | None,
    ) -> str:
        if not dataset:
            return "未绑定数据集"
        if snapshot is None:
            return "缺少评分快照"
        if snapshot.get("阶段状态") != "已完成":
            return "快照阶段不完整"
        if not snapshot.get("是否有效"):
            return f"快照已失效：{snapshot.get('失效原因') or '规则版本切换'}"
        if rule is None or not rule.get("是否生效版本"):
            return "规则版本已停用"
        threshold = quality_gate._threshold_of(rule)
        if float(snapshot.get("总分数", 0.0)) < threshold:
            return f"总分低于门槛 {threshold:g}"
        return "有效"

    # ------------------------------------------------------------ 状态流转
    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务接口 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据服务可执行范围"

        if action == "上线服务":
            return self._publish(entry, values or {})
        if action == "下线服务":
            entry["status"] = "已下线"
            entry["pending"] = False
            entry["abnormal"] = True
            return entry, "服务已下线，发布准入链不再接受新请求"

        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"服务接口已{action}"

    def _publish(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """发布准入：上线请求必须绑定满足当前门槛判定的评分快照。

        冲突权衡（质量分未达标与发布请求同时发生）：发布动作采用严格门槛，
        不允许带未达标/失效快照上线；已上线服务在快照失效后保留绑定关系，
        但新请求在 invoke 处被阻断——即「发布看门槛、运行看快照有效性」。
        """
        dataset = str(entry.get("数据集名称") or "")
        snapshot: dict[str, Any] | None = None
        explicit_snapshot_id = values.get("snapshot_id")
        if explicit_snapshot_id not in (None, ""):
            try:
                snapshot = store.find("quality_snapshot", int(explicit_snapshot_id))
            except (TypeError, ValueError):
                snapshot = None
            if snapshot is None:
                return None, f"评分快照 {explicit_snapshot_id} 不存在，无法绑定发布"
        else:
            snapshot = snapshot_service.latest_valid_snapshot(dataset)
        rule = self._rule_of_snapshot(snapshot)

        try:
            quality_gate.admission_decision(
                scene="服务发布准入", service=entry, dataset=dataset,
                snapshot=snapshot, rule=rule,
            )
        except GateError as exc:
            return None, f"发布被质量准入链阻断：{exc.reason}"

        entry["绑定评分快照"] = snapshot.get("id")
        entry["绑定快照编号"] = snapshot.get("快照编号")
        entry["绑定评分周期"] = snapshot.get("评分周期")
        entry["准入总分"] = snapshot.get("总分数")
        entry["准入结论"] = "通过"
        entry["准入判定时间"] = datetime.now().isoformat(timespec="seconds")
        entry["status"] = "已上线"
        entry["pending"] = False
        entry["abnormal"] = False
        return entry, f"服务已上线，绑定评分快照 {snapshot.get('快照编号')}（{snapshot.get('评分周期')}）"

    # ------------------------------------------------------------ 服务调用
    def invoke(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """服务调用：返回结果必须记录对应评分快照；快照失效后阻断新请求。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务接口 {entry_id} 不存在或已归档"
        if entry.get("status") != "已上线":
            return None, f"服务当前状态为「{entry.get('status')}」，只有已上线服务可调用"

        dataset, snapshot, rule = self._resolve_gate_context(entry)
        try:
            audit = quality_gate.admission_decision(
                scene="服务调用准入", service=entry, dataset=dataset,
                snapshot=snapshot, rule=rule,
            )
        except GateError as exc:
            entry["准入结论"] = "准入阻断"
            return None, f"服务调用被质量准入链阻断：{exc.reason}"

        entry["最近调用时间"] = datetime.now().isoformat(timespec="seconds")
        result = {
            "服务编号": entry.get("服务编号"),
            "数据集名称": dataset,
            "返回数据": f"{dataset} 的最新指标数据（演示载荷）",
            "评分快照编号": snapshot.get("快照编号"),
            "评分快照ID": snapshot.get("id"),
            "评分周期": snapshot.get("评分周期"),
            "质量总分": snapshot.get("总分数"),
            "维度得分": snapshot.get("维度得分"),
            "快照是否有效": snapshot.get("是否有效"),
            "准入审计ID": audit.get("id"),
        }
        return result, "调用成功，返回结果已记录当前引用的评分快照"


dataservice_service = DataserviceService()
