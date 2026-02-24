"""Unit tests for orchestrate.preflight()."""

import subprocess
import sys
import unittest
from unittest import mock

from styx.config import StyxConfig
from styx.discover import ClusterTopology
from styx.orchestrate import preflight, _log_runtime_budget
from styx.policy import Policy, DryRunPolicy


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


def _pvecm_calls(mock_run):
    return [c for c in mock_run.call_args_list if c.args and c.args[0][0] == 'pvecm']


def _subprocess_default(cmd, **kwargs):
    """Default side_effect: SSH OK, ceph HEALTH_OK, pvecm quorate."""
    r = mock.MagicMock(stdout='', stderr='', returncode=0)
    if cmd[0] == 'ceph':
        r.stdout = 'HEALTH_OK'
    elif cmd[0] == 'pvecm':
        r.stdout = 'Quorate: Yes\n'
    return r


# ── SSH ───────────────────────────────────────────────────────────────────────

class TestPreflightSSH(unittest.TestCase):

    def test_orchestrator_not_sshed(self):
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default) as m:
            preflight(_topo(), _cfg(), Policy())
        self.assertNotIn('root@10.0.0.1', _ssh_targets(m))

    def test_non_orchestrator_hosts_are_sshed(self):
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default) as m:
            preflight(_topo(), _cfg(), Policy())
        targets = _ssh_targets(m)
        self.assertIn('root@10.0.0.2', targets)
        self.assertIn('root@10.0.0.3', targets)

    def test_ssh_uses_correct_options(self):
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default) as m:
            preflight(_topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                           orchestrator='pve1'), _cfg(), Policy())
        ssh_cmd = next(
            c.args[0] for c in m.call_args_list if c.args[0][0] == 'ssh'
        )
        self.assertIn('ConnectTimeout=5', ssh_cmd)
        self.assertIn('BatchMode=yes', ssh_cmd)
        self.assertEqual(ssh_cmd[-1], 'exit')

    def test_ssh_failure_does_not_raise_emergency(self):
        """SSH failure in emergency mode warns but does not raise."""
        err = subprocess.CalledProcessError(255, 'ssh')
        def _run(cmd, **kwargs):
            if cmd[0] == 'ssh':
                raise err
            return _subprocess_default(cmd, **kwargs)
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(), _cfg(), Policy())   # must not raise

    def test_ssh_timeout_does_not_raise_emergency(self):
        err = subprocess.TimeoutExpired('ssh', 10)
        def _run(cmd, **kwargs):
            if cmd[0] == 'ssh':
                raise err
            return _subprocess_default(cmd, **kwargs)
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(), _cfg(), Policy())   # must not raise

    def test_ssh_unreachable_fatal_in_dryrun(self):
        """SSH failure in dry-run mode is fatal."""
        err = subprocess.CalledProcessError(255, 'ssh')
        def _run(cmd, **kwargs):
            if cmd[0] == 'ssh':
                raise err
            return _subprocess_default(cmd, **kwargs)
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            with self.assertRaises(SystemExit):
                preflight(_topo(), _cfg(), DryRunPolicy())

    def test_ssh_unreachable_warning_in_emergency(self):
        """SSH failure in emergency mode warns and continues."""
        err = subprocess.CalledProcessError(255, 'ssh')
        def _run(cmd, **kwargs):
            if cmd[0] == 'ssh':
                raise err
            return _subprocess_default(cmd, **kwargs)
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(), _cfg(), Policy())   # must not raise


# ── Kubernetes ────────────────────────────────────────────────────────────────

