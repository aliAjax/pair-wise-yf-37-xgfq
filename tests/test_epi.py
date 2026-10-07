import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.domain import (
    Actor,
    BatchMergeError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.http_api import create_handler
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def case_payload(**overrides):
    data = {
        "person_id": "CASE-P-1",
        "onset_date": "2026-03-10",
        "location": "Township-A",
        "symptoms": ["fever"],
        "chain_id": "CHAIN-7",
    }
    data.update(overrides)
    return data


def contact_item(item_id, **overrides):
    suffix = "".join(ch for ch in str(item_id) if ch.isdigit()) or "0"
    data = {
        "item_id": item_id,
        "person_id": "P-%s" % item_id,
        "id_card": "ID-%s" % item_id,
        "phone": "1390000%04d" % int(suffix),
        "name": "接触者%s" % item_id,
        "exposure_start": "2026-03-08",
        "exposure_end": "2026-03-09",
    }
    data.update(overrides)
    return data


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class EpiTestBase(unittest.TestCase):
    def setUp(self, as_of=date(2026, 3, 15)):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.clock = MutableClock(as_of)
        self.service = DomainService(self.repo, RuleEngine(), clock=self.clock)
        self.admin = Actor("u-admin", "admin")
        self.investigator = Actor("u-fd-1", "investigator")
        self.case = self.service.create(self.admin, "case", case_payload())

    def tearDown(self):
        self.tmp.cleanup()

    def _batch(self, batch_id, items, team_id, actor=None, **overrides):
        payload = {
            "batch_id": batch_id,
            "case_id": self.case["id"],
            "chain_id": "CHAIN-7",
            "team_id": team_id,
            "township": "Township-A",
            "items": items,
        }
        payload.update(overrides)
        return self.service.merge_batch(actor or self.investigator, payload)

    def _contact_by_id_card(self, id_card):
        with self.repo._connect() as connection:
            row = connection.execute(
                "SELECT person_id FROM persons WHERE id_card = ?", (id_card,)
            ).fetchone()
        return row["person_id"] if row else None


class CrossTeamMergeTest(EpiTestBase):
    def test_same_person_registered_by_two_teams_merges_into_one_contact(self):
        # Team 1 registers the contact while offline.
        report1 = self._batch(
            "B-team-1", [contact_item("1", phone="13800000001")], "TEAM-1"
        )
        # Team 2 registers the SAME physical person (same id_card) for the
        # same case with a different exposure window.
        report2 = self._batch(
            "B-team-2",
            [contact_item(
                "1",
                person_id="P-1-ALIAS",
                phone="13800000002",
                exposure_start="2026-03-05",
                exposure_end="2026-03-12",
            )],
            "TEAM-2",
        )
        self.assertEqual(len(report1["applied"]), 1)
        self.assertEqual(len(report2["applied"]), 1)
        self.assertFalse(report2["applied"][0]["created"])  # reused, not new
        self.assertEqual(
            report1["applied"][0]["contact_entity_id"],
            report2["applied"][0]["contact_entity_id"],
        )
        contacts = self.service.list_contacts(self.case["id"])
        self.assertEqual(len(contacts), 1)
        # The merged window must cover both teams' observations.
        data = contacts[0]["data"]
        self.assertEqual(data["exposure_start"], "2026-03-05")
        self.assertEqual(data["exposure_end"], "2026-03-12")
        # Follow-up window is recomputed off the widened window immediately.
        self.assertEqual(data["followup_start"], "2026-03-12")
        self.assertEqual(data["followup_end"], "2026-03-25")
        # Person master index has exactly one row for the id_card.
        self.assertEqual(self._contact_by_id_card("ID-1"), data["person_id"])

    def test_phone_also_deduplicates_when_id_card_absent(self):
        self._batch(
            "B-a",
            [contact_item("9", person_id="P9-A", id_card=None)],
            "TEAM-1",
        )
        self._batch(
            "B-b",
            [contact_item("9", person_id="P9-B", id_card=None)],
            "TEAM-2",
        )
        self.assertEqual(len(self.service.list_contacts(self.case["id"])), 1)

    def test_same_person_on_two_cases_is_one_person_two_contacts(self):
        other_case = self.service.create(
            self.admin, "case",
            case_payload(person_id="CASE-P-2", location="Township-B"),
        )
        self._batch("B-x", [contact_item("1")], "TEAM-1")
        payload = {
            "batch_id": "B-y",
            "case_id": other_case["id"],
            "chain_id": "CHAIN-7",
            "team_id": "TEAM-2",
            "items": [contact_item("1")],
        }
        self.service.merge_batch(self.investigator, payload)
        self.assertEqual(len(self.service.list_contacts(self.case["id"])), 1)
        self.assertEqual(len(self.service.list_contacts(other_case["id"])), 1)
        with self.repo._connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_batch_rejects_wrong_transmission_chain(self):
        with self.assertRaises(ValidationError):  # missing batch_id
            self.service.merge_batch(self.investigator, {"items": []})
        payload = {
            "batch_id": "B-wrong-chain",
            "case_id": self.case["id"],
            "chain_id": "CHAIN-OTHER",
            "team_id": "TEAM-9",
            "items": [contact_item("1")],
        }
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self.service.merge_batch(self.investigator, payload)

    def test_concurrent_teams_submitting_same_person_create_one_row(self):
        errors = []

        def worker(batch_id, team_id):
            try:
                self._batch(batch_id, [contact_item("7")], team_id)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("B-c1", "TEAM-1")),
            threading.Thread(target=worker, args=("B-c2", "TEAM-2")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.service.list_contacts(self.case["id"])), 1)
        with self.repo._connect() as connection:
            count = connection.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
        self.assertEqual(count, 1)


