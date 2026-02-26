"""Unit tests for styx.policy — Policy, DryRunPolicy, and MaintenancePolicy."""

import unittest

from styx.policy import Policy, DryRunPolicy, MaintenancePolicy


class TestPolicy(unittest.TestCase):
    """Emergency mode: executes, warns, no gates."""

    def test_dry_run_is_false(self):
        self.assertFalse(Policy().dry_run)

    def test_execute_calls_fn(self):
        called = []
        Policy().execute('op', called.append, 'x')
        self.assertEqual(called, ['x'])

    def test_execute_returns_fn_result(self):
        self.assertEqual(Policy().execute('op', lambda: 42), 42)

    def test_on_warning_does_not_raise(self):
        Policy().on_warning('something bad')

    def test_on_preflight_failure_does_not_raise(self):
        Policy().on_preflight_failure('something bad')

    def test_phase_gate_is_noop(self):
        Policy().phase_gate('any summary')


class TestDryRunPolicy(unittest.TestCase):
    """Dry-run mode: logs, executes nothing."""

    def test_dry_run_is_true(self):
        self.assertTrue(DryRunPolicy().dry_run)

    def test_execute_skips_fn(self):
        called = []
        DryRunPolicy().execute('op', called.append, 'x')
        self.assertEqual(called, [])

    def test_execute_returns_none(self):
        self.assertIsNone(DryRunPolicy().execute('op', lambda: 42))

    def test_on_warning_does_not_raise(self):
        DryRunPolicy().on_warning('something bad')

    def test_on_preflight_failure_raises_system_exit(self):
        with self.assertRaises(SystemExit) as cm:
            DryRunPolicy().on_preflight_failure('something bad')
        self.assertIn('FATAL', str(cm.exception))

    def test_phase_gate_is_noop(self):
        DryRunPolicy().phase_gate('any summary')


class TestMaintenancePolicy(unittest.TestCase):

    def _policy(self, responses):
        """Build a MaintenancePolicy that feeds responses in order."""
        it = iter(responses)
        return MaintenancePolicy(_input=lambda _: next(it))

    # ── on_warning ────────────────────────────────────────────────────────────

    def test_on_warning_skip_continues(self):
        self._policy(['s']).on_warning('test warning')

    def test_on_warning_skip_full_word(self):
        self._policy(['skip']).on_warning('test warning')

    def test_on_warning_empty_input_skips(self):
        self._policy(['']).on_warning('test warning')

    def test_on_warning_abort_exits_1(self):
        with self.assertRaises(SystemExit) as cm:
            self._policy(['a']).on_warning('test warning')
        self.assertEqual(cm.exception.code, 1)

    def test_on_warning_abort_full_word(self):
        with self.assertRaises(SystemExit) as cm:
            self._policy(['abort']).on_warning('test warning')
        self.assertEqual(cm.exception.code, 1)

    def test_on_warning_reprompts_on_unknown_input(self):
        self._policy(['?', 'skip']).on_warning('test warning')

    def test_on_warning_eoferror_aborts(self):
        def raise_eof(_):
            raise EOFError
        with self.assertRaises(SystemExit) as cm:
            MaintenancePolicy(_input=raise_eof).on_warning('test warning')
        self.assertEqual(cm.exception.code, 1)

    # ── phase_gate ────────────────────────────────────────────────────────────

    def test_phase_gate_yes_continues(self):
        self._policy(['y']).phase_gate('summary')

    def test_phase_gate_yes_full_word(self):
        self._policy(['yes']).phase_gate('summary')

    def test_phase_gate_empty_input_continues(self):
        self._policy(['']).phase_gate('summary')

    def test_phase_gate_abort_exits_0(self):
        with self.assertRaises(SystemExit) as cm:
            self._policy(['a']).phase_gate('summary')
        self.assertEqual(cm.exception.code, 0)

    def test_phase_gate_no_exits_0(self):
        with self.assertRaises(SystemExit) as cm:
            self._policy(['n']).phase_gate('summary')
        self.assertEqual(cm.exception.code, 0)

    def test_phase_gate_reprompts_on_unknown_input(self):
        self._policy(['maybe', 'y']).phase_gate('summary')

    def test_phase_gate_eoferror_aborts(self):
        def raise_eof(_):
            raise EOFError
        with self.assertRaises(SystemExit) as cm:
            MaintenancePolicy(_input=raise_eof).phase_gate('summary')
        self.assertEqual(cm.exception.code, 1)

    # ── on_preflight_failure ─────────────────────────────────────────────────

    def test_on_preflight_failure_raises_system_exit(self):
        pol = MaintenancePolicy(_input=lambda _: 's')
        with self.assertRaises(SystemExit) as cm:
            pol.on_preflight_failure('something bad')
        self.assertIn('FATAL', str(cm.exception))

    # ── inherits Policy behaviour ─────────────────────────────────────────────

    def test_dry_run_is_false(self):
        self.assertFalse(MaintenancePolicy(_input=lambda _: 'y').dry_run)

    def test_execute_calls_fn(self):
        called = []
        MaintenancePolicy(_input=lambda _: 'y').execute('op', called.append, 'z')
        self.assertEqual(called, ['z'])


if __name__ == '__main__':
    unittest.main()
