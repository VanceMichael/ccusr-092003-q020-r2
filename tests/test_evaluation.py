
import os
import tempfile
import unittest
from datetime import datetime

_tmp_dir = tempfile.mkdtemp(prefix="eval-test-")
os.environ["DATABASE_PATH"] = os.path.join(_tmp_dir, "test.sqlite3")

from scripts.migrate import run_migrations  # noqa: E402
from app import service as svc  # noqa: E402
from app.db import default_database_path  # noqa: E402

run_migrations(default_database_path())

SHA_A = "a" * 64
SHA_B = "b" * 64

# 固定时间窗口，覆盖测试运行日期
WINDOW = ("2000-01-01T00:00:00+08:00", "2100-01-01T00:00:00+08:00")
FAR_FUTURE = "2100-01-01T00:00:00+08:00"
PAST = "2000-01-01T00:00:00+08:00"


def attrs_power(node: str, tariff: float = 320.0) -> dict:
    return {"grid_node_ref": node, "window_start": WINDOW[0],
            "window_end": WINDOW[1], "tariff_cny_per_mwh": tariff}


def make_evidence(ref: str, dimension: str, version: str, attributes: dict,
                  sha: str = SHA_A, effective: str = "2026-08-01T00:00:00+08:00") -> dict:
    return svc.register_evidence({
        "evidence_ref": ref, "dimension": dimension, "version_label": version,
        "source_ref": f"vault://docs/{ref}/{version}", "source_sha256": sha,
        "attributes": attributes, "effective_at": effective,
    })


def make_proposal(ref: str, mode: str, mw: float) -> dict:
    return svc.register_proposal({
        "proposal_ref": ref, "title": f"方案-{ref}", "service_mode": mode,
        "capacity_mw": mw, "submitted_at": "2026-09-01T09:00:00+08:00",
    })


def full_in_domain(ref: str, node: str, *, mw: float = 10,
                   data_status: str = "GRANTED",
                   residency: str = "IN_DOMAIN_ONLY") -> None:
    make_proposal(ref, "IN_DOMAIN", mw)
    make_evidence(f"EV-PW-{ref}", "power_window", "v1", attrs_power(node))
    make_evidence(f"EV-DC-{ref}", "datacenter_stage", "v1",
                  {"stage": "OPERATIONAL", "delivered_mw": mw + 2})
    make_evidence(f"EV-DS-{ref}", "data_scope", "v1",
                  {"authorization_status": data_status,
                   "scope_labels": ["telemetry", "maintenance"],
                   "expires_at": FAR_FUTURE})
    make_evidence(f"EV-RS-{ref}", "residency", "v1", {"mode": residency})


def lock_required(round_id: str, ref: str, mode: str) -> None:
    pairs = [("power_window", f"EV-PW-{ref}"),
             ("datacenter_stage", f"EV-DC-{ref}"),
             ("data_scope", f"EV-DS-{ref}"),
             ("residency", f"EV-RS-{ref}")]
    if mode == "CROSS_BORDER":
        pairs += [("research_authorization", f"EV-RA-{ref}"),
                  ("service_area", f"EV-SA-{ref}")]
    for dimension, evref in pairs:
        svc.lock_evidence(round_id, {
            "proposal_ref": ref, "dimension": dimension,
            "evidence_ref": evref, "version_label": "v1",
        })