class BatchRetryTest(EpiTestBase):
    def test_failure_mid_batch_resumes_without_double_registration(self):
        items = [contact_item("1"), contact_item("2"), contact_item("3")]

        # First attempt crashes while applying item 2.
        self.service.fail_next_batch_item("2")
        with self.assertRaises(BatchMergeError) as caught:
            self._batch("B-retry", items, "TEAM-1")
        partial = caught.exception.report
        self.assertEqual([row["item_id"] for row in partial["applied"]], ["1"])
        self.assertEqual([row["item_id"] for row in partial["failed"]], ["2"])
        self.assertEqual(len(self.service.list_contacts(self.case["id"])), 1)

        status = self.service.get_batch_status("B-retry")
        self.assertEqual(status["batch"]["status"], "failed")
        ledger = {row["item_id"]: row["status"] for row in status["items"]}
        self.assertEqual(ledger, {"1": "applied", "2": "pending"})

        # Retry the SAME batch (same batch_id, identical payload).
        report = self._batch("B-retry", items, "TEAM-1")
        self.assertEqual([row["item_id"] for row in report["applied"]], ["1", "2", "3"])
        self.assertTrue(report["applied"][0]["skipped"])
        self.assertEqual(report["status"], "applied")
        self.assertEqual(len(self.service.list_contacts(self.case["id"])), 3)

        # Submitting the batch yet again registers nothing new.
        again = self._batch("B-retry", items, "TEAM-1")
        self.assertTrue(all(row.get("skipped") for row in again["applied"]))
        self.assertEqual(len(self.service.list_contacts(self.case["id"])), 3)
        with self.repo._connect() as connection:
            exposures = connection.execute(
                "SELECT COUNT(*) AS n FROM contact_exposures WHERE batch_id = 'B-retry'"
            ).fetchone()["n"]
        self.assertEqual(exposures, 3)

    def test_permanently_invalid_items_are_rejected_and_sticky(self):
        items = [
            contact_item("1"),
            contact_item("bad", person_id=None, id_card=None, phone=None),
            contact_item("3"),
        ]
        report = self._batch("B-validation", items, "TEAM-1")
        self.assertEqual([row["item_id"] for row in report["applied"]], ["1", "3"])
        self.assertEqual(report["rejected"][0]["item_id"], "bad")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(len(self.service.list_contacts(self.case["id"])), 2)

        # Retry keeps the rejected item rejected; nothing changes.
        again = self._batch("B-validation", items, "TEAM-1")
        self.assertEqual(len(again["rejected"]), 1)
        self.assertTrue(again["rejected"][0].get("skipped"))
        self.assertEqual(len(again["applied"]), 2)

    def test_investigator_role_required_for_merge(self):
        with self.assertRaises(PermissionDenied):
            self._batch(
                "B-role", [contact_item("1")], "TEAM-1",
                actor=Actor("viewer-1", "viewer"),
            )

    def test_closed_case_rejects_new_merges(self):
        self._discharge_and_close_case()
        from src.domain import ConflictError
        with self.assertRaises(ConflictError):
            self._batch("B-after-close", [contact_item("1")], "TEAM-1")

    def _discharge_and_close_case(self):
        self.service.transition(self.admin, self.case["id"], "triage", {"clinician": "C"})
        self.service.transition(
            self.admin, self.case["id"], "lab_positive",
            {"lab_id": "L", "result": "positive"},
        )
        self.service.transition(
            self.admin, self.case["id"], "recover", {"recovered_at": "2026-03-20"}
        )
        self.service.transition(
            self.admin, self.case["id"], "close", {"outcome": "recovered"}
        )


