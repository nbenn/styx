"""Unit tests for styx.policy — Policy and MaintenancePolicy."""

import sys
import unittest

from styx.policy import Policy, MaintenancePolicy


class TestPolicy(unittest.TestCase):

    def test_dry_run_false_by_default(self):
        self.assertFalse(Policy().dry_run)

    def test_dry_run_true(self):
        self.assertTrue(Policy(dry_run=True).dry_run)

    def test_execute_calls_fn_when_not_dry_run(self):
        called = []
        Policy().execute('op', called.append, 'x')
        self.assertEqual(called, ['x'])

    def test_execute_skips_fn_in_dry_run(self):
        called = []
        Policy(dry_run=True).execute('op', called.append, 'x')
        self.assertEqual(called, [])

    def test_execute_returns_none_in_dry_run(self):
        result = Policy(dry_run=True).execute('op', lambda: 42)
        self.assertIsNone(result)

    def test_execute_returns_fn_result(self):
        result = Policy().execute('op', lambda: 42)
        self.assertEqual(result, 42)

    def test_on_warning_does_not_raise(self):
        Policy().on_warning('something bad')   # should just log

    def test_phase_gate_is_noop(self):
        Policy().phase_gate('any summary')     # must not prompt or raise


class TestMaintenancePolicy(unittest.TestCase):

    def _policy(self, responses):
        """Build a MaintenancePolicy that feeds responses in order."""
        it = iter(responses)
        return MaintenancePolicy(_input=lambda _: next(it))

    # ── on_warning ────────────────────────────────────────────────────────────

    def test_on_warning_skip_continues(self):
        p = self._policy(['s'])
        p.on_warning('test warning')   # should return without raising

    def test_on_warning_skip_full_word(self):
        p = self._policy(['skip'])
        p.on_warning('test warning')

    def test_on_warning_empty_input_skips(self):
        p = self._policy([''])
        p.on_warning('test warning')

    def test_on_warning_abort_exits_1(self):
        p = self._policy(['a'])
        with self.assertRaises(SystemExit) as cm:
            p.on_warning('test warning')
        self.assertEqual(cm.exception.code, 1)

    def test_on_warning_abort_full_word(self):
        p = self._policy(['abort'])
        with self.assertRaises(SystemExit) as cm:
            p.on_warning('test warning')
        self.assertEqual(cm.exception.code, 1)

    def test_on_warning_reprompts_on_unknown_input(self):
        p = self._policy(['?', 'skip'])
        p.on_warning('test warning')   # first input unknown → reprompt → skip

    def test_on_warning_eoferror_skips(self):
        def raise_eof(_):
            raise EOFError
        p = MaintenancePolicy(_input=raise_eof)
        p.on_warning('test warning')   # EOFError → treat as skip

    # ── phase_gate ────────────────────────────────────────────────────────────

    def test_phase_gate_yes_continues(self):
        p = self._policy(['y'])
        p.phase_gate('summary')        # should return without raising

    def test_phase_gate_yes_full_word(self):
        p = self._policy(['yes'])
        p.phase_gate('summary')

    def test_phase_gate_empty_input_continues(self):
        p = self._policy([''])
        p.phase_gate('summary')

    def test_phase_gate_abort_exits_0(self):
        p = self._policy(['a'])
        with self.assertRaises(SystemExit) as cm:
            p.phase_gate('summary')
        self.assertEqual(cm.exception.code, 0)

    def test_phase_gate_no_exits_0(self):
        p = self._policy(['n'])
        with self.assertRaises(SystemExit) as cm:
            p.phase_gate('summary')
        self.assertEqual(cm.exception.code, 0)

    def test_phase_gate_reprompts_on_unknown_input(self):
        p = self._policy(['maybe', 'y'])
        p.phase_gate('summary')   # first input unknown → reprompt → yes

    def test_phase_gate_eoferror_continues(self):
        def raise_eof(_):
            raise EOFError
        p = MaintenancePolicy(_input=raise_eof)
        p.phase_gate('summary')   # EOFError → treat as yes (non-interactive)

    # ── dry_run inherited ─────────────────────────────────────────────────────

    def test_dry_run_inherited(self):
        p = MaintenancePolicy(dry_run=True, _input=lambda _: 'y')
        result = p.execute('op', lambda: 99)
        self.assertIsNone(result)

    def test_execute_calls_fn(self):
        p = MaintenancePolicy(_input=lambda _: 'y')
        called = []
        p.execute('op', called.append, 'z')
        self.assertEqual(called, ['z'])


if __name__ == '__main__':
    unittest.main()