class TestPreflightK8s(unittest.TestCase):

    def test_k8s_skipped_when_disabled(self):
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client') as mk:
                preflight(_topo(k8s_enabled=False),
                          _cfg(k8s_server='https://k8s', k8s_token='/tok'), Policy())
        mk.assert_not_called()

    def test_k8s_skipped_when_no_credentials(self):
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client') as mk:
                preflight(_topo(k8s_enabled=True), _cfg(), Policy())
        mk.assert_not_called()

    def test_k8s_skipped_when_server_only(self):
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client') as mk:
                preflight(_topo(k8s_enabled=True),
                          _cfg(k8s_server='https://k8s'), Policy())
        mk.assert_not_called()

    def test_k8s_api_called_when_enabled_with_credentials(self):
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': [
            {'metadata': {'name': 'n1'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
            {'metadata': {'name': 'n2'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
        ]}
        fake_k8s.list_pods_on_node.return_value = {'items': []}
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'])
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'), Policy())
        fake_k8s.list_nodes.assert_called_once()

    def test_pod_count_checked_for_each_k8s_node(self):
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': []}
        fake_k8s.list_pods_on_node.return_value = {'items': []}
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'])
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'), Policy())
        # one call per node: worker + cp = 2
        self.assertEqual(fake_k8s.list_pods_on_node.call_count, 2)
        nodes_checked = {c.args[0] for c in fake_k8s.list_pods_on_node.call_args_list}
        self.assertEqual(nodes_checked, {'k8s-worker-1', 'k8s-cp-1'})

    def test_k8s_api_unreachable_does_not_raise_emergency(self):
        topo = _topo(k8s_enabled=True)
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client',
                            side_effect=ConnectionError('refused')):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'), Policy())

    def test_pod_list_failure_does_not_raise(self):
        """Exception from list_pods_on_node (inner try) is silently caught."""
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': [
            {'metadata': {'name': 'n1'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
        ]}
        fake_k8s.list_pods_on_node.side_effect = TimeoutError('pods')
        topo = _topo(k8s_enabled=True, k8s_workers=['211'])
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'), Policy())

    def test_k8s_not_ready_fatal_in_dryrun(self):
        """NotReady node in dry-run mode is fatal."""
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': [
            {'metadata': {'name': 'node1'}, 'status': {'conditions': [
                {'type': 'Ready', 'status': 'False'},
            ]}},
        ]}
        fake_k8s.list_pods_on_node.return_value = {'items': []}
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'])
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                with self.assertRaises(SystemExit):
                    preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'),
                              DryRunPolicy())

    def test_k8s_not_ready_warning_in_emergency(self):
        """NotReady node in emergency mode warns and continues."""
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': [
            {'metadata': {'name': 'node1'}, 'status': {'conditions': [
                {'type': 'Ready', 'status': 'False'},
            ]}},
        ]}
        fake_k8s.list_pods_on_node.return_value = {'items': []}
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'])
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'),
                          Policy())  # must not raise

    def test_k8s_api_unreachable_fatal_in_dryrun(self):
        """k8s API unreachable in dry-run mode is fatal."""
        topo = _topo(k8s_enabled=True)
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client',
                            side_effect=ConnectionError('refused')):
                with self.assertRaises(SystemExit):
                    preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'),
                              DryRunPolicy())


# ── Ceph ──────────────────────────────────────────────────────────────────────

class TestPreflightCeph(unittest.TestCase):

    def test_ceph_skipped_when_disabled(self):
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default) as m:
            preflight(_topo(ceph_enabled=False), _cfg(), Policy())
        self.assertEqual(_ceph_calls(m), [])

    def test_ceph_health_called_when_enabled(self):
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default) as m:
            preflight(_topo(ceph_enabled=True), _cfg(), Policy())
        calls = _ceph_calls(m)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0][:2], ['ceph', 'health'])

    def test_ceph_unavailable_does_not_raise_emergency(self):
        def _run(cmd, **kwargs):
            if cmd[0] == 'ceph':
                raise FileNotFoundError('ceph not found')
            return _subprocess_default(cmd, **kwargs)

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(ceph_enabled=True), _cfg(), Policy())   # must not raise

    def test_ceph_not_healthy_fatal_in_dryrun(self):
        """Ceph HEALTH_WARN in dry-run mode is fatal."""
        def _run(cmd, **kwargs):
            r = mock.MagicMock(stdout='', stderr='', returncode=0)
            if cmd[0] == 'ceph':
                r.stdout = 'HEALTH_WARN too few PGs per OSD'
            elif cmd[0] == 'pvecm':
                r.stdout = 'Quorate: Yes\n'
            return r

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            with self.assertRaises(SystemExit):
                preflight(_topo(ceph_enabled=True), _cfg(), DryRunPolicy())

    def test_ceph_not_healthy_warning_in_emergency(self):
        """Ceph HEALTH_WARN in emergency mode warns and continues."""
        def _run(cmd, **kwargs):
            r = mock.MagicMock(stdout='', stderr='', returncode=0)
            if cmd[0] == 'ceph':
                r.stdout = 'HEALTH_WARN too few PGs per OSD'
            elif cmd[0] == 'pvecm':
                r.stdout = 'Quorate: Yes\n'
            return r

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(ceph_enabled=True), _cfg(), Policy())   # must not raise

    def test_ceph_unavailable_fatal_in_dryrun(self):
        """Ceph command failure in dry-run mode is fatal."""
        def _run(cmd, **kwargs):
            if cmd[0] == 'ceph':
                raise FileNotFoundError('ceph not found')
            return _subprocess_default(cmd, **kwargs)

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            with self.assertRaises(SystemExit):
                preflight(_topo(ceph_enabled=True), _cfg(), DryRunPolicy())


# ── Quorum ────────────────────────────────────────────────────────────────────

