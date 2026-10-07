import unittest

from src.rules import cluster_cases
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        cases = [
            {"id": "1", "location": "A", "onset_date": "2026-01-01"},
            {"id": "2", "location": "A", "onset_date": "2026-01-05"},
            {"id": "3", "location": "A", "onset_date": "2026-02-20"},
        ]
        groups = cluster_cases(cases, max_days=14)
        self.assertEqual(groups[0]["members"], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
