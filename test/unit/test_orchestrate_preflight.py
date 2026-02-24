"""Unit tests for orchestrate.preflight()."""

import subprocess
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


if __name__ == '__main__':
    unittest.main()
