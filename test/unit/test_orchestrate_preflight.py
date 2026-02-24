"""Unit tests for orchestrate.preflight()."""

import subprocess
import sys
import unittest
from unittest import mock

from styx.config import StyxConfig
from styx.discover import ClusterTopology
from styx.orchestrate import preflight, _log_runtime_budget


def _topo(**kwargs):
    t = ClusterTopology()
    t.host_ips    = {'pve1': '10.0.0.1', 'pve2': '10.0.0.2', 'pve3': '10.0.0.3'}
    t.orchestrator = 'pve1'
    t.vm_name     = {'201': 'k8s-cp-1', '211': 'k8s-worker-1'}
    t.vm_type     = {'201': 'qemu', '211': 'qemu'}
    for k, v in kwargs.items():
        setattr(t, k, v)
    return t


def _cfg(**kwargs):
    cfg = StyxConfig()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def _ssh_targets(mock_run):
    """Extract the 'root@IP' argument from every SSH call."""
    return [
        call.args[0][5]          # ['ssh', '-o', ..., '-o', ..., 'root@IP', 'exit']
        for call in mock_run.call_args_list
        if call.args and call.args[0][0] == 'ssh'
    ]


def _ceph_calls(mock_run):
    return [c for c in mock_run.call_args_list if c.args and c.args[0][0] == 'ceph']


# ── SSH ───────────────────────────────────────────────────────────────────────

class TestPreflightSSH(unittest.TestCase):

    def test_orchestrator_not_sshed(self):
        with mock.patch('styx.orchestrate.subprocess.run') as m:
            preflight(_topo(), _cfg())
        self.assertNotIn('root@10.0.0.1', _ssh_targets(m))

    def test_non_orchestrator_hosts_are_sshed(self):
        with mock.patch('styx.orchestrate.subprocess.run') as m:
            preflight(_topo(), _cfg())
        targets = _ssh_targets(m)
        self.assertIn('root@10.0.0.2', targets)
        self.assertIn('root@10.0.0.3', targets)

    def test_ssh_uses_correct_options(self):
        with mock.patch('styx.orchestrate.subprocess.run') as m:
            preflight(_topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                           orchestrator='pve1'), _cfg())
        ssh_cmd = next(
            c.args[0] for c in m.call_args_list if c.args[0][0] == 'ssh'
        )
        self.assertIn('ConnectTimeout=5', ssh_cmd)
        self.assertIn('BatchMode=yes', ssh_cmd)
        self.assertEqual(ssh_cmd[-1], 'exit')

    def test_ssh_failure_does_not_raise(self):
        err = subprocess.CalledProcessError(255, 'ssh')
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=err):
            preflight(_topo(), _cfg())   # must not raise

    def test_ssh_timeout_does_not_raise(self):
        err = subprocess.TimeoutExpired('ssh', 10)
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=err):
            preflight(_topo(), _cfg())   # must not raise


# ── Kubernetes ────────────────────────────────────────────────────────────────

