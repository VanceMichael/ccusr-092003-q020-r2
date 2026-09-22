"""候选方案评估系统 HTTP 服务。

接口围绕“轮次锁定证据版本 → 展示缺口 → 容量原子预留 → 决策冻结 → 历史对照”组织。
任何报文只接受受控引用编号与 sha256 摘要；原始材料字段在入口即被拒绝。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import evaluation
from .store import Conflict, NotFound, Store, StoreError

FORBIDDEN_KEYS = frozenset({
    "raw_data", "raw_payload", "payload_content", "original_data",
    "enterprise_data", "data_dump", "source_data", "secret",
})


def scan_forbidden_keys(body: object, path: str = "$") -> None:
    """递归拒绝任何疑似原始材料的字段（白名单之外的兜底防线）。"""
    if isinstance(body, dict):
        for key, value in body.items():
            if key in FORBIDDEN_KEYS:
                raise StoreError(
                    f"字段 {path}.{key} 属于原始材料，系统只接受受控引用"
                    " payload_ref 与 payload_sha256 摘要"
                )
            scan_forbidden_keys(value, f"{path}.{key}")
    elif isinstance(body, list):
        for index, value in enumerate(body):
            scan_forbidden_keys(value, f"{path}[{index}]")


def _json_default(value: object) -> object:
    return str(value)


class Handler(BaseHTTPRequestHandler):
    server_version = "NortheastComputeEval/1.0"
    store: Store  # 由 make_server 注入到类上

    # ---------- 基础收发 ----------

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, default=_json_default
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 65536:
            raise StoreError("请求体过大（上限 64 KiB）")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError("请求体必须是 UTF-8 JSON") from exc
        if not isinstance(body, dict):
            raise StoreError("请求体必须是 JSON 对象")
        scan_forbidden_keys(body)
        return body

    def _handle(self, method: str) -> None:
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        query = parse_qs(urlsplit(self.path).query)
        try:
            body = self._read_body() if method in ("POST", "PUT") else {}
            status, payload = self._route(method, parts, query, body)
            self._send_json(status, payload)
        except StoreError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 服务不得因单个请求崩溃
            self._send_json(500, {"error": f"内部错误：{exc}"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, format: str, *args: object) -> None:
        return

    # ---------- 路由 ----------

    def _route(self, method, parts, query, body):
        if method == "GET" and parts == ["health"]:
            return 200, {"status": "ok"}

        if method == "POST" and parts == ["evidence"]:
            return 201, self.store.register_evidence(
                body.get("evidence_id"),
                body.get("dimension"),
                body.get("subject_ref"),
                body.get("title"),
            )
        if (
            method == "POST" and len(parts) == 3
            and parts[0] == "evidence" and parts[2] == "revisions"
        ):
            return 201, self.store.add_revision(
                parts[1],
                body.get("revision"),
                body.get("status", "issued"),
                body.get("effective_from"),
                body.get("effective_to"),
                body.get("payload_ref"),
                body.get("payload_sha256"),
                body.get("fact"),
            )
        if (
            method == "POST" and len(parts) == 4
            and parts[0] == "evidence" and parts[2] == "revisions"
            and parts[3] == "transition"
        ):
            return 200, self.store.transition_status(
                parts[1], body.get("revision"),
                body.get("to_status"), body.get("note"),
            )
        if method == "GET" and len(parts) == 2 and parts[0] == "evidence":
            return 200, self.store.get_evidence(parts[1])

        if method == "POST" and parts == ["grid-nodes"]:
            return 201, self.store.create_grid_node(
                body.get("grid_node_ref"), body.get("site_ref"),
                body.get("label"),
            )
        if (
            method == "POST" and len(parts) == 3
            and parts[0] == "grid-nodes" and parts[2] == "commitments"
        ):
            return 201, self.store.add_commitment(
                parts[1], body.get("revision"), body.get("committed_mw"),
                body.get("effective_from"), body.get("note"),
            )
        if method == "GET" and len(parts) == 2 and parts[0] == "grid-nodes":
            at = query.get("at", [None])[0]
            return 200, self.store.get_grid_node(parts[1], at)

        if method == "POST" and parts == ["proposals"]:
            return 201, self.store.create_proposal(body)
        if method == "GET" and parts == ["proposals"]:
            return 200, {"proposals": self.store.list_proposals()}
        if method == "GET" and len(parts) == 2 and parts[0] == "proposals":
            return 200, self.store.get_proposal(parts[1])

        if method == "POST" and parts == ["rounds"]:
            return 201, self.store.open_round(
                body.get("round_id"), body.get("note")
            )

        if (
            method == "POST" and len(parts) == 5
            and parts[0] == "rounds" and parts[2] == "proposals"
            and parts[4] == "locks"
        ):
            return 201, self.store.lock_evidence(
                parts[1], parts[3], body.get("dimension"),
                body.get("evidence_id"), body.get("revision"),
            )
        if (
            method == "POST" and len(parts) == 5
            and parts[0] == "rounds" and parts[2] == "proposals"
            and parts[4] == "capacity"
        ):
            return 201, self.store.lock_capacity(parts[1], parts[3])
        if (
            method == "GET" and len(parts) == 5
            and parts[0] == "rounds" and parts[2] == "proposals"
            and parts[4] == "snapshot"
        ):
            at = query.get("at", [None])[0]
            return 200, self.store.snapshot(parts[1], parts[3], at)
        if (
            method == "GET" and len(parts) == 3
            and parts[0] == "rounds" and parts[2] == "comparison"
        ):
            mode = query.get("mode", ["auto"])[0]
            return 200, self.store.comparison(parts[1], mode)
        if (
            method == "POST" and len(parts) == 5
            and parts[0] == "rounds" and parts[2] == "proposals"
            and parts[4] == "decision"
        ):
            return 201, self.store.decide(
                parts[1], parts[3], body.get("outcome"),
                body.get("mode"), body.get("rationale", ""),
            )

        if (
            method == "POST" and len(parts) == 3
            and parts[0] == "decisions" and parts[2] == "notes"
        ):
            return 201, self.store.add_decision_note(
                parts[1], body.get("kind"), body.get("note", "")
            )
        if method == "GET" and len(parts) == 2 and parts[0] == "decisions":
            return 200, self.store.get_decision(parts[1])

        if method == "GET" and parts in ([""], []):
            return 200, {
                "service": "东北亚算力候选方案评估系统",
                "dimensions": [
                    {"key": key, "label": evaluation.DIMENSION_LABELS[key]}
                    for key in evaluation.DIMENSIONS
                ],
                "endpoints": [
                    "POST /evidence",
                    "POST /evidence/{id}/revisions",
                    "POST /evidence/{id}/revisions/transition",
                    "GET  /evidence/{id}",
                    "POST /grid-nodes",
                    "POST /grid-nodes/{ref}/commitments",
                    "GET  /grid-nodes/{ref}",
                    "POST /proposals",
                    "GET  /proposals",
                    "POST /rounds",
                    "POST /rounds/{id}/proposals/{ref}/locks",
                    "POST /rounds/{id}/proposals/{ref}/capacity",
                    "GET  /rounds/{id}/proposals/{ref}/snapshot",
                    "GET  /rounds/{id}/comparison?mode=auto",
                    "POST /rounds/{id}/proposals/{ref}/decision",
                    "GET  /decisions/{id}",
                    "POST /decisions/{id}/notes",
                ],
                "privacy": "仅保存受控引用与 sha256 摘要，不接收企业原始数据",
            }

        raise NotFound(f"未知路径：{self.path}")


def make_server(host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    port = port or int(os.getenv("PORT", "8080"))
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    Handler.store = Store(database_path)
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    server = make_server()
    server.serve_forever()


if __name__ == "__main__":
    main()
