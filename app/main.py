
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import service
from .db import ensure_migrated
from .validation import ValidationError


MAX_BODY_BYTES = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValidationError("请求体超过 64KiB 上限")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("请求体必须是 UTF-8 JSON")
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    def _handle(self, method: str):
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            body = self._read_json() if method in ("POST", "PUT", "DELETE") else {}
            self.route(method, parts, query, body)
        except ValidationError as exc:
            self._error(400, str(exc))
        except service.DomainError as exc:
            self._error(exc.status, str(exc))
        except Exception as exc:  # noqa: BLE001 - 统一错误出口
            traceback.print_exc()
            self._error(500, f"服务器内部错误：{type(exc).__name__}")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    # ------------------------------------------------------------ 路由

    def route(self, method: str, parts: list[str], query: dict, body: dict) -> None:
        if method == "GET" and parts == ["health"]:
            self._send_json(200, {"status": "ok"})
            return

        if parts == ["proposals"]:
            if method == "POST":
                self._send_json(201, service.register_proposal(body))
                return
            if method == "GET":
                self._send_json(200, {"proposals": service.list_proposals()})
                return

        if len(parts) == 2 and parts[0] == "proposals":
            if method == "GET":
                self._send_json(200, service.get_proposal(parts[1]))
                return

        if parts == ["evidence"]:
            if method == "POST":
                self._send_json(201, service.register_evidence(body))
                return
            if method == "GET":
                self._send_json(200, {"evidence": service.list_evidence(
                    query.get("dimension"))})
                return

        if parts == ["rounds"] and method == "POST":
            self._send_json(201, service.create_round(body))
            return

        if len(parts) == 2 and parts[0] == "rounds":
            if method == "GET":
                self._send_json(200, service.get_round(parts[1]))
                return

        if len(parts) == 3 and parts[0] == "rounds" and parts[2] == "close":
            if method == "POST":
                self._send_json(200, service.close_round(parts[1]))
                return

        # /rounds/{id}/locks
        if len(parts) == 3 and parts[0] == "rounds" and parts[2] == "locks":
            round_id = parts[1]
            if method == "GET":
                self._send_json(200, {"locks": service.list_locks(
                    round_id, query.get("proposal_ref"))})
                return
            if method == "PUT":
                self._send_json(200, service.lock_evidence(round_id, body))
                return

        # /rounds/{id}/locks/{proposal_ref}/{dimension}
        if len(parts) == 5 and parts[0] == "rounds" and parts[2] == "locks":
            if method == "DELETE":
                self._send_json(200, service.unlock_evidence(
                    parts[1], parts[3], parts[4]))
                return
            if method == "GET":
                self._send_json(200, service.get_lock(parts[1], parts[3], parts[4]))
                return

        # /rounds/{id}/evaluations/{proposal_ref}
        if len(parts) == 4 and parts[0] == "rounds" and parts[2] == "evaluations":
            if method == "GET":
                from .validation import parse_timestamp
                as_of = parse_timestamp(query["as_of"], "as_of") if "as_of" in query else None
                self._send_json(200, service.evaluate(parts[1], parts[3], as_of))
                return

        # /rounds/{id}/comparison
        if len(parts) == 3 and parts[0] == "rounds" and parts[2] == "comparison":
            if method == "GET":
                self._send_json(200, service.compare(
                    parts[1], query.get("service_mode")))
                return

        # /grid-nodes/{ref}/commitment
        if len(parts) == 3 and parts[0] == "grid-nodes" and parts[2] == "commitment":
            node = parts[1]
            if method == "PUT":
                self._send_json(200, service.set_commitment(node, body))
                return
            if method == "GET":
                commitment = service.get_commitment(node)
                if commitment is None:
                    self._error(404, "变电节点承诺容量尚未登记")
                    return
                self._send_json(200, commitment)
                return

        if parts == ["reservations"] and method == "GET":
            self._send_json(200, {"reservations": service.list_reservations(
                query.get("grid_node_ref"))})
            return

        # /proposals/{ref}/decision
        if len(parts) == 3 and parts[0] == "proposals" and parts[2] == "decision":
            if method == "POST":
                self._send_json(201, service.decide(parts[1], body))
                return
            if method == "GET":
                self._send_json(200, service.get_decision(parts[1]))
                return

        # /proposals/{ref}/changes
        if len(parts) == 3 and parts[0] == "proposals" and parts[2] == "changes":
            if method == "POST":
                self._send_json(201, service.record_change(parts[1], body))
                return

        # /proposals/{ref}/history
        if len(parts) == 3 and parts[0] == "proposals" and parts[2] == "history":
            if method == "GET":
                self._send_json(200, service.history(parts[1]))
                return

        self._error(404, f"未找到接口：{method} /{'/'.join(parts)}")

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    ensure_migrated()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
