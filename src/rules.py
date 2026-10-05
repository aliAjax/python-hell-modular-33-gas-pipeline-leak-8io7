from .domain import DomainError

ENTITY_TYPE = "pipeline_leak"
INITIAL_STATUS = "reported"
CREATE_ROLES = {"dispatcher", "responder"}
SOURCE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
ACTION_ROLES = {
    "verify": {"dispatcher", "responder"},
    "isolate": {"supervisor", "responder"},
    "repair": {"technician"},
    "pressure_test": {"technician"},
    "restore": {"supervisor"},
    "cancel": {"supervisor"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"isolate", "repair", "pressure_test", "restore", "cancel"}

LEVEL_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}
SENSOR_TYPE = "sensor"
SOURCE_METRICS = ("pressure_drop_kpa", "sensor_value_ppm", "odor_reports")


def merged_readings(payload, sources):
    """以建单读数为基线，叠加所有来源记录，逐项取观测到的最大值。"""
    readings = {
        "pressure_drop_kpa": float(payload.get("pressure_drop_kpa", 0) or 0),
        "sensor_value_ppm": float(payload.get("sensor_value_ppm", 0) or 0),
        "odor_reports": int(payload.get("odor_reports", 0) or 0),
    }
    for source in sources:
        record = source.get("payload", source)
        for metric in SOURCE_METRICS:
            value = record.get(metric)
            if value is None:
                continue
            if metric == "odor_reports":
                readings[metric] = max(readings[metric], int(value))
            else:
                readings[metric] = max(readings[metric], float(value))
    return readings


def assess(payload):
    pressure = float(payload.get("pressure_drop_kpa", 0))
    ppm = float(payload.get("sensor_value_ppm", 0))
    odor = int(payload.get("odor_reports", 0))
    score = min(100.0, pressure * 2.0 + min(ppm, 500.0) * 0.1 + odor * 5.0)
    if score >= 75:
        level = "critical"
    elif score >= 45:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level}


def _source_kind(source_type):
    return SENSOR_TYPE if source_type == SENSOR_TYPE else "manual"


def _record_raises_readings(record, readings):
    raised_metrics = []
    for metric in SOURCE_METRICS:
        value = record.get(metric)
        if value is None:
            continue
        if int(value) if metric == "odor_reports" else float(value) > readings[metric]:
            raised_metrics.append(metric)
    return raised_metrics


def plan_backfill(status, payload, before_sources, after_sources, merge_results, expected):
    """根据补交后的全部来源记录重算读数与等级，规划状态/审计变更。

    merge_results: [{"outcome": inserted|updated|unchanged, "source_type",
                     "external_id", "observed_at", "record": {...}}]
    """
    old_readings = merged_readings(payload, before_sources)
    old_assessment = assess(old_readings)
    new_readings = merged_readings(payload, after_sources)
    new_assessment = assess(new_readings)
    old_rank = LEVEL_ORDER.get(old_assessment["level"], 0)
    new_rank = LEVEL_ORDER.get(new_assessment["level"], 0)
    level_raised = new_rank > old_rank

    current = dict(payload)
    events = []
    triggered_by = []
    if level_raised:
        for result in merge_results:
            if result["outcome"] not in ("inserted", "updated"):
                continue
            raised_metrics = _record_raises_readings(result["record"], old_readings)
            if raised_metrics:
                triggered_by.append({
                    "external_id": result["external_id"],
                    "source_type": result["source_type"],
                    "observed_at": result["observed_at"],
                    "raised_metrics": raised_metrics,
                })
        events.append({
            "event_type": "level_reassessed",
            "payload": {
                "old_assessment": old_assessment,
                "new_assessment": new_assessment,
                "old_readings": old_readings,
                "new_readings": new_readings,
                "triggered_by": triggered_by,
            },
        })

    escalated = False
    voided_valve_sequence = []
    baseline_assessment = current.get("isolated_assessment") or current.get("assessment") or old_assessment
    if status == "isolated" and new_rank > LEVEL_ORDER.get(baseline_assessment.get("level"), 0):
        escalated = True
        voided_valve_sequence = list(current.get("valve_sequence", []))
        status = "verified"
        current["valve_sequence"] = []
        current["reverify_required"] = True
        current.setdefault("escalations", []).append({
            "from_level": baseline_assessment.get("level"),
            "to_level": new_assessment["level"],
            "voided_valve_sequence": voided_valve_sequence,
            "triggered_by": triggered_by,
        })
        events.append({
            "event_type": "isolation_voided",
            "payload": {
                "isolated_assessment": current.get("isolated_assessment"),
                "new_assessment": new_assessment,
                "voided_valve_sequence": voided_valve_sequence,
                "triggered_by": triggered_by,
            },
        })

    current["assessment"] = new_assessment
    if expected is not None:
        current["expected_sources"] = expected

    coverage = source_coverage(after_sources, expected if expected is not None else payload.get("expected_sources"))
    return {
        "status": status,
        "payload": current,
        "events": events,
        "old_assessment": old_assessment,
        "new_assessment": new_assessment,
        "old_readings": old_readings,
        "new_readings": new_readings,
        "level_raised": level_raised,
        "triggered_by": triggered_by,
        "escalated": escalated,
        "voided_valve_sequence": voided_valve_sequence,
        "coverage": coverage,
    }


