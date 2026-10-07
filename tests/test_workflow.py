import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'case', 'kind': 'case', 'data': {'person_id': 'P-1', 'onset_date': '2026-03-01', 'location': 'District-A', 'symptoms': ['fever']}}, {'op': 'transition', 'target': 'case', 'action': 'triage', 'data': {'clinician': 'C-1'}, 'expect': 'investigating'}, {'op': 'transition', 'target': 'case', 'action': 'lab_positive', 'data': {'lab_id': 'L-1', 'result': 'positive'}, 'expect': 'confirmed'}, {'op': 'transition', 'target': 'case', 'action': 'recover', 'data': {'recovered_at': '2026-03-10'}, 'expect': 'recovered'}, {'op': 'transition', 'target': 'case', 'action': 'close', 'data': {'outcome': 'recovered'}, 'expect': 'closed'}, {'op': 'create', 'as': 'contact', 'kind': 'contact', 'data': {'case_id': '{case}', 'person_id': 'P-2', 'exposure_start': '2026-02-25'}}, {'op': 'transition', 'target': 'contact', 'action': 'begin_followup', 'data': {'followup_start': '2026-03-02', 'due_at': '2026-03-16'}, 'expect': 'following'}, {'op': 'transition', 'target': 'contact', 'action': 'complete_followup', 'data': {'outcome': 'no symptoms'}, 'expect': 'completed'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])


if __name__ == "__main__":
    unittest.main()