class EvidenceLockingTest(unittest.TestCase):
    def test_new_revision_does_not_change_locked_version(self) -> None:
        make_proposal("P-LOCK", "IN_DOMAIN", 10)
        svc.set_commitment("GRID-LOCK", {"committed_mw": 20})
        make_evidence("EV-PW-P-LOCK", "power_window", "v1", attrs_power("GRID-LOCK"))
        make_evidence("EV-DC-P-LOCK", "datacenter_stage", "v1",
                      {"stage": "PLANNING", "delivered_mw": 0})
        make_evidence("EV-DS-P-LOCK", "data_scope", "v1",
                      {"authorization_status": "PENDING", "scope_labels": ["x"]})
        make_evidence("EV-RS-P-LOCK", "residency", "v1", {"mode": "NONE"})
        round_id = svc.create_round({"note": "锁定测试"})["round_id"]
        lock_required(round_id, "P-LOCK", "IN_DOMAIN")

        # 后来登记的新版本不会被评审悄悄采用
        make_evidence("EV-DS-P-LOCK", "data_scope", "v2",
                      {"authorization_status": "GRANTED", "scope_labels": ["x"],
                       "expires_at": FAR_FUTURE})
        make_evidence("EV-RS-P-LOCK", "residency", "v2", {"mode": "IN_DOMAIN_ONLY"})
        evaluation = svc.evaluate(round_id, "P-LOCK")
        dims = {d["dimension"]: d for d in evaluation["dimensions"]}
        self.assertEqual(dims["data_scope"]["locked_revision"]["version_label"], "v1")
        self.assertEqual(dims["residency"]["locked_revision"]["version_label"], "v1")
        self.assertEqual(dims["data_scope"]["status"], "GAP")

        # 显式重锁到新版本后才采用
        svc.lock_evidence(round_id, {"proposal_ref": "P-LOCK",
                                     "dimension": "data_scope",
                                     "evidence_ref": "EV-DS-P-LOCK",
                                     "version_label": "v2"})
        svc.lock_evidence(round_id, {"proposal_ref": "P-LOCK",
                                     "dimension": "residency",
                                     "evidence_ref": "EV-RS-P-LOCK",
                                     "version_label": "v2"})
        evaluation = svc.evaluate(round_id, "P-LOCK")
        dims = {d["dimension"]: d for d in evaluation["dimensions"]}
        self.assertEqual(dims["data_scope"]["status"], "OK")
        self.assertEqual(dims["residency"]["status"], "OK")
        # 机房阶段仍 PLANNING，整体尚不可获批
        self.assertEqual(dims["datacenter_stage"]["status"], "GAP")
        self.assertFalse(evaluation["ready_for_approval"])

    def test_evidence_is_immutable_new_version_is_new_row(self) -> None:
        make_evidence("EV-IMMUT", "residency", "v1", {"mode": "NONE"})
        with self.assertRaises(svc.DomainError) as ctx:
            make_evidence("EV-IMMUT", "residency", "v1",
                          {"mode": "IN_DOMAIN_ONLY"})
        self.assertEqual(ctx.exception.status, 409)

    def test_dimension_mismatch_rejected(self) -> None:
        make_proposal("P-MISMATCH", "IN_DOMAIN", 1)
        svc.set_commitment("GRID-MM", {"committed_mw": 5})
        make_evidence("EV-MM", "residency", "v1", {"mode": "IN_DOMAIN_ONLY"})
        round_id = svc.create_round({"note": "维度错配"})["round_id"]
        with self.assertRaises(svc.DomainError):
            svc.lock_evidence(round_id, {"proposal_ref": "P-MISMATCH",
                                         "dimension": "power_window",
                                         "evidence_ref": "EV-MM",
                                         "version_label": "v1"})

    def test_closed_round_is_frozen(self) -> None:
        make_proposal("P-CLOSED", "IN_DOMAIN", 1)
        make_evidence("EV-CLOSED", "residency", "v1", {"mode": "IN_DOMAIN_ONLY"})
        round_id = svc.create_round({"note": "关闭测试"})["round_id"]
        svc.close_round(round_id)
        with self.assertRaises(svc.DomainError):
            svc.lock_evidence(round_id, {"proposal_ref": "P-CLOSED",
                                         "dimension": "residency",
                                         "evidence_ref": "EV-CLOSED",
                                         "version_label": "v1"})


