"""Unit tests for wrappers parsing helpers and _styx_cmd.

New HA parsing tests (_parse_ha_resources, _parse_ha_groups,
_parse_ha_services_on_nodes) are in test_ha_smart.py.
"""

import sys
import unittest
from unittest.mock import patch

from styx.wrappers import (
    _parse_ha_status, _parse_running_vmids, _parse_osd_tree,
    _styx_cmd, _local_pyz,
    _VM_LOG, Operations,
)


# ── _parse_ha_status ──────────────────────────────────────────────────────────

class TestParseHaStatus(unittest.TestCase):

    def test_empty_list_returns_empty(self):
        self.assertEqual(_parse_ha_status([]), [])

    def test_started_sid_returned(self):
        data = [{'sid': 'vm:101', 'state': 'started'}]
        self.assertEqual(_parse_ha_status(data), ['vm:101'])

    def test_multiple_started_sids_all_returned(self):
        data = [
            {'sid': 'vm:101', 'state': 'started'},
            {'sid': 'vm:102', 'state': 'started'},
            {'sid': 'vm:103', 'state': 'started'},
        ]
        self.assertEqual(_parse_ha_status(data), ['vm:101', 'vm:102', 'vm:103'])

    def test_non_started_states_excluded(self):
        data = [
            {'sid': 'vm:101', 'state': 'stopped'},
            {'sid': 'vm:102', 'state': 'error'},
            {'sid': 'vm:103', 'state': 'fence'},
            {'sid': 'vm:104', 'state': 'disabled'},
            {'sid': 'vm:105', 'state': 'ignored'},
        ]
        self.assertEqual(_parse_ha_status(data), [])

    def test_mixed_states_only_started_returned(self):
        data = [
            {'sid': 'vm:101', 'state': 'started'},
            {'sid': 'vm:102', 'state': 'stopped'},
            {'sid': 'vm:103', 'state': 'started'},
            {'sid': 'vm:104', 'state': 'disabled'},
        ]
        self.assertEqual(_parse_ha_status(data), ['vm:101', 'vm:103'])

    def test_entries_without_sid_skipped(self):
        data = [
            {'state': 'started'},
            {'sid': 'vm:101', 'state': 'started'},
        ]
        self.assertEqual(_parse_ha_status(data), ['vm:101'])

    def test_entries_without_state_skipped(self):
        data = [
            {'sid': 'vm:101'},
            {'sid': 'vm:102', 'state': 'started'},
        ]
        self.assertEqual(_parse_ha_status(data), ['vm:102'])

    def test_non_service_entries_skipped(self):
        data = [
            {'id': 'master', 'type': 'crm', 'node': 'pve1', 'status': 'active'},
            {'id': 'lrm', 'type': 'lrm', 'node': 'pve2', 'status': 'active'},
            {'sid': 'vm:101', 'state': 'started'},
        ]
        self.assertEqual(_parse_ha_status(data), ['vm:101'])

    def test_realistic_api_output(self):
        """Real pvesh get /cluster/ha/status/current structure (anonymized)."""
        data = [
            {'id': 'quorum', 'node': 'pve1', 'quorate': 1,
             'status': 'OK', 'type': 'quorum'},
            {'id': 'master', 'node': 'pve2', 'timestamp': 1772048533,
             'status': 'pve2 (active, Wed Feb 25 20:42:13 2026)', 'type': 'master'},
            {'id': 'lrm:pve1', 'node': 'pve1', 'timestamp': 1772048534,
             'status': 'pve1 (idle, Wed Feb 25 20:42:14 2026)', 'type': 'lrm'},
            {'id': 'lrm:pve2', 'node': 'pve2', 'timestamp': 1772048535,
             'status': 'pve2 (active, Wed Feb 25 20:42:15 2026)', 'type': 'lrm'},
            {'id': 'lrm:pve3', 'node': 'pve3', 'timestamp': 1772048531,
             'status': 'pve3 (active, Wed Feb 25 20:42:11 2026)', 'type': 'lrm'},
            {'crm_state': 'started', 'group': 'grp1', 'id': 'service:vm:100',
             'max_relocate': 1, 'max_restart': 1, 'node': 'pve3',
             'request_state': 'started', 'sid': 'vm:100', 'state': 'started',
             'status': 'vm:100 (pve3, started)', 'type': 'service'},
            {'crm_state': 'started', 'group': 'grp2', 'id': 'service:vm:104',
             'max_relocate': 1, 'max_restart': 1, 'node': 'pve2',
             'request_state': 'started', 'sid': 'vm:104', 'state': 'started',
             'status': 'vm:104 (pve2, started)', 'type': 'service'},
            {'crm_state': 'started', 'group': 'grp2', 'id': 'service:vm:106',
             'max_relocate': 1, 'max_restart': 1, 'node': 'pve2',
             'request_state': 'started', 'sid': 'vm:106', 'state': 'started',
             'status': 'vm:106 (pve2, started)', 'type': 'service'},
            {'crm_state': 'started', 'group': 'grp2', 'id': 'service:vm:110',
             'max_relocate': 1, 'max_restart': 1, 'node': 'pve3',
             'request_state': 'started', 'sid': 'vm:110', 'state': 'started',
             'status': 'vm:110 (pve3, started)', 'type': 'service'},
            {'crm_state': 'stopped', 'group': 'grp2', 'id': 'service:vm:112',
             'max_relocate': 1, 'max_restart': 1, 'node': 'pve1',
             'request_state': 'stopped', 'sid': 'vm:112', 'state': 'stopped',
             'status': 'vm:112 (pve1, stopped)', 'type': 'service'},
        ]
        result = _parse_ha_status(data)
        self.assertEqual(result, ['vm:100', 'vm:104', 'vm:106', 'vm:110'])


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


