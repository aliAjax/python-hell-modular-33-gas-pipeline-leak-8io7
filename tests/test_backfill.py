import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "pipeline_id": "P-9",
            "segment_id": "S-9",
            "reported_at": "2026-10-05T07:00:00+00:00",
            "pressure_drop_kpa": 5,
            "sensor_value_ppm": 10,
            "odor_reports": 0,
            "reporter": "dispatch-9",
        }, "dispatch-9", "dispatcher")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _backfill(self, records, expected=None, version=None):
        _, summary = self.service.backfill(
            self.item["id"], records, expected, "patrol-9", "patrol", version
        )
        return summary

    def test_same_external_id_merges_and_keeps_two_versions(self):
        # 外部编号 RD-1 在同一观测时刻有传感、人工两版
        records = [
            {
                "source_type": "sensor", "external_id": "RD-1",
                "observed_at": "2026-10-05T08:00:00+00:00",
                "sensor_value_ppm": 120, "pressure_drop_kpa": 10, "odor_reports": 0,
            },
            {
                "source_type": "manual", "external_id": "RD-1",
                "observed_at": "2026-10-05T08:00:00+00:00",
                "sensor_value_ppm": 140, "pressure_drop_kpa": 12, "odor_reports": 1,
                "note": "巡线员闻味",
            },
        ]
        summary = self._backfill(records)
        self.assertEqual(summary["inserted_count"], 2)
        self.assertEqual(summary["status"], "reported")

        item = self.service.get_item(self.item["id"])
        self.assertEqual(len(item["sources"]), 2)
        obs = item["source_coverage"]["observations"][0]
        self.assertEqual(obs["versions_present"], ["sensor", "manual"])
        # 取两版中较大值参与评分
        self.assertEqual(item["readings"]["sensor_value_ppm"], 140.0)
        self.assertEqual(item["readings"]["pressure_drop_kpa"], 12.0)
        self.assertEqual(item["readings"]["odor_reports"], 1)

    def test_idempotent_retransmit_and_update(self):
        records = [
            {
                "source_type": "manual", "external_id": "RD-7",
                "observed_at": "2026-10-05T08:30:00+00:00",
                "sensor_value_ppm": 30, "pressure_drop_kpa": 6, "odor_reports": 0,
            },
        ]
        first = self._backfill(records)
        self.assertEqual(first["inserted_count"], 1)
        version_before = self.service.get_item(self.item["id"])["version"]
        # 终端重发同一批：按外部编号合并，不产生重复，也不白涨版本
        repeat = self._backfill(records)
        self.assertEqual(repeat["inserted_count"], 0)
        self.assertEqual(repeat["unchanged_count"], 1)
        self.assertEqual(len(self.service.get_item(self.item["id"])["sources"]), 1)
        self.assertEqual(self.service.get_item(self.item["id"])["version"], version_before)
        # 回到值班室修正读数后重传：按外部编号覆盖更新
        records[0]["sensor_value_ppm"] = 300
        updated = self._backfill(records)
        self.assertEqual(updated["updated_count"], 1)
        self.assertTrue(updated["level_raised"])

    def test_level_escalation_voids_valve_sequence_and_returns_to_verify(self):
        item = self.item
        # 低等级核验、隔离
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-9", "responder", item["version"])
        low_level = item["payload"]["assessment"]["level"]
        self.assertEqual(low_level, "low")
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "sup-9", "supervisor", item["version"])
        self.assertEqual(item["status"], "isolated")
        self.assertEqual(item["payload"]["valve_sequence"], ["V-1", "V-2"])

        # 断网补交：人工读数把等级抬到 critical
        summary = self._backfill([
            {
                "source_type": "manual", "external_id": "RD-2",
                "observed_at": "2026-10-05T09:00:00+00:00",
                "sensor_value_ppm": 500, "pressure_drop_kpa": 60, "odor_reports": 5,
            },
        ])
        self.assertTrue(summary["level_raised"])
        self.assertTrue(summary["isolation_voided"])
        self.assertEqual(summary["assessment"]["level"], "critical")
        self.assertEqual(summary["voided_valve_sequence"], ["V-1", "V-2"])
        trigger = summary["triggered_by"][0]
        self.assertEqual(trigger["external_id"], "RD-2")
        self.assertIn("sensor_value_ppm", trigger["raised_metrics"])

        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["payload"]["valve_sequence"], [])
        self.assertTrue(item["payload"]["reverify_required"])
        # 审计链标明是哪条补交记录把等级抬上去的
        event_types = [event["event_type"] for event in item["audit"]]
        self.assertIn("level_reassessed", event_types)
        self.assertIn("isolation_voided", event_types)
        voided = next(event for event in item["audit"] if event["event_type"] == "isolation_voided")
        self.assertEqual(voided["payload"]["triggered_by"][0]["external_id"], "RD-2")

        # 未重新核验前，隔离被拒绝
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "sup-9", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "reverify_required")
        # 重新核验后可重新排阀隔离
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-9", "responder", item["version"])
        self.assertNotIn("reverify_required", item["payload"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-3", "V-4"]}, "sup-9", "supervisor", item["version"])
        self.assertEqual(item["status"], "isolated")
        self.assertEqual(item["payload"]["valve_sequence"], ["V-3", "V-4"])

    def test_lower_readings_do_not_void_isolation(self):
        item = self.item
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-9", "responder", item["version"])
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "sup-9", "supervisor", item["version"])
        summary = self._backfill([
            {
                "source_type": "sensor", "external_id": "RD-3",
                "observed_at": "2026-10-05T09:10:00+00:00",
                "sensor_value_ppm": 12, "pressure_drop_kpa": 6, "odor_reports": 0,
            },
        ])
        self.assertFalse(summary["level_raised"])
        self.assertFalse(summary["isolation_voided"])
        self.assertEqual(self.service.get_item(self.item["id"])["status"], "isolated")

    def test_missing_records_and_missing_version_are_visible(self):
        # 终端声明 RD-1 两版、RD-2 人工版；只补交 RD-1 传感版
        records = [
            {
                "source_type": "sensor", "external_id": "RD-1",
                "observed_at": "2026-10-05T08:00:00+00:00",
                "sensor_value_ppm": 100, "pressure_drop_kpa": 8, "odor_reports": 0,
            },
        ]
        expected = [
            {"external_id": "RD-1", "source_type": "sensor"},
            {"external_id": "RD-1", "source_type": "manual"},
            "RD-2",
        ]
        summary = self._backfill(records, expected=expected)
        pending = summary["coverage"]["expected_pending"]
        pending_ids = {(entry["external_id"], entry["source_type"]) for entry in pending}
        self.assertIn(("RD-1", "manual"), pending_ids)
        self.assertIn(("RD-2", None), pending_ids)
        # 同一时刻缺人工版本也单独列出
        missing_versions = summary["coverage"]["missing_versions"]
        self.assertEqual(missing_versions[0]["missing"], "manual")
        # 缺哪条在管段详情里持续可见
        item = self.service.get_item(self.item["id"])
        self.assertIn(("RD-2", None),
                      {(e["external_id"], e["source_type"]) for e in item["source_coverage"]["expected_pending"]})

        # 补齐后清单清空
        summary = self._backfill([
            {
                "source_type": "manual", "external_id": "RD-1",
                "observed_at": "2026-10-05T08:00:00+00:00",
                "sensor_value_ppm": 105, "pressure_drop_kpa": 9, "odor_reports": 1,
            },
            {
                "source_type": "manual", "external_id": "RD-2",
                "observed_at": "2026-10-05T08:05:00+00:00",
                "sensor_value_ppm": 110, "pressure_drop_kpa": 9, "odor_reports": 0,
            },
        ], expected=expected)
        self.assertEqual(summary["coverage"]["expected_pending"], [])


if __name__ == "__main__":
    unittest.main()