class CapacityTest(unittest.TestCase):
    def test_total_reserved_cannot_exceed_commitment(self) -> None:
        node = "GRID-CONC"
        svc.set_commitment(node, {"committed_mw": 20})
        round_id = svc.create_round({"note": "容量并发"})["round_id"]
        for i in range(10):
            ref = f"P-CONC{i}"
            full_in_domain(ref, node, mw=3)

        import threading
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def worker(i: int) -> None:
            try:
                barrier.wait()
                svc.lock_evidence(round_id, {
                    "proposal_ref": f"P-CONC{i}", "dimension": "power_window",
                    "evidence_ref": f"EV-PW-P-CONC{i}", "version_label": "v1"})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        held = [r for r in svc.list_reservations(node) if r["status"] == "held"
                and r["round_id"] == round_id]
        total = sum(r["reserved_mw"] for r in held)
        self.assertLessEqual(total, 20 + 1e-9)
        # 10 * 3 = 30 > 20，至少有方案被挡下
        self.assertTrue(errors)
        self.assertTrue(any("容量不足" in str(e) for e in errors))

    def test_commitment_reduction_only_affects_unlocked(self) -> None:
        node = "GRID-REDUCE"
        svc.set_commitment(node, {"committed_mw": 15})
        round_id = svc.create_round({"note": "承诺下调"})["round_id"]
        full_in_domain("P-HELD", node, mw=10)
        full_in_domain("P-WAIT", node, mw=8)
        svc.lock_evidence(round_id, {"proposal_ref": "P-HELD",
                                     "dimension": "power_window",
                                     "evidence_ref": "EV-PW-P-HELD",
                                     "version_label": "v1"})
        # 节点容量被别的项目占用：承诺下调到 10（已持有 10）
        status = svc.set_commitment(node, {"committed_mw": 10})
        self.assertAlmostEqual(status["available_mw"], 0.0, places=6)

        # 尚未锁定的方案被挡住
        with self.assertRaises(svc.DomainError) as ctx:
            svc.lock_evidence(round_id, {"proposal_ref": "P-WAIT",
                                         "dimension": "power_window",
                                         "evidence_ref": "EV-PW-P-WAIT",
                                         "version_label": "v1"})
        self.assertEqual(ctx.exception.status, 409)

        # 已锁定方案的预留与评估不受影响
        held = [r for r in svc.list_reservations(node) if r["status"] == "held"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["proposal_ref"], "P-HELD")

        # 解锁后容量立即还给未锁定方案
        svc.unlock_evidence(round_id, "P-HELD", "power_window")
        svc.lock_evidence(round_id, {"proposal_ref": "P-WAIT",
                                     "dimension": "power_window",
                                     "evidence_ref": "EV-PW-P-WAIT",
                                     "version_label": "v1"})
        held = [r for r in svc.list_reservations(node) if r["status"] == "held"]
        self.assertEqual([r["proposal_ref"] for r in held], ["P-WAIT"])

    def test_power_lock_without_commitment_rejected(self) -> None:
        make_proposal("P-NOCOMMIT", "IN_DOMAIN", 5)
        make_evidence("EV-PW-NOCOMMIT", "power_window", "v1",
                      attrs_power("GRID-NEVER"))
        round_id = svc.create_round({"note": "无承诺"})["round_id"]
        with self.assertRaises(svc.DomainError):
            svc.lock_evidence(round_id, {"proposal_ref": "P-NOCOMMIT",
                                         "dimension": "power_window",
                                         "evidence_ref": "EV-PW-NOCOMMIT",
                                         "version_label": "v1"})