class FollowupRecomputeTest(EpiTestBase):
    def test_onset_change_recomputes_contact_windows_and_status(self):
        # Exposure 03-05..03-09; onset 03-10 -> window 03-10..03-23 (14 days).
        self._batch("B-f", [contact_item("1", exposure_start="2026-03-05",
                                         exposure_end="2026-03-09")], "TEAM-1")
        contact = self.service.list_contacts(self.case["id"])[0]
        self.assertEqual(contact["data"]["followup_start"], "2026-03-10")
        self.assertEqual(contact["data"]["followup_end"], "2026-03-23")
        self.assertEqual(contact["data"]["followup_status"], "active")

        # Move onset earlier to 03-01: window starts at the last exposure (03-09).
        updated = self.service.update_onset_date(
            self.admin, self.case["id"], "2026-03-01"
        )
        self.assertEqual(updated["followup_recompute"]["items"][0]["followup_start"],
                         "2026-03-09")
        self.assertEqual(updated["followup_recompute"]["items"][0]["followup_end"],
                         "2026-03-22")

        # Move onset into the future past the exposure: window starts at onset.
        self.service.update_onset_date(self.admin, self.case["id"], "2026-03-20")
        contact = self.service.list_contacts(self.case["id"])[0]
        self.assertEqual(contact["data"]["followup_start"], "2026-03-20")
        self.assertEqual(contact["data"]["followup_end"], "2026-04-02")
        self.assertEqual(contact["data"]["followup_status"], "pending")

        # Advance the reference clock past the end -> completed automatically.
        self.clock.value = date(2026, 4, 3)
        self.service.recompute_case_followups(self.admin, self.case["id"])
        contact = self.service.list_contacts(self.case["id"])[0]
        self.assertEqual(contact["data"]["followup_status"], "completed")

    def test_uncomputable_contacts_flagged_for_manual_review(self):
        # A contact row with an unparseable exposure date cannot produce a window.
        self.service.create(
            self.admin, "contact",
            {"case_id": self.case["id"], "person_id": "P-BAD",
             "exposure_start": "not-a-date"},
        )
        summary = self.service.recompute_case_followups(self.admin, self.case["id"])
        self.assertEqual(summary["needs_review"], 1)
        item = summary["items"][0]
        self.assertEqual(item["followup_status"], "needs_review")
        self.assertIn("missing_or_invalid_exposure_date", item["review_reasons"])
        contact = self.service.list_contacts(self.case["id"])[0]
        self.assertIsNone(contact["data"]["followup_end"])

    def test_onset_date_input_validated(self):
        with self.assertRaises(ValidationError):
            self.service.update_onset_date(self.admin, self.case["id"], "03/10/2026")
        with self.assertRaises(PermissionDenied):
            self.service.update_onset_date(
                Actor("v", "viewer"), self.case["id"], "2026-03-01"
            )

    def test_onset_change_blocked_for_closed_case(self):
        self._batch("B-f2", [contact_item("1", exposure_start="2026-03-05",
                                          exposure_end=None)], "TEAM-1")
        self.clock.value = date(2026, 4, 1)
        self._discharge_and_close_case()
        with self.assertRaises(InvalidTransition):
            self.service.update_onset_date(
                self.admin, self.case["id"], "2026-03-01"
            )

    def _discharge_and_close_case(self):
        self.service.transition(self.admin, self.case["id"], "triage", {"clinician": "C"})
        self.service.transition(
            self.admin, self.case["id"], "lab_positive",
            {"lab_id": "L", "result": "positive"},
        )
        self.service.transition(
            self.admin, self.case["id"], "recover", {"recovered_at": "2026-03-30"}
        )
        self.service.transition(
            self.admin, self.case["id"], "close", {"outcome": "recovered"}
        )


