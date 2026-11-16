"""数据质量评分快照服务。

一条快照只能按完整阶段顺序推进，阶段定义（SNAPSHOT_STAGES）即评分流水：

1. 加载聚合数据集      —— 从 dataset_profile 读取四维原始分，确认数据缺口
2. 解析生效评分规则    —— 同一数据集同一时刻只允许一个生效规则版本
3. 计算维度加权得分    —— 调 quality_gate 的纯函数，权重/维度得分全部固化
4. 生成评分周期快照    —— 写入 quality_snapshot，标记阶段全部完成、快照有效
5. 回写聚合记录        —— 把快照编号/总分同步回 aggregate 的输出数据集

中断恢复语义：进程中断后再次请求同一（数据集, 评分周期），从最后一个完整阶段
继续，不会回到半成品，也不会产生第二份结果——重复计算同一周期只形成一份快照。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.services import quality_gate
from app.store import store

SNAPSHOT_MODULE = "quality_snapshot"
RULE_MODULE = "qualityscore"
PROFILE_MODULE = "dataset_profile"
AGGREGATE_MODULE = "aggregate"
SERVICE_MODULE = "dataservice"

SNAPSHOT_STAGES = [
    "加载聚合数据集",
    "解析生效评分规则",
    "计算维度加权得分",
    "生成评分周期快照",
    "回写聚合记录",
]

# 测试用故障注入：{(数据集, 评分周期): 阶段名}，推进到该阶段时模拟中断，只触发一次。
_FAULTS: dict[tuple[str, str], str] = {}


class StageFault(RuntimeError):
    """阶段中断注入：模拟评分流水在某个完整阶段之后崩溃。"""


def inject_fault(dataset: str, period: str, stage: str) -> None:
    """测试辅助：让下一次生成在指定阶段完成后中断，用于验证恢复逻辑。"""
    _FAULTS[(dataset, period)] = stage


def _fault_key(snapshot: dict[str, Any]) -> tuple[str, str]:
    return str(snapshot.get("数据集名称")), str(snapshot.get("评分周期"))


class QualitySnapshotService:
    def __init__(self) -> None:
        # 评分规则并发启停与同周期重复计算都靠这把锁串行化，保证只有一个版本/一份结果生效。
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 读取
    def list_snapshots(
        self,
        *,
        dataset: str | None = None,
        period: str | None = None,
        valid_only: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(SNAPSHOT_MODULE)
        if dataset:
            rows = [row for row in rows if row.get("数据集名称") == dataset]
        if period:
            rows = [row for row in rows if row.get("评分周期") == period]
        if valid_only:
            rows = [row for row in rows if row.get("是否有效")]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return store.find(SNAPSHOT_MODULE, snapshot_id)

    def find_snapshot(self, dataset: str, period: str) -> dict[str, Any] | None:
        for row in store.rows(SNAPSHOT_MODULE):
            if row.get("数据集名称") == dataset and row.get("评分周期") == period:
                return row
        return None

    def latest_valid_snapshot(self, dataset: str) -> dict[str, Any] | None:
        """数据集当前可引用的有效快照：已完成全部阶段且未失效，取最新一条。"""
        candidates = [
            row for row in store.rows(SNAPSHOT_MODULE)
            if row.get("数据集名称") == dataset
            and row.get("阶段状态") == "已完成"
            and row.get("是否有效")
        ]
        return max(candidates, key=lambda row: int(row.get("id", 0)), default=None)

    def list_audits(self, *, dataset: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows("quality_audit")
        if dataset:
            rows = [row for row in rows if row.get("数据集名称") == dataset]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    # ------------------------------------------------------------ 规则版本
    def active_rule(self, dataset: str) -> dict[str, Any] | None:
        rules = [
            row for row in store.rows(RULE_MODULE)
            if row.get("数据集名称") == dataset and row.get("是否生效版本")
        ]
        return rules[0] if rules else None

    def activate_rule(self, rule: dict[str, Any]) -> tuple[bool, str]:
        """启用规则：并发启停经锁串行化，同一数据集只保留一个生效版本。

        切换生效版本时，旧版本产出的有效快照全部失效，已绑定这些快照的服务
        再发起请求会被准入链阻断（评分失效阻断新请求）。
        """
        with self._lock:
            dataset = str(rule.get("数据集名称") or "")
            current = self.active_rule(dataset)
            if current is not None and int(current.get("id", 0)) == int(rule.get("id", 0)):
                rule["status"] = "已启用"
                rule["pending"] = False
                rule["abnormal"] = False
                return True, "规则已是生效版本，无需重复启用"

            for other in store.rows(RULE_MODULE):
                if other.get("数据集名称") == dataset and int(other.get("id", 0)) != int(rule.get("id", 0)):
                    other["是否生效版本"] = False
                    other["status"] = "已停用"
                    other["pending"] = False
                    other["abnormal"] = True
            rule["是否生效版本"] = True
            rule["status"] = "已启用"
            rule["pending"] = False
            rule["abnormal"] = False

            reason = f"规则版本切换至 {rule.get('规则编号')} v{rule.get('规则版本')}"
            for snapshot in store.rows(SNAPSHOT_MODULE):
                if snapshot.get("数据集名称") == dataset and snapshot.get("是否有效"):
                    snapshot["是否有效"] = False
                    snapshot["失效原因"] = reason
            return True, f"规则已启用为唯一生效版本；同数据集历史快照按规则失效处理"

    def deactivate_rule(self, rule: dict[str, Any]) -> tuple[bool, str]:
        with self._lock:
            rule["是否生效版本"] = False
            rule["status"] = "已停用"
            rule["pending"] = False
            rule["abnormal"] = True
            dataset = str(rule.get("数据集名称") or "")
            for snapshot in store.rows(SNAPSHOT_MODULE):
                if snapshot.get("数据集名称") == dataset and snapshot.get("是否有效"):
                    snapshot["是否有效"] = False
                    snapshot["失效原因"] = f"生效规则 {rule.get('规则编号')} 已停用"
            return True, "规则已停用，其评分周期快照同步失效"

    # ------------------------------------------------------------ 阶段流水
    def _profile(self, dataset: str) -> dict[str, Any] | None:
        for row in store.rows(PROFILE_MODULE):
            if row.get("数据集名称") == dataset:
                return row
        return None

    def _new_snapshot(self, rule: dict[str, Any], dataset: str, period: str) -> dict[str, Any]:
        rows = store.rows(SNAPSHOT_MODULE)
        snapshot_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        return {
            "id": snapshot_id,
            "快照编号": f"SNAP-{snapshot_id:04d}",
            "数据集名称": dataset,
            "评分周期": period,
            "规则编号": rule.get("规则编号"),
            "规则版本": rule.get("规则版本"),
            "阶段状态": "进行中",
            "已完成阶段": [],
            "维度得分": {},
            "权重快照": {},
            "加权明细": [],
            "总分数": None,
            "是否有效": False,
            "失效原因": None,
            "回写聚合规则": None,
        }

    def generate_snapshot(self, dataset: str, period: str) -> tuple[dict[str, Any] | None, str]:
        """生成（或恢复）一份评分快照；同周期重复调用幂等，只形成一份结果。"""
        with self._lock:
            existing = self.find_snapshot(dataset, period)
            # 必须五个阶段全部完成才视为可复用的完整快照；若在阶段 4 之后、
            # 阶段 5 回写之前中断，状态虽为「已完成」也要落到下面的阶段守卫续跑。
            if existing is not None and len(existing.get("已完成阶段", [])) >= len(SNAPSHOT_STAGES):
                return existing, "该评分周期已有完整快照，重复计算直接复用，不再生成第二份"
            snapshot = existing or None

            # 阶段 1：加载聚合数据集（中断后若已完成则跳过，不回退到半成品）。
            if snapshot is None or "加载聚合数据集" not in snapshot["已完成阶段"]:
                profile = self._profile(dataset)
                if profile is None:
                    return None, f"数据集「{dataset}」缺少聚合数据集记录，评分流水在阶段 1 阻断"
                if snapshot is None:
                    rule = self.active_rule(dataset)
                    if rule is None:
                        return None, f"数据集「{dataset}」没有生效中的评分规则，评分流水在阶段 2 阻断"
                    snapshot = self._new_snapshot(rule, dataset, period)
                    store.rows(SNAPSHOT_MODULE).append(snapshot)
                snapshot["聚合数据集来源"] = profile.get("来源聚合规则")
                snapshot["已完成阶段"].append("加载聚合数据集")
                self._maybe_fault(snapshot)

            # 阶段 2：解析生效评分规则。
            if "解析生效评分规则" not in snapshot["已完成阶段"]:
                rule = self.active_rule(dataset)
                if rule is None:
                    return snapshot, "评分规则已停用且无新生效版本，快照停留在阶段 1，等待规则恢复后续跑"
                snapshot["规则编号"] = rule.get("规则编号")
                snapshot["规则版本"] = rule.get("规则版本")
                snapshot["已完成阶段"].append("解析生效评分规则")
                self._maybe_fault(snapshot)

            # 阶段 3：计算维度加权得分（纯函数，权重与维度得分固化进快照，可追溯）。
            if "计算维度加权得分" not in snapshot["已完成阶段"]:
                rule = self.active_rule(dataset)
                profile = self._profile(dataset)
                if rule is None:
                    return snapshot, "评分规则已停用且无新生效版本，快照停留在阶段 2，等待规则恢复后续跑"
                if profile is None:
                    return snapshot, f"数据集「{dataset}」聚合记录缺失，快照停留在阶段 2，等待数据补齐后续跑"
                evaluation = quality_gate.evaluate_dataset(rule, profile)
                snapshot["维度得分"] = evaluation["维度得分"]
                snapshot["权重快照"] = evaluation["权重快照"]
                snapshot["加权明细"] = evaluation["加权明细"]
                snapshot["总分数"] = evaluation["总分数"]
                snapshot["已完成阶段"].append("计算维度加权得分")
                self._maybe_fault(snapshot)

            # 阶段 4：生成评分周期快照（只有走到这里快照才有效、可被服务引用）。
            if "生成评分周期快照" not in snapshot["已完成阶段"]:
                snapshot["阶段状态"] = "已完成"
                snapshot["是否有效"] = True
                snapshot["已完成阶段"].append("生成评分周期快照")
                self._maybe_fault(snapshot)

            # 阶段 5：回写聚合记录（另一个业务面的聚合记录随评分结果同步更新）。
            if "回写聚合记录" not in snapshot["已完成阶段"]:
                self._writeback_aggregate(snapshot)
                snapshot["已完成阶段"].append("回写聚合记录")
                self._maybe_fault(snapshot)

            return snapshot, "评分快照已按完整阶段生成，维度得分与评分周期可追溯"

    def _maybe_fault(self, snapshot: dict[str, Any]) -> None:
        key = _fault_key(snapshot)
        planned = _FAULTS.get(key)
        done = snapshot["已完成阶段"][-1] if snapshot["已完成阶段"] else None
        if planned and done == planned:
            _FAULTS.pop(key, None)
            raise StageFault(f"评分流水在阶段「{planned}」完成后中断，等待从最后完整阶段恢复")

    def _writeback_aggregate(self, snapshot: dict[str, Any]) -> None:
        """把评分结果同步回写聚合配置记录：沿「输出数据集」字段定位同一业务面。"""
        dataset = str(snapshot.get("数据集名称") or "")
        for row in store.rows(AGGREGATE_MODULE):
            if row.get("输出数据集") == dataset:
                row["最近评分快照"] = snapshot.get("快照编号")
                row["最近质量总分"] = snapshot.get("总分数")
                row["最近评分周期"] = snapshot.get("评分周期")
                row["质量回写时间"] = datetime.now().isoformat(timespec="seconds")
                snapshot["回写聚合规则"] = row.get("规则编号")

    # ------------------------------------------------------------ 调用链追溯
    def trace_gap(self, *, dataset: str | None = None, service_id: int | None = None) -> dict[str, Any]:
        """沿调用链 聚合数据集 → 评分规则 → 评分快照 → 数据服务 查找数据缺口。

        每个节点给出 present 状态与缺口说明；first_gap 即链路上第一个断点位，
        别的页面拿到同一份结构即可定位「服务为什么调不到合格快照」。
        """
        service: dict[str, Any] | None = None
        if service_id is not None:
            service = store.find(SERVICE_MODULE, service_id)
            if service is None:
                return {"ok": False, "reason": f"数据服务 {service_id} 不存在", "chain": [], "first_gap": "数据服务"}
            dataset = str(service.get("数据集名称") or "")
        if not dataset:
            return {"ok": False, "reason": "未提供数据集或服务未绑定数据集", "chain": [], "first_gap": "数据集绑定"}

        profile = self._profile(dataset)
        rule = self.active_rule(dataset)
        snapshot = self.latest_valid_snapshot(dataset)
        aggregate_rows = [
            row for row in store.rows(AGGREGATE_MODULE) if row.get("输出数据集") == dataset
        ]
        bound_services = [
            row for row in store.rows(SERVICE_MODULE) if row.get("数据集名称") == dataset
        ]

        chain = [
            {
                "node": "聚合数据集",
                "present": profile is not None,
                "detail": (profile or {}).get("来源聚合规则") or f"缺少数据集「{dataset}」的聚合记录",
            },
            {
                "node": "聚合配置回写",
                "present": bool(aggregate_rows and aggregate_rows[0].get("最近评分快照")),
                "detail": (aggregate_rows[0].get("最近评分快照") if aggregate_rows else None)
                or "聚合记录尚未回写评分结果",
            },
            {
                "node": "生效评分规则",
                "present": rule is not None,
                "detail": (f"{rule.get('规则编号')} v{rule.get('规则版本')}" if rule else "同数据集无生效规则版本"),
            },
            {
                "node": "有效评分快照",
                "present": snapshot is not None,
                "detail": (f"{snapshot.get('快照编号')}（{snapshot.get('评分周期')}，总分 {snapshot.get('总分数')}）"
                           if snapshot else "无完成且有效的评分周期快照"),
            },
            {
                "node": "数据服务绑定",
                "present": bool(bound_services),
                "detail": "、".join(str(row.get("服务编号")) for row in bound_services)
                or "没有数据服务绑定该数据集",
            },
            {
                "node": "服务快照引用",
                "present": bool(service and service.get("绑定评分快照")),
                "detail": (service or {}).get("绑定评分快照") if service else "未指定数据服务，跳过服务级引用检查",
            },
        ]
        first_gap = next((node["node"] for node in chain if not node["present"]), None)
        return {
            "ok": first_gap is None,
            "reason": "调用链完整，数据服务可引用当前门槛快照"
            if first_gap is None else f"调用链在「{first_gap}」节点出现数据缺口",
            "dataset": dataset,
            "service": service,
            "snapshot": snapshot,
            "chain": chain,
            "first_gap": first_gap,
        }


snapshot_service = QualitySnapshotService()
