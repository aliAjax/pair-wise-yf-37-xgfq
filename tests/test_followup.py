import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import Actor, InvalidTransition
from src.repository import SQLiteRepository
from src.rules import RuleEngine, compute_contact_followup, INCUBATION_DAYS
from src.service import DomainService


class FollowupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.today = date.today()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_case(self, onset, person="P-1"):
        return self.service.create(
            self.actor,
            "case",
            {"person_id": person, "onset_date": onset, "location": "A", "symptoms": ["fever"]},
        )

    def _make_contact(self, case, person, exposure, status="identified", due=None):
        data = {"case_id": case["id"], "person_id": person, "exposure_start": exposure}
        contact = self.service.create(self.actor, "contact", data)
        if status == "following":
            self.service.transition(
                self.actor,
                contact["id"],
                "begin_followup",
                {
                    "followup_start": exposure,
                    "due_at": due or (self.today + timedelta(days=10)).isoformat(),
                },
            )
        return self.service.get(contact["id"])

    def _advance_to_recovered(self, case):
        self.service.transition(self.actor, case["id"], "triage", {"clinician": "C-1"})
        self.service.transition(
            self.actor, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"}
        )
        self.service.transition(
            self.actor, case["id"], "recover", {"recovered_at": self.today.isoformat()}
        )

    def test_compute_followup_deadline(self):
        contact = {"status": "following", "data": {"exposure_start": "2026-03-01"}}
        patch, needs_review, reason = compute_contact_followup(
            contact, "2026-03-05", today=date(2026, 3, 18)
        )
        self.assertEqual(patch["due_at"], "2026-03-19")
        self.assertFalse(needs_review)
        self.assertEqual(patch["status"], "following")

    def test_compute_followup_overdue(self):
        contact = {"status": "following", "data": {"exposure_start": "2026-03-01"}}
        patch, needs_review, reason = compute_contact_followup(
            contact, "2026-03-05", today=date(2026, 4, 1)
        )
        self.assertEqual(patch["due_at"], "2026-03-19")
        self.assertEqual(patch["status"], "overdue")

    def test_compute_followup_extends_back_to_following(self):
        contact = {"status": "overdue", "data": {"exposure_start": "2026-03-01"}}
        patch, needs_review, reason = compute_contact_followup(
            contact, "2026-03-20", today=date(2026, 3, 20)
        )
        self.assertEqual(patch["due_at"], "2026-04-03")
        self.assertEqual(patch["status"], "following")

    def test_compute_followup_missing_exposure_review(self):
        contact = {"status": "following", "data": {}}
        patch, needs_review, reason = compute_contact_followup(
            contact, "2026-03-05", today=date(2026, 3, 20)
        )
        self.assertTrue(needs_review)
        self.assertEqual(reason, "missing exposure_start")

    def test_onset_change_recalculates_contacts(self):
        onset = (self.today + timedelta(days=2)).isoformat()
        case = self._make_case(onset)
        contact = self._make_contact(
            case, "P-2", (self.today - timedelta(days=2)).isoformat(), status="following"
        )
        new_onset = (self.today - timedelta(days=20)).isoformat()
        updated = self.service.transition(
            self.actor, case["id"], "update_onset", {"onset_date": new_onset}
        )
        self.assertEqual(updated["data"]["onset_date"], new_onset)
        recalculated = self.service.get(contact["id"])
        expected_due = (
            date.fromisoformat(new_onset) + timedelta(days=INCUBATION_DAYS)
        ).isoformat()
        self.assertEqual(recalculated["data"]["due_at"], expected_due)
        self.assertEqual(recalculated["status"], "overdue")

    def test_onset_change_extends_followup(self):
        old_onset = (self.today - timedelta(days=30)).isoformat()
        case = self._make_case(old_onset)
        contact = self._make_contact(
            case, "P-2", (self.today - timedelta(days=30)).isoformat(), status="following"
        )
        new_onset = (self.today + timedelta(days=5)).isoformat()
        self.service.transition(
            self.actor, case["id"], "update_onset", {"onset_date": new_onset}
        )
        recalculated = self.service.get(contact["id"])
        self.assertEqual(recalculated["status"], "following")

    def test_onset_change_keeps_identified_status(self):
        onset = (self.today + timedelta(days=2)).isoformat()
        case = self._make_case(onset)
        contact = self._make_contact(
            case, "P-2", (self.today - timedelta(days=2)).isoformat(), status="identified"
        )
        new_onset = (self.today - timedelta(days=20)).isoformat()
        self.service.transition(
            self.actor, case["id"], "update_onset", {"onset_date": new_onset}
        )
        recalculated = self.service.get(contact["id"])
        expected_due = (
            date.fromisoformat(new_onset) + timedelta(days=INCUBATION_DAYS)
        ).isoformat()
        self.assertEqual(recalculated["data"]["due_at"], expected_due)
        self.assertEqual(recalculated["status"], "identified")

    def test_close_blocked_with_active_contacts(self):
        case = self._make_case((self.today + timedelta(days=2)).isoformat())
        self._make_contact(
            case, "P-2", (self.today - timedelta(days=2)).isoformat(), status="following"
        )
        self._advance_to_recovered(case)
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.actor, case["id"], "close", {"outcome": "recovered"})

    def test_close_allowed_after_contacts_completed(self):
        case = self._make_case((self.today + timedelta(days=2)).isoformat())
        contact = self._make_contact(
            case, "P-2", (self.today - timedelta(days=2)).isoformat(), status="following"
        )
        self.service.transition(
            self.actor, contact["id"], "complete_followup", {"outcome": "no symptoms"}
        )
        self._advance_to_recovered(case)
        closed = self.service.transition(
            self.actor, case["id"], "close", {"outcome": "recovered"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_close_blocked_with_review_contacts(self):
        case = self._make_case((self.today + timedelta(days=2)).isoformat())
        contact = self.service.create(
            self.actor,
            "contact",
            {
                "case_id": case["id"],
                "person_id": "P-2",
                "exposure_start": (self.today - timedelta(days=2)).isoformat(),
            },
        )
        self.service.repository.update_entity(
            contact["id"],
            contact["version"],
            "identified",
            {**contact["data"], "needs_review": True},
        )
        self._advance_to_recovered(case)
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.actor, case["id"], "close", {"outcome": "recovered"})


if __name__ == "__main__":
    unittest.main()