def source_coverage(sources, expected=None):
    """统计同一观测时刻的传感/人工双版本覆盖情况，以及终端清单中尚缺的记录。"""
    observed = {}
    present = set()
    for source in sources:
        record = source.get("payload", source)
        observed_at = source.get("observed_at") or record.get("observed_at")
        kind = _source_kind(source.get("source_type", record.get("source_type", "")))
        bucket = observed.setdefault(observed_at, {"sensor": [], "manual": []})
        entry = {"external_id": source.get("external_id"), "source_type": source.get("source_type")}
        if entry not in bucket[kind]:
            bucket[kind].append(entry)
        present.add((source.get("external_id"), source.get("source_type")))

    observations = []
    missing_versions = []
    for observed_at in sorted(observed):
        bucket = observed[observed_at]
        kinds_present = [kind for kind in ("sensor", "manual") if bucket[kind]]
        observations.append({
            "observed_at": observed_at,
            "versions_present": kinds_present,
            "sensor": bucket["sensor"],
            "manual": bucket["manual"],
        })
        for kind in ("sensor", "manual"):
            if not bucket[kind]:
                missing_versions.append({"observed_at": observed_at, "missing": kind})

    expected_pending = []
    if expected:
        for entry in expected:
            external_id = entry["external_id"]
            source_type = entry.get("source_type")
            if source_type:
                found = (external_id, source_type) in present
            else:
                found = any(key[0] == external_id for key in present)
            if not found:
                expected_pending.append({"external_id": external_id, "source_type": source_type})

    return {
        "observations": observations,
        "missing_versions": missing_versions,
        "expected_pending": expected_pending,
    }


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role, readings=None):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"reported", "verified"})
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能完成核验", 409)
        confirmed = bool(payload.get("field_confirmed"))
        if not confirmed:
            raise DomainError("field_confirmation_required", "需要现场确认", 409)
        basis = readings if readings is not None else current
        current["assessment"] = assess(basis)
        current["verification"] = {"confirmed": True, "note": payload.get("note", "")}
        current.pop("reverify_required", None)
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "isolate":
        _need_status(item, {"verified"})
        if current.get("reverify_required"):
            raise DomainError("reverify_required", "补交读数抬高了泄漏等级，阀门顺序已作废，请先重新核验", 409)
        sequence = payload.get("valve_sequence")
        if not isinstance(sequence, list) or len(sequence) < 2:
            raise DomainError("valve_sequence_required", "至少需要提交两个阀门及顺序")
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能隔离", 409)
        if not all(isinstance(value, str) and value.strip() for value in sequence):
            raise DomainError("invalid_valve_sequence", "阀门顺序格式无效")
        current["valve_sequence"] = [value.strip() for value in sequence]
        basis = readings if readings is not None else current
        current["isolated_assessment"] = current.get("assessment") or assess(basis)
        return "isolated", current, {"valve_sequence": current["valve_sequence"], "assessment": current["isolated_assessment"]}

    if action == "repair":
        _need_status(item, {"isolated", "repaired"})
        work_order = _text(payload, "work_order")
        current["repair"] = {"work_order": work_order, "result": payload.get("result", "completed")}
        return "repaired", current, {"work_order": work_order}

    if action == "pressure_test":
        _need_status(item, {"repaired", "tested"})
        if not payload.get("test_passed"):
            raise DomainError("pressure_test_failed", "压力测试未通过，不能恢复供气", 409)
        pressure = float(payload.get("pressure_kpa", 0))
        minimum = float(payload.get("minimum_pressure_kpa", 100))
        if pressure < minimum:
            raise DomainError("pressure_below_threshold", "试验压力低于最低要求", 409)
        current["pressure_test"] = {"passed": True, "pressure_kpa": pressure, "minimum_pressure_kpa": minimum}
        return "tested", current, {"pressure_test": current["pressure_test"]}

    if action == "restore":
        _need_status(item, {"tested"})
        if not payload.get("hazards_clear"):
            raise DomainError("hazards_not_clear", "现场危险条件尚未解除", 409)
        if not current.get("pressure_test", {}).get("passed"):
            raise DomainError("pressure_test_missing", "缺少通过的压力测试", 409)
        current["hazards_clear"] = True
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"reported", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
