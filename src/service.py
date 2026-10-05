from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        normalized = domain.normalize_source(payload)
        result = self.backfill(item_id, [normalized], None, actor, role)[0]
        record = next(
            (
                source
                for source in result["sources"]
                if source["source_type"] == normalized["source_type"]
                and source["external_id"] == normalized["external_id"]
            ),
            None,
        )
        return record

    def backfill(self, item_id, records, expected, actor, role, expected_version=None):
        """断网期间终端记录的批量补交：按外部编号合并入同一管段并重算等级。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        # 提前确认管段存在，给出 404 而不是事务内失败
        self.repository.get_item(item_id)
        records = [domain.normalize_source(record) for record in records]
        expected = domain.normalize_expected(expected)
        item, merge_info = self.repository.merge_sources(
            item_id, records, expected, rules.plan_backfill, actor, role, expected_version
        )
        result = self.get_item(item["id"])
        plan = merge_info["plan"]
        summary = {
            "item_id": item["id"],
            "version": result["version"],
            "status": result["status"],
            "merged": [entry for entry in merge_info["merge_results"] if entry["outcome"] in ("inserted", "updated")],
            "unchanged": [entry["external_id"] for entry in merge_info["merge_results"] if entry["outcome"] == "unchanged"],
            "inserted_count": sum(1 for entry in merge_info["merge_results"] if entry["outcome"] == "inserted"),
            "updated_count": sum(1 for entry in merge_info["merge_results"] if entry["outcome"] == "updated"),
            "unchanged_count": sum(1 for entry in merge_info["merge_results"] if entry["outcome"] == "unchanged"),
            "old_assessment": plan["old_assessment"],
            "assessment": plan["new_assessment"],
            "readings": plan["new_readings"],
            "level_raised": plan["level_raised"],
            "triggered_by": plan["triggered_by"],
            "isolation_voided": plan["escalated"],
            "voided_valve_sequence": plan["voided_valve_sequence"],
            "coverage": plan["coverage"],
        }
        return result, summary

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        readings = rules.merged_readings(item["payload"], self.repository.list_sources(item_id))
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role, readings=readings)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        readings = rules.merged_readings(item["payload"], item["sources"])
        item["readings"] = readings
        item["assessment"] = rules.assess(readings)
        expected = item["payload"].get("expected_sources")
        item["source_coverage"] = rules.source_coverage(item["sources"], expected)
        item["audit"] = self.repository.audit_trail(item_id)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