class TestPreflightK8s(unittest.TestCase):

    def test_k8s_skipped_when_disabled(self):
        with mock.patch('styx.orchestrate.subprocess.run'):
            with mock.patch('styx.orchestrate._make_k8s_client') as mk:
                preflight(_topo(k8s_enabled=False),
                          _cfg(k8s_server='https://k8s', k8s_token='/tok'))
        mk.assert_not_called()

    def test_k8s_skipped_when_no_credentials(self):
        with mock.patch('styx.orchestrate.subprocess.run'):
            with mock.patch('styx.orchestrate._make_k8s_client') as mk:
                preflight(_topo(k8s_enabled=True), _cfg())   # no server/token
        mk.assert_not_called()

    def test_k8s_skipped_when_server_only(self):
        with mock.patch('styx.orchestrate.subprocess.run'):
            with mock.patch('styx.orchestrate._make_k8s_client') as mk:
                preflight(_topo(k8s_enabled=True),
                          _cfg(k8s_server='https://k8s'))    # token missing
        mk.assert_not_called()

    def test_k8s_api_called_when_enabled_with_credentials(self):
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': [{}, {}]}
        fake_k8s.list_pods_on_node.return_value = {'items': []}
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'])
        with mock.patch('styx.orchestrate.subprocess.run'):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'))
        fake_k8s.list_nodes.assert_called_once()

    def test_pod_count_checked_for_each_k8s_node(self):
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': []}
        fake_k8s.list_pods_on_node.return_value = {'items': []}
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'])
        with mock.patch('styx.orchestrate.subprocess.run'):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'))
        # one call per node: worker + cp = 2
        self.assertEqual(fake_k8s.list_pods_on_node.call_count, 2)
        nodes_checked = {c.args[0] for c in fake_k8s.list_pods_on_node.call_args_list}
        self.assertEqual(nodes_checked, {'k8s-worker-1', 'k8s-cp-1'})

    def test_k8s_api_unreachable_does_not_raise(self):
        topo = _topo(k8s_enabled=True)
        with mock.patch('styx.orchestrate.subprocess.run'):
            with mock.patch('styx.orchestrate._make_k8s_client',
                            side_effect=ConnectionError('refused')):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'))

    def test_pod_list_failure_does_not_raise(self):
        """Exception from list_pods_on_node (inner try) is silently caught."""
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': [{}]}
        fake_k8s.list_pods_on_node.side_effect = TimeoutError('pods')
        topo = _topo(k8s_enabled=True, k8s_workers=['211'])
        with mock.patch('styx.orchestrate.subprocess.run'):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'))


# ── Ceph ──────────────────────────────────────────────────────────────────────

class TestPreflightCeph(unittest.TestCase):

    def test_ceph_skipped_when_disabled(self):
        with mock.patch('styx.orchestrate.subprocess.run') as m:
            preflight(_topo(ceph_enabled=False), _cfg())
        self.assertEqual(_ceph_calls(m), [])

    def test_ceph_health_called_when_enabled(self):
        r = mock.MagicMock(stdout='HEALTH_OK', stderr='')

        def _run(cmd, **kwargs):
            return r if cmd[0] == 'ceph' else mock.MagicMock()

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run) as m:
            preflight(_topo(ceph_enabled=True), _cfg())
        calls = _ceph_calls(m)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0][:2], ['ceph', 'health'])

    def test_ceph_unavailable_does_not_raise(self):
        def _run(cmd, **kwargs):
            if cmd[0] == 'ceph':
                raise FileNotFoundError('ceph not found')
            return mock.MagicMock()

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(ceph_enabled=True), _cfg())   # must not raise


# ── Runtime Budget ───────────────────────────────────────────────────────────

class TestRuntimeBudget(unittest.TestCase):

    @mock.patch('styx.orchestrate.log')
    def test_includes_drain_and_vm_shutdown(self, mock_log):
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'],
                     vm_host={'211': 'pve2', '201': 'pve3'})
        _log_runtime_budget(topo, _cfg(timeout_drain=120, timeout_vm=120), phase=3)
        msgs = [c.args[0] for c in mock_log.call_args_list]
        self.assertTrue(any('120s' in m and 'drain' in m for m in msgs))
        self.assertTrue(any('135s' in m and 'VM' in m for m in msgs))
        self.assertTrue(any('4m 25s' in m for m in msgs))

    @mock.patch('styx.orchestrate.log')
    def test_no_k8s_omits_drain(self, mock_log):
        topo = _topo(k8s_enabled=False,
                     vm_host={'101': 'pve1'})
        _log_runtime_budget(topo, _cfg(timeout_vm=120), phase=3)
        msgs = [c.args[0] for c in mock_log.call_args_list]
        self.assertFalse(any('drain' in m for m in msgs))
        self.assertTrue(any('135s' in m and 'VM' in m for m in msgs))
        self.assertTrue(any('2m 25s' in m for m in msgs))

    @mock.patch('styx.orchestrate.log')
    def test_phase1_no_vm_wait(self, mock_log):
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'],
                     vm_host={'211': 'pve2', '201': 'pve3'})
        _log_runtime_budget(topo, _cfg(timeout_drain=60, timeout_vm=120), phase=1)
        msgs = [c.args[0] for c in mock_log.call_args_list]
        self.assertTrue(any('60s' in m and 'drain' in m for m in msgs))
        self.assertTrue(any('fire-and-forget' in m for m in msgs))
        self.assertTrue(any('1m 00s' in m for m in msgs))

    @mock.patch('styx.orchestrate.log')
    def test_no_vms_shows_zero(self, mock_log):
        topo = _topo(k8s_enabled=False, vm_host={})
        _log_runtime_budget(topo, _cfg(), phase=3)
        msgs = [c.args[0] for c in mock_log.call_args_list]
        self.assertTrue(any('0m 00s' in m for m in msgs))