class CaseCloseGuardTest(EpiTestBase):
    def _move_to_recovered(self):
        self.service.transition(self.admin, self.case["id"], "triage", {"clinician": "C"})
        self.service.transition(
            self.admin, self.case["id"], "lab_positive",
            {"lab_id": "L", "result": "positive"},
        )
        self.service.transition(
            self.admin, self.case["id"], "recover", {"recovered_at": "2026-03-12"}
        )

    def test_close_blocked_while_contact_is_in_window(self):
        self._batch(
            "B-g",
            [contact_item("1", exposure_start="2026-03-08", exposure_end=None)],
            "TEAM-1",
        )
        self._move_to_recovered()
        with self.assertRaises(InvalidTransition) as caught:
            self.service.transition(
                self.admin, self.case["id"], "close", {"outcome": "recovered"}
            )
        self.assertIn("still within", str(caught.exception))
        self.assertEqual(self.service.get(self.case["id"])["status"], "recovered")

    def test_close_blocked_by_pending_contact_before_window(self):
        self._batch(
            "B-g2",
            [contact_item("1", exposure_start="2026-03-20", exposure_end=None)],
            "TEAM-1",
        )
        self._move_to_recovered()
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, self.case["id"], "close", {"outcome": "recovered"}
            )

    def test_close_blocked_by_needs_review_contact(self):
        self.service.create(
            self.admin, "contact",
            {"case_id": self.case["id"], "person_id": "P-BAD",
             "exposure_start": "garbage"},
        )
        self._move_to_recovered()
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, self.case["id"], "close", {"outcome": "recovered"}
            )

    def test_close_allowed_once_every_window_completed(self):
        self._batch(
            "B-g3",
            [contact_item("1", exposure_start="2026-03-05", exposure_end=None)],
            "TEAM-1",
        )
        self._move_to_recovered()
        # Window 03-10..03-23; on 03-15 it is still active.
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, self.case["id"], "close", {"outcome": "recovered"}
            )
        self.clock.value = date(2026, 3, 24)
        closed = self.service.transition(
            self.admin, self.case["id"], "close", {"outcome": "recovered"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_close_with_no_contacts_is_allowed(self):
        self._move_to_recovered()
        closed = self.service.transition(
            self.admin, self.case["id"], "close", {"outcome": "recovered"}
        )
        self.assertEqual(closed["status"], "closed")


class ConsistencyReportTest(EpiTestBase):
    def test_report_detects_and_repairs_drift(self):
        self._batch(
            "B-r",
            [contact_item("1", exposure_start="2026-03-05", exposure_end=None)],
            "TEAM-1",
        )
        # Tamper with stored follow-up fields to simulate stale/divergent data.
        contact = self.service.list_contacts(self.case["id"])[0]
        data = dict(contact["data"])
        data["followup_end"] = "2026-01-01"
        data["followup_status"] = "completed"
        self.repo.update_entity(contact["id"], None, contact["status"], data)

        report = self.service.consistency_report(self.case["id"])
        self.assertFalse(report["consistent"])
        fields = {m["field"] for m in report["drift"][0]["mismatches"]}
        self.assertIn("followup_end", fields)
        self.assertIn("followup_status", fields)
        # Recomputing against the case makes the two sides agree again.
        self.service.recompute_case_followups(self.admin, self.case["id"])
        repaired = self.service.consistency_report(self.case["id"])
        self.assertTrue(repaired["consistent"])
        self.assertEqual(repaired["drift"], [])

    def _batch(self, batch_id, items, team_id, actor=None, **overrides):
        payload = {
            "batch_id": batch_id,
            "case_id": self.case["id"],
            "chain_id": "CHAIN-7",
            "team_id": team_id,
            "township": "Township-A",
            "items": items,
        }
        payload.update(overrides)
        return self.service.merge_batch(actor or self.investigator, payload)


class HttpApiTest(EpiTestBase):
    def setUp(self, as_of=date(2026, 3, 15)):
        super().setUp(as_of)
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        handler = create_handler(
            self.service, RuleEngine(), static_dir
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def _request(self, method, path, payload=None, headers=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-User-Id", "u-fd-1")
        req.add_header("X-Role", "investigator")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_batch_merge_and_status_endpoints(self):
        payload = {
            "batch_id": "B-http",
            "case_id": self.case["id"],
            "chain_id": "CHAIN-7",
            "team_id": "TEAM-WEB",
            "items": [contact_item("1")],
        }
        status, body = self._request("POST", "/api/batches/merge", payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["applied"]), 1)
        status, body = self._request("GET", "/api/batches/B-http")
        self.assertEqual(status, 200)
        self.assertEqual(body["batch"]["status"], "applied")

    def test_onset_and_consistency_endpoints(self):
        self._batch("B-h", [contact_item("1", exposure_start="2026-03-05",
                                         exposure_end=None)], "TEAM-1")
        status, body = self._request(
            "POST", "/api/cases/%s/onset" % self.case["id"],
            {"onset_date": "2026-03-01"},
        )
        self.assertEqual(status, 200)
        status, body = self._request(
            "GET", "/api/cases/%s/consistency" % self.case["id"]
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["consistent"])


if __name__ == "__main__":
    unittest.main()