# ── _parse_osd_tree ──────────────────────────────────────────────────────────

class TestParseOsdTree(unittest.TestCase):

    def test_empty_nodes_returns_empty(self):
        self.assertEqual(_parse_osd_tree({'nodes': []}), {})

    def test_typical_tree_two_hosts(self):
        data = {'nodes': [
            {'id': -1, 'type': 'root', 'name': 'default', 'children': [-2, -3]},
            {'id': -2, 'type': 'host', 'name': 'pve1', 'children': [0, 1, 2]},
            {'id': -3, 'type': 'host', 'name': 'pve2', 'children': [3, 4, 5]},
            {'id': 0, 'type': 'osd', 'name': 'osd.0'},
            {'id': 1, 'type': 'osd', 'name': 'osd.1'},
            {'id': 2, 'type': 'osd', 'name': 'osd.2'},
            {'id': 3, 'type': 'osd', 'name': 'osd.3'},
            {'id': 4, 'type': 'osd', 'name': 'osd.4'},
            {'id': 5, 'type': 'osd', 'name': 'osd.5'},
        ]}
        result = _parse_osd_tree(data)
        self.assertEqual(result, {
            'pve1': ['0', '1', '2'],
            'pve2': ['3', '4', '5'],
        })

    def test_host_with_no_children(self):
        data = {'nodes': [
            {'id': -1, 'type': 'host', 'name': 'empty-host', 'children': []},
        ]}
        self.assertEqual(_parse_osd_tree(data), {'empty-host': []})

    def test_missing_nodes_key(self):
        self.assertEqual(_parse_osd_tree({}), {})

    def test_stray_entries_ignored(self):
        data = {'nodes': [
            {'id': -1, 'type': 'root', 'name': 'default', 'children': [-2]},
            {'id': -2, 'type': 'host', 'name': 'pve1', 'children': [0]},
            {'id': 0, 'type': 'osd', 'name': 'osd.0'},
        ], 'stray': []}
        self.assertEqual(_parse_osd_tree(data), {'pve1': ['0']})

    def test_realistic_ceph_output(self):
        """Real ceph osd tree --format json structure (anonymized)."""
        data = {"nodes": [
            {"id": -1, "name": "default", "type": "root", "type_id": 11,
             "children": [-11, -9, -7, -5, -3]},
            {"id": -3, "name": "pve1", "type": "host", "type_id": 1,
             "pool_weights": {}, "children": [1, 0]},
            {"id": 0, "device_class": "ssd", "name": "osd.0", "type": "osd",
             "type_id": 0, "crush_weight": 1.4554901123046875, "depth": 2,
             "pool_weights": {}, "exists": 1, "status": "up", "reweight": 1,
             "primary_affinity": 1},
            {"id": 1, "device_class": "ssd", "name": "osd.1", "type": "osd",
             "type_id": 0, "crush_weight": 1.4554901123046875, "depth": 2,
             "pool_weights": {}, "exists": 1, "status": "up", "reweight": 1,
             "primary_affinity": 1},
            {"id": -5, "name": "pve2", "type": "host", "type_id": 1,
             "pool_weights": {}, "children": [2]},
            {"id": 2, "device_class": "ssd", "name": "osd.2", "type": "osd",
             "type_id": 0, "crush_weight": 2.9109954833984375, "depth": 2,
             "pool_weights": {}, "exists": 1, "status": "up", "reweight": 1,
             "primary_affinity": 1},
            {"id": -7, "name": "pve3", "type": "host", "type_id": 1,
             "pool_weights": {}, "children": [4, 3]},
            {"id": 3, "device_class": "ssd", "name": "osd.3", "type": "osd",
             "type_id": 0, "crush_weight": 1.4554901123046875, "depth": 2,
             "pool_weights": {}, "exists": 1, "status": "up", "reweight": 1,
             "primary_affinity": 1},
            {"id": 4, "device_class": "ssd", "name": "osd.4", "type": "osd",
             "type_id": 0, "crush_weight": 1.4554901123046875, "depth": 2,
             "pool_weights": {}, "exists": 1, "status": "up", "reweight": 1,
             "primary_affinity": 1},
            {"id": -9, "name": "pve4", "type": "host", "type_id": 1,
             "pool_weights": {}, "children": [7, 6]},
            {"id": 6, "device_class": "ssd", "name": "osd.6", "type": "osd",
             "type_id": 0, "crush_weight": 1.4554901123046875, "depth": 2,
             "pool_weights": {}, "exists": 1, "status": "up", "reweight": 1,
             "primary_affinity": 1},
            {"id": 7, "device_class": "ssd", "name": "osd.7", "type": "osd",
             "type_id": 0, "crush_weight": 1.4554901123046875, "depth": 2,
             "pool_weights": {}, "exists": 1, "status": "up", "reweight": 1,
             "primary_affinity": 1},
            {"id": -11, "name": "pve5", "type": "host", "type_id": 1,
             "pool_weights": {}, "children": [5]},
            {"id": 5, "device_class": "ssd", "name": "osd.5", "type": "osd",
             "type_id": 0, "crush_weight": 2.9109954833984375, "depth": 2,
             "pool_weights": {}, "exists": 1, "status": "up", "reweight": 1,
             "primary_affinity": 1},
        ], "stray": []}
        result = _parse_osd_tree(data)
        self.assertEqual(result, {
            'pve1': ['1', '0'],
            'pve2': ['2'],
            'pve3': ['4', '3'],
            'pve4': ['7', '6'],
            'pve5': ['5'],
        })


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

    def test_shutdown_vm_peer_uses_local_pyz(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['/mnt/pve/shared/snippets/styx.pyz']):
            with patch.object(ops, 'run_on_host') as mock_run:
                ops.shutdown_vm('pve2', '102', 120)
        log_file = _VM_LOG.format(vmid='102')
        mock_run.assert_called_once_with(
            'pve2',
            f'nohup python3 /mnt/pve/shared/snippets/styx.pyz vm-shutdown 102 120 </dev/null >{log_file} 2>&1 &',
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

    def test_check_vm_peer_uses_local_pyz(self):
        ops = Operations({'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, 'pve1')
        with patch.object(sys, 'argv', ['/mnt/pve/shared/styx.pyz']):
            with patch.object(ops, 'run_on_host', return_value='VM 211 is not running\n') as mock_run:
                ops.check_vm('pve2', '211')
        mock_run.assert_called_once_with(
            'pve2',
            'python3 /mnt/pve/shared/styx.pyz vm-shutdown 211 --dry-run',
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
