
import re
from datetime import datetime, timezone


REF_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,63}$")
SOURCE_REF_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:/~-]{1,128}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

MAX_STRING = 128
MAX_SUMMARY = 200


class ValidationError(ValueError):
    """请求内容不符合字段约定。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是带偏移量的 ISO 8601 字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"{field} 不是合法的 ISO 8601 时间") from None
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须带时区偏移量")
    return parsed


def require_ref(value: object, field: str) -> str:
    if not isinstance(value, str) or not REF_PATTERN.match(value):
        raise ValidationError(f"{field} 必须是受控引用编号（字母开头，仅限字母数字 . _ -）")
    return value


def require_source_ref(value: object) -> str:
    if not isinstance(value, str) or not SOURCE_REF_PATTERN.match(value):
        raise ValidationError("source_ref 必须是受控材料库引用")
    return value


def require_sha256(value: object) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.match(value.lower()):
        raise ValidationError("source_sha256 必须是 64 位小写十六进制摘要")
    return value.lower()


def _require_label(value: object) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= MAX_STRING):
        raise ValidationError("标签必须是短字符串")
    if any(ch in value for ch in "\x00\n\r"):
        raise ValidationError("标签包含非法字符")
    return value


def _require_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("数值属性必须是数字")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValidationError("数值属性必须有限")
    return float(value)


def _require_string(value: object) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= MAX_STRING):
        raise ValidationError("字符串属性长度需在 1..128 之间")
    return value


def _require_timestamp_attr(value: object) -> str:
    return parse_timestamp(value, "时间属性").isoformat()


def _require_enum(options: set[str]):
    def check(value: object) -> str:
        if not isinstance(value, str) or value not in options:
            raise ValidationError(f"枚举属性取值必须是 {sorted(options)} 之一")
        return value

    return check


def _require_string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError("列表属性必须是非空字符串数组")
    if len(value) > 32:
        raise ValidationError("列表属性最多 32 项")
    return [_require_label(item) for item in value]


# 仅布尔/数值/枚举/时间/受控标签可以入库；拒绝嵌套对象与任意自由文本，
# 从结构上保证企业原始数据无法经由证据属性进入系统。
def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("布尔属性必须是 true/false")
    return value


def _checker(spec: dict[str, object]):
    kind = spec["kind"]
    if kind == "str":
        return _require_string
    if kind == "num":
        return _require_number
    if kind == "bool":
        return _require_bool
    if kind == "ts":
        return _require_timestamp_attr
    if kind == "enum":
        return _require_enum(set(spec["options"]))
    if kind == "labels":
        return _require_string_list
    raise ValidationError(f"未知属性规则：{kind}")


# 六个证据维度的白名单：键名即允许出现的全部属性
DIMENSION_SCHEMA: dict[str, dict[str, dict[str, object]]] = {
    "power_window": {
        "grid_node_ref": {"kind": "str"},
        "window_start": {"kind": "ts"},
        "window_end": {"kind": "ts"},
        "tariff_cny_per_mwh": {"kind": "num"},
    },
    "datacenter_stage": {
        "stage": {"kind": "enum", "options": [
            "PLANNING", "STRUCTURE", "EQUIPMENT_READY", "OPERATIONAL",
        ]},
        "delivered_mw": {"kind": "num"},
    },
    "data_scope": {
        "authorization_status": {"kind": "enum", "options": [
            "GRANTED", "PENDING", "NONE",
        ]},
        "scope_labels": {"kind": "labels"},
        "expires_at": {"kind": "ts"},
    },
    "residency": {
        "mode": {"kind": "enum", "options": [
            "IN_DOMAIN_ONLY", "CROSS_BORDER_ALLOWED", "NONE",
        ]},
    },
    "research_authorization": {
        "status": {"kind": "enum", "options": ["GRANTED", "PENDING", "NONE"]},
        "partner_refs": {"kind": "labels"},
        "expires_at": {"kind": "ts"},
    },
    "service_area": {
        "regions": {"kind": "labels"},
        "cross_border_intent": {"kind": "enum", "options": [
            "SIGNED", "INTENDED", "NONE",
        ]},
    },
}

DIMENSIONS = tuple(DIMENSION_SCHEMA.keys())


def validate_attributes(dimension: str, attributes: object) -> dict[str, object]:
    schema = DIMENSION_SCHEMA.get(dimension)
    if schema is None:
        raise ValidationError(f"未知证据维度：{dimension}")
    if not isinstance(attributes, dict):
        raise ValidationError("attributes 必须是白名单内的扁平属性对象")
    unknown = set(attributes) - set(schema)
    if unknown:
        raise ValidationError(f"维度 {dimension} 不接受属性：{sorted(unknown)}")
    missing = set(schema) - set(attributes)
    # expires_at 为可选
    missing.discard("expires_at")
    if missing:
        raise ValidationError(f"维度 {dimension} 缺少属性：{sorted(missing)}")
    normalized: dict[str, object] = {}
    for key, spec in schema.items():
        if key not in attributes:
            continue
        normalized[key] = _checker(spec)(attributes[key])
    return normalized


def require_summary(value: object) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= MAX_SUMMARY):
        raise ValidationError("summary 需为 1..200 字的规范化说明")
    if any(ch in value for ch in "\x00"):
        raise ValidationError("summary 包含非法字符")
    return value
