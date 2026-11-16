"""质量评分准入链回归测试。

只依赖标准库 unittest，直接驱动 service 层，覆盖：
- 按权重计算维度得分/总分与快照可追溯字段
- 同周期重复计算只形成一份结果（幂等）
- 快照只能按完整阶段推进，中断后从最后完整阶段恢复
- 评分规则并发启停时同一数据集只有一个生效版本
- 发布准入绑定快照、调用准入失效阻断、审计留痕
- 聚合记录回写与调用链数据缺口追溯
"""
from __future__ import annotations

import threading
import unittest

from app.services import quality_gate
from app.services.dataservice import dataservice_service
from app.services.quality_gate import GateError
from app.services.quality_snapshot import (
    StageFault,
    inject_fault,
    snapshot_service,
)
from app.services.qualityscore import QualityscoreService
from app.store import store


class QualityChainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.quality_service = QualityscoreService()

    # ------------------------------------------------------------ 评分计算
    def test_weighted_score_and_traceable_snapshot(self) -> None:
        snapshot, _ = snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        # 92*.3 + 88*.3 + 85*.2 + 90*.2 = 89.0
        self.assertAlmostEqual(float(snapshot["总分数"]), 89.0, places=2)
        self.assertEqual(snapshot["阶段状态"], "已完成")
        self.assertEqual(snapshot["已完成阶段"], [
            "加载聚合数据集", "解析生效评分规则", "计算维度加权得分",
            "生成评分周期快照", "回写聚合记录",
        ])
        self.assertTrue(snapshot["是否有效"])
        # 维度得分与权重快照都固化在快照里，保证可追溯
        self.assertEqual(set(snapshot["维度得分"]), {
            "完整性得分", "准确性得分", "时效性得分", "一致性得分",
        })
        self.assertAlmostEqual(sum(snapshot["权重快照"].values()), 1.0, places=4)
        self.assertEqual(snapshot["评分周期"], "2026-W46")
        self.assertEqual(len(snapshot["加权明细"]), 4)

    def test_percent_weights_normalized(self) -> None:
        rule = {
            "完整性权重": 30, "准确性权重": 30, "时效性权重": 20, "一致性权重": 20,
        }
        weights = quality_gate.normalize_weights(rule)
        self.assertAlmostEqual(weights["完整性"], 0.3, places=4)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=4)

    def test_invalid_weights_rejected(self) -> None:
        with self.assertRaises(GateError):
            quality_gate.normalize_weights({
                "完整性权重": 0, "准确性权重": 0, "时效性权重": 0, "一致性权重": 0,
            })
        with self.assertRaises(GateError):
            quality_gate.normalize_weights({
                "完整性权重": "x", "准确性权重": 0.3, "时效性权重": 0.3, "一致性权重": 0.1,
            })

    # ------------------------------------------------------------ 幂等
    def test_same_period_single_result(self) -> None:
        first, m1 = snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        second, m2 = snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        self.assertEqual(first["id"], second["id"])  # type: ignore[index]
        self.assertIn("复用", m2)
        rows = [r for r in store.rows("quality_snapshot")
                if r["数据集名称"] == "销售汇总数据集" and r["评分周期"] == "2026-W46"]
        self.assertEqual(len(rows), 1)

    # ------------------------------------------------------------ 阶段恢复
    def test_resume_from_last_completed_stage(self) -> None:
        inject_fault("客流汇总数据集", "2026-W46", "计算维度加权得分")
        with self.assertRaises(StageFault):
            snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        half = snapshot_service.find_snapshot("客流汇总数据集", "2026-W46")
        self.assertIsNotNone(half)
        self.assertEqual(half["阶段状态"], "进行中")  # 半成品不得标记完成
        self.assertEqual(half["已完成阶段"][-1], "计算维度加权得分")
        self.assertFalse(half["是否有效"])

        snapshot, _ = snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        self.assertEqual(snapshot["阶段状态"], "已完成")
        self.assertEqual(len(snapshot["已完成阶段"]), 5)
        # 恢复不产生第二份结果
        rows = [r for r in store.rows("quality_snapshot")
                if r["数据集名称"] == "客流汇总数据集"]
        self.assertEqual(len(rows), 1)

    def test_resume_waits_when_rule_taken_offline_midway(self) -> None:
        # 阶段 2 完成后停用唯一规则，恢复时应返回可读提示而不是异常
        inject_fault("客流汇总数据集", "2026-W46", "解析生效评分规则")
        with self.assertRaises(StageFault):
            snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        self.quality_service.run_action(2, "停用规则")
        snapshot, message = snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["阶段状态"], "进行中")
        self.assertIn("规则恢复", message)
        # 规则恢复（重新启用）后能从中断处继续并完成
        self.quality_service.run_action(2, "启用规则")
        completed, _ = snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        self.assertEqual(completed["阶段状态"], "已完成")
        self.assertEqual(len(completed["已完成阶段"]), 5)

    def test_resume_completes_writeback_after_stage4_fault(self) -> None:
        # 阶段 4 完成后、阶段 5 回写前中断：快照状态已是「已完成」，恢复仍须补回写
        inject_fault("客流汇总数据集", "2026-W46", "生成评分周期快照")
        with self.assertRaises(StageFault):
            snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        half = snapshot_service.find_snapshot("客流汇总数据集", "2026-W46")
        self.assertEqual(half["阶段状态"], "已完成")
        aggregate_row = next(r for r in store.rows("aggregate")
                             if r["输出数据集"] == "客流汇总数据集")
        self.assertIsNone(aggregate_row.get("最近评分快照"))

        completed, _ = snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        self.assertEqual(completed["已完成阶段"][-1], "回写聚合记录")
        self.assertEqual(aggregate_row.get("最近评分快照"), completed["快照编号"])
        # 再次调用回到幂等路径，仍是一份结果
        again, _ = snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        self.assertEqual(again["id"], completed["id"])

    # ------------------------------------------------------------ 规则版本
    def test_only_one_active_rule_version(self) -> None:
        entry, missing = self.quality_service.create_entry({
            "规则编号": "QUAL-0010", "数据集名称": "客流汇总数据集",
            "完整性权重": 0.4, "准确性权重": 0.2, "时效性权重": 0.2, "一致性权重": 0.2,
            "评分周期": "2026-W47", "总分数": 65,
        })
        self.assertEqual(missing, [])
        self.assertEqual(entry["规则版本"], 2)
        self.assertFalse(entry["是否生效版本"])
        self.quality_service.run_action(entry["id"], "启用规则")
        active = [r for r in store.rows("qualityscore")
                  if r["数据集名称"] == "客流汇总数据集" and r["是否生效版本"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["规则编号"], "QUAL-0010")
        self.assertEqual(store.find("qualityscore", 2)["status"], "已停用")

    def test_concurrent_activation_single_winner(self) -> None:
        entry, _ = self.quality_service.create_entry({
            "规则编号": "QUAL-0010", "数据集名称": "客流汇总数据集",
            "完整性权重": 0.4, "准确性权重": 0.2, "时效性权重": 0.2, "一致性权重": 0.2,
            "评分周期": "2026-W47", "总分数": 65,
        })
        threads = [
            threading.Thread(target=self.quality_service.run_action, args=(entry["id"], "启用规则")),
            threading.Thread(target=self.quality_service.run_action, args=(2, "启用规则")),
            threading.Thread(target=self.quality_service.run_action, args=(entry["id"], "启用规则")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        active = [r for r in store.rows("qualityscore")
                  if r["数据集名称"] == "客流汇总数据集" and r["是否生效版本"]]
        self.assertEqual(len(active), 1)

    def test_rule_switch_invalidates_snapshots(self) -> None:
        snapshot_service.generate_snapshot("客流汇总数据集", "2026-W46")
        entry, _ = self.quality_service.create_entry({
            "规则编号": "QUAL-0010", "数据集名称": "客流汇总数据集",
            "完整性权重": 0.4, "准确性权重": 0.2, "时效性权重": 0.2, "一致性权重": 0.2,
            "评分周期": "2026-W47", "总分数": 65,
        })
        self.quality_service.run_action(entry["id"], "启用规则")
        snapshot = snapshot_service.find_snapshot("客流汇总数据集", "2026-W46")
        self.assertFalse(snapshot["是否有效"])
        self.assertIn("版本切换", snapshot["失效原因"])

    # ------------------------------------------------------------ 发布/调用准入
    def test_publish_blocked_without_snapshot(self) -> None:
        entry, message = dataservice_service.run_action(2, "上线服务", {})
        self.assertIsNone(entry)
        self.assertIn("质量准入链阻断", message)

    def test_publish_binds_snapshot_and_invoke_records_it(self) -> None:
        snapshot, _ = snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        entry, message = dataservice_service.run_action(1, "上线服务", {})
        self.assertIsNotNone(entry)
        self.assertEqual(entry["绑定评分快照"], snapshot["id"])
        self.assertEqual(entry["准入结论"], "通过")

        result, invoke_message = dataservice_service.invoke(1)
        self.assertIsNotNone(result)
        self.assertEqual(result["评分快照编号"], snapshot["快照编号"])
        self.assertEqual(result["评分周期"], "2026-W46")
        self.assertIn("维度得分", result)

    def test_invoke_blocked_after_snapshot_invalidated(self) -> None:
        snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        dataservice_service.run_action(1, "上线服务", {})
        result, _ = dataservice_service.invoke(1)
        self.assertIsNotNone(result)

        self.quality_service.run_action(1, "停用规则")
        blocked, message = dataservice_service.invoke(1)
        self.assertIsNone(blocked)
        self.assertIn("阻断", message)

    def test_below_threshold_blocks_publish(self) -> None:
        # 把销售画像改成全维度 10 分，总分 10，低于门槛 60
        profile = next(r for r in store.rows("dataset_profile")
                       if r["数据集名称"] == "销售汇总数据集")
        for field in ("完整性原始分", "准确性原始分", "时效性原始分", "一致性原始分"):
            profile[field] = 10.0
        snapshot, _ = snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        self.assertAlmostEqual(float(snapshot["总分数"]), 10.0, places=2)
        entry, message = dataservice_service.run_action(1, "上线服务", {})
        self.assertIsNone(entry)
        self.assertIn("低于准入门槛", message)

    def test_every_decision_audited(self) -> None:
        snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        dataservice_service.run_action(1, "上线服务", {})
        dataservice_service.invoke(1)
        self.quality_service.run_action(1, "停用规则")
        dataservice_service.invoke(1)
        audits = snapshot_service.list_audits(dataset="销售汇总数据集")
        scenes = {(a["审计场景"], a["是否通过"]) for a in audits}
        self.assertIn(("服务发布准入", True), scenes)
        self.assertIn(("服务调用准入", True), scenes)
        self.assertIn(("服务调用准入", False), scenes)
        # 失败审计同样固化维度得分与门槛，可回放
        failed = next(a for a in audits if not a["是否通过"])
        self.assertIsNotNone(failed["维度得分"])
        self.assertEqual(failed["准入门槛"], 60.0)

    # ------------------------------------------------------------ 回写与追溯
    def test_aggregate_writeback(self) -> None:
        snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        aggregate_row = next(r for r in store.rows("aggregate")
                             if r["输出数据集"] == "销售汇总数据集")
        self.assertEqual(aggregate_row["最近评分快照"], "SNAP-0001")
        self.assertEqual(aggregate_row["最近评分周期"], "2026-W46")
        self.assertAlmostEqual(float(aggregate_row["最近质量总分"]), 89.0, places=2)

    def test_trace_gap_finds_missing_node(self) -> None:
        # 库存数据集没有生效规则，调用链应在规则节点之前/之处报告缺口
        trace = snapshot_service.trace_gap(service_id=3)
        self.assertFalse(trace["ok"])
        self.assertIn(trace["first_gap"], {"聚合配置回写", "生效评分规则", "有效评分快照"})
        node_names = [node["node"] for node in trace["chain"]]
        self.assertEqual(node_names, [
            "聚合数据集", "聚合配置回写", "生效评分规则",
            "有效评分快照", "数据服务绑定", "服务快照引用",
        ])

    def test_trace_complete_chain(self) -> None:
        snapshot_service.generate_snapshot("销售汇总数据集", "2026-W46")
        dataservice_service.run_action(1, "上线服务", {})
        trace = snapshot_service.trace_gap(service_id=1)
        self.assertTrue(trace["ok"], trace["reason"])
        self.assertIsNone(trace["first_gap"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
