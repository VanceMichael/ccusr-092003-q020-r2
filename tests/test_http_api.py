
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import db
from app.main import Handler
from scripts.migrate import run_migrations


SHA = "9" * 64
WINDOW = {"grid_node_ref": "GRID-HTTP",
          "window_start": "2000-01-01T00:00:00+08:00",
          "window_end": "2100-01-01T00:00:00+08:00",
          "tariff_cny_per_mwh": 320}


class HttpApiTest(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: threading.Thread
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmp.name) / "http.sqlite3"
        db.set_path_override(db_path)
        run_migrations(db_path)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        db.set_path_override(None)
        cls.tmp.cleanup()

    def request(self, method: str, path: str, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_round_flow_over_http(self) -> None:
        self.assertEqual(self.request("GET", "/health")[0], 200)

        status, _ = self.request("PUT", "/grid-nodes/GRID-HTTP/commitment",
                                 {"committed_mw": 15})
        self.assertEqual(status, 200)

        status, _ = self.request("POST", "/proposals", {
            "proposal_ref": "P-HTTP", "title": "不出域方案",
            "service_mode": "IN_DOMAIN", "capacity_mw": 10,
            "submitted_at": "2026-09-01T09:00:00+08:00"})
        self.assertEqual(status, 201)

        evidence = [
            ("EV-PW-P-HTTP", "power_window", WINDOW),
            ("EV-DC-P-HTTP", "datacenter_stage",
             {"stage": "OPERATIONAL", "delivered_mw": 12}),
            ("EV-DS-P-HTTP", "data_scope",
             {"authorization_status": "GRANTED", "scope_labels": ["telemetry"],
              "expires_at": "2100-01-01T00:00:00+08:00"}),
            ("EV-RS-P-HTTP", "residency", {"mode": "IN_DOMAIN_ONLY"}),
        ]
        for ref, dimension, attributes in evidence:
            status, body = self.request("POST", "/evidence", {
                "evidence_ref": ref, "dimension": dimension,
                "version_label": "v1", "source_ref": f"vault://docs/{ref}/v1",
                "source_sha256": SHA, "attributes": attributes,
                "effective_at": "2026-08-01T00:00:00+08:00"})
            self.assertEqual(status, 201, body)

        status, round_body = self.request("POST", "/rounds", {"note": "HTTP 轮次"})
        self.assertEqual(status, 201)
        rid = round_body["round_id"]

        for ref, dimension, _ in evidence:
            status, body = self.request("PUT", f"/rounds/{rid}/locks", {
                "proposal_ref": "P-HTTP", "dimension": dimension,
                "evidence_ref": ref, "version_label": "v1"})
            self.assertEqual(status, 200, body)

        status, evaluation = self.request(
            "GET", f"/rounds/{rid}/evaluations/P-HTTP")
        self.assertEqual(status, 200)
        self.assertTrue(evaluation["ready_for_approval"])

        status, comparison = self.request(
            "GET", f"/rounds/{rid}/comparison?service_mode=IN_DOMAIN")
        self.assertEqual(status, 200)
        self.assertEqual(comparison["items"][0]["proposal_ref"], "P-HTTP")

        status, _ = self.request("POST", "/proposals/P-HTTP/decision", {
            "round_id": rid, "outcome": "APPROVED",
            "rationale_ref": "vault://decisions/P-HTTP"})
        self.assertEqual(status, 201)

        # 获批后历史：原判断与后来变化分列
        status, history = self.request("GET", "/proposals/P-HTTP/history")
        self.assertEqual(status, 200)
        self.assertEqual(history["original_judgement"]["outcome"], "APPROVED")
        self.assertEqual(history["later_changes_recorded"], [])

    def test_overcommitment_and_bad_payloads(self) -> None:
        # 节点承诺 15MW；此前流程已持有 10MW，99MW 的新锁定必须被挡下
        self.request("PUT", "/grid-nodes/GRID-HTTP/commitment",
                     {"committed_mw": 15})
        status, _ = self.request("POST", "/proposals", {
            "proposal_ref": "P-OC", "title": "超额",
            "service_mode": "IN_DOMAIN", "capacity_mw": 99,
            "submitted_at": "2026-09-01T09:00:00+08:00"})
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/evidence", {
            "evidence_ref": "EV-PW-P-OC", "dimension": "power_window",
            "version_label": "v1", "source_ref": "vault://docs/EV-PW-P-OC/v1",
            "source_sha256": SHA, "attributes": WINDOW,
            "effective_at": "2026-08-01T00:00:00+08:00"})
        self.assertEqual(status, 201)
        status, round_body = self.request("POST", "/rounds", {"note": "超额轮次"})
        rid = round_body["round_id"]
        status, body = self.request("PUT", f"/rounds/{rid}/locks", {
            "proposal_ref": "P-OC", "dimension": "power_window",
            "evidence_ref": "EV-PW-P-OC", "version_label": "v1"})
        self.assertEqual(status, 409)
        self.assertIn("容量不足", body["error"])

        # 非白名单原始字段在接口边界被拒绝
        status, body = self.request("POST", "/evidence", {
            "evidence_ref": "EV-RAW", "dimension": "data_scope",
            "version_label": "v1", "source_ref": "vault://x",
            "source_sha256": SHA,
            "attributes": {"authorization_status": "GRANTED",
                           "scope_labels": ["x"],
                           "enterprise_ledger": "企业台账明细"},
            "effective_at": "2026-08-01T00:00:00+08:00"})
        self.assertEqual(status, 400)
        self.assertIn("不接受属性", body["error"])

        # 非法时间格式
        status, body = self.request("POST", "/proposals", {
            "proposal_ref": "P-BADTIME", "title": "时间",
            "service_mode": "IN_DOMAIN", "capacity_mw": 1,
            "submitted_at": "2026-09-01 09:00"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
