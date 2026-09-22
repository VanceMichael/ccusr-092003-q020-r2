
"""领域服务：证据登记、按轮次锁版本、容量预留、缺口评估、决策快照。

所有写操作在单事务内完成；容量校验使用 BEGIN IMMEDIATE，
保证并发预留总量不越过节点承诺值。
"""

import json
import sqlite3

from . import validation as v
from .db import connect


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 证据登记


def register_evidence(payload: dict, connection: sqlite3.Connection | None = None) -> dict:
    evidence_ref = v.require_ref(payload.get("evidence_ref"), "evidence_ref")
    dimension = payload.get("dimension")
    if dimension not in v.DIMENSIONS:
        raise DomainError(f"dimension 必须是 {list(v.DIMENSIONS)} 之一")
    version_label = v.require_ref(payload.get("version_label"), "version_label")
    source_ref = v.require_source_ref(payload.get("source_ref"))
    source_sha256 = v.require_sha256(payload.get("source_sha256"))
    attributes = v.validate_attributes(dimension, payload.get("attributes"))
    effective = v.parse_timestamp(payload.get("effective_at"), "effective_at")

    own = connection is None
    connection = connection or connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cur = connection.execute(
            "INSERT INTO evidence_revisions"
            "(evidence_ref, dimension, version_label, source_ref, source_sha256,"
            " attributes_json, effective_at, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (evidence_ref, dimension, version_label, source_ref, source_sha256,
             json.dumps(attributes, ensure_ascii=False, sort_keys=True),
             effective.isoformat(), v.now_iso()),
        )
        connection.execute("COMMIT")
        revision_id = cur.lastrowid
    except sqlite3.IntegrityError:
        connection.execute("ROLLBACK")
        raise DomainError("该 evidence_ref + version_label 已存在；证据不可覆盖，请登记新版本", 409)
    finally:
        if own:
            connection.close()
    return get_evidence(revision_id)


def _revision_by_label(connection: sqlite3.Connection, evidence_ref: str,
                       version_label: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM evidence_revisions WHERE evidence_ref=? AND version_label=?",
        (evidence_ref, version_label),
    ).fetchone()
    if row is None:
        raise DomainError(f"证据版本不存在：{evidence_ref}@{version_label}", 404)
    return row


def get_evidence(revision_id: int, connection: sqlite3.Connection | None = None) -> dict:
    own = connection is None
    connection = connection or connect()
    try:
        row = connection.execute(
            "SELECT * FROM evidence_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
    finally:
        if own:
            connection.close()
    if row is None:
        raise DomainError("证据版本不存在", 404)
    return _revision_dict(row)


def list_evidence(dimension: str | None = None) -> list[dict]:
    connection = connect()
    try:
        if dimension:
            rows = connection.execute(
                "SELECT * FROM evidence_revisions WHERE dimension=? ORDER BY revision_id",
                (dimension,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM evidence_revisions ORDER BY revision_id"
            ).fetchall()
    finally:
        connection.close()
    return [_revision_dict(r) for r in rows]


def _revision_dict(row: sqlite3.Row) -> dict:
    return {
        "revision_id": row["revision_id"],
        "evidence_ref": row["evidence_ref"],
        "dimension": row["dimension"],
        "version_label": row["version_label"],
        "source_ref": row["source_ref"],
        "source_sha256": row["source_sha256"],
        "attributes": json.loads(row["attributes_json"]),
        "effective_at": row["effective_at"],
        "recorded_at": row["recorded_at"],
    }


def get_latest_revision(evidence_ref: str) -> dict | None:
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM evidence_revisions WHERE evidence_ref=?"
            " ORDER BY revision_id DESC LIMIT 1",
            (evidence_ref,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else _revision_dict(row)


# ---------------------------------------------------------------- 方案


def register_proposal(payload: dict) -> dict:
    proposal_ref = v.require_ref(payload.get("proposal_ref"), "proposal_ref")
    title = payload.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 128):
        raise DomainError("title 需为 1..128 字的方案名称")
    mode = payload.get("service_mode")
    if mode not in ("IN_DOMAIN", "CROSS_BORDER"):
        raise DomainError("service_mode 必须是 IN_DOMAIN 或 CROSS_BORDER")
    capacity = payload.get("capacity_mw")
    if isinstance(capacity, bool) or not isinstance(capacity, (int, float)) or capacity <= 0:
        raise DomainError("capacity_mw 必须是正数")
    submitted = v.parse_timestamp(payload.get("submitted_at"), "submitted_at")

    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "INSERT INTO proposals(proposal_ref, title, service_mode,"
                " capacity_mw, submitted_at, created_at) VALUES (?,?,?,?,?,?)",
                (proposal_ref, title, mode, float(capacity),
                 submitted.isoformat(), v.now_iso()),
            )
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            raise DomainError("proposal_ref 已存在", 409)
        connection.execute("COMMIT")
    finally:
        connection.close()
    return get_proposal(proposal_ref)


def get_proposal(proposal_ref: str, connection: sqlite3.Connection | None = None) -> dict:
    own = connection is None
    connection = connection or connect()
    try:
        row = connection.execute(
            "SELECT * FROM proposals WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
    finally:
        if own:
            connection.close()
    if row is None:
        raise DomainError("方案不存在", 404)
    return dict(row)


def list_proposals() -> list[dict]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT * FROM proposals ORDER BY submitted_at, proposal_ref"
        ).fetchall()
    finally:
        connection.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 轮次


def create_round(payload: dict) -> dict:
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > 200:
        raise DomainError("note 需为 200 字以内")
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        seq_row = connection.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next FROM review_rounds"
        ).fetchone()
        sequence_no = seq_row["next"]
        round_id = payload.get("round_id") or f"ROUND-{sequence_no:04d}"
        v.require_ref(round_id, "round_id")
        try:
            connection.execute(
                "INSERT INTO review_rounds(round_id, sequence_no, opened_at, note)"
                " VALUES (?,?,?,?)",
                (round_id, sequence_no, v.now_iso(), note),
            )
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            raise DomainError("round_id 已存在", 409)
        connection.execute("COMMIT")
    finally:
        connection.close()
    return get_round(round_id)


