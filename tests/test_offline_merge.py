import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _base_payload(self, **overrides):
        payload = {
            "pipeline_id": "P-1",
            "segment_id": "S-8",
            "reported_at": "2026-09-27T08:00:00+00:00",
            "pressure_drop_kpa": 5,
            "sensor_value_ppm": 50,
            "odor_reports": 0,
            "reporter": "dispatch-1",
        }
        payload.update(overrides)
        return payload

    def test_merge_by_external_id(self):
        # 第一次补交：建立管段
        item1 = self.service.create_item(
            self._base_payload(external_id="SEG-EXT-1", reported_at="2026-09-27T08:00:00+00:00"),
            "dispatch-1", "dispatcher",
        )
        self.assertEqual(item1["status"], "reported")
        # 第二次补交：同一外部编号，应合并进同一管段，而不是新建
        item2 = self.service.create_item(
            self._base_payload(
                external_id="SEG-EXT-1",
                reported_at="2026-09-27T09:00:00+00:00",
                sensor_value_ppm=120,
                source_type="manual",
            ),
            "dispatch-1", "dispatcher",
        )
        self.assertEqual(item2["id"], item1["id"])
        self.assertEqual(len(item2["sources"]), 1)
        # 不同外部编号 → 不同管段
        item3 = self.service.create_item(
            self._base_payload(external_id="SEG-EXT-2", reported_at="2026-09-27T10:00:00+00:00"),
            "dispatch-1", "dispatcher",
        )
        self.assertNotEqual(item3["id"], item1["id"])

    def test_same_observed_at_keeps_sensor_and_manual(self):
        item = self.service.create_item(
            self._base_payload(external_id="SEG-EXT-3"), "dispatch-1", "dispatcher"
        )
        observed = "2026-09-27T08:30:00+00:00"
        self.service.add_source(
            item["id"],
            {
                "source_type": "sensor",
                "external_id": "SENSOR-1",
                "observed_at": observed,
                "sensor_value_ppm": 200,
            },
            "patrol-1", "patrol",
        )
        self.service.add_source(
            item["id"],
            {
                "source_type": "manual",
                "external_id": "MANUAL-1",
                "observed_at": observed,
                "sensor_value_ppm": 180,
                "odor_reports": 2,
            },
            "patrol-1", "patrol",
        )
        item = self.service.get_item(item["id"])
        self.assertEqual(len(item["sources"]), 2)
        types = {s["source_type"] for s in item["sources"]}
        self.assertEqual(types, {"sensor", "manual"})

    def test_reading_change_recalculates_level(self):
        item = self.service.create_item(
            self._base_payload(external_id="SEG-EXT-4", sensor_value_ppm=80),
            "dispatch-1", "dispatcher",
        )
        self.assertEqual(item["assessment"]["level"], "low")
        # 补交一份高读数来源
        self.service.add_source(
            item["id"],
            {
                "source_type": "sensor",
                "external_id": "SENSOR-2",
                "observed_at": "2026-09-27T09:00:00+00:00",
                "sensor_value_ppm": 500,
                "pressure_drop_kpa": 60,
                "odor_reports": 4,
            },
            "patrol-1", "patrol",
        )
        item = self.service.get_item(item["id"])
        self.assertEqual(item["assessment"]["level"], "critical")

    def test_escalation_invalidates_valve_sequence_and_reverts(self):
        item = self.service.create_item(
            self._base_payload(external_id="SEG-EXT-5"),
            "dispatch-1", "dispatcher",
        )
        item = self.service.act(item["id"], "verify", {"field_confirmed": True}, "resp-1", "responder", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "low")
        item = self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "sup-1", "supervisor", item["version"])
        self.assertEqual(item["status"], "isolated")
        self.assertEqual(item["payload"]["isolation_level"], "low")
        # 补交一份把等级抬到 critical 的读数
        self.service.add_source(
            item["id"],
            {
                "source_type": "sensor",
                "external_id": "SENSOR-3",
                "observed_at": "2026-09-27T10:00:00+00:00",
                "sensor_value_ppm": 500,
                "pressure_drop_kpa": 60,
                "odor_reports": 4,
            },
            "patrol-1", "patrol",
        )
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["payload"]["valve_sequence"], [])
        self.assertEqual(item["assessment"]["level"], "critical")
        # 值班员能看到是哪条补交记录把等级抬上去的
        escalation = item["payload"]["last_escalation"]
        self.assertEqual(escalation["external_id"], "SENSOR-3")
        self.assertEqual(escalation["from_level"], "low")
        self.assertEqual(escalation["to_level"], "critical")
        # 审计链中也有升级事件
        event_types = [e["event_type"] for e in item["audit"]]
        self.assertIn("level_escalated", event_types)

    def test_missing_sources_visible(self):
        item = self.service.create_item(
            self._base_payload(external_id="SEG-EXT-6"), "dispatch-1", "dispatcher"
        )
        # 初始没有任何来源，传感和人工都缺
        self.assertEqual(set(item["missing_sources"]), {"sensor", "manual"})
        self.service.add_source(
            item["id"],
            {
                "source_type": "sensor",
                "external_id": "SENSOR-4",
                "observed_at": "2026-09-27T09:00:00+00:00",
                "sensor_value_ppm": 100,
            },
            "patrol-1", "patrol",
        )
        item = self.service.get_item(item["id"])
        # 传感已到，人工还缺
        self.assertEqual(item["missing_sources"], ["manual"])
        self.service.add_source(
            item["id"],
            {
                "source_type": "manual",
                "external_id": "MANUAL-2",
                "observed_at": "2026-09-27T09:05:00+00:00",
                "sensor_value_ppm": 90,
            },
            "patrol-1", "patrol",
        )
        item = self.service.get_item(item["id"])
        self.assertEqual(item["missing_sources"], [])

    def test_duplicate_source_still_rejected(self):
        item = self.service.create_item(
            self._base_payload(external_id="SEG-EXT-7"), "dispatch-1", "dispatcher"
        )
        source = {
            "source_type": "sensor",
            "external_id": "SENSOR-5",
            "observed_at": "2026-09-27T09:00:00+00:00",
            "sensor_value_ppm": 100,
        }
        self.service.add_source(item["id"], source, "patrol-1", "patrol")
        with self.assertRaises(ConflictError):
            self.service.add_source(item["id"], source, "patrol-1", "patrol")


if __name__ == "__main__":
    unittest.main()
