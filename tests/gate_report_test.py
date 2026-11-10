#!/usr/bin/env python3
"""The gate's report: what a suite's own output is read as.

The gate decides coverage from the text a suite printed, so a case it
misreads is a case it credits wrongly: a blocked prerequisite counted as a
pass is a run that exited zero without showing the property. These cases
feed the parser the shapes a suite and the TypeScript harness print.
"""

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run


class ReportTests(unittest.TestCase):
    """One suite's output in, the gate's verdict out."""

    def _step(self, output):
        step = run.Step("example", ["true"], suite=True)
        step._read(output)
        return step

    def test_a_blocked_skip_is_blocked_not_unsupported(self):
        step = self._step(
            "test_example (example.Tests) ... skipped "
            "'blocked: prerequisite unavailable'\n"
            "Ran 1 test in 0.001s\n")
        self.assertEqual(1, step.ran)
        self.assertEqual([], step.unsupported)
        self.assertEqual([("test_example (example.Tests)",
                           "prerequisite unavailable")], step.blocked)
        self.assertEqual([], step.failed)

    def test_an_ordinary_skip_is_unsupported(self):
        step = self._step(
            "test_example (example.Tests) ... skipped 'no /proc on Windows'\n"
            "Ran 1 test in 0.001s\n")
        self.assertEqual([], step.blocked)
        self.assertEqual([("test_example (example.Tests)",
                           "no /proc on Windows")], step.unsupported)

    def test_a_failure_line_names_the_case(self):
        step = self._step(
            "test_example (example.Tests) ... FAIL\n"
            "FAIL: test_example (example.Tests)\n"
            "Ran 1 test in 0.001s\n")
        self.assertEqual(["test_example (example.Tests)"], step.failed)

    def test_a_summary_blocked_entry_is_blocked(self):
        step = self._step(
            'pi-ts-summary {"ran": 2, "failed": [], '
            '"skipped": ["case (mod): blocked: no node"]}\n')
        self.assertEqual(2, step.ran)
        self.assertEqual([("case (mod)", "no node")], step.blocked)


if __name__ == "__main__":
    unittest.main()