class TestPreflightQuorum(unittest.TestCase):

    def test_quorum_ok_no_failure(self):
        """pvecm status with Quorate: Yes → no failure."""
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            preflight(_topo(), _cfg(), Policy())   # must not raise

    def test_quorum_lost_fatal_in_dryrun(self):
        """Quorate: No in dry-run mode is fatal."""
        def _run(cmd, **kwargs):
            r = mock.MagicMock(stdout='', stderr='', returncode=0)
            if cmd[0] == 'pvecm':
                r.stdout = 'Quorate: No\n'
            return r

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            with self.assertRaises(SystemExit):
                preflight(_topo(), _cfg(), DryRunPolicy())

    def test_quorum_lost_warning_in_emergency(self):
        """Quorate: No in emergency mode warns and continues."""
        def _run(cmd, **kwargs):
            r = mock.MagicMock(stdout='', stderr='', returncode=0)
            if cmd[0] == 'pvecm':
                r.stdout = 'Quorate: No\n'
            return r

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(), _cfg(), Policy())   # must not raise

    def test_quorum_check_unavailable_fatal_in_dryrun(self):
        """pvecm command failure in dry-run mode is fatal."""
        def _run(cmd, **kwargs):
            if cmd[0] == 'pvecm':
                raise FileNotFoundError('pvecm not found')
            return _subprocess_default(cmd, **kwargs)

        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            with self.assertRaises(SystemExit):
                preflight(_topo(), _cfg(), DryRunPolicy())


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
        if cmd[0] == 'pvecm':
            r.stdout = 'Quorate: Yes\n'
            return r
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
            preflight(topo, _cfg(), DryRunPolicy())  # must not raise

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_version_mismatch_aborts(self, _mock_pyz):
        """Peer returns different version → SystemExit."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(version_stdout='0.0.9')):
            with self.assertRaises(SystemExit):
                preflight(topo, _cfg(), DryRunPolicy())

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
                preflight(topo, _cfg(), DryRunPolicy())

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_check_skips_orchestrator(self, _mock_pyz):
        """Orchestrator is never version-checked via SSH."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(
                            version_stdout='0.1.0')) as m:
            preflight(topo, _cfg(), DryRunPolicy())
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
                preflight(topo, _cfg(), DryRunPolicy())

    @mock.patch('styx.orchestrate._local_pyz', return_value=None)
    def test_styx_check_skipped_when_not_zipapp(self, _mock_pyz):
        """_local_pyz() returns None → no version check at all."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default) as m:
            preflight(topo, _cfg(), Policy())  # must not raise
        # No --version SSH calls should have been made
        version_calls = [
            c for c in m.call_args_list
            if c.args and c.args[0][0] == 'ssh' and '--version' in c.args[0][-1]
        ]
        self.assertEqual(version_calls, [])

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_ssh_failure_still_aborts_due_to_unreachable(self, _mock_pyz):
        """An SSH-unreachable host triggers abort (counted as SSH unreachable)."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        # pve2 is unreachable at SSH level
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(reachable_ips=set())):
            with self.assertRaises(SystemExit):
                preflight(topo, _cfg(), DryRunPolicy())

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_version_mismatch_warning_in_emergency(self, _mock_pyz):
        """Version mismatch in emergency mode warns and continues."""
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(version_stdout='0.0.9')):
            preflight(topo, _cfg(), Policy())  # must not raise


# ── Emergency mode: failures detected but never abort ─────────────────────────

class _SpyPolicy(Policy):
    """Policy subclass that records on_preflight_failure calls."""
    def __init__(self):
        self.preflight_failure_calls = []

    def on_preflight_failure(self, msg):
        self.preflight_failure_calls.append(msg)