class GapAndComparisonTest(unittest.TestCase):
    def test_cross_border_requires_six_dimensions(self) -> None:
        node = "GRID-XB"
        svc.set_commitment(node, {"committed_mw": 30})
        make_proposal("P-XB", "CROSS_BORDER", 10)
        make_evidence("EV-PW-P-XB", "power_window", "v1", attrs_power(node))
        make_evidence("EV-DC-P-XB", "datacenter_stage", "v1",
                      {"stage": "OPERATIONAL", "delivered_mw": 12})
        make_evidence("EV-DS-P-XB", "data_scope", "v1",
                      {"authorization_status": "GRANTED", "scope_labels": ["t"],
                       "expires_at": FAR_FUTURE})
        # 驻留只允许境内
        make_evidence("EV-RS-P-XB", "residency", "v1", {"mode": "IN_DOMAIN_ONLY"})
        make_evidence("EV-RA-P-XB", "research_authorization", "v1",
                      {"status": "PENDING", "partner_refs": ["UNIV-NE"],
                       "expires_at": FAR_FUTURE})
        make_evidence("EV-SA-P-XB", "service_area", "v1",
                      {"regions": ["NE-ASIA"], "cross_border_intent": "NONE"})
        round_id = svc.create_round({"note": "跨境"})["round_id"]
        lock_required(round_id, "P-XB", "CROSS_BORDER")
        evaluation = svc.evaluate(round_id, "P-XB")
        dims = {d["dimension"]: d for d in evaluation["dimensions"]}
        self.assertFalse(evaluation["ready_for_approval"])
        self.assertIn("跨境", dims["residency"]["reason"])
        self.assertEqual(dims["research_authorization"]["status"], "GAP")
        self.assertEqual(dims["service_area"]["status"], "GAP")

    def test_in_domain_comparable_without_cross_border_conditions(self) -> None:
        node = "GRID-DOM"
        svc.set_commitment(node, {"committed_mw": 30})
        round_id = svc.create_round({"note": "不出域比较"})["round_id"]
        full_in_domain("P-DOM1", node, mw=10)
        full_in_domain("P-DOM2", node, mw=8, data_status="PENDING")
        lock_required(round_id, "P-DOM1", "IN_DOMAIN")
        lock_required(round_id, "P-DOM2", "IN_DOMAIN")

        comparison = svc.compare(round_id, "IN_DOMAIN")
        refs = [i["proposal_ref"] for i in comparison["items"]]
        self.assertEqual(refs[0], "P-DOM1")  # 就绪方案排前
        self.assertTrue(comparison["items"][0]["ready_for_approval"])
        self.assertFalse(comparison["items"][1]["ready_for_approval"])

    def test_expired_authorization_is_a_gap(self) -> None:
        node = "GRID-EXP"
        svc.set_commitment(node, {"committed_mw": 10})
        full_in_domain("P-EXP", node, mw=5)
        round_id = svc.create_round({"note": "过期授权"})["round_id"]
        lock_required(round_id, "P-EXP", "IN_DOMAIN")
        as_of = datetime.fromisoformat("2150-01-01T00:00:00+08:00")
        evaluation = svc.evaluate(round_id, "P-EXP", as_of)
        dims = {d["dimension"]: d for d in evaluation["dimensions"]}
        self.assertEqual(dims["data_scope"]["status"], "GAP")
        self.assertIn("过期", dims["data_scope"]["reason"])

    def test_window_not_effective_is_a_gap(self) -> None:
        node = "GRID-WIN"
        svc.set_commitment(node, {"committed_mw": 10})
        full_in_domain("P-WIN", node, mw=5)
        round_id = svc.create_round({"note": "窗口外"})["round_id"]
        lock_required(round_id, "P-WIN", "IN_DOMAIN")
        evaluation = svc.evaluate(
            round_id, "P-WIN",
            datetime.fromisoformat("1999-01-01T00:00:00+08:00"))
        dims = {d["dimension"]: d for d in evaluation["dimensions"]}
        self.assertEqual(dims["power_window"]["status"], "GAP")