def get_round(round_id: str) -> dict:
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM review_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise DomainError("评审轮次不存在", 404)
    return dict(row)


def close_round(round_id: str) -> dict:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM review_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            raise DomainError("评审轮次不存在", 404)
        if row["closed_at"]:
            connection.execute("ROLLBACK")
            raise DomainError("轮次已关闭", 409)
        connection.execute(
            "UPDATE review_rounds SET closed_at=? WHERE round_id=?",
            (v.now_iso(), round_id),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return get_round(round_id)


def _require_open_round(connection: sqlite3.Connection, round_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM review_rounds WHERE round_id=?", (round_id,)
    ).fetchone()
    if row is None:
        raise DomainError("评审轮次不存在", 404)
    if row["closed_at"]:
        raise DomainError("评审轮次已关闭，不能再改变锁定", 409)
    return row


# ---------------------------------------------------------------- 容量承诺


def set_commitment(grid_node_ref: str, payload: dict) -> dict:
    v.require_ref(grid_node_ref, "grid_node_ref")
    committed = payload.get("committed_mw")
    if isinstance(committed, bool) or not isinstance(committed, (int, float)) or committed < 0:
        raise DomainError("committed_mw 必须是非负数")
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO grid_capacity_commitments(grid_node_ref, committed_mw, updated_at)"
            " VALUES (?,?,?) ON CONFLICT(grid_node_ref) DO UPDATE SET"
            " committed_mw=excluded.committed_mw, updated_at=excluded.updated_at",
            (grid_node_ref, float(committed), v.now_iso()),
        )
        # 已持有的预留不受承诺下调影响；可用性可能为负，只会挡住新锁定
        held = connection.execute(
            "SELECT COALESCE(SUM(reserved_mw),0) AS s FROM capacity_reservations"
            " WHERE grid_node_ref=? AND status='held'",
            (grid_node_ref,),
        ).fetchone()["s"]
        connection.execute("COMMIT")
    finally:
        connection.close()
    return {
        "grid_node_ref": grid_node_ref,
        "committed_mw": float(committed),
        "held_mw": held,
        "available_mw": round(float(committed) - held, 6),
    }


def get_commitment(grid_node_ref: str, connection: sqlite3.Connection | None = None) -> dict | None:
    own = connection is None
    connection = connection or connect()
    try:
        row = connection.execute(
            "SELECT * FROM grid_capacity_commitments WHERE grid_node_ref=?",
            (grid_node_ref,),
        ).fetchone()
    finally:
        if own:
            connection.close()
    return None if row is None else dict(row)


def _held_mw(connection: sqlite3.Connection, grid_node_ref: str,
             exclude_reservation: int | None = None) -> float:
    sql = ("SELECT COALESCE(SUM(reserved_mw),0) AS s FROM capacity_reservations"
           " WHERE grid_node_ref=? AND status='held'")
    params: list = [grid_node_ref]
    if exclude_reservation is not None:
        sql += " AND reservation_id<>?"
        params.append(exclude_reservation)
    return float(connection.execute(sql, params).fetchone()["s"])