class TestPreflightEmergencyNeverAborts(unittest.TestCase):
    """Verify that every failure type is detected (on_preflight_failure called)
    but never causes an abort in emergency mode."""

    def test_ssh_unreachable_detected(self):
        err = subprocess.CalledProcessError(255, 'ssh')
        def _run(cmd, **kwargs):
            if cmd[0] == 'ssh':
                raise err
            return _subprocess_default(cmd, **kwargs)

        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(), _cfg(), spy)
        self.assertEqual(len(spy.preflight_failure_calls), 1)
        self.assertIn('SSH unreachable', spy.preflight_failure_calls[0])

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_styx_version_mismatch_detected(self, _mock_pyz):
        topo = _topo(host_ips={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                     orchestrator='pve1')
        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run',
                        side_effect=_ssh_side_effect(version_stdout='0.0.9')):
            preflight(topo, _cfg(), spy)
        self.assertEqual(len(spy.preflight_failure_calls), 1)
        self.assertIn('version mismatch', spy.preflight_failure_calls[0])

    def test_k8s_not_ready_detected(self):
        fake_k8s = mock.MagicMock()
        fake_k8s.list_nodes.return_value = {'items': [
            {'metadata': {'name': 'node1'}, 'status': {'conditions': [
                {'type': 'Ready', 'status': 'False'},
            ]}},
        ]}
        fake_k8s.list_pods_on_node.return_value = {'items': []}
        topo = _topo(k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'])
        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'), spy)
        self.assertEqual(len(spy.preflight_failure_calls), 1)
        self.assertIn('NotReady', spy.preflight_failure_calls[0])

    def test_k8s_api_unreachable_detected(self):
        topo = _topo(k8s_enabled=True)
        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            with mock.patch('styx.orchestrate._make_k8s_client',
                            side_effect=ConnectionError('refused')):
                preflight(topo, _cfg(k8s_server='https://k8s', k8s_token='/tok'), spy)
        self.assertEqual(len(spy.preflight_failure_calls), 1)
        self.assertIn('k8s API unreachable', spy.preflight_failure_calls[0])

    def test_ceph_not_healthy_detected(self):
        def _run(cmd, **kwargs):
            r = mock.MagicMock(stdout='', stderr='', returncode=0)
            if cmd[0] == 'ceph':
                r.stdout = 'HEALTH_WARN too few PGs per OSD'
            elif cmd[0] == 'pvecm':
                r.stdout = 'Quorate: Yes\n'
            return r

        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(ceph_enabled=True), _cfg(), spy)
        self.assertEqual(len(spy.preflight_failure_calls), 1)
        self.assertIn('Ceph not healthy', spy.preflight_failure_calls[0])

    def test_ceph_unavailable_detected(self):
        def _run(cmd, **kwargs):
            if cmd[0] == 'ceph':
                raise FileNotFoundError('ceph not found')
            return _subprocess_default(cmd, **kwargs)

        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(ceph_enabled=True), _cfg(), spy)
        self.assertEqual(len(spy.preflight_failure_calls), 1)
        self.assertIn('Ceph unavailable', spy.preflight_failure_calls[0])

    def test_quorum_lost_detected(self):
        def _run(cmd, **kwargs):
            r = mock.MagicMock(stdout='', stderr='', returncode=0)
            if cmd[0] == 'pvecm':
                r.stdout = 'Quorate: No\n'
            return r

        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(), _cfg(), spy)
        self.assertEqual(len(spy.preflight_failure_calls), 1)
        self.assertIn('Quorum lost', spy.preflight_failure_calls[0])

    def test_quorum_unavailable_detected(self):
        def _run(cmd, **kwargs):
            if cmd[0] == 'pvecm':
                raise FileNotFoundError('pvecm not found')
            return _subprocess_default(cmd, **kwargs)

        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            preflight(_topo(), _cfg(), spy)
        self.assertEqual(len(spy.preflight_failure_calls), 1)
        self.assertIn('Quorum check failed', spy.preflight_failure_calls[0])

    def test_no_failures_means_no_call(self):
        """When everything is healthy, on_preflight_failure is never called."""
        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_subprocess_default):
            preflight(_topo(), _cfg(), spy)
        self.assertEqual(spy.preflight_failure_calls, [])

    @mock.patch('styx.orchestrate._local_pyz', return_value='/opt/styx/styx.pyz')
    @mock.patch('styx.orchestrate.__version__', '0.1.0')
    def test_everything_fails_never_aborts(self, _mock_pyz):
        """Every preflight check fails simultaneously — emergency mode must
        still never abort.  This is the structural guard: if any code path
        sneaks in a direct sys.exit(), this test catches it."""
        def _run(cmd, **kwargs):
            if cmd[0] == 'ssh':
                raise subprocess.CalledProcessError(255, 'ssh')
            if cmd[0] == 'ceph':
                raise FileNotFoundError('ceph not found')
            if cmd[0] == 'pvecm':
                raise FileNotFoundError('pvecm not found')
            return mock.MagicMock(stdout='', stderr='', returncode=0)

        topo = _topo(
            k8s_enabled=True, k8s_workers=['211'], k8s_cp=['201'],
            ceph_enabled=True,
        )
        cfg = _cfg(k8s_server='https://k8s', k8s_token='/tok')
        spy = _SpyPolicy()
        with mock.patch('styx.orchestrate.subprocess.run', side_effect=_run):
            with mock.patch('styx.orchestrate._make_k8s_client',
                            side_effect=ConnectionError('refused')):
                preflight(topo, cfg, spy)       # ← MUST NOT raise

        self.assertEqual(len(spy.preflight_failure_calls), 1)
        msg = spy.preflight_failure_calls[0]
        # Every failure category must be present
        self.assertIn('SSH unreachable', msg)
        self.assertIn('k8s API unreachable', msg)
        self.assertIn('Ceph unavailable', msg)
        self.assertIn('Quorum check failed', msg)
        # styx version check is skipped for unreachable hosts (no SSH),
        # but unreachable hosts themselves are already counted


if __name__ == '__main__':
    unittest.main()
