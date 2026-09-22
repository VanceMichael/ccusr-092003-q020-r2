
import json
import os
import tempfile
import threading
import unittest
import http.client
from datetime import datetime, timedelta, timezone

from app.main import make_server

SHA_OK = "a" * 64


def iso(offset_days: float = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(days=offset_days)
    ).isoformat(timespec="seconds")


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.db_path)
        self._old_db = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = self.db_path
        self.server = make_server(host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self._old_db is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_db
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def call(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        conn.request(
            method, path, body=payload,
            headers={"Content-Type": "application/json"} if payload else {},
        )
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        conn.close()
        parsed = json.loads(raw) if raw else {}
        return response.status, parsed

    # ---------- 场景搭建助手 ----------

    def add_evidence(
        self, evidence_id, dimension, subject_ref, revision=1,
        status="issued", fact=None, offset_from=-30, offset_to=30,
        create=True,
    ):
        if create:
            status_code, created = self.call("POST", "/evidence", {
                "evidence_id": evidence_id,
                "dimension": dimension,
                "subject_ref": subject_ref,
                "title": f"{evidence_id} title",
            })
            self.assertEqual(status_code, 201, created)
        body = {
            "revision": revision,
            "status": status,
            "effective_from": iso(offset_from),
            "effective_to": iso(offset_to) if offset_to is not None else None,
            "payload_ref": f"DOC-{evidence_id}",
            "payload_sha256": SHA_OK,
        }
        if fact is not None:
            body["fact"] = fact
        status_code, resp = self.call(
            "POST", f"/evidence/{evidence_id}/revisions", body
        )
        self.assertEqual(status_code, 201, resp)
        return resp

    def add_node(self, ref="GRID-A", site="SITE-A", committed="30"):
        code, resp = self.call("POST", "/grid-nodes", {
            "grid_node_ref": ref, "site_ref": site, "label": f"节点{ref}",
        })
        self.assertEqual(code, 201, resp)
        code, resp = self.call(
            "POST", f"/grid-nodes/{ref}/commitments",
            {"revision": 1, "committed_mw": committed,
             "effective_from": iso(-10)},
        )
        self.assertEqual(code, 201, resp)

    def add_proposal(
        self, ref="P-1", demand="20", target="cross_border",
        categories=None, site="SITE-A", node="GRID-A",
        collection="COLL-A", grant="PROG-A", max_price=None,
    ):
        body = {
            "proposal_ref": ref,
            "site_ref": site,
            "grid_node_ref": node,
            "data_collection_ref": collection,
            "research_grant_ref": grant,
            "demand_mw": demand,
            "required_data_categories": categories or ["energy", "logistics"],
            "target_mode": target,
        }
        if max_price is not None:
            body["max_price_cny_per_kwh"] = max_price
        code, resp = self.call("POST", "/proposals", body)
        self.assertEqual(code, 201, resp)
        return resp

    def ready_facts(self, *, cross_border=True, stage="operational"):
        return {
            "power_window": {
                "price_cny_per_kwh": "0.32",
                "window_from": iso(-20), "window_to": iso(20),
            },
            "datacenter_stage": {"stage": stage, "available_mw": "25"},
            "data_scope": {"categories": ["energy", "logistics"]},
            "residency": {
                "cross_border_allowed": cross_border,
                "destinations": ["KR", "JP"] if cross_border else [],
            },
            "research_grant": {"granted": True, "program_ref": "PROG-A"},
            "service_region": {
                "regions": ["CN", "KR", "JP"] if cross_border else ["CN"]
            },
        }

    def seed_ready_world(
        self, *, cross_border=True, subjects=None, prefix="E",
    ):
        subjects = subjects or {
            "power_window": "SITE-A",
            "datacenter_stage": "SITE-A",
            "data_scope": "COLL-A",
            "research_grant": "PROG-A",
        }
        facts = self.ready_facts(cross_border=cross_border)
        ids = {}
        for dimension, fact in facts.items():
            subject = subjects.get(dimension, "POLICY-REF")
            evidence_id = f"{prefix}-{dimension}"
            self.add_evidence(
                evidence_id, dimension, subject, fact=fact
            )
            ids[dimension] = evidence_id
        return ids

    def open_round(self, round_id="R1"):
        code, resp = self.call("POST", "/rounds", {"round_id": round_id})
        self.assertEqual(code, 201, resp)

    def lock_all(self, round_id, proposal_ref, ids):
        for dimension, evidence_id in ids.items():
            code, resp = self.call(
                "POST",
                f"/rounds/{round_id}/proposals/{proposal_ref}/locks",
                {"dimension": dimension, "evidence_id": evidence_id,
                 "revision": 1},
            )
            self.assertEqual(code, 201, resp)


class FullFlowTest(ApiCase):
    def test_happy_path_to_approval_and_history(self):
        self.add_node()
        self.add_proposal()
        ids = self.seed_ready_world()
        self.open_round()

        # 锁定前快照：六个维度全部缺锁，容量未锁
        code, snap = self.call("GET", "/rounds/R1/proposals/P-1/snapshot")
        self.assertEqual(code, 200)
        self.assertEqual(
            {g["gap"] for g in snap["assessment"]["cross_border"]["gaps"]},
            {"missing_lock"},
        )
        self.assertEqual(len(snap["assessment"]["cross_border"]["gaps"]), 6)
        self.assertFalse(snap["capacity"]["locked"])

        self.lock_all("R1", "P-1", ids)
        code, resp = self.call(
            "POST", "/rounds/R1/proposals/P-1/capacity", {}
        )
        self.assertEqual(code, 201, resp)

        code, snap = self.call("GET", "/rounds/R1/proposals/P-1/snapshot")
        self.assertEqual(code, 200)
        self.assertTrue(snap["assessment"]["cross_border_ready"])
        self.assertEqual(snap["capacity"]["remaining_mw"], "10")

        code, decision = self.call(
            "POST", "/rounds/R1/proposals/P-1/decision",
            {"outcome": "approved", "rationale": "六维证据齐备且容量已锁"},
        )
        self.assertEqual(code, 201, decision)
        self.assertEqual(decision["outcome"], "approved")
        decision_id = decision["decision_id"]

        # 决策后：证据出新版本、旧版本被取代
        self.add_evidence(
            ids["power_window"], "power_window", "SITE-A",
            revision=2, create=False, fact={
                "price_cny_per_kwh": "0.45",
                "window_from": iso(-1), "window_to": iso(40),
            },
        )
        code, resp = self.call(
            "POST", f"/evidence/{ids['power_window']}/revisions/transition",
            {"revision": 1, "to_status": "superseded",
             "note": "新一轮电价窗口发布"},
        )
        self.assertEqual(code, 200, resp)

        code, history = self.call("GET", f"/decisions/{decision_id}")
        self.assertEqual(code, 200)
        # 冻结快照保持原判断：按锁定版本电价 0.32、跨境就绪
        frozen = history["frozen_snapshot"]
        self.assertTrue(
            frozen["assessment"]["ready"]
        )
        self.assertEqual(
            frozen["locks"]["power_window"]["fact"]["price_cny_per_kwh"],
            0.32,
        )
        # 后来变化单独呈现
        kinds = {c["kind"] for c in history["history"]["subsequent_changes"]}
        self.assertIn("evidence_superseded", kinds)
        notes = [n["kind"] for n in history["history"]["subsequent_notes"]]
        self.assertIn("evidence_superseded", notes)
        self.assertIn("当时", history["history"]["basis_statement"])


class EvidenceVersionTest(ApiCase):
    def test_draft_and_future_revision_cannot_be_locked(self):
        self.add_node()
        self.add_proposal()
        # 电力证据：草案版本，且发布生效日在未来
        self.add_evidence(
            "E-PW", "power_window", "SITE-A",
            status="draft", offset_from=1, offset_to=10,
            fact={"price_cny_per_kwh": "0.30",
                  "window_from": iso(-1), "window_to": iso(10)},
        )
        self.open_round()
        code, resp = self.call(
            "POST", "/rounds/R1/proposals/P-1/locks",
            {"dimension": "power_window", "evidence_id": "E-PW", "revision": 1},
        )
        self.assertEqual(code, 409)
        self.assertIn("issued", resp["error"])

        # 发布后窗口若在未来，仍不能锁（意向不等于落实）
        code, resp = self.call(
            "POST", "/evidence/E-PW/revisions/transition",
            {"revision": 1, "to_status": "issued"},
        )
        self.assertEqual(code, 200, resp)
        code, resp = self.call(
            "POST", "/rounds/R1/proposals/P-1/locks",
            {"dimension": "power_window", "evidence_id": "E-PW", "revision": 1},
        )
        self.assertEqual(code, 409)
        self.assertIn("生效", resp["error"])

    def test_locked_dimension_cannot_be_swapped_within_round(self):
        self.add_node()
        self.add_proposal()
        self.add_evidence(
            "E-PW", "power_window", "SITE-A",
            fact={"price_cny_per_kwh": "0.32",
                  "window_from": iso(-5), "window_to": iso(5)},
        )
        self.open_round()
        code, _ = self.call(
            "POST", "/rounds/R1/proposals/P-1/locks",
            {"dimension": "power_window", "evidence_id": "E-PW", "revision": 1},
        )
        self.assertEqual(code, 201)
        code, resp = self.call(
            "POST", "/rounds/R1/proposals/P-1/locks",
            {"dimension": "power_window", "evidence_id": "E-PW", "revision": 1},
        )
        self.assertEqual(code, 409)
        self.assertIn("不可更换", resp["error"])

    def test_evidence_subject_must_match_proposal(self):
        self.add_node()
        self.add_proposal()
        self.add_evidence(
            "E-DS", "data_scope", "COLL-OTHER",
            fact={"categories": ["energy"]},
        )
        self.open_round()
        code, resp = self.call(
            "POST", "/rounds/R1/proposals/P-1/locks",
            {"dimension": "data_scope", "evidence_id": "E-DS", "revision": 1},
        )
        self.assertEqual(code, 400)
        self.assertIn("不一致", resp["error"])

    def test_data_category_gap_and_price_gap(self):
        self.add_node()
        self.add_proposal(categories=["energy", "patent"])
        self.add_evidence(
            "E-DS", "data_scope", "COLL-A",
            fact={"categories": ["energy"]},
        )
        self.open_round()
        code, _ = self.call(
            "POST", "/rounds/R1/proposals/P-1/locks",
            {"dimension": "data_scope", "evidence_id": "E-DS", "revision": 1},
        )
        self.assertEqual(code, 201)
        code, snap = self.call("GET", "/rounds/R1/proposals/P-1/snapshot")
        gaps = {g["gap"] for g in snap["assessment"]["cross_border"]["gaps"]}
        self.assertIn("data_category_uncovered", gaps)

        # 电价高于方案上限
        self.add_evidence(
            "E-PW", "power_window", "SITE-A",
            fact={"price_cny_per_kwh": "0.5",
                  "window_from": iso(-5), "window_to": iso(5)},
        )
        code, _ = self.call(
            "POST", "/rounds/R1/proposals/P-1/locks",
            {"dimension": "power_window", "evidence_id": "E-PW", "revision": 1},
        )
        self.assertEqual(code, 201)
        code, snap = self.call("GET", "/rounds/R1/proposals/P-1/snapshot")
        gap_dims = {g["dimension"]: g["gap"]
                    for g in snap["assessment"]["cross_border"]["gaps"]}
        # 方案未设上限 -> 不报价格缺口；改设上限后再验
        self.assertNotIn("power_window", gap_dims)

    def test_price_above_limit_flagged(self):
        self.add_node()
        self.add_proposal(max_price="0.35")
        self.add_evidence(
            "E-PW", "power_window", "SITE-A",
            fact={"price_cny_per_kwh": "0.5",
                  "window_from": iso(-5), "window_to": iso(5)},
        )
        self.open_round()
        self.call(
            "POST", "/rounds/R1/proposals/P-1/locks",
            {"dimension": "power_window", "evidence_id": "E-PW", "revision": 1},
        )
        code, snap = self.call("GET", "/rounds/R1/proposals/P-1/snapshot")
        gap_dims = {g["dimension"]: g["gap"]
                    for g in snap["assessment"]["cross_border"]["gaps"]}
        self.assertEqual(gap_dims.get("power_window"), "power_price_above_limit")


class CapacityTest(ApiCase):
    def _proposal(self, ref, demand):
        self.add_proposal(ref=ref, demand=demand,
                          categories=["energy"])

    def test_concurrent_locks_never_exceed_commitment(self):
        self.add_node(committed="50")
        demands = ["20", "20", "20"]  # 共 60，超过承诺 50
        for index, demand in enumerate(demands, start=1):
            self._proposal(f"P-{index}", demand)
        self.open_round()

        results = []

        def worker(index):
            code, resp = self.call(
                "POST", f"/rounds/R1/proposals/P-{index}/capacity", {}
            )
            results.append((index, code, resp))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in (1, 2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[1] == 201]
        failures = [r for r in results if r[1] == 409]
        self.assertEqual(len(successes), 2)
        self.assertEqual(len(failures), 1)
        self.assertIn("承诺值", failures[0][2]["error"])

        code, node = self.call("GET", "/grid-nodes/GRID-A")
        self.assertEqual(code, 200)
        self.assertEqual(node["held_total_mw"], "40")
        self.assertEqual(node["remaining_mw"], "10")

    def test_occupation_only_affects_unlocked_proposal(self):
        self.add_node(committed="30")
        self.add_proposal(ref="P-A", demand="20")
        self.add_proposal(ref="P-B", demand="20")
        ids_a = self.seed_ready_world(prefix="EA")
        ids_b = self.seed_ready_world(prefix="EB")
        self.open_round()
        self.lock_all("R1", "P-A", ids_a)
        self.lock_all("R1", "P-B", ids_b)

        # A 先锁容量并获批
        code, _ = self.call("POST", "/rounds/R1/proposals/P-A/capacity", {})
        self.assertEqual(code, 201)
        code, decision = self.call(
            "POST", "/rounds/R1/proposals/P-A/decision",
            {"outcome": "approved", "rationale": "A 先行锁定"},
        )
        self.assertEqual(code, 201, decision)

        # B 的证据锁仍在，但容量缺口出现
        code, snap_b = self.call("GET", "/rounds/R1/proposals/P-B/snapshot")
        self.assertEqual(code, 200)
        self.assertTrue(snap_b["assessment"]["cross_border_ready"])
        self.assertFalse(snap_b["capacity"]["locked"])
        self.assertEqual(snap_b["capacity"]["gap"], "capacity_exhausted")

        # B 尝试锁容量被拒
        code, resp = self.call("POST", "/rounds/R1/proposals/P-B/capacity", {})
        self.assertEqual(code, 409)

        # A 的获批与预留不受影响
        code, hist_a = self.call("GET", "/decisions/DEC-R1-P-A")
        self.assertEqual(code, 200)
        self.assertEqual(hist_a["outcome"], "approved")

    def test_commitment_reduction_does_not_touch_held_or_approved(self):
        self.add_node(committed="30")
        self.add_proposal(ref="P-A", demand="20")
        ids_a = self.seed_ready_world(prefix="EA")
        self.add_proposal(ref="P-B", demand="20")
        self.seed_ready_world(prefix="EB")
        self.open_round()
        self.lock_all("R1", "P-A", ids_a)

        code, _ = self.call("POST", "/rounds/R1/proposals/P-A/capacity", {})
        self.assertEqual(code, 201)

        # A 先按承诺 30 MW 获批
        code, decision = self.call(
            "POST", "/rounds/R1/proposals/P-A/decision",
            {"outcome": "approved", "rationale": "锁定时承诺 30MW"},
        )
        self.assertEqual(code, 201, decision)
        self.assertEqual(
            decision["frozen_snapshot"]["capacity"]
            ["reservation"]["commitment_revision"],
            1,
        )

        # 获批后承诺值下调到 15 MW：A 的预留与决策不回溯
        code, _ = self.call(
            "POST", "/grid-nodes/GRID-A/commitments",
            {"revision": 2, "committed_mw": "15",
             "effective_from": iso(-1)},
        )
        self.assertEqual(code, 201)

        # B 尚未锁定，按现行承诺值衡量，锁容量失败
        self.lock_all(
            "R1", "P-B",
            {d: f"EB-{d}" for d in ids_a},
        )
        code, resp = self.call("POST", "/rounds/R1/proposals/P-B/capacity", {})
        self.assertEqual(code, 409)

        # A 的历史记录承诺值后续变化，但原判断不变
        code, hist = self.call("GET", "/decisions/DEC-R1-P-A")
        self.assertEqual(code, 200)
        note_kinds = [n["kind"] for n in hist["history"]["subsequent_notes"]]
        self.assertIn("commitment_changed", note_kinds)
        change_kinds = {c["kind"] for c in hist["history"]["subsequent_changes"]}
        self.assertIn("commitment_changed", change_kinds)

    def test_capacity_lock_is_idempotent_per_proposal(self):
        self.add_node(committed="30")
        self.add_proposal(demand="20")
        self.open_round()
        code, first = self.call(
            "POST", "/rounds/R1/proposals/P-1/capacity", {}
        )
        self.assertEqual(code, 201)
        self.assertFalse(first["already_held"])
        code, second = self.call(
            "POST", "/rounds/R1/proposals/P-1/capacity", {}
        )
        self.assertEqual(code, 201)
        self.assertTrue(second["already_held"])
        code, node = self.call("GET", "/grid-nodes/GRID-A")
        self.assertEqual(node["held_total_mw"], "20")


class CrossBorderFallbackTest(ApiCase):
    def test_domestic_fallback_when_cross_border_insufficient(self):
        self.add_node()
        self.add_proposal(target="cross_border")
        # 驻留不允许出境、服务区域仅 CN，其余条件齐备
        ids = self.seed_ready_world(cross_border=False)
        self.open_round()
        self.lock_all("R1", "P-1", ids)
        code, _ = self.call("POST", "/rounds/R1/proposals/P-1/capacity", {})
        self.assertEqual(code, 201)

        code, snap = self.call("GET", "/rounds/R1/proposals/P-1/snapshot")
        self.assertEqual(code, 200)
        self.assertFalse(snap["assessment"]["cross_border_ready"])
        cb_gaps = {g["gap"] for g in snap["assessment"]["cross_border"]["gaps"]}
        self.assertEqual(
            cb_gaps,
            {"cross_border_not_permitted", "service_region_domestic_only"},
        )
        self.assertTrue(snap["assessment"]["domestic_ready"])
        self.assertEqual(snap["assessment"]["comparable_mode"], "domestic")

        # 比较端点自动降级
        code, comp = self.call("GET", "/rounds/R1/comparison?mode=auto")
        self.assertEqual(code, 200)
        row = comp["proposals"][0]
        self.assertEqual(row["evaluated_mode"], "domestic")
        self.assertTrue(row["ready"])

        # 按境内口径可获批
        code, decision = self.call(
            "POST", "/rounds/R1/proposals/P-1/decision",
            {"outcome": "approved", "mode": "domestic",
             "rationale": "转不出域计算"},
        )
        self.assertEqual(code, 201, decision)
        self.assertEqual(decision["evaluated_mode"], "domestic")

    def test_hard_gap_blocks_fallback_and_approval(self):
        self.add_node()
        # 数据授权只覆盖 energy，缺类别 -> 境内口径同样不成立
        self.add_proposal(categories=["energy", "patent"])
        facts = self.ready_facts(cross_border=False)
        facts["data_scope"] = {"categories": ["energy"]}
        ids = {}
        for dimension, fact in facts.items():
            evidence_id = f"E-{dimension}"
            self.add_evidence(
                evidence_id, dimension,
                {"power_window": "SITE-A", "datacenter_stage": "SITE-A",
                 "data_scope": "COLL-A", "research_grant": "PROG-A"}
                .get(dimension, "POLICY-REF"),
                fact=fact,
            )
            ids[dimension] = evidence_id
        self.open_round()
        self.lock_all("R1", "P-1", ids)
        self.call("POST", "/rounds/R1/proposals/P-1/capacity", {})

        code, snap = self.call("GET", "/rounds/R1/proposals/P-1/snapshot")
        self.assertIsNone(snap["assessment"]["comparable_mode"])
        code, resp = self.call(
            "POST", "/rounds/R1/proposals/P-1/decision",
            {"outcome": "approved", "rationale": "试图强行批准"},
        )
        self.assertEqual(code, 409)


class PrivacyTest(ApiCase):
    def test_raw_payload_fields_rejected(self):
        code, resp = self.call("POST", "/evidence", {
            "evidence_id": "E-1", "dimension": "power_window",
            "subject_ref": "SITE-A", "title": "t",
            "raw_data": "企业内部明细……",
        })
        self.assertEqual(code, 400)
        self.assertIn("原始材料", resp["error"])

    def test_unknown_fact_fields_rejected(self):
        code, resp = self.call("POST", "/evidence", {
            "evidence_id": "E-1", "dimension": "power_window",
            "subject_ref": "SITE-A", "title": "t",
        })
        self.assertEqual(code, 201)
        code, resp = self.call("POST", "/evidence/E-1/revisions", {
            "revision": 1, "status": "issued",
            "effective_from": iso(-1),
            "payload_ref": "DOC-1",
            "payload_sha256": SHA_OK,
            "fact": {"price_cny_per_kwh": "0.3",
                     "customer_contract_text": "绝密合同正文"},
        })
        self.assertEqual(code, 400)
        self.assertIn("未约定字段", resp["error"])

    def test_sha256_must_be_digest(self):
        self.call("POST", "/evidence", {
            "evidence_id": "E-1", "dimension": "power_window",
            "subject_ref": "SITE-A", "title": "t",
        })
        code, resp = self.call("POST", "/evidence/E-1/revisions", {
            "revision": 1, "status": "issued",
            "effective_from": iso(-1),
            "payload_ref": "DOC-1",
            "payload_sha256": "not-a-digest",
        })
        self.assertEqual(code, 400)

    def test_naive_timestamp_rejected(self):
        self.add_node()
        code, resp = self.call(
            "POST", "/grid-nodes/GRID-A/commitments",
            {"revision": 2, "committed_mw": "40",
             "effective_from": "2026-09-01T00:00:00"},
        )
        self.assertEqual(code, 400)
        self.assertIn("偏移量", resp["error"])


class DecisionRulesTest(ApiCase):
    def test_cannot_approve_without_capacity_lock(self):
        self.add_node()
        self.add_proposal()
        ids = self.seed_ready_world()
        self.open_round()
        self.lock_all("R1", "P-1", ids)
        code, resp = self.call(
            "POST", "/rounds/R1/proposals/P-1/decision",
            {"outcome": "approved", "rationale": "未锁容量"},
        )
        self.assertEqual(code, 409)
        self.assertIn("容量", resp["error"])

    def test_rejection_releases_capacity(self):
        self.add_node(committed="30")
        self.add_proposal(demand="20")
        ids = self.seed_ready_world()
        self.open_round()
        self.lock_all("R1", "P-1", ids)
        code, _ = self.call("POST", "/rounds/R1/proposals/P-1/capacity", {})
        self.assertEqual(code, 201)
        code, _ = self.call(
            "POST", "/rounds/R1/proposals/P-1/decision",
            {"outcome": "rejected", "rationale": "改址"},
        )
        self.assertEqual(code, 201)
        code, node = self.call("GET", "/grid-nodes/GRID-A")
        self.assertEqual(node["held_total_mw"], "0")
        self.assertEqual(node["remaining_mw"], "30")

    def test_withdrawn_evidence_recorded_as_later_change(self):
        self.add_node()
        self.add_proposal()
        ids = self.seed_ready_world()
        self.open_round()
        self.lock_all("R1", "P-1", ids)
        self.call("POST", "/rounds/R1/proposals/P-1/capacity", {})
        code, decision = self.call(
            "POST", "/rounds/R1/proposals/P-1/decision",
            {"outcome": "approved", "rationale": "ok"},
        )
        self.assertEqual(code, 201, decision)
        code, _ = self.call(
            "POST", f"/evidence/{ids['research_grant']}/revisions/transition",
            {"revision": 1, "to_status": "withdrawn",
             "note": "授权事后撤销"},
        )
        self.assertEqual(code, 200)
        code, hist = self.call("GET", "/decisions/DEC-R1-P-1")
        notes = [n["kind"] for n in hist["history"]["subsequent_notes"]]
        self.assertIn("evidence_withdrawn", notes)
        # 原冻结判断不受撤销影响
        self.assertTrue(hist["frozen_snapshot"]["assessment"]["ready"])


if __name__ == "__main__":
    unittest.main()