# ---------------------------------------------------------------- 版本锁定


def lock_evidence(round_id: str, payload: dict) -> dict:
    proposal_ref = v.require_ref(payload.get("proposal_ref"), "proposal_ref")
    dimension = payload.get("dimension")
    if dimension not in v.DIMENSIONS:
        raise DomainError(f"dimension 必须是 {list(v.DIMENSIONS)} 之一")
    evidence_ref = v.require_ref(payload.get("evidence_ref"), "evidence_ref")
    version_label = v.require_ref(payload.get("version_label"), "version_label")

    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_open_round(connection, round_id)
        proposal = connection.execute(
            "SELECT * FROM proposals WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
        if proposal is None:
            connection.execute("ROLLBACK")
            raise DomainError("方案不存在", 404)
        if connection.execute(
            "SELECT 1 FROM decisions WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone():
            connection.execute("ROLLBACK")
            raise DomainError("方案已有决策，不能再改变锁定", 409)

        revision = _revision_by_label(connection, evidence_ref, version_label)
        if revision["dimension"] != dimension:
            connection.execute("ROLLBACK")
            raise DomainError(
                f"证据 {evidence_ref}@{version_label} 属于 {revision['dimension']}，与 {dimension} 不符",
                409,
            )

        # 电力窗口维度：同事务预留变电节点容量
        reservation_out: dict | None = None
        old_reservation_id: int | None = None
        if dimension == "power_window":
            attrs = json.loads(revision["attributes_json"])
            grid_node_ref = attrs["grid_node_ref"]
            wanted = float(proposal["capacity_mw"])
            old = connection.execute(
                "SELECT * FROM capacity_reservations"
                " WHERE round_id=? AND proposal_ref=? AND status='held'"
                " ORDER BY reservation_id DESC LIMIT 1",
                (round_id, proposal_ref),
            ).fetchone()
            if old is not None:
                old_reservation_id = old["reservation_id"]
            commitment = connection.execute(
                "SELECT * FROM grid_capacity_commitments WHERE grid_node_ref=?",
                (grid_node_ref,),
            ).fetchone()
            if commitment is None:
                connection.execute("ROLLBACK")
                raise DomainError(f"变电节点 {grid_node_ref} 尚无承诺容量，不能锁定电力窗口", 409)
            held = _held_mw(connection, grid_node_ref, exclude_reservation=old_reservation_id)
            if held + wanted > float(commitment["committed_mw"]) + 1e-9:
                connection.execute("ROLLBACK")
                raise DomainError(
                    f"节点 {grid_node_ref} 容量不足：已预留 {held} MW，"
                    f"承诺 {commitment['committed_mw']} MW，本次需要 {wanted} MW",
                    409,
                )

        existing = connection.execute(
            "SELECT * FROM evidence_locks WHERE round_id=? AND proposal_ref=? AND dimension=?",
            (round_id, proposal_ref, dimension),
        ).fetchone()
        if existing is not None:
            if existing["revision_id"] == revision["revision_id"]:
                connection.execute("ROLLBACK")
                raise DomainError("该维度已锁定到同一证据版本", 409)
            connection.execute(
                "DELETE FROM evidence_locks WHERE round_id=? AND proposal_ref=? AND dimension=?",
                (round_id, proposal_ref, dimension),
            )
            if dimension == "power_window" and old_reservation_id is not None:
                connection.execute(
                    "UPDATE capacity_reservations SET status='released',"
                    " released_at=? WHERE reservation_id=?",
                    (v.now_iso(), old_reservation_id),
                )

        connection.execute(
            "INSERT INTO evidence_locks(round_id, proposal_ref, dimension,"
            " revision_id, locked_at) VALUES (?,?,?,?,?)",
            (round_id, proposal_ref, dimension, revision["revision_id"], v.now_iso()),
        )
        if dimension == "power_window":
            attrs = json.loads(revision["attributes_json"])
            cur = connection.execute(
                "INSERT INTO capacity_reservations(round_id, grid_node_ref,"
                " proposal_ref, reserved_mw, status, created_at)"
                " VALUES (?,?,?,?, 'held', ?)",
                (round_id, attrs["grid_node_ref"], proposal_ref,
                 float(proposal["capacity_mw"]), v.now_iso()),
            )
            reservation_out = {
                "reservation_id": cur.lastrowid,
                "grid_node_ref": attrs["grid_node_ref"],
                "reserved_mw": float(proposal["capacity_mw"]),
                "status": "held",
            }
        connection.execute("COMMIT")
    finally:
        connection.close()
    result = get_lock(round_id, proposal_ref, dimension)
    if reservation_out is not None:
        result["capacity_reservation"] = reservation_out
    return result


def get_lock(round_id: str, proposal_ref: str, dimension: str) -> dict:
    connection = connect()
    try:
        row = connection.execute(
            "SELECT l.round_id, l.proposal_ref, l.dimension, l.locked_at,"
            " r.revision_id, r.evidence_ref, r.version_label, r.source_ref,"
            " r.source_sha256, r.attributes_json, r.effective_at"
            " FROM evidence_locks l JOIN evidence_revisions r"
            " ON r.revision_id=l.revision_id"
            " WHERE l.round_id=? AND l.proposal_ref=? AND l.dimension=?",
            (round_id, proposal_ref, dimension),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise DomainError("锁定不存在", 404)
    return _lock_dict(row)


def list_locks(round_id: str, proposal_ref: str | None = None) -> list[dict]:
    connection = connect()
    try:
        sql = ("SELECT l.round_id, l.proposal_ref, l.dimension, l.locked_at,"
               " r.revision_id, r.evidence_ref, r.version_label, r.source_ref,"
               " r.source_sha256, r.attributes_json, r.effective_at"
               " FROM evidence_locks l JOIN evidence_revisions r"
               " ON r.revision_id=l.revision_id WHERE l.round_id=?")
        params: list = [round_id]
        if proposal_ref:
            sql += " AND l.proposal_ref=?"
            params.append(proposal_ref)
        sql += " ORDER BY l.proposal_ref, l.dimension"
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    return [_lock_dict(r) for r in rows]


def _lock_dict(row: sqlite3.Row) -> dict:
    return {
        "round_id": row["round_id"],
        "proposal_ref": row["proposal_ref"],
        "dimension": row["dimension"],
        "locked_at": row["locked_at"],
        "revision": {
            "revision_id": row["revision_id"],
            "evidence_ref": row["evidence_ref"],
            "version_label": row["version_label"],
            "source_ref": row["source_ref"],
            "source_sha256": row["source_sha256"],
            "attributes": json.loads(row["attributes_json"]),
            "effective_at": row["effective_at"],
        },
    }


def unlock_evidence(round_id: str, proposal_ref: str, dimension: str) -> dict:
    if dimension not in v.DIMENSIONS:
        raise DomainError(f"dimension 必须是 {list(v.DIMENSIONS)} 之一")
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_open_round(connection, round_id)
        lock = connection.execute(
            "SELECT * FROM evidence_locks WHERE round_id=? AND proposal_ref=? AND dimension=?",
            (round_id, proposal_ref, dimension),
        ).fetchone()
        if lock is None:
            connection.execute("ROLLBACK")
            raise DomainError("锁定不存在", 404)
        if connection.execute(
            "SELECT 1 FROM decisions WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone():
            connection.execute("ROLLBACK")
            raise DomainError("方案已有决策，不能再改变锁定", 409)
        released: dict | None = None
        if dimension == "power_window":
            rev = connection.execute(
                "SELECT attributes_json FROM evidence_revisions WHERE revision_id=?",
                (lock["revision_id"],),
            ).fetchone()
            grid_node_ref = json.loads(rev["attributes_json"])["grid_node_ref"]
            res = connection.execute(
                "SELECT * FROM capacity_reservations"
                " WHERE round_id=? AND proposal_ref=? AND grid_node_ref=? AND status='held'",
                (round_id, proposal_ref, grid_node_ref),
            ).fetchone()
            if res is not None:
                connection.execute(
                    "UPDATE capacity_reservations SET status='released', released_at=?"
                    " WHERE reservation_id=?",
                    (v.now_iso(), res["reservation_id"]),
                )
                released = {"reservation_id": res["reservation_id"], "status": "released"}
        connection.execute(
            "DELETE FROM evidence_locks WHERE round_id=? AND proposal_ref=? AND dimension=?",
            (round_id, proposal_ref, dimension),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return {"unlocked": {"round_id": round_id, "proposal_ref": proposal_ref,
                         "dimension": dimension}, "capacity_reservation": released}


def list_reservations(grid_node_ref: str | None = None) -> list[dict]:
    connection = connect()
    try:
        if grid_node_ref:
            rows = connection.execute(
                "SELECT * FROM capacity_reservations WHERE grid_node_ref=?"
                " ORDER BY reservation_id",
                (grid_node_ref,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM capacity_reservations ORDER BY reservation_id"
            ).fetchall()
    finally:
        connection.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 缺口评估


REQUIRED_FOR_MODE = {
    "IN_DOMAIN": ("power_window", "datacenter_stage", "data_scope", "residency"),
    "CROSS_BORDER": ("power_window", "datacenter_stage", "data_scope",
                     "residency", "research_authorization", "service_area"),
}


def _as_of(payload_as_of: str | None) -> "v.datetime":
    if payload_as_of:
        return v.parse_timestamp(payload_as_of, "as_of")
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def evaluate(round_id: str, proposal_ref: str, as_of: "v.datetime | None" = None) -> dict:
    """基于该轮次已锁定的证据版本评估建设条件缺口。"""
    from datetime import datetime
    as_of = as_of or datetime.now().astimezone()

    connection = connect()
    try:
        round_row = connection.execute(
            "SELECT * FROM review_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
        if round_row is None:
            raise DomainError("评审轮次不存在", 404)
        proposal = connection.execute(
            "SELECT * FROM proposals WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
        if proposal is None:
            raise DomainError("方案不存在", 404)
        locks = {
            row["dimension"]: _lock_dict(row)
            for row in connection.execute(
                "SELECT l.round_id, l.proposal_ref, l.dimension, l.locked_at,"
                " r.revision_id, r.evidence_ref, r.version_label, r.source_ref,"
                " r.source_sha256, r.attributes_json, r.effective_at"
                " FROM evidence_locks l JOIN evidence_revisions r"
                " ON r.revision_id=l.revision_id"
                " WHERE l.round_id=? AND l.proposal_ref=?",
                (round_id, proposal_ref),
            ).fetchall()
        }
        reservations = connection.execute(
            "SELECT * FROM capacity_reservations"
            " WHERE round_id=? AND proposal_ref=? AND status='held'",
            (round_id, proposal_ref),
        ).fetchall()
    finally:
        connection.close()

    mode = proposal["service_mode"]
    wanted_mw = float(proposal["capacity_mw"])
    dimensions: list[dict] = []
    gap_count = 0

    for dimension in v.DIMENSIONS:
        required = dimension in REQUIRED_FOR_MODE[mode]
        lock = locks.get(dimension)
        if not required and lock is None:
            dimensions.append({"dimension": dimension, "required": False,
                               "status": "NOT_REQUIRED"})
            continue
        if lock is None:
            gap_count += 1
            dimensions.append({"dimension": dimension, "required": required,
                               "status": "GAP", "reason": "本轮未锁定证据版本"})
            continue
        check = _check_dimension(dimension, lock["revision"]["attributes"],
                                 wanted_mw, as_of, mode)
        if required and check is not None:
            gap_count += 1
            dimensions.append({"dimension": dimension, "required": True,
                               "status": "GAP", "reason": check,
                               "locked_revision": _revision_ref(lock)})
        else:
            dimensions.append({"dimension": dimension, "required": required,
                               "status": "OK",
                               "locked_revision": _revision_ref(lock)})

    capacity: dict | None = None
    if reservations:
        res = reservations[0]
        commitment = get_commitment(res["grid_node_ref"])
        capacity = {
            "grid_node_ref": res["grid_node_ref"],
            "reserved_mw": res["reserved_mw"],
            "committed_mw_at_eval": commitment["committed_mw"] if commitment else None,
            "reservation_id": res["reservation_id"],
            "status": "HELD",
        }
    elif "power_window" in REQUIRED_FOR_MODE[mode]:
        capacity = {"status": "GAP", "reason": "未持有变电节点容量预留"}

    ready = gap_count == 0 and capacity is not None and capacity["status"] == "HELD"
    return {
        "round_id": round_id,
        "proposal_ref": proposal_ref,
        "service_mode": mode,
        "capacity_mw": wanted_mw,
        "evaluated_at": as_of.isoformat(),
        "ready_for_approval": ready,
        "gap_count": gap_count,
        "dimensions": dimensions,
        "capacity": capacity,
    }


def _revision_ref(lock: dict) -> dict:
    rev = lock["revision"]
    return {
        "evidence_ref": rev["evidence_ref"],
        "version_label": rev["version_label"],
        "revision_id": rev["revision_id"],
        "source_ref": rev["source_ref"],
        "source_sha256": rev["source_sha256"],
    }


def _parse(ts: str):
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    from datetime import datetime
    return datetime.fromisoformat(text)


def _check_dimension(dimension: str, a: dict, wanted_mw: float, as_of,
                     service_mode: str | None = None) -> str | None:
    """返回 None 表示满足；否则给出规范化缺口原因。"""
    if dimension == "power_window":
        if not (_parse(a["window_start"]) <= as_of <= _parse(a["window_end"])):
            return "电力窗口在评估时点不生效"
        return None
    if dimension == "datacenter_stage":
        if a["stage"] not in ("EQUIPMENT_READY", "OPERATIONAL"):
            return f"机房阶段为 {a['stage']}，未达到设备就绪"
        if float(a["delivered_mw"]) + 1e-9 < wanted_mw:
            return f"机房已交付 {a['delivered_mw']} MW，低于申请 {wanted_mw} MW"
        return None
    if dimension == "data_scope":
        if a["authorization_status"] != "GRANTED":
            return f"工业数据授权状态为 {a['authorization_status']}"
        expires = a.get("expires_at")
        if expires and _parse(expires) <= as_of:
            return "工业数据授权已过期"
        return None
    if dimension == "residency":
        mode = a["mode"]
        if mode == "NONE":
            return "尚无数据驻留安排"
        if service_mode == "CROSS_BORDER" and mode != "CROSS_BORDER_ALLOWED":
            return "跨境服务方案要求驻留证据允许跨境，当前仅允许境内驻留"
        return None
    if dimension == "research_authorization":
        if a["status"] != "GRANTED":
            return f"科研合作授权状态为 {a['status']}"
        expires = a.get("expires_at")
        if expires and _parse(expires) <= as_of:
            return "科研合作授权已过期"
        return None
    if dimension == "service_area":
        if a["cross_border_intent"] == "NONE":
            return "缺少跨境服务意向"
        if not a.get("regions"):
            return "服务区域为空"
        return None
    return None


def compare(round_id: str, service_mode: str | None = None) -> dict:
    """横向比较：跨境条件不足时，仍可只看出域（IN_DOMAIN）方案。"""
    connection = connect()
    try:
        round_row = connection.execute(
            "SELECT * FROM review_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
        if round_row is None:
            raise DomainError("评审轮次不存在", 404)
        proposal_rows = connection.execute(
            "SELECT proposal_ref FROM proposals ORDER BY proposal_ref"
        ).fetchall()
        refs = [r["proposal_ref"] for r in proposal_rows]
    finally:
        connection.close()
    items = [evaluate(round_id, ref) for ref in refs]
    if service_mode:
        if service_mode not in ("IN_DOMAIN", "CROSS_BORDER"):
            raise DomainError("service_mode 过滤值非法")
        items = [i for i in items if i["service_mode"] == service_mode]
    items.sort(key=lambda i: (not i["ready_for_approval"], i["gap_count"], i["proposal_ref"]))
    return {
        "round_id": round_id,
        "filter_service_mode": service_mode,
        "compared": len(items),
        "modes_present": sorted({i["service_mode"] for i in items}),
        "items": items,
    }


# ---------------------------------------------------------------- 决策与历史


def decide(proposal_ref: str, payload: dict) -> dict:
    round_id = v.require_ref(payload.get("round_id"), "round_id")
    outcome = payload.get("outcome")
    if outcome not in ("APPROVED", "REJECTED"):
        raise DomainError("outcome 必须是 APPROVED 或 REJECTED")
    rationale_ref = v.require_source_ref(payload.get("rationale_ref"))

    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        proposal = connection.execute(
            "SELECT * FROM proposals WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
        if proposal is None:
            connection.execute("ROLLBACK")
            raise DomainError("方案不存在", 404)
        if connection.execute(
            "SELECT 1 FROM decisions WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone():
            connection.execute("ROLLBACK")
            raise DomainError("方案已有决策，不可重复决策", 409)
        round_row = connection.execute(
            "SELECT * FROM review_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
        if round_row is None:
            connection.execute("ROLLBACK")
            raise DomainError("评审轮次不存在", 404)

        locks = [_lock_dict(r) for r in connection.execute(
            "SELECT l.round_id, l.proposal_ref, l.dimension, l.locked_at,"
            " r.revision_id, r.evidence_ref, r.version_label, r.source_ref,"
            " r.source_sha256, r.attributes_json, r.effective_at"
            " FROM evidence_locks l JOIN evidence_revisions r"
            " ON r.revision_id=l.revision_id"
            " WHERE l.round_id=? AND l.proposal_ref=?",
            (round_id, proposal_ref),
        ).fetchall()]
        reservations = [dict(r) for r in connection.execute(
            "SELECT * FROM capacity_reservations"
            " WHERE round_id=? AND proposal_ref=? AND status='held'",
            (round_id, proposal_ref),
        ).fetchall()]
        commitments = {}
        for res in reservations:
            c = connection.execute(
                "SELECT * FROM grid_capacity_commitments WHERE grid_node_ref=?",
                (res["grid_node_ref"],),
            ).fetchone()
            commitments[res["grid_node_ref"]] = dict(c) if c else None

        decided_at = v.now_iso()
        as_of = _parse(decided_at)
        # 直接在同一连接上复用已取数据做评估快照
        gap_eval = _evaluate_with_data(proposal, locks, reservations, commitments, as_of)
        if outcome == "APPROVED" and not gap_eval["ready_for_approval"]:
            connection.execute("ROLLBACK")
            raise DomainError("仍有建设条件缺口，不能获批；请先补齐锁定或改为驳回", 409)

        snapshot = {
            "proposal": dict(proposal),
            "round_id": round_id,
            "decided_at": decided_at,
            "evaluation": gap_eval,
            "locked_evidence": locks,
            "capacity_reservations": reservations,
            "capacity_commitments": commitments,
        }
        connection.execute(
            "INSERT INTO decisions(proposal_ref, round_id, outcome, service_mode,"
            " snapshot_json, rationale_ref, decided_at) VALUES (?,?,?,?,?,?,?)",
            (proposal_ref, round_id, outcome, proposal["service_mode"],
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
             rationale_ref, decided_at),
        )
        if outcome == "REJECTED":
            # 驳回释放容量，供其他尚未锁定的方案使用
            connection.execute(
                "UPDATE capacity_reservations SET status='released', released_at=?"
                " WHERE proposal_ref=? AND status='held'",
                (decided_at, proposal_ref),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return get_decision(proposal_ref)


def _evaluate_with_data(proposal, locks: list[dict], reservations: list[dict],
                        commitments: dict, as_of) -> dict:
    wanted_mw = float(proposal["capacity_mw"])
    mode = proposal["service_mode"]
    by_dim = {lock["dimension"]: lock for lock in locks}
    dimensions = []
    gap_count = 0
    for dimension in v.DIMENSIONS:
        required = dimension in REQUIRED_FOR_MODE[mode]
        lock = by_dim.get(dimension)
        if not required and lock is None:
            dimensions.append({"dimension": dimension, "required": False,
                               "status": "NOT_REQUIRED"})
            continue
        if lock is None:
            gap_count += 1
            dimensions.append({"dimension": dimension, "required": required,
                               "status": "GAP", "reason": "本轮未锁定证据版本"})
            continue
        reason = _check_dimension(dimension, lock["revision"]["attributes"],
                                  wanted_mw, as_of, mode)
        if required and reason is not None:
            gap_count += 1
            dimensions.append({"dimension": dimension, "required": True,
                               "status": "GAP", "reason": reason,
                               "locked_revision": _revision_ref(lock)})
        else:
            dimensions.append({"dimension": dimension, "required": required,
                               "status": "OK", "locked_revision": _revision_ref(lock)})
    capacity = None
    if reservations:
        res = reservations[0]
        c = commitments.get(res["grid_node_ref"])
        capacity = {
            "grid_node_ref": res["grid_node_ref"],
            "reserved_mw": res["reserved_mw"],
            "committed_mw_at_eval": c["committed_mw"] if c else None,
            "reservation_id": res["reservation_id"],
            "status": "HELD",
        }
    else:
        capacity = {"status": "GAP", "reason": "未持有变电节点容量预留"}
    ready = gap_count == 0 and capacity["status"] == "HELD"
    return {
        "service_mode": mode,
        "capacity_mw": wanted_mw,
        "evaluated_at": as_of.isoformat(),
        "ready_for_approval": ready,
        "gap_count": gap_count,
        "dimensions": dimensions,
        "capacity": capacity,
    }


def get_decision(proposal_ref: str) -> dict:
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM decisions WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise DomainError("该方案尚无决策", 404)
    return {
        "proposal_ref": proposal_ref,
        "round_id": row["round_id"],
        "outcome": row["outcome"],
        "service_mode": row["service_mode"],
        "rationale_ref": row["rationale_ref"],
        "decided_at": row["decided_at"],
        "snapshot": json.loads(row["snapshot_json"]),
    }


def record_change(proposal_ref: str, payload: dict) -> dict:
    dimension = payload.get("dimension")
    allowed = set(v.DIMENSIONS) | {"grid_capacity"}
    if dimension not in allowed:
        raise DomainError(f"dimension 必须是 {sorted(allowed)} 之一")
    summary = v.require_summary(payload.get("summary"))
    changed_at = v.parse_timestamp(payload.get("changed_at"), "changed_at")
    new_evidence_ref = payload.get("new_evidence_ref")
    new_sha = payload.get("new_source_sha256")
    if new_evidence_ref is not None:
        v.require_ref(new_evidence_ref, "new_evidence_ref")
    if new_sha is not None:
        new_sha = v.require_sha256(new_sha)

    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        decision = connection.execute(
            "SELECT 1 FROM decisions WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
        if decision is None:
            connection.execute("ROLLBACK")
            raise DomainError("仅已决策方案登记后续变化", 409)
        cur = connection.execute(
            "INSERT INTO post_decision_changes(proposal_ref, changed_at, dimension,"
            " summary, new_evidence_ref, new_source_sha256) VALUES (?,?,?,?,?,?)",
            (proposal_ref, changed_at.isoformat(), dimension, summary,
             new_evidence_ref, new_sha),
        )
        connection.execute("COMMIT")
        change_id = cur.lastrowid
    finally:
        connection.close()
    return {
        "change_id": change_id,
        "proposal_ref": proposal_ref,
        "changed_at": changed_at.isoformat(),
        "dimension": dimension,
        "summary": summary,
        "new_evidence_ref": new_evidence_ref,
        "new_source_sha256": new_sha,
    }


def history(proposal_ref: str) -> dict:
    """获批/驳回后回看：原判断（冻结快照）与后来变化严格分列。"""
    connection = connect()
    try:
        proposal = connection.execute(
            "SELECT * FROM proposals WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
        if proposal is None:
            raise DomainError("方案不存在", 404)
        decision_row = connection.execute(
            "SELECT * FROM decisions WHERE proposal_ref=?", (proposal_ref,)
        ).fetchone()
        changes = [dict(r) for r in connection.execute(
            "SELECT * FROM post_decision_changes WHERE proposal_ref=?"
            " ORDER BY changed_at, change_id",
            (proposal_ref,),
        ).fetchall()]
    finally:
        connection.close()

    if decision_row is None:
        return {
            "proposal_ref": proposal_ref,
            "decided": False,
            "message": "方案尚未决策，暂无历史判断",
        }

    snapshot = json.loads(decision_row["snapshot_json"])
    decided_at = decision_row["decided_at"]

    # 后来状态：同一受控证据引用是否出现新版本（不回写、不修改快照）
    current_commitments: dict[str, dict] = {}
    for res in snapshot["capacity_reservations"]:
        c = get_commitment(res["grid_node_ref"])
        if c is not None:
            current_commitments[res["grid_node_ref"]] = c

    locked_now: dict[str, dict] = {}
    for lock in snapshot["locked_evidence"]:
        dim = lock["dimension"]
        evidence_ref = lock["revision"]["evidence_ref"]
        latest = get_latest_revision(evidence_ref)
        changed_since = (
            latest is not None
            and latest["revision_id"] != lock["revision"]["revision_id"]
        )
        locked_now[dim] = {
            "at_decision": {
                "evidence_ref": lock["revision"]["evidence_ref"],
                "version_label": lock["revision"]["version_label"],
                "revision_id": lock["revision"]["revision_id"],
                "source_sha256": lock["revision"]["source_sha256"],
            },
            "latest_now": None if latest is None else {
                "evidence_ref": latest["evidence_ref"],
                "version_label": latest["version_label"],
                "revision_id": latest["revision_id"],
                "source_sha256": latest["source_sha256"],
                "recorded_at": latest["recorded_at"],
            },
            "newer_version_exists": changed_since,
        }

    capacity_now = []
    for res in snapshot["capacity_reservations"]:
        node = res["grid_node_ref"]
        c = current_commitments.get(node)
        capacity_now.append({
            "grid_node_ref": node,
            "at_decision": {
                "reserved_mw": res["reserved_mw"],
                "committed_mw": snapshot["capacity_commitments"].get(node, {}).get(
                    "committed_mw") if snapshot["capacity_commitments"].get(node) else None,
                "reservation_status": res["status"],
            },
            "committed_mw_now": c["committed_mw"] if c else None,
        })

    return {
        "proposal_ref": proposal_ref,
        "decided": True,
        "original_judgement": {
            "outcome": decision_row["outcome"],
            "round_id": decision_row["round_id"],
            "decided_at": decided_at,
            "rationale_ref": decision_row["rationale_ref"],
            "why_feasible": snapshot["evaluation"],
            "evidence_versions_locked": snapshot["locked_evidence"],
            "capacity_basis": snapshot["capacity_reservations"],
        },
        "later_changes_recorded": changes,
        "current_state_vs_snapshot": {
            "evidence": locked_now,
            "capacity": capacity_now,
        },
        "note": "original_judgement 为决策时点冻结内容；later_changes_recorded 与 "
                "current_state_vs_snapshot 为决策后状态，不构成对原判断的修改。",
    }
