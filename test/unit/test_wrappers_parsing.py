"""Unit tests for wrappers parsing helpers and _styx_cmd."""

import sys
import unittest
from unittest.mock import patch

from styx.wrappers import (
    _parse_ha_status, _parse_running_vmids, _styx_cmd, _local_pyz,
    _INSTALLED_PYZ, _VM_LOG, Operations,
)


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


# ── _styx_cmd ────────────────────────────────────────────────────────────────

class TestStyxCmd(unittest.TestCase):

    def test_pyz_path_used_when_running_as_zipapp(self):
        with patch.object(sys, 'argv', ['/var/lib/vz/snippets/styx.pyz']):
            self.assertEqual(_styx_cmd(), 'python3 /var/lib/vz/snippets/styx.pyz')

    def test_relative_pyz_path_preserved(self):
        with patch.object(sys, 'argv', ['styx.pyz']):
            self.assertEqual(_styx_cmd(), 'python3 styx.pyz')

    def test_module_invocation_for_source_install(self):
        with patch.object(sys, 'argv', ['/opt/styx/styx/__main__.py']):
            self.assertEqual(_styx_cmd(), 'python3 -m styx')

    def test_module_invocation_when_argv_empty(self):
        with patch.object(sys, 'argv', []):
            self.assertEqual(_styx_cmd(), 'python3 -m styx')


# ── Operations.shutdown_vm command construction ───────────────────────────────

class TestOperationsShutdownVmCmd(unittest.TestCase):

    def test_shutdown_vm_orchestrator_uses_local_pyz(self):
        ops = Operations({'pve1': '10.0.0.1'}, 'pve1')
        with patch.object(sys, 'argv', ['/mnt/pve/shared/snippets/styx.pyz']):
            with patch.object(ops, 'run_on_host') as mock_run:
                ops.shutdown_vm('pve1', '101', 120)
        log_file = _VM_LOG.format(vmid='101')
        mock_run.assert_called_once_with(
            'pve1',
            f'nohup python3 /mnt/pve/shared/snippets/styx.pyz vm-shutdown 101 120 </dev/null >{log_file} 2>&1 &',
        )

    def test_shutdown_vm_peer_uses_remote_pyz(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['/mnt/pve/shared/snippets/styx.pyz']):
            with patch.object(ops, 'run_on_host') as mock_run:
                ops.shutdown_vm('pve2', '102', 120)
        log_file = _VM_LOG.format(vmid='102')
        mock_run.assert_called_once_with(
            'pve2',
            f'nohup python3 {_INSTALLED_PYZ} vm-shutdown 102 120 </dev/null >{log_file} 2>&1 &',
        )

    def test_shutdown_vm_uses_module_from_source(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['styx/__main__.py']):
            with patch.object(ops, 'run_on_host') as mock_run:
                ops.shutdown_vm('pve2', '102', 120)
        log_file = _VM_LOG.format(vmid='102')
        mock_run.assert_called_once_with(
            'pve2',
            f'nohup python3 -m styx vm-shutdown 102 120 </dev/null >{log_file} 2>&1 &',
        )


# ── Operations.check_vm ───────────────────────────────────────────────────────

class TestOperationsCheckVm(unittest.TestCase):

    def test_check_vm_orchestrator_uses_local_pyz(self):
        ops = Operations({'pve1': '10.0.0.1'}, 'pve1')
        with patch.object(sys, 'argv', ['/mnt/pve/shared/styx.pyz']):
            with patch.object(ops, 'run_on_host', return_value='VM 101 is running (pid 1234) — would shut down\n') as mock_run:
                ops.check_vm('pve1', '101')
        mock_run.assert_called_once_with(
            'pve1',
            'python3 /mnt/pve/shared/styx.pyz vm-shutdown 101 --dry-run',
        )

    def test_check_vm_peer_uses_remote_pyz(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['/mnt/pve/shared/styx.pyz']):
            with patch.object(ops, 'run_on_host', return_value='VM 211 is not running\n') as mock_run:
                ops.check_vm('pve2', '211')
        mock_run.assert_called_once_with(
            'pve2',
            f'python3 {_INSTALLED_PYZ} vm-shutdown 211 --dry-run',
        )

    def test_check_vm_dev_mode_uses_module(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['styx/__main__.py']):
            with patch.object(ops, 'run_on_host', return_value='VM 211 is not running\n') as mock_run:
                ops.check_vm('pve2', '211')
        mock_run.assert_called_once_with(
            'pve2',
            'python3 -m styx vm-shutdown 211 --dry-run',
        )


# ── Operations.dispatch_local_shutdown command construction ────────────────

class TestOperationsDispatchLocalShutdown(unittest.TestCase):

    def test_dispatch_uses_type_prefixed_format(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['styx/__main__.py']):
            with patch.object(ops, 'run_on_host') as mock_run:
                ops.dispatch_local_shutdown(
                    'pve2', [('qemu', '101'), ('qemu', '201')],
                    timeout_vm=120,
                )
        cmd = mock_run.call_args[0][1]
        self.assertIn('qemu:101', cmd)
        self.assertIn('qemu:201', cmd)
        self.assertIn('local-shutdown', cmd)
        self.assertIn('--timeout 120', cmd)

    def test_dispatch_with_poweroff_delay(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['styx/__main__.py']):
            with patch.object(ops, 'run_on_host') as mock_run:
                ops.dispatch_local_shutdown(
                    'pve2', [('qemu', '101')],
                    timeout_vm=120, poweroff_delay=135,
                )
        cmd = mock_run.call_args[0][1]
        self.assertIn('--poweroff-delay 135', cmd)

    def test_dispatch_with_dry_run(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['styx/__main__.py']):
            with patch.object(ops, 'run_on_host') as mock_run:
                ops.dispatch_local_shutdown(
                    'pve2', [('qemu', '101')],
                    timeout_vm=120, dry_run=True,
                )
        cmd = mock_run.call_args[0][1]
        self.assertIn('--dry-run', cmd)


if __name__ == '__main__':
    unittest.main()