# ── Styx version check ───────────────────────────────────────────────────────

def _ssh_side_effect(reachable_ips=None, version_stdout='0.1.0',
                     version_fail_ips=None):
    """Build a subprocess.run side_effect for SSH + styx --version calls.

    reachable_ips:    set of IPs whose SSH reachability succeeds (default: all)
    version_stdout:   stdout returned by the --version SSH call
    version_fail_ips: set of IPs whose --version call raises CalledProcessError
    """
    version_fail_ips = version_fail_ips or set()

    def handler(cmd, **kwargs):
        r = mock.MagicMock(stdout='', stderr='', returncode=0)
        if cmd[0] != 'ssh':
            return r
        target = cmd[5]  # 'root@IP'
        ip = target.split('@')[1]
        remote_cmd = cmd[6]  # 'exit' or 'python3 ... --version'
        if remote_cmd == 'exit':
            if reachable_ips is not None and ip not in reachable_ips:
                raise subprocess.CalledProcessError(255, 'ssh')
            return r
        # --version call
        if ip in version_fail_ips:
            raise subprocess.CalledProcessError(127, 'ssh')
        r.stdout = version_stdout + '\n'
        return r
    return handler


class TestPreflightStyx(unittest.TestCase):

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_version_match_ok(self, _mock_pyz):
        """Peer returns matching version → no abort."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(version_stdout='0.1.0')):
            preflight(topo, _cfg())  # must not raise

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_version_mismatch_aborts(self, _mock_pyz):
        """Peer returns different version → SystemExit."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(version_stdout='0.0.9')):
            with self.assertRaises(SystemExit):
                preflight(topo, _cfg())

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_not_available_aborts(self, _mock_pyz):
        """SSH command for --version fails → SystemExit."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(
                            version_fail_ips={'10.0.0.2'})):
            with self.assertRaises(SystemExit):
                preflight(topo, _cfg())

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_check_skips_orchestrator(self, _mock_pyz):
        """Orchestrator is never version-checked via SSH."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(
                            version_stdout='0.1.0')) as m:
            preflight(topo, _cfg())
        # Only pve2's IP should appear in --version calls, not pve1's
        version_targets = [
            c.args[0][5] for c in m.call_args_list
            if c.args and c.args[0][0] == 'ssh' and '--version' in c.args[0][6]
        ]
        self.assertNotIn('root@10.0.0.1', version_targets)
        self.assertIn('root@10.0.0.2', version_targets)

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_check_skips_unreachable_hosts(self, _mock_pyz):
        """SSH-failed hosts are skipped for version check but count as failures."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2',
                               'pve3': '10.0.0.3'},
                     orchestrator='pve1')
        # pve3 unreachable, pve2 reachable with matching version
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(
                            reachable_ips={'10.0.0.2'},
                            version_stdout='0.1.0')):
            with self.assertRaises(SystemExit):
                preflight(topo, _cfg())

    @mock.patch('styx.orchestrate._local_pyz', return_value=None)
    def test_styx_check_skipped_when_not_zipapp(self, _mock_pyz):
        """_local_pyz() returns None → no version check at all."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run') as m:
            preflight(topo, _cfg())  # must not raise
        # No --version SSH calls should have been made
        version_calls = [
            c for c in m.call_args_list
            if c.args and c.args[0][0] == 'ssh' and '--version' in c.args[0][-1]
        ]
        self.assertEqual(version_calls, [])

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_ssh_failure_still_aborts_due_to_unreachable(self, _mock_pyz):
        """An SSH-unreachable host triggers abort even though styx check
        was skipped for it (counted via unreachable_count)."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        # pve2 is unreachable at SSH level
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(reachable_ips=set())):
            with self.assertRaises(SystemExit):
                preflight(topo, _cfg())


if __name__ == '__main__':
    unittest.main()
