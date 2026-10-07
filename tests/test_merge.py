import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.case = self.service.create(
            self.actor,
            "case",
            {"person_id": "P-1", "onset_date": "2026-03-01", "location": "A", "symptoms": ["fever"]},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _batch(self, items):
        return [
            {
                "client_id": cid,
                "case_id": self.case["id"],
                "person_id": pid,
                "exposure_start": exp,
            }
            for cid, pid, exp in items
        ]

    def test_merge_creates_contacts(self):
        contacts = self._batch([("c1", "P-2", "2026-02-25"), ("c2", "P-3", "2026-02-26")])
        result = self.service.merge_contacts(self.actor, "B-1", contacts)
        self.assertEqual(result["merged"], 2)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(result["errors"], 0)
        for item in result["items"]:
            self.assertEqual(item["status"], "merged")
        self.assertEqual(len(self.service.list("contact")), 2)

    def test_merge_retry_is_idempotent(self):
        contacts = self._batch([("c1", "P-2", "2026-02-25"), ("c2", "P-3", "2026-02-26")])
        first = self.service.merge_contacts(self.actor, "B-1", contacts)
        second = self.service.merge_contacts(self.actor, "B-1", contacts)
        self.assertEqual(second["merged"], 2)
        for a, b in zip(first["items"], second["items"]):
            self.assertEqual(a["entity_id"], b["entity_id"])
        self.assertEqual(len(self.service.list("contact")), 2)

    def test_merge_dedups_same_person(self):
        self.service.merge_contacts(self.actor, "B-1", self._batch([("c1", "P-2", "2026-02-25")]))
        result = self.service.merge_contacts(
            self.actor, "B-2", self._batch([("c9", "P-2", "2026-02-24")])
        )
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["items"][0]["status"], "duplicate")
        contacts = [c for c in self.service.list("contact") if c["data"].get("person_id") == "P-2"]
        self.assertEqual(len(contacts), 1)

    def test_merge_partial_failure_retry(self):
        bad = [
            {"client_id": "c1", "case_id": self.case["id"], "person_id": "P-2", "exposure_start": "2026-02-25"},
            {"client_id": "c2", "case_id": "missing-case", "person_id": "P-3", "exposure_start": "2026-02-26"},
        ]
        first = self.service.merge_contacts(self.actor, "B-1", bad)
        self.assertEqual(first["merged"], 1)
        self.assertEqual(first["errors"], 1)
        fixed = [
            {"client_id": "c1", "case_id": self.case["id"], "person_id": "P-2", "exposure_start": "2026-02-25"},
            {"client_id": "c2", "case_id": self.case["id"], "person_id": "P-3", "exposure_start": "2026-02-26"},
        ]
        second = self.service.merge_contacts(self.actor, "B-1", fixed)
        self.assertEqual(second["merged"], 2)
        self.assertEqual(second["errors"], 0)
        statuses = {i["client_id"]: i["status"] for i in second["items"]}
        self.assertEqual(statuses["c1"], "already_merged")
        self.assertEqual(statuses["c2"], "merged")
        self.assertEqual(len(self.service.list("contact")), 2)

    def test_merge_missing_exposure_flagged_for_review(self):
        item = {"client_id": "c1", "case_id": self.case["id"], "person_id": "P-2"}
        result = self.service.merge_contacts(self.actor, "B-1", [item])
        self.assertEqual(result["merged"], 1)
        contact = self.service.get(result["items"][0]["entity_id"])
        self.assertTrue(contact["data"]["needs_review"])
        self.assertEqual(contact["data"]["review_reason"], "missing exposure_start")

    def test_merge_requires_batch_id(self):
        with self.assertRaises(ValidationError):
            self.service.merge_contacts(self.actor, None, [])

    def test_merge_requires_investigator_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.merge_contacts(Actor("viewer", "viewer"), "B-1", [])

    def test_get_merge_batch(self):
        self.service.merge_contacts(self.actor, "B-1", self._batch([("c1", "P-2", "2026-02-25")]))
        batch = self.service.get_merge_batch("B-1")
        self.assertEqual(batch["batch_id"], "B-1")
        self.assertEqual(batch["merged_count"], 1)
        self.assertEqual(batch["status"], "completed")


if __name__ == "__main__":
    unittest.main()
