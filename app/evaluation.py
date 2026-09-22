"""候选方案评估引擎：对“已锁定的证据快照”做缺口判定。

引擎是纯函数：输入锁快照与方案需求，输出逐维度缺口。
它不读数据库、不接触任何原始材料，保证“按锁定时点判断”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DIMENSIONS = (
    "power_window",
    "datacenter_stage",
    "data_scope",
    "residency",
    "research_grant",
    "service_region",
)

DIMENSION_LABELS = {
    "power_window": "园区电力窗口",
    "datacenter_stage": "机房阶段",
    "data_scope": "工业数据可用范围",
    "residency": "驻留限制",
    "research_grant": "科研合作授权",
    "service_region": "服务区域",
}

CROSS_BORDER = "cross_border"
DOMESTIC = "domestic"

# 机房阶段距可承载算力的就绪程度
READY_STAGES = ("operational", "fitting")
STAGE_LABELS = {
    "operational": "已投运",
    "fitting": "机电装配中",
    "civil": "土建中",
    "planned": "仅规划",
}

# 仅与驻留/服务区域有关的缺口；出现这类缺口时仍可按不出域口径比较
DOMESTIC_SAFE_GAPS = frozenset(
    {"cross_border_not_permitted", "service_region_domestic_only"}
)


@dataclass
class DimensionResult:
    dimension: str
    label: str
    locked: bool
    gap: str | None
    detail: str
    evidence_id: str | None = None
    revision: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "label": self.label,
            "locked": self.locked,
            "gap": self.gap,
            "detail": self.detail,
            "evidence_id": self.evidence_id,
            "revision": self.revision,
        }


def _missing(dimension: str) -> DimensionResult:
    return DimensionResult(
        dimension=dimension,
        label=DIMENSION_LABELS[dimension],
        locked=False,
        gap="missing_lock",
        detail="本轮尚未锁定证据版本，不得把其他日期的材料当作建设条件",
    )


def _base_gap(
    dimension: str,
    lock: dict[str, Any],
    gap: str,
    detail: str,
) -> DimensionResult:
    return DimensionResult(
        dimension=dimension,
        label=DIMENSION_LABELS[dimension],
        locked=True,
        gap=gap,
        detail=detail,
        evidence_id=lock["evidence_id"],
        revision=lock["revision"],
    )


def _ok(dimension: str, lock: dict[str, Any], detail: str) -> DimensionResult:
    return DimensionResult(
        dimension=dimension,
        label=DIMENSION_LABELS[dimension],
        locked=True,
        gap=None,
        detail=detail,
        evidence_id=lock["evidence_id"],
        revision=lock["revision"],
    )


def _effective_at(lock: dict[str, Any]) -> bool:
    """锁定时点必须落在该版本的生效窗口内。"""
    locked_at = lock["locked_at"]
    if locked_at < lock["effective_from"]:
        return False
    effective_to = lock.get("effective_to")
    if effective_to and locked_at >= effective_to:
        return False
    return True


def _check_power(
    lock: dict[str, Any], proposal: dict[str, Any]
) -> DimensionResult:
    if lock["status_snapshot"] != "issued":
        return _base_gap(
            "power_window", lock, "not_issued",
            f"证据版本状态为 {lock['status_snapshot']}，电价窗口尚未正式发布",
        )
    if not _effective_at(lock):
        return _base_gap(
            "power_window", lock, "not_effective",
            "锁定时点不在该电价窗口版本的生效期内",
        )
    fact = lock.get("fact") or {}
    price = fact.get("price_cny_per_kwh")
    try:
        price_value = float(price)
    except (TypeError, ValueError):
        return _base_gap(
            "power_window", lock, "fact_malformed",
            "电价档位缺少可比较的 price_cny_per_kwh 数值",
        )
    if price_value <= 0:
        return _base_gap(
            "power_window", lock, "fact_malformed", "电价档位必须为正数"
        )
    cap = proposal.get("max_price_cny_per_kwh")
    if cap is not None:
        try:
            if price_value > float(cap):
                return _base_gap(
                    "power_window", lock, "power_price_above_limit",
                    f"窗口电价 {price} 元/度高于方案上限 {cap} 元/度",
                )
        except (TypeError, ValueError):
            return _base_gap(
                "power_window", lock, "fact_malformed",
                "方案电价上限不是合法数值",
            )
    window_from = fact.get("window_from")
    window_to = fact.get("window_to")
    if window_from and lock["locked_at"] < window_from:
        return _base_gap(
            "power_window", lock, "not_effective",
            "低电价档位在锁定时点尚未开始执行",
        )
    if window_to and lock["locked_at"] >= window_to:
        return _base_gap(
            "power_window", lock, "not_effective",
            "低电价档位在锁定时点已到期",
        )
    return _ok(
        "power_window", lock,
        f"电价窗口生效，档位 {price} 元/度",
    )


def _check_stage(
    lock: dict[str, Any], proposal: dict[str, Any]
) -> DimensionResult:
    if lock["status_snapshot"] != "issued":
        return _base_gap(
            "datacenter_stage", lock, "not_issued",
            f"机房阶段证据状态为 {lock['status_snapshot']}",
        )
    if not _effective_at(lock):
        return _base_gap(
            "datacenter_stage", lock, "not_effective",
            "锁定时点不在该机房阶段版本的生效期内",
        )
    fact = lock.get("fact") or {}
    stage = fact.get("stage")
    if stage not in STAGE_LABELS:
        return _base_gap(
            "datacenter_stage", lock, "fact_malformed",
            f"未知机房阶段：{stage!r}",
        )
    if stage not in READY_STAGES:
        return _base_gap(
            "datacenter_stage", lock, "stage_not_ready",
            f"机房处于{STAGE_LABELS[stage]}，尚不具备装机条件",
        )
    try:
        demand = float(proposal["demand_mw"])
        available = float(fact.get("available_mw"))
    except (TypeError, ValueError):
        return _base_gap(
            "datacenter_stage", lock, "fact_malformed",
            "机房可用容量或方案需求不是合法数值",
        )
    if available < demand:
        return _base_gap(
            "datacenter_stage", lock, "datacenter_capacity_short",
            f"机房当前可承载 {available} MW，低于方案需求 {demand} MW",
        )
    return _ok(
        "datacenter_stage", lock,
        f"机房{STAGE_LABELS[stage]}，可用容量 {available:g} MW",
    )


def _check_data_scope(
    lock: dict[str, Any], proposal: dict[str, Any]
) -> DimensionResult:
    if lock["status_snapshot"] != "issued":
        return _base_gap(
            "data_scope", lock, "not_issued",
            f"数据授权证据状态为 {lock['status_snapshot']}，授权未落实",
        )
    if not _effective_at(lock):
        return _base_gap(
            "data_scope", lock, "not_effective",
            "锁定时点不在该数据授权版本的生效期内",
        )
    fact = lock.get("fact") or {}
    granted = fact.get("categories")
    if not isinstance(granted, list) or not all(
        isinstance(c, str) for c in granted
    ):
        return _base_gap(
            "data_scope", lock, "fact_malformed",
            "授权范围 categories 必须是字符串列表",
        )
    required = proposal.get("required_data_categories") or []
    missing = [c for c in required if c not in granted]
    if missing:
        return _base_gap(
            "data_scope", lock, "data_category_uncovered",
            f"授权未覆盖所需数据类别：{', '.join(missing)}",
        )
    return _ok(
        "data_scope", lock,
        f"授权覆盖 {len(granted)} 类工业数据"
        + (f"（含所需 {len(required)} 类）" if required else "（方案无额外类别要求）"),
    )


def _check_residency(
    lock: dict[str, Any], mode: str
) -> DimensionResult:
    if lock["status_snapshot"] != "issued":
        return _base_gap(
            "residency", lock, "not_issued",
            f"驻留限制证据状态为 {lock['status_snapshot']}",
        )
    if not _effective_at(lock):
        return _base_gap(
            "residency", lock, "not_effective",
            "锁定时点不在该驻留政策版本的生效期内",
        )
    fact = lock.get("fact") or {}
    allowed = fact.get("cross_border_allowed")
    if mode == CROSS_BORDER:
        if allowed is not True:
            return _base_gap(
                "residency", lock, "cross_border_not_permitted",
                "锁定版本不允许数据出境，跨境方案不成立（可转不出域口径比较）",
            )
        return _ok("residency", lock, "锁定版本允许在所列目的地跨境处理")
    if allowed is True:
        return _ok("residency", lock, "驻留政策允许跨境，不出域计算自然允许")
    return _ok("residency", lock, "数据不出域，符合境内计算口径")


def _check_research(lock: dict[str, Any]) -> DimensionResult:
    if lock["status_snapshot"] != "issued":
        return _base_gap(
            "research_grant", lock, "not_issued",
            f"科研合作授权状态为 {lock['status_snapshot']}，仅有合作意向",
        )
    if not _effective_at(lock):
        return _base_gap(
            "research_grant", lock, "not_effective",
            "锁定时点不在该科研授权版本的生效期内",
        )
    fact = lock.get("fact") or {}
    if fact.get("granted") is not True:
        return _base_gap(
            "research_grant", lock, "research_grant_absent",
            "科研合作尚未正式授权，不能计为建设条件",
        )
    program = fact.get("program_ref", "")
    return _ok(
        "research_grant", lock,
        f"科研合作已授权（项目 {program}）" if program else "科研合作已授权",
    )


def _check_service_region(
    lock: dict[str, Any], mode: str
) -> DimensionResult:
    if lock["status_snapshot"] != "issued":
        return _base_gap(
            "service_region", lock, "not_issued",
            f"服务区域证据状态为 {lock['status_snapshot']}，跨境服务仅为意向",
        )
    if not _effective_at(lock):
        return _base_gap(
            "service_region", lock, "not_effective",
            "锁定时点不在该服务区域版本的生效期内",
        )
    fact = lock.get("fact") or {}
    regions = fact.get("regions")
    if not isinstance(regions, list) or not all(
        isinstance(r, str) for r in regions
    ):
        return _base_gap(
            "service_region", lock, "fact_malformed",
            "服务区域 regions 必须是字符串列表",
        )
    overseas = [r for r in regions if r != "CN"]
    if mode == CROSS_BORDER:
        if not overseas:
            return _base_gap(
                "service_region", lock, "service_region_domestic_only",
                "仅取得境内服务意向，跨境服务区域未落实（可转不出域口径比较）",
            )
        return _ok(
            "service_region", lock,
            f"跨境服务区域含 {', '.join(overseas)}",
        )
    if "CN" not in regions:
        return _base_gap(
            "service_region", lock, "service_region_uncovered",
            "服务区域证据未包含境内区域",
        )
    return _ok("service_region", lock, "境内服务区域已覆盖")


def assess(
    locks: dict[str, dict[str, Any]],
    proposal: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    """对六个维度逐一判定。

    locks: 维度 -> 锁快照（含状态、生效窗口、fact、locked_at）
    mode: cross_border 或 domestic
    """
    if mode not in (CROSS_BORDER, DOMESTIC):
        raise ValueError(f"未知评估口径：{mode}")

    results: list[DimensionResult] = []
    for dimension in DIMENSIONS:
        lock = locks.get(dimension)
        if lock is None:
            results.append(_missing(dimension))
            continue
        if dimension == "power_window":
            results.append(_check_power(lock, proposal))
        elif dimension == "datacenter_stage":
            results.append(_check_stage(lock, proposal))
        elif dimension == "data_scope":
            results.append(_check_data_scope(lock, proposal))
        elif dimension == "residency":
            results.append(_check_residency(lock, mode))
        elif dimension == "research_grant":
            results.append(_check_research(lock))
        else:
            results.append(_check_service_region(lock, mode))

    gaps = [r.as_dict() for r in results if r.gap is not None]
    return {
        "mode": mode,
        "ready": not gaps,
        "gaps": gaps,
        "dimensions": [r.as_dict() for r in results],
    }


def assess_both_modes(
    locks: dict[str, dict[str, Any]],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """同时给出跨境与不出域两种口径，供比较端点使用。"""
    cross_border = assess(locks, proposal, CROSS_BORDER)
    domestic = assess(locks, proposal, DOMESTIC)

    comparable_mode: str | None
    if not cross_border["ready"]:
        # 只有当全部缺口都属于“跨境特有”问题时，才能降级为不出域比较
        blocking = {g["gap"] for g in cross_border["gaps"]}
        comparable_mode = (
            DOMESTIC if blocking <= DOMESTIC_SAFE_GAPS and domestic["ready"] else None
        )
    else:
        comparable_mode = CROSS_BORDER

    return {
        "cross_border": cross_border,
        "domestic": domestic,
        "target_mode": proposal.get("target_mode", CROSS_BORDER),
        "cross_border_ready": cross_border["ready"],
        "domestic_ready": domestic["ready"],
        "comparable_mode": comparable_mode,
        "comparable": comparable_mode is not None,
    }