class DecisionAndHistoryTest(unittest.TestCase):
    def test_approval_gating_snapshot_and_history(self) -> None:
        node = "GRID-DEC"
        svc.set_commitment(node, {"committed_mw": 20})
        round_id = svc.create_round({"note": "决策"})["round_id"]
        full_in_domain("P-DEC", node, mw=10)
        lock_required(round_id, "P-DEC", "IN_DOMAIN")

        # 全部满足，获批成功
        decision = svc.decide("P-DEC", {
            "round_id": round_id, "outcome": "APPROVED",
            "rationale_ref": "vault://decisions/P-DEC"})
        self.assertEqual(decision["outcome"], "APPROVED")
        self.assertTrue(decision["snapshot"]["evaluation"]["ready_for_approval"])

        # 获批后锁定冻结
        with self.assertRaises(svc.DomainError):
            svc.unlock_evidence(round_id, "P-DEC", "residency")

        # 登记证据新版本与外部变化
        make_evidence("EV-DS-P-DEC", "data_scope", "v2",
                      {"authorization_status": "PENDING", "scope_labels": ["t"]},
                      sha=SHA_B)
        svc.set_commitment(node, {"committed_mw": 8})  # 被别的项目占用
        svc.record_change("P-DEC", {
            "changed_at": "2026-10-05T10:00:00+08:00", "dimension": "data_scope",
            "summary": "工业数据授权范围收缩",
            "new_evidence_ref": "EV-DS-P-DEC", "new_source_sha256": SHA_B})
        svc.record_change("P-DEC", {
            "changed_at": "2026-10-06T10:00:00+08:00", "dimension": "grid_capacity",
            "summary": "变电节点余量被新项目占用"})

        history = svc.history("P-DEC")
        original = history["original_judgement"]
        self.assertEqual(original["why_feasible"]["gap_count"], 0)
        locked_ds = [l for l in original["evidence_versions_locked"]
                     if l["dimension"] == "data_scope"][0]
        self.assertEqual(locked_ds["revision"]["version_label"], "v1")

        # 原判断容量口径保留决策时的承诺值
        self.assertEqual(len(original["capacity_basis"]), 1)

        changes = history["later_changes_recorded"]
        self.assertEqual([c["dimension"] for c in changes],
                         ["data_scope", "grid_capacity"])

        current_ds = history["current_state_vs_snapshot"]["evidence"]["data_scope"]
        self.assertEqual(current_ds["at_decision"]["version_label"], "v1")
        self.assertEqual(current_ds["latest_now"]["version_label"], "v2")
        self.assertTrue(current_ds["newer_version_exists"])

        current_cap = history["current_state_vs_snapshot"]["capacity"][0]
        self.assertEqual(current_cap["at_decision"]["reserved_mw"], 10)
        self.assertEqual(current_cap["committed_mw_now"], 8)

    def test_rejection_releases_capacity(self) -> None:
        node = "GRID-REJ"
        svc.set_commitment(node, {"committed_mw": 10})
        round_id = svc.create_round({"note": "驳回释放"})["round_id"]
        full_in_domain("P-REJ", node, mw=10, data_status="PENDING")
        lock_required(round_id, "P-REJ", "IN_DOMAIN")
        svc.decide("P-REJ", {"round_id": round_id, "outcome": "REJECTED",
                             "rationale_ref": "vault://decisions/P-REJ"})
        held = [r for r in svc.list_reservations(node) if r["status"] == "held"]
        self.assertEqual(held, [])


class RawDataProtectionTest(unittest.TestCase):
    def test_unknown_attributes_rejected(self) -> None:
        with self.assertRaises(Exception):
            svc.register_evidence({
                "evidence_ref": "EV-SECRET", "dimension": "data_scope",
                "version_label": "v1", "source_ref": "vault://x",
                "source_sha256": SHA_A,
                "attributes": {"authorization_status": "GRANTED",
                               "scope_labels": ["x"],
                               "company_raw_record": "企业原始台账内容"},
                "effective_at": "2026-08-01T00:00:00+08:00"})

    def test_outputs_contain_only_controlled_fields(self) -> None:
        make_evidence("EV-PUBLIC", "residency", "v1", {"mode": "IN_DOMAIN_ONLY"})
        revisions = svc.list_evidence("residency")
        target = [r for r in revisions if r["evidence_ref"] == "EV-PUBLIC"][0]
        self.assertEqual(set(target.keys()),
                         {"revision_id", "evidence_ref", "dimension",
                          "version_label", "source_ref", "source_sha256",
                          "attributes", "effective_at", "recorded_at"})
        self.assertEqual(target["attributes"], {"mode": "IN_DOMAIN_ONLY"})
        # 材料只保留引用与摘要
        self.assertEqual(len(target["source_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
