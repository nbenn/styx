"""Unit tests for wrappers parsing helpers."""

import unittest

from styx.wrappers import _parse_ha_status, _parse_running_vmids


# ── _parse_ha_status ──────────────────────────────────────────────────────────

class TestParseHaStatus(unittest.TestCase):

    def test_empty_output_returns_empty(self):
        self.assertEqual(_parse_ha_status(''), [])

    def test_started_sid_returned(self):
        output = 'vm:101 started node pve1\n'
        self.assertEqual(_parse_ha_status(output), ['vm:101'])

    def test_multiple_started_sids_all_returned(self):
        output = (
            'vm:101 started node pve1\n'
            'vm:102 started node pve2\n'
            'vm:103 started node pve3\n'
        )
        self.assertEqual(_parse_ha_status(output), ['vm:101', 'vm:102', 'vm:103'])

    def test_non_started_states_excluded(self):
        output = (
            'vm:101 stopped\n'
            'vm:102 error\n'
            'vm:103 fence\n'
            'vm:104 disabled\n'
            'vm:105 ignored\n'
        )
        self.assertEqual(_parse_ha_status(output), [])

    def test_mixed_states_only_started_returned(self):
        output = (
            'vm:101 started node pve1\n'
            'vm:102 stopped\n'
            'vm:103 started node pve2\n'
            'vm:104 disabled\n'
        )
        self.assertEqual(_parse_ha_status(output), ['vm:101', 'vm:103'])

    def test_header_lines_ignored(self):
        # 'quorum OK' → parts[1] is 'OK', not 'started'
        # 'resources:' → single token, len(parts) < 2
        output = (
            'quorum OK\n'
            'resources:\n'
            'vm:101 started node pve1\n'
        )
        self.assertEqual(_parse_ha_status(output), ['vm:101'])

    def test_blank_lines_ignored(self):
        output = '\n\nvm:101 started\n\n'
        self.assertEqual(_parse_ha_status(output), ['vm:101'])

    def test_single_token_lines_ignored(self):
        output = 'vm:101\nvm:102 started\n'
        self.assertEqual(_parse_ha_status(output), ['vm:102'])

    def test_sid_is_first_field_not_full_line(self):
        output = 'vm:201 started node pve1 extra info\n'
        result = _parse_ha_status(output)
        self.assertEqual(result, ['vm:201'])
        self.assertNotIn(' ', result[0])

    def test_realistic_ha_manager_output(self):
        output = (
            'quorum OK\n'
            '\n'
            'resources:\n'
            '\n'
            'vm:101 started         node pve1\n'
            'vm:102 stopped\n'
            'vm:201 started         node pve2\n'
            'vm:211 started         node pve3\n'
            'vm:212 disabled\n'
        )
        result = _parse_ha_status(output)
        self.assertEqual(result, ['vm:101', 'vm:201', 'vm:211'])


# ── _parse_running_vmids ──────────────────────────────────────────────────────

class TestParseRunningVmids(unittest.TestCase):

    def test_empty_output_returns_empty(self):
        self.assertEqual(_parse_running_vmids(''), [])

    def test_single_vmid(self):
        self.assertEqual(_parse_running_vmids('101\n'), ['101'])

    def test_multiple_vmids(self):
        output = '101\n102\n201\n'
        self.assertEqual(_parse_running_vmids(output), ['101', '102', '201'])

    def test_blank_lines_excluded(self):
        output = '101\n\n102\n\n'
        self.assertEqual(_parse_running_vmids(output), ['101', '102'])

    def test_whitespace_stripped(self):
        output = '  101  \n  102\n'
        self.assertEqual(_parse_running_vmids(output), ['101', '102'])

    def test_no_trailing_newline(self):
        self.assertEqual(_parse_running_vmids('101'), ['101'])

    def test_whitespace_only_lines_excluded(self):
        output = '101\n   \n102\n\t\n'
        self.assertEqual(_parse_running_vmids(output), ['101', '102'])


if __name__ == '__main__':
    unittest.main()
