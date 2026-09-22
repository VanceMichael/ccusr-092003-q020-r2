"""存储层：证据版本、轮次锁定、容量预留、决策冻结。

关键并发语义：
- 所有“锁定”操作在单个 BEGIN IMMEDIATE 事务中完成读改写；
- 容量超售由数据库触发器兜底，并发锁定总和不可能越过承诺值；
- 已锁定/已获批方案不会因承诺值下调或他项占用而被回溯判失败。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import evaluation

REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

DIMENSION_SUBJECT_FIELD = {
    "power_window": "site_ref",
    "datacenter_stage": "site_ref",
    "data_scope": "data_collection_ref",
    "research_grant": "research_grant_ref",
    # residency / service_region 属政策与区域证据，不限定单一主体
}


class StoreError(ValueError):
    status = 400


class NotFound(StoreError):
    status = 404


class Conflict(StoreError):
    status = 409


class CapacityExceeded(Conflict):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_ref(value: Any, name: str) -> str:
    if not isinstance(value, str) or not REF_RE.match(value):
        raise StoreError(f"{name} 必须是 1-64 位字母数字及 ._- 组成的引用编号")
    return value


def parse_ts(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise StoreError(f"{name} 必须是带偏移量的 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StoreError(f"{name} 不是合法 ISO 8601 时间：{value}") from exc
    if parsed.tzinfo is None:
        raise StoreError(f"{name} 必须带时区偏移量")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def number_text(
    value: Any, name: str, *, minimum: float | None = None
) -> str:
    if isinstance(value, bool):
        raise StoreError(f"{name} 必须是数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{name} 必须是数值") from exc
    if number != number:
        raise StoreError(f"{name} 不能是 NaN")
    if minimum is not None and number < minimum:
        raise StoreError(f"{name} 不得小于 {minimum:g}")
    return str(value).strip() if isinstance(value, str) else _num_str(number)


def _num_str(number: float) -> str:
    return f"{number:.6f}".rstrip("0").rstrip(".") or "0"


def require_sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA_RE.match(value):
        raise StoreError("payload_sha256 必须是 64 位小写十六进制摘要")
    return value


def bounded_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"{name} 必须是非空字符串")
    if len(value) > limit:
        raise StoreError(f"{name} 长度不得超过 {limit}")
    return value.strip()


def string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise StoreError(f"{name} 必须是字符串列表")
    return value


# ---- fact 白名单：只有这些键能进入系统，结构上拒绝企业原始材料 ----

def clean_fact(dimension: str, fact: Any) -> dict[str, Any]:
    if fact is None:
        fact = {}
    if not isinstance(fact, dict):
        raise StoreError("fact 必须是对象")
    allowed = FACT_FIELDS[dimension]
    unknown = set(fact) - set(allowed)
    if unknown:
        raise StoreError(
            f"{dimension} 的 fact 含未约定字段：{', '.join(sorted(unknown))}；"
            "原始材料请以受控引用+摘要登记"
        )
    cleaned: dict[str, Any] = {}
    for key, spec in allowed.items():
        if key not in fact:
            continue
        value = fact[key]
        kind = spec["kind"]
        if kind == "number":
            cleaned[key] = float(number_text(value, key, minimum=spec.get("min")))
        elif kind == "bool":
            if not isinstance(value, bool):
                raise StoreError(f"{key} 必须是布尔值")
            cleaned[key] = value
        elif kind == "enum":
            if value not in spec["values"]:
                raise StoreError(f"{key} 取值必须是 {spec['values']} 之一")
            cleaned[key] = value
        elif kind == "string_list":
            cleaned[key] = string_list(value, key)
        elif kind == "ts":
            cleaned[key] = parse_ts(value, key)
        elif kind == "string":
            cleaned[key] = bounded_text(value, key, 200)
    return cleaned


FACT_FIELDS = {
    "power_window": {
        "price_cny_per_kwh": {"kind": "number", "min": 0},
        "window_from": {"kind": "ts"},
        "window_to": {"kind": "ts"},
    },
    "datacenter_stage": {
        "stage": {"kind": "enum", "values": list(evaluation.STAGE_LABELS)},
        "available_mw": {"kind": "number", "min": 0},
    },
    "data_scope": {
        "categories": {"kind": "string_list"},
    },
    "residency": {
        "cross_border_allowed": {"kind": "bool"},
        "destinations": {"kind": "string_list"},
    },
    "research_grant": {
        "granted": {"kind": "bool"},
        "program_ref": {"kind": "string"},
    },
    "service_region": {
        "regions": {"kind": "string_list"},
    },
}


class Store:
    def __init__(self, path: str | Path, *, auto_migrate: bool = True) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if auto_migrate:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        """对新库自动按序应用 migrations/*.sql，已应用的版本跳过。"""
        migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY,"
                "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            applied = {
                row[0]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for sql_file in sorted(migrations_dir.glob("*.sql")):
                if sql_file.stem in applied:
                    continue
                conn.executescript(sql_file.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version)"
                    " VALUES (?)",
                    (sql_file.stem,),
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ---------- 证据 ----------

    def register_evidence(
        self,
        evidence_id: str,
        dimension: str,
        subject_ref: str,
        title: str,
    ) -> dict[str, Any]:
        require_ref(evidence_id, "evidence_id")
        if dimension not in evaluation.DIMENSIONS:
            raise StoreError(f"dimension 必须是 {evaluation.DIMENSIONS} 之一")
        require_ref(subject_ref, "subject_ref")
        title = bounded_text(title, "title", 200)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO evidence(evidence_id, dimension, subject_ref,"
                    " title, created_at) VALUES (?,?,?,?,?)",
                    (evidence_id, dimension, subject_ref, title, now_iso()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"证据 {evidence_id} 已存在") from exc
        return {
            "evidence_id": evidence_id,
            "dimension": dimension,
            "subject_ref": subject_ref,
            "title": title,
        }

    def add_revision(
        self,
        evidence_id: str,
        revision: int,
        status: str,
        effective_from: str,
        effective_to: str | None,
        payload_ref: str,
        payload_sha256: str,
        fact: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(revision, int) or revision <= 0:
            raise StoreError("revision 必须是正整数")
        if status not in ("draft", "issued"):
            raise StoreError("登记新版本时 status 只能是 draft 或 issued")
        effective_from = parse_ts(effective_from, "effective_from")
        if effective_to is not None:
            effective_to = parse_ts(effective_to, "effective_to")
            if effective_to <= effective_from:
                raise StoreError("effective_to 必须晚于 effective_from")
        require_ref(payload_ref, "payload_ref")
        require_sha(payload_sha256)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT dimension FROM evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"证据 {evidence_id} 不存在")
            dimension = row["dimension"]
            cleaned = clean_fact(dimension, fact)
            recorded_at = now_iso()
            try:
                conn.execute(
                    "INSERT INTO evidence_revision(evidence_id, revision,"
                    " status, effective_from, effective_to, payload_ref,"
                    " payload_sha256, fact, recorded_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id, revision, status, effective_from,
                        effective_to, payload_ref, payload_sha256,
                        json.dumps(cleaned, ensure_ascii=False), recorded_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(
                    f"证据 {evidence_id} 版本 {revision} 已存在"
                ) from exc
            conn.execute(
                "INSERT INTO evidence_event(evidence_id, revision, event,"
                " from_status, to_status, occurred_at)"
                " VALUES (?,?,?,?,?,?)",
                (evidence_id, revision, "registered", None, status, recorded_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "evidence_id": evidence_id,
            "revision": revision,
            "dimension": dimension,
            "status": status,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "payload_ref": payload_ref,
            "payload_sha256": payload_sha256,
            "fact": cleaned,
        }

    def transition_status(
        self, evidence_id: str, revision: int, to_status: str, note: str | None
    ) -> dict[str, Any]:
        if to_status not in ("issued", "superseded", "withdrawn"):
            raise StoreError("目标状态只能是 issued / superseded / withdrawn")
        if note is not None:
            note = bounded_text(note, "note", 500)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM evidence_revision"
                " WHERE evidence_id=? AND revision=?",
                (evidence_id, revision),
            ).fetchone()
            if row is None:
                raise NotFound(f"证据 {evidence_id} 版本 {revision} 不存在")
            current = row["status"]
            allowed = {
                "draft": {"issued"},
                "issued": {"superseded", "withdrawn"},
                "superseded": set(),
                "withdrawn": set(),
            }
            if to_status not in allowed[current]:
                raise Conflict(f"证据版本不能从 {current} 转为 {to_status}")
            occurred = now_iso()
            conn.execute(
                "UPDATE evidence_revision SET status=? WHERE evidence_id=? AND revision=?",
                (to_status, evidence_id, revision),
            )
            conn.execute(
                "INSERT INTO evidence_event(evidence_id, revision, event,"
                " from_status, to_status, note, occurred_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (evidence_id, revision, "transitioned", current,
                 to_status, note, occurred),
            )
            # 引用该版本的既有决策：自动登记“后续变化”，不改写冻结判断
            if to_status in ("superseded", "withdrawn"):
                kind = (
                    "evidence_superseded" if to_status == "superseded"
                    else "evidence_withdrawn"
                )
                refs = conn.execute(
                    "SELECT decision_id FROM decision_lock_ref"
                    " WHERE evidence_id=? AND revision=?",
                    (evidence_id, revision),
                ).fetchall()
                for ref in refs:
                    conn.execute(
                        "INSERT INTO decision_note(decision_id, kind, note,"
                        " recorded_at) VALUES (?,?,?,?)",
                        (
                            ref["decision_id"], kind,
                            f"锁定所依据的证据 {evidence_id} 版本 {revision}"
                            f" 已{('被新版本取代' if to_status == 'superseded' else '撤回')}",
                            occurred,
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"evidence_id": evidence_id, "revision": revision,
                "status": to_status}

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            head = conn.execute(
                "SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if head is None:
                raise NotFound(f"证据 {evidence_id} 不存在")
            revisions = [
                self._revision_dict(r)
                for r in conn.execute(
                    "SELECT * FROM evidence_revision WHERE evidence_id=?"
                    " ORDER BY revision",
                    (evidence_id,),
                ).fetchall()
            ]
        return {
            "evidence_id": evidence_id,
            "dimension": head["dimension"],
            "subject_ref": head["subject_ref"],
            "title": head["title"],
            "revisions": revisions,
        }

    @staticmethod
    def _revision_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "revision": row["revision"],
            "status": row["status"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
            "payload_ref": row["payload_ref"],
            "payload_sha256": row["payload_sha256"],
            "fact": json.loads(row["fact"]),
            "recorded_at": row["recorded_at"],
        }

    # ---------- 变电节点与承诺值 ----------

    def create_grid_node(
        self, grid_node_ref: str, site_ref: str, label: str
    ) -> dict[str, Any]:
        require_ref(grid_node_ref, "grid_node_ref")
        require_ref(site_ref, "site_ref")
        label = bounded_text(label, "label", 200)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO grid_node(grid_node_ref, site_ref, label)"
                    " VALUES (?,?,?)",
                    (grid_node_ref, site_ref, label),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"变电节点 {grid_node_ref} 已存在") from exc
        return {"grid_node_ref": grid_node_ref, "site_ref": site_ref,
                "label": label}

    def add_commitment(
        self,
        grid_node_ref: str,
        revision: int,
        committed_mw: str,
        effective_from: str,
        note: str | None,
    ) -> dict[str, Any]:
        if not isinstance(revision, int) or revision <= 0:
            raise StoreError("revision 必须是正整数")
        mw_text = number_text(committed_mw, "committed_mw", minimum=0)
        effective_from = parse_ts(effective_from, "effective_from")
        if note is not None:
            note = bounded_text(note, "note", 500)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            node = conn.execute(
                "SELECT 1 FROM grid_node WHERE grid_node_ref=?",
                (grid_node_ref,),
            ).fetchone()
            if node is None:
                raise NotFound(f"变电节点 {grid_node_ref} 不存在")
            previous = conn.execute(
                "SELECT committed_mw FROM grid_commitment"
                " WHERE grid_node_ref=? ORDER BY revision DESC LIMIT 1",
                (grid_node_ref,),
            ).fetchone()
            try:
                conn.execute(
                    "INSERT INTO grid_commitment(grid_node_ref, revision,"
                    " committed_mw, effective_from, recorded_at, note)"
                    " VALUES (?,?,?,?,?,?)",
                    (grid_node_ref, revision, mw_text, effective_from,
                     now_iso(), note),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(
                    f"节点 {grid_node_ref} 承诺值版本 {revision} 已存在"
                ) from exc
            # 对已获批决策登记承诺值后续变化
            if previous is not None and float(previous["committed_mw"]) != float(
                mw_text
            ):
                for d in conn.execute(
                    "SELECT decision_id FROM decision WHERE grid_node_ref=?",
                    (grid_node_ref,),
                ).fetchall():
                    conn.execute(
                        "INSERT INTO decision_note(decision_id, kind, note,"
                        " recorded_at) VALUES (?,?,?,?)",
                        (
                            d["decision_id"], "commitment_changed",
                            f"节点承诺值由 {previous['committed_mw']} MW "
                            f"调整为 {mw_text} MW（版本 {revision}）；"
                            "原决策仍按当时承诺值成立",
                            now_iso(),
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"grid_node_ref": grid_node_ref, "revision": revision,
                "committed_mw": mw_text, "effective_from": effective_from}

    def get_grid_node(self, grid_node_ref: str, at: str | None = None) -> dict[str, Any]:
        at = parse_ts(at, "at") if at else now_iso()
        with self._connect() as conn:
            node = conn.execute(
                "SELECT * FROM grid_node WHERE grid_node_ref=?",
                (grid_node_ref,),
            ).fetchone()
            if node is None:
                raise NotFound(f"变电节点 {grid_node_ref} 不存在")
            commitment = conn.execute(
                "SELECT * FROM grid_commitment WHERE grid_node_ref=?"
                " AND effective_from<=? ORDER BY revision DESC LIMIT 1",
                (grid_node_ref, at),
            ).fetchone()
            held = conn.execute(
                "SELECT proposal_ref, round_id, mw, commitment_revision, locked_at"
                " FROM capacity_reservation WHERE grid_node_ref=? AND status='held'"
                " ORDER BY reservation_id",
                (grid_node_ref,),
            ).fetchall()
        held_total = sum(float(r["mw"]) for r in held)
        committed = float(commitment["committed_mw"]) if commitment else None
        return {
            "grid_node_ref": grid_node_ref,
            "site_ref": node["site_ref"],
            "label": node["label"],
            "at": at,
            "current_commitment": (
                None if commitment is None
                else {
                    "revision": commitment["revision"],
                    "committed_mw": commitment["committed_mw"],
                    "effective_from": commitment["effective_from"],
                }
            ),
            "held_total_mw": _num_str(held_total),
            "remaining_mw": (
                None if committed is None
                else _num_str(committed - held_total)
            ),
            "held_by": [dict(r) for r in held],
        }

    def _current_commitment(
        self, conn: sqlite3.Connection, grid_node_ref: str, at: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM grid_commitment WHERE grid_node_ref=?"
            " AND effective_from<=? ORDER BY revision DESC LIMIT 1",
            (grid_node_ref, at),
        ).fetchone()
        if row is None:
            raise StoreError(
                f"节点 {grid_node_ref} 在 {at} 没有生效中的承诺值版本，无法锁定容量"
            )
        return row

    # ---------- 方案 ----------

    def create_proposal(self, body: dict[str, Any]) -> dict[str, Any]:
        proposal_ref = require_ref(body.get("proposal_ref"), "proposal_ref")
        site_ref = require_ref(body.get("site_ref"), "site_ref")
        grid_node_ref = require_ref(body.get("grid_node_ref"), "grid_node_ref")
        data_collection_ref = require_ref(
            body.get("data_collection_ref"), "data_collection_ref"
        )
        research_grant_ref = require_ref(
            body.get("research_grant_ref"), "research_grant_ref"
        )
        demand_mw = number_text(body.get("demand_mw"), "demand_mw", minimum=0)
        if float(demand_mw) == 0:
            raise StoreError("demand_mw 必须大于 0")
        max_price = body.get("max_price_cny_per_kwh")
        max_price_text = (
            None if max_price is None
            else number_text(max_price, "max_price_cny_per_kwh", minimum=0)
        )
        categories = string_list(
            body.get("required_data_categories", []),
            "required_data_categories",
        )
        target_mode = body.get("target_mode", "cross_border")
        if target_mode not in ("cross_border", "domestic"):
            raise StoreError("target_mode 必须是 cross_border 或 domestic")
        try:
            with self._connect() as conn:
                node = conn.execute(
                    "SELECT site_ref FROM grid_node WHERE grid_node_ref=?",
                    (grid_node_ref,),
                ).fetchone()
                if node is None:
                    raise NotFound(f"变电节点 {grid_node_ref} 不存在")
                if node["site_ref"] != site_ref:
                    raise StoreError("grid_node_ref 不属于该 site_ref")
                conn.execute(
                    "INSERT INTO proposal(proposal_ref, site_ref,"
                    " grid_node_ref, data_collection_ref, research_grant_ref,"
                    " demand_mw, max_price_cny_per_kwh,"
                    " required_data_categories, target_mode, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        proposal_ref, site_ref, grid_node_ref,
                        data_collection_ref, research_grant_ref, demand_mw,
                        max_price_text, json.dumps(categories), target_mode,
                        now_iso(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"方案 {proposal_ref} 已存在") from exc
        return self.get_proposal(proposal_ref)

    def _get_proposal_row(
        self, conn: sqlite3.Connection, proposal_ref: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM proposal WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
        if row is None:
            raise NotFound(f"方案 {proposal_ref} 不存在")
        return row

    @staticmethod
    def _proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "proposal_ref": row["proposal_ref"],
            "site_ref": row["site_ref"],
            "grid_node_ref": row["grid_node_ref"],
            "data_collection_ref": row["data_collection_ref"],
            "research_grant_ref": row["research_grant_ref"],
            "demand_mw": row["demand_mw"],
            "max_price_cny_per_kwh": row["max_price_cny_per_kwh"],
            "required_data_categories": json.loads(
                row["required_data_categories"]
            ),
            "target_mode": row["target_mode"],
            "created_at": row["created_at"],
        }

    def get_proposal(self, proposal_ref: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._proposal_dict(
                self._get_proposal_row(conn, proposal_ref)
            )

    def list_proposals(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                self._proposal_dict(r)
                for r in conn.execute(
                    "SELECT * FROM proposal ORDER BY proposal_ref"
                ).fetchall()
            ]

    # ---------- 评审轮次 ----------

    def open_round(self, round_id: str, note: str | None) -> dict[str, Any]:
        require_ref(round_id, "round_id")
        if note is not None:
            note = bounded_text(note, "note", 500)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO review_round(round_id, opened_at, note)"
                    " VALUES (?,?,?)",
                    (round_id, now_iso(), note),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"轮次 {round_id} 已存在") from exc
        return {"round_id": round_id, "note": note}

    # ---------- 证据锁定 ----------

    def lock_evidence(
        self,
        round_id: str,
        proposal_ref: str,
        dimension: str,
        evidence_id: str,
        revision: int,
    ) -> dict[str, Any]:
        if dimension not in evaluation.DIMENSIONS:
            raise StoreError(f"dimension 必须是 {evaluation.DIMENSIONS} 之一")
        if not isinstance(revision, int) or revision <= 0:
            raise StoreError("revision 必须是正整数")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM review_round WHERE round_id=?", (round_id,)
            ).fetchone() is None:
                raise NotFound(f"轮次 {round_id} 不存在")
            proposal = self._get_proposal_row(conn, proposal_ref)
            existing = conn.execute(
                "SELECT evidence_id, revision FROM round_lock"
                " WHERE round_id=? AND proposal_ref=? AND dimension=?",
                (round_id, proposal_ref, dimension),
            ).fetchone()
            if existing is not None:
                raise Conflict(
                    f"方案 {proposal_ref} 的 {dimension} 在本轮已锁定为"
                    f" {existing['evidence_id']} 版本 {existing['revision']}；"
                    "轮次内证据版本不可更换，请开新一轮评审"
                )
            ev = conn.execute(
                "SELECT e.dimension, e.subject_ref, r.* FROM evidence e"
                " JOIN evidence_revision r USING (evidence_id)"
                " WHERE e.evidence_id=? AND r.revision=?",
                (evidence_id, revision),
            ).fetchone()
            if ev is None:
                raise NotFound(f"证据版本 {evidence_id}@{revision} 不存在")
            if ev["dimension"] != dimension:
                raise StoreError(
                    f"证据 {evidence_id} 属于 {ev['dimension']}，不能锁为 {dimension}"
                )
            subject_field = DIMENSION_SUBJECT_FIELD.get(dimension)
            if subject_field and ev["subject_ref"] != proposal[subject_field]:
                raise StoreError(
                    f"证据主体 {ev['subject_ref']} 与方案 {subject_field}"
                    f" {proposal[subject_field]} 不一致"
                )
            locked_at = now_iso()
            # 只允许锁定锁定时点已生效（且未到期）的 issued 版本，
            # 避免把“尚未落实”的材料拼进建设条件
            if ev["status"] != "issued":
                raise Conflict(
                    f"证据版本状态为 {ev['status']}，只有 issued 版本可锁定"
                )
            if locked_at < ev["effective_from"]:
                raise Conflict("证据版本在锁定时点尚未生效")
            if ev["effective_to"] and locked_at >= ev["effective_to"]:
                raise Conflict("证据版本在锁定时点已失效")
            conn.execute(
                "INSERT INTO round_lock(round_id, proposal_ref, dimension,"
                " evidence_id, revision, status_snapshot, effective_from_snapshot,"
                " effective_to_snapshot, payload_ref_snapshot,"
                " payload_sha256_snapshot, fact_snapshot, locked_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    round_id, proposal_ref, dimension, evidence_id, revision,
                    ev["status"], ev["effective_from"], ev["effective_to"],
                    ev["payload_ref"], ev["payload_sha256"], ev["fact"],
                    locked_at,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self._get_lock(round_id, proposal_ref, dimension)

    def _get_lock(
        self, round_id: str, proposal_ref: str, dimension: str
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM round_lock WHERE round_id=? AND proposal_ref=?"
                " AND dimension=?",
                (round_id, proposal_ref, dimension),
            ).fetchone()
            if row is None:
                raise NotFound(
                    f"轮次 {round_id} 方案 {proposal_ref} 未锁定 {dimension}"
                )
            return self._lock_dict(row)

    @staticmethod
    def _lock_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "dimension": row["dimension"],
            "evidence_id": row["evidence_id"],
            "revision": row["revision"],
            "locked_at": row["locked_at"],
            "status_snapshot": row["status_snapshot"],
            "effective_from": row["effective_from_snapshot"],
            "effective_to": row["effective_to_snapshot"],
            "payload_ref": row["payload_ref_snapshot"],
            "payload_sha256": row["payload_sha256_snapshot"],
            "fact": json.loads(row["fact_snapshot"]),
        }

    def _locks_for(
        self, conn: sqlite3.Connection, round_id: str, proposal_ref: str
    ) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM round_lock WHERE round_id=? AND proposal_ref=?",
            (round_id, proposal_ref),
        ).fetchall()
        return {r["dimension"]: self._lock_dict(r) for r in rows}

    # ---------- 容量锁定 ----------

    def lock_capacity(self, round_id: str, proposal_ref: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM review_round WHERE round_id=?", (round_id,)
            ).fetchone() is None:
                raise NotFound(f"轮次 {round_id} 不存在")
            proposal = self._get_proposal_row(conn, proposal_ref)
            existing = conn.execute(
                "SELECT * FROM capacity_reservation"
                " WHERE proposal_ref=? AND status='held'",
                (proposal_ref,),
            ).fetchone()
            if existing is not None:
                # 同一方案的既有预留持续有效，不随新一轮或他项占用而消失
                return {
                    "proposal_ref": proposal_ref,
                    "round_id": existing["round_id"],
                    "grid_node_ref": existing["grid_node_ref"],
                    "commitment_revision": existing["commitment_revision"],
                    "mw": existing["mw"],
                    "locked_at": existing["locked_at"],
                    "already_held": True,
                }
            at = now_iso()
            commitment = self._current_commitment(
                conn, proposal["grid_node_ref"], at
            )
            try:
                cur = conn.execute(
                    "INSERT INTO capacity_reservation(round_id, proposal_ref,"
                    " grid_node_ref, commitment_revision, mw, status, locked_at)"
                    " VALUES (?,?,?,?,?,'held',?)",
                    (
                        round_id, proposal_ref, proposal["grid_node_ref"],
                        commitment["revision"], proposal["demand_mw"], at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "committed" in str(exc):
                    held_total = conn.execute(
                        "SELECT COALESCE(SUM(CAST(mw AS REAL)),0) AS t"
                        " FROM capacity_reservation"
                        " WHERE grid_node_ref=? AND status='held'",
                        (proposal["grid_node_ref"],),
                    ).fetchone()["t"]
                    raise CapacityExceeded(
                        f"节点 {proposal['grid_node_ref']} 承诺值"
                        f" {commitment['committed_mw']} MW，已锁定"
                        f" {_num_str(held_total)} MW，方案再需"
                        f" {proposal['demand_mw']} MW，并发锁定总量将越过承诺值"
                    ) from exc
                raise
            conn.commit()
            reservation_id = cur.lastrowid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "reservation_id": reservation_id,
            "proposal_ref": proposal_ref,
            "round_id": round_id,
            "grid_node_ref": proposal["grid_node_ref"],
            "commitment_revision": commitment["revision"],
            "mw": proposal["demand_mw"],
            "locked_at": at,
            "already_held": False,
        }

    def _held_reservation(
        self, conn: sqlite3.Connection, proposal_ref: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM capacity_reservation WHERE proposal_ref=?"
            " AND status='held'",
            (proposal_ref,),
        ).fetchone()

    # ---------- 缺口快照 ----------

    def snapshot(
        self, round_id: str, proposal_ref: str, at: str | None = None
    ) -> dict[str, Any]:
        at = parse_ts(at, "at") if at else now_iso()
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM review_round WHERE round_id=?", (round_id,)
            ).fetchone() is None:
                raise NotFound(f"轮次 {round_id} 不存在")
            proposal = self._proposal_dict(
                self._get_proposal_row(conn, proposal_ref)
            )
            locks = self._locks_for(conn, round_id, proposal_ref)
            reservation = self._held_reservation(conn, proposal_ref)
            commitment = conn.execute(
                "SELECT * FROM grid_commitment WHERE grid_node_ref=?"
                " AND effective_from<=? ORDER BY revision DESC LIMIT 1",
                (proposal["grid_node_ref"], at),
            ).fetchone()
            held_total = conn.execute(
                "SELECT COALESCE(SUM(CAST(mw AS REAL)),0) AS t"
                " FROM capacity_reservation WHERE grid_node_ref=?"
                " AND status='held'",
                (proposal["grid_node_ref"],),
            ).fetchone()["t"]
        comparison = evaluation.assess_both_modes(locks, proposal)
        capacity = {
            "locked": reservation is not None,
            "mw": proposal["demand_mw"],
        }
        if reservation is not None:
            capacity.update(
                {
                    "reservation_round_id": reservation["round_id"],
                    "locked_against_commitment_revision":
                        reservation["commitment_revision"],
                    "locked_at": reservation["locked_at"],
                }
            )
        if commitment is not None:
            committed_now = float(commitment["committed_mw"])
            capacity["current_commitment"] = {
                "revision": commitment["revision"],
                "committed_mw": commitment["committed_mw"],
            }
            capacity["held_total_mw"] = _num_str(held_total)
            capacity["remaining_mw"] = _num_str(committed_now - held_total)
            if reservation is None:
                demand = float(proposal["demand_mw"])
                if demand > committed_now - held_total:
                    capacity["gap"] = "capacity_exhausted"
                    capacity["detail"] = (
                        f"剩余 {_num_str(committed_now - held_total)} MW"
                        f" 不足方案需求 {proposal['demand_mw']} MW；"
                        "容量已被其他项目锁定，本方案尚未锁定"
                    )
        else:
            capacity["gap"] = "no_effective_commitment"
        return {
            "round_id": round_id,
            "proposal_ref": proposal_ref,
            "at": at,
            "proposal": proposal,
            "locks": locks,
            "capacity": capacity,
            "assessment": comparison,
        }

    def comparison(
        self, round_id: str, mode: str = "auto"
    ) -> dict[str, Any]:
        """按口径列出可比较方案。

        auto：跨境就绪按跨境；仅因跨境限制不足时降级按不出域比较。
        domestic：只按不出域口径。
        """
        if mode not in ("auto", "cross_border", "domestic"):
            raise StoreError("mode 必须是 auto / cross_border / domestic")
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM review_round WHERE round_id=?", (round_id,)
            ).fetchone() is None:
                raise NotFound(f"轮次 {round_id} 不存在")
            proposals = [
                self._proposal_dict(r)
                for r in conn.execute("SELECT * FROM proposal ORDER BY proposal_ref")
            ]
            rows: list[dict[str, Any]] = []
            for p in proposals:
                locks = self._locks_for(conn, round_id, p["proposal_ref"])
                reservation = self._held_reservation(conn, p["proposal_ref"])
                comp = evaluation.assess_both_modes(locks, p)
                if mode == "cross_border":
                    used, ready = "cross_border", comp["cross_border_ready"]
                    result = comp["cross_border"]
                elif mode == "domestic":
                    used, ready = "domestic", comp["domestic_ready"]
                    result = comp["domestic"]
                else:
                    used = comp["comparable_mode"]
                    ready = comp["comparable"]
                    result = (
                        comp[used] if used else comp["cross_border"]
                    )
                rows.append({
                    "proposal_ref": p["proposal_ref"],
                    "target_mode": p["target_mode"],
                    "comparable": ready,
                    "evaluated_mode": used,
                    "capacity_locked": reservation is not None,
                    "ready": ready and reservation is not None,
                    "demand_mw": p["demand_mw"],
                    "gaps": [] if ready else result["gaps"],
                })
        return {
            "round_id": round_id,
            "requested_mode": mode,
            "note": (
                "跨境条件不足但仅涉驻留/服务区域时，自动转不出域口径；"
                "响应只含受控引用与摘要，不含企业原始数据"
                if mode == "auto" else None
            ),
            "proposals": rows,
        }

    # ---------- 决策 ----------

    def decide(
        self,
        round_id: str,
        proposal_ref: str,
        outcome: str,
        mode: str | None,
        rationale: str,
    ) -> dict[str, Any]:
        if outcome not in ("approved", "rejected"):
            raise StoreError("outcome 必须是 approved 或 rejected")
        rationale = bounded_text(rationale, "rationale", 1000)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM review_round WHERE round_id=?", (round_id,)
            ).fetchone() is None:
                raise NotFound(f"轮次 {round_id} 不存在")
            proposal_row = self._get_proposal_row(conn, proposal_ref)
            proposal = self._proposal_dict(proposal_row)
            locks = self._locks_for(conn, round_id, proposal_ref)
            reservation = self._held_reservation(conn, proposal_ref)
            comp = evaluation.assess_both_modes(locks, proposal)

            if mode is None:
                chosen = comp["comparable_mode"]
                if outcome == "approved" and chosen is None:
                    raise Conflict(
                        "不存在可判定就绪的口径，不能批准；缺口见轮次快照"
                    )
            else:
                if mode not in ("cross_border", "domestic"):
                    raise StoreError("mode 必须是 cross_border 或 domestic")
                chosen = mode if comp[mode]["ready"] else None
                if outcome == "approved" and chosen is None:
                    raise Conflict(
                        f"按 {mode} 口径仍有缺口，不能批准"
                    )
            if outcome == "approved" and reservation is None:
                raise Conflict("方案尚未锁定变电容量，不能批准")
            evaluated_mode = chosen or (mode or "cross_border")

            decided_at = now_iso()
            at_commitment = conn.execute(
                "SELECT * FROM grid_commitment WHERE grid_node_ref=?"
                " AND effective_from<=? ORDER BY revision DESC LIMIT 1",
                (proposal["grid_node_ref"], decided_at),
            ).fetchone()
            held_total = conn.execute(
                "SELECT COALESCE(SUM(CAST(mw AS REAL)),0) AS t"
                " FROM capacity_reservation WHERE grid_node_ref=?"
                " AND status='held'",
                (proposal["grid_node_ref"],),
            ).fetchone()["t"]
            frozen = {
                "schema_version": 1,
                "round_id": round_id,
                "proposal": proposal,
                "decided_at": decided_at,
                "evaluated_mode": evaluated_mode,
                "assessment": (
                    comp[evaluated_mode] if evaluated_mode in comp else comp["cross_border"]
                ),
                "both_modes": comp,
                "locks": locks,
                "capacity": {
                    "reservation": (
                        None if reservation is None
                        else {
                            "mw": reservation["mw"],
                            "commitment_revision":
                                reservation["commitment_revision"],
                            "locked_at": reservation["locked_at"],
                        }
                    ),
                    "commitment_at_decision": (
                        None if at_commitment is None
                        else {
                            "revision": at_commitment["revision"],
                            "committed_mw": at_commitment["committed_mw"],
                        }
                    ),
                    "held_total_mw": _num_str(held_total),
                },
            }
            decision_id = f"DEC-{round_id}-{proposal_ref}"
            conn.execute(
                "INSERT INTO decision(decision_id, round_id, proposal_ref,"
                " grid_node_ref, outcome, evaluated_mode, decided_at,"
                " rationale, frozen_snapshot) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, round_id, proposal_ref,
                    proposal["grid_node_ref"], outcome, evaluated_mode,
                    decided_at, rationale,
                    json.dumps(frozen, ensure_ascii=False),
                ),
            )
            for dimension, lock in locks.items():
                conn.execute(
                    "INSERT INTO decision_lock_ref(decision_id, dimension,"
                    " evidence_id, revision) VALUES (?,?,?,?)",
                    (decision_id, dimension, lock["evidence_id"],
                     lock["revision"]),
                )
            if outcome == "rejected" and reservation is not None:
                conn.execute(
                    "UPDATE capacity_reservation SET status='released',"
                    " released_at=?, release_reason=?"
                    " WHERE reservation_id=?",
                    (decided_at, f"方案在轮次 {round_id} 被驳回", reservation["reservation_id"]),
                )
            if outcome == "approved":
                # 给同节点的既有获批决策挂“后续被占用”变化
                for other in conn.execute(
                    "SELECT decision_id, proposal_ref FROM decision"
                    " WHERE grid_node_ref=? AND outcome='approved'"
                    " AND decision_id<>?",
                    (proposal["grid_node_ref"], decision_id),
                ).fetchall():
                    conn.execute(
                        "INSERT INTO decision_note(decision_id, kind, note,"
                        " recorded_at) VALUES (?,?,?,?)",
                        (
                            other["decision_id"], "capacity_occupied",
                            f"项目 {proposal_ref} 随后获批并锁定"
                            f" {reservation['mw']} MW；不影响本决策当时的成立依据",
                            decided_at,
                        ),
                    )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("该方案在本轮已有决策记录") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_decision(
            f"DEC-{round_id}-{proposal_ref}", include_history=False
        )

    def add_decision_note(
        self, decision_id: str, kind: str, note: str
    ) -> dict[str, Any]:
        valid_kinds = (
            "commitment_changed", "capacity_occupied",
            "evidence_superseded", "evidence_withdrawn",
            "window_changed", "general",
        )
        if kind not in valid_kinds:
            raise StoreError(f"kind 必须是 {valid_kinds} 之一")
        note = bounded_text(note, "note", 1000)
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM decision WHERE decision_id=?", (decision_id,)
            ).fetchone() is None:
                raise NotFound(f"决策 {decision_id} 不存在")
            conn.execute(
                "INSERT INTO decision_note(decision_id, kind, note, recorded_at)"
                " VALUES (?,?,?,?)",
                (decision_id, kind, note, now_iso()),
            )
        return {"decision_id": decision_id, "kind": kind, "note": note}

    def get_decision(
        self, decision_id: str, include_history: bool = True
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM decision WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"决策 {decision_id} 不存在")
            frozen = json.loads(row["frozen_snapshot"])
            result = {
                "decision_id": decision_id,
                "round_id": row["round_id"],
                "proposal_ref": row["proposal_ref"],
                "outcome": row["outcome"],
                "evaluated_mode": row["evaluated_mode"],
                "decided_at": row["decided_at"],
                "rationale": row["rationale"],
                "frozen_snapshot": frozen,
            }
            if include_history:
                result["history"] = self._history(conn, row, frozen)
        return result

    def _history(
        self, conn: sqlite3.Connection, decision: sqlite3.Row, frozen: dict
    ) -> dict[str, Any]:
        notes = [
            dict(r)
            for r in conn.execute(
                "SELECT note_id, kind, note, recorded_at"
                " FROM decision_note WHERE decision_id=?"
                " ORDER BY note_id",
                (decision["decision_id"],),
            ).fetchall()
        ]
        # 现行视图：与冻结版本逐项对照，只描述“后来变化”
        changes: list[dict[str, Any]] = []
        for dimension, lock in frozen["locks"].items():
            latest = conn.execute(
                "SELECT revision, status FROM evidence_revision"
                " WHERE evidence_id=? ORDER BY revision DESC LIMIT 1",
                (lock["evidence_id"],),
            ).fetchone()
            locked_rev_row = conn.execute(
                "SELECT status FROM evidence_revision WHERE evidence_id=?"
                " AND revision=?",
                (lock["evidence_id"], lock["revision"]),
            ).fetchone()
            if locked_rev_row is not None and locked_rev_row["status"] != "issued":
                changes.append({
                    "dimension": dimension,
                    "kind": f"evidence_{locked_rev_row['status']}",
                    "detail": f"锁定版本 {lock['evidence_id']}@{lock['revision']}"
                              f" 现为 {locked_rev_row['status']}",
                })
            elif latest is not None and latest["revision"] > lock["revision"]:
                changes.append({
                    "dimension": dimension,
                    "kind": "newer_revision_exists",
                    "detail": f"证据 {lock['evidence_id']} 已有版本"
                              f" {latest['revision']}（锁定时为 {lock['revision']}）",
                })
        current_commitment = conn.execute(
            "SELECT revision, committed_mw FROM grid_commitment"
            " WHERE grid_node_ref=? ORDER BY revision DESC LIMIT 1",
            (decision["grid_node_ref"],),
        ).fetchone()
        frozen_commit = frozen["capacity"]["commitment_at_decision"]
        if (
            current_commitment is not None and frozen_commit is not None
            and (
                current_commitment["revision"] != frozen_commit["revision"]
                or current_commitment["committed_mw"] != frozen_commit["committed_mw"]
            )
        ):
            changes.append({
                "dimension": "capacity",
                "kind": "commitment_changed",
                "detail": f"承诺值现为 {current_commitment['committed_mw']} MW"
                          f"（版本 {current_commitment['revision']}），决策时为"
                          f" {frozen_commit['committed_mw']} MW"
                          f"（版本 {frozen_commit['revision']}）",
            })
        return {
            "basis_statement": (
                f"该方案于 {decision['decided_at']} 在轮次"
                f" {decision['round_id']} 按 {decision['evaluated_mode']}"
                " 口径评审，依据为上方冻结快照中的六个证据版本与当时承诺值；"
                "下列变化均发生在决策之后，不改变原判断。"
            ),
            "subsequent_changes": changes,
            "subsequent_notes": notes,
        }
