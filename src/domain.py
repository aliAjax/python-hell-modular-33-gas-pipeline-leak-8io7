from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    pipeline_id = require_text(payload, "pipeline_id")
    segment_id = require_text(payload, "segment_id")
    reported_at = parse_timestamp(payload, "reported_at")
    pressure_drop = number(payload, "pressure_drop_kpa", 0)
    sensor_ppm = number(payload, "sensor_value_ppm", 0)
    odor_reports = _odor_count(payload)
    reporter = require_text(payload, "reporter")
    stable_key = "%s|%s|%s" % (pipeline_id, segment_id, reported_at)
    return {
        "pipeline_id": pipeline_id,
        "segment_id": segment_id,
        "reported_at": reported_at,
        "pressure_drop_kpa": pressure_drop,
        "sensor_value_ppm": sensor_ppm,
        "odor_reports": odor_reports,
        "reporter": reporter,
        "source_comparison": [],
        "valve_sequence": [],
        "hazards_clear": False,
        "_stable_key": stable_key,
    }


def _odor_count(payload):
    try:
        odor = int(payload.get("odor_reports", 0) or 0)
    except (TypeError, ValueError):
        raise DomainError("invalid_odor_reports", "异味报告数必须是整数")
    if odor < 0:
        raise DomainError("invalid_odor_reports", "异味报告数不能为负数")
    return odor


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    note = payload.get("note", "")
    result = {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "sensor_value_ppm": number(payload, "sensor_value_ppm", 0) if "sensor_value_ppm" in payload else None,
        "pressure_drop_kpa": number(payload, "pressure_drop_kpa", 0) if "pressure_drop_kpa" in payload else None,
        "odor_reports": _odor_count(payload),
        "note": note if isinstance(note, str) else str(note),
    }
    return result


def normalize_expected(value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise DomainError("invalid_expected", "expected 必须是终端记录编号清单")
    expected = []
    for entry in value:
        if isinstance(entry, str):
            external_id = entry.strip()
            if not external_id:
                raise DomainError("invalid_expected", "expected 中的外部编号不能为空")
            expected.append({"external_id": external_id, "source_type": None})
        elif isinstance(entry, dict):
            external_id = require_text(entry, "external_id")
            source_type = entry.get("source_type")
            if source_type is not None:
                if not isinstance(source_type, str) or not source_type.strip():
                    raise DomainError("invalid_expected", "expected 中的来源类型无效")
                source_type = source_type.strip()
            expected.append({"external_id": external_id, "source_type": source_type})
        else:
            raise DomainError("invalid_expected", "expected 条目必须是编号字符串或对象")
    return expected


def normalize_batch(payload):
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise DomainError("records_required", "补交记录至少需要一条读数")
    normalized = [normalize_source(record) for record in records]
    expected = normalize_expected(payload.get("expected"))
    return normalized, expected
