"""数据质量评分准入链的可审计核心。

设计约束（对应需求中的「评分准入判断需要抽取独立实现并写成一份可审计逻辑」）：

1. 本模块只做评分计算与门槛判定，不写任何状态流转；状态流转仍在各自 service 里。
2. 数据服务列表、服务发布动作、服务调用三处共用这里的同一套函数，
   不允许在路由或页面里再各写一遍准入判断。
3. 每一次判定（通过或阻断）都落一条审计记录到 ``quality_audit`` 表，
   审计记录里同时固化权重、维度得分、总分、门槛与阻断原因，事后可完整回放。
4. 评分与权重归一化为纯函数，给定相同入参必然得到相同结果，便于单元测试与审计复核。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

# 维度 -> (权重字段, 快照得分字段, 聚合数据集原始分字段)
DIMENSIONS: list[tuple[str, str, str, str]] = [
    ("完整性", "完整性权重", "完整性得分", "完整性原始分"),
    ("准确性", "准确性权重", "准确性得分", "准确性原始分"),
    ("时效性", "时效性权重", "时效性得分", "时效性原始分"),
    ("一致性", "一致性权重", "一致性得分", "一致性原始分"),
]

DEFAULT_PASS_SCORE = 60.0
AUDIT_MODULE = "quality_audit"


class GateError(Exception):
    """准入门槛未通过时抛出；message 即展示给业务方的阻断原因。"""

    def __init__(self, reason: str, audit: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.audit = audit


def _invalid_input(message: str) -> GateError:
    """参数解析类错误：此时还没有判定上下文，审计记录留空，由上层拒绝请求。"""
    return GateError(message, audit={})


def parse_ratio(value: Any) -> float:
    """解析权重：兼容 0.3 这种小数权重与 30 这种百分数权重，统一归一到 0~1。"""
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_input(f"权重「{value}」不是合法数字") from exc
    if ratio > 1:  # 30 表示 30%
        ratio = ratio / 100.0
    if not 0.0 <= ratio <= 1.0:
        raise _invalid_input(f"权重「{value}」超出 0~100 的允许范围")
    return ratio


def parse_score(value: Any) -> float:
    """解析维度原始分：合法区间 0~100。"""
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise _invalid_input(f"维度得分「{value}」不是合法数字") from exc
    if not 0.0 <= score <= 100.0:
        raise _invalid_input(f"维度得分「{value}」超出 0~100 的允许范围")
    return round(score, 2)


def normalize_weights(rule: dict[str, Any]) -> dict[str, float]:
    """读取规则的四维权重并归一化，保证权重之和为 1；全为 0 时拒绝计算。"""
    raw = {label: parse_ratio(rule.get(weight_field)) for label, weight_field, _, _ in DIMENSIONS}
    total = sum(raw.values())
    if total <= 0:
        raise _invalid_input("评分规则四维权重之和为 0，无法计算加权总分")
    return {label: round(weight / total, 6) for label, weight in raw.items()}


def evaluate_dataset(rule: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """按权重计算数据质量得分：维度得分、加权过程与总分都在返回值里，供快照与审计追溯。"""
    weights = normalize_weights(rule)
    dimension_scores: dict[str, float] = {}
    weight_trace: list[dict[str, Any]] = []
    total = 0.0
    for label, weight_field, score_field, raw_field in DIMENSIONS:
        raw_score = parse_score(profile.get(raw_field))
        weight = weights[label]
        weighted = round(raw_score * weight, 4)
        total += weighted
        dimension_scores[score_field] = raw_score
        weight_trace.append({
            "维度": label,
            "原始分": raw_score,
            "权重": weight,
            "加权得分": weighted,
        })
    return {
        "维度得分": dimension_scores,
        "权重快照": weights,
        "加权明细": weight_trace,
        "总分数": round(total, 2),
    }


def _threshold_of(rule: dict[str, Any] | None) -> float:
    if not rule:
        return DEFAULT_PASS_SCORE
    try:
        return parse_score(rule.get("总分数"))
    except GateError:
        return DEFAULT_PASS_SCORE

def build_audit(
    *,
    scene: str,
    service: dict[str, Any] | None,
    dataset: str,
    period: str | None,
    snapshot: dict[str, Any] | None,
    rule: dict[str, Any] | None,
    passed: bool,
    reason: str,
) -> dict[str, Any]:
    """组装一条准入审计记录；无论通过与否都调用，保证阻断请求也留痕。"""
    rows = store.rows(AUDIT_MODULE)
    audit_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    return {
        "id": audit_id,
        "审计场景": scene,
        "服务编号": (service or {}).get("服务编号"),
        "数据集名称": dataset,
        "评分周期": period,
        "快照编号": (snapshot or {}).get("id") if snapshot else None,
        "规则编号": (rule or {}).get("规则编号") if rule else None,
        "规则版本": (rule or {}).get("规则版本") if rule else None,
        "维度得分": (snapshot or {}).get("维度得分") if snapshot else None,
        "权重快照": (snapshot or {}).get("权重快照") if snapshot else None,
        "总分数": (snapshot or {}).get("总分数") if snapshot else None,
        "准入门槛": _threshold_of(rule),
        "是否通过": passed,
        "阻断原因": reason,
        "判定时间": datetime.now().isoformat(timespec="seconds"),
    }


def save_audit(audit: dict[str, Any]) -> dict[str, Any]:
    """审计记录落库；集中在一处写入，避免各调用方自行拼装导致口径漂移。"""
    store.rows(AUDIT_MODULE).append(audit)
    return audit


def admission_decision(
    *,
    scene: str,
    service: dict[str, Any] | None,
    dataset: str,
    snapshot: dict[str, Any] | None,
    rule: dict[str, Any] | None,
) -> dict[str, Any]:
    """发布准入与调用准入的唯一判定入口。

    判定顺序即审计顺序：先确认快照存在且属于当前数据集，再确认阶段完整、
    快照仍有效，最后比较总分与门槛。任一环节不通过都写失败审计并抛 GateError。
    """
    period = snapshot.get("评分周期") if snapshot else None

    def fail(reason: str) -> GateError:
        audit = build_audit(
            scene=scene, service=service, dataset=dataset, period=period,
            snapshot=snapshot, rule=rule, passed=False, reason=reason,
        )
        save_audit(audit)
        return GateError(reason, audit)

    if not dataset:
        raise fail("服务未绑定数据集，无法匹配质量评分快照")
    if snapshot is None:
        raise fail(f"数据集「{dataset}」当前评分周期没有可引用的质量评分快照")
    if snapshot.get("数据集名称") != dataset:
        raise fail("服务引用的评分快照与服务数据集不一致")
    if snapshot.get("阶段状态") != "已完成":
        raise fail("评分快照未走完所有完整阶段，不能作为发布依据")
    if not snapshot.get("是否有效"):
        raise fail(f"评分快照已失效（{snapshot.get('失效原因') or '规则版本已切换'}），阻断新请求")
    if rule is None or not rule.get("是否生效版本"):
        raise fail("快照对应的评分规则已不是生效版本，准入链断开")
    threshold = _threshold_of(rule)
    total = float(snapshot.get("总分数", 0.0))
    if total < threshold:
        raise fail(f"质量总分 {total:.2f} 低于准入门槛 {threshold:.2f}，不允许发布")

    audit = build_audit(
        scene=scene, service=service, dataset=dataset, period=period,
        snapshot=snapshot, rule=rule, passed=True, reason="",
    )
    save_audit(audit)
    return audit
