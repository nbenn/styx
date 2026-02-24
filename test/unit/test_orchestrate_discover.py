"""Unit tests for orchestrate.discover()."""

import subprocess
import unittest
from unittest import mock

from styx.config import StyxConfig
from styx.orchestrate import discover

# ── shared test data ──────────────────────────────────────────────────────────

_CLUSTER_STATUS = [
    {'type': 'cluster', 'name': 'mycluster'},
    {'type': 'node', 'name': 'pve1', 'ip': '10.0.0.1', 'local': 1},
    {'type': 'node', 'name': 'pve2', 'ip': '10.0.0.2', 'local': 0},
    {'type': 'node', 'name': 'pve3', 'ip': '10.0.0.3', 'local': 0},
]

_CLUSTER_RESOURCES = [
    {'type': 'qemu', 'vmid': 101, 'name': 'infra',         'node': 'pve1', 'status': 'running', 'template': 0},
    {'type': 'qemu', 'vmid': 201, 'name': 'k8s-cp-1',      'node': 'pve1', 'status': 'running', 'template': 0},
    {'type': 'qemu', 'vmid': 211, 'name': 'k8s-worker-1',  'node': 'pve2', 'status': 'running', 'template': 0},
    {'type': 'qemu', 'vmid': 212, 'name': 'k8s-worker-2',  'node': 'pve3', 'status': 'running', 'template': 0},
]


def _pvesh(path, *args):
    if path == '/cluster/status':
        return _CLUSTER_STATUS
    if path == '/cluster/resources':
        return _CLUSTER_RESOURCES
    raise ValueError(f'Unexpected pvesh call: {path!r}')


def _cfg(**kwargs):
    """Return a StyxConfig with optional field overrides."""
    cfg = StyxConfig()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


# ── hosts ─────────────────────────────────────────────────────────────────────

class TestDiscoverHosts(unittest.TestCase):

    def test_hosts_from_pvesh(self):
        topo = discover(_cfg(), _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertEqual(set(topo.host_ips.keys()), {'pve1', 'pve2', 'pve3'})
        self.assertEqual(topo.host_ips['pve2'], '10.0.0.2')

    def test_hosts_from_config_override_skips_pvesh_status(self):
        """pvesh('/cluster/status') is never called when config.hosts is set."""
        calls = []
        def pvesh(path, *args):
            calls.append(path)
            return _pvesh(path, *args)

        cfg = _cfg(hosts={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, orchestrator='pve1')
        topo = discover(cfg, _pvesh_fn=pvesh, _pveceph_fn=lambda: False)
        self.assertEqual(topo.host_ips, {'pve1': '10.0.0.1', 'pve2': '10.0.0.2'})
        self.assertNotIn('/cluster/status', calls)

    def test_orchestrator_detected_from_pvesh(self):
        """Local node (local=1) becomes orchestrator when no config override."""
        topo = discover(_cfg(), _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertEqual(topo.orchestrator, 'pve1')

    def test_orchestrator_config_override_replaces_pvesh(self):
        """config.orchestrator overrides whatever pvesh identifies as local."""
        topo = discover(_cfg(orchestrator='pve3'), _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertEqual(topo.orchestrator, 'pve3')

    def test_orchestrator_config_override_with_host_config(self):
        """config.orchestrator also overrides when hosts come from config."""
        cfg = _cfg(hosts={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}, orchestrator='pve2')
        topo = discover(cfg, _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertEqual(topo.orchestrator, 'pve2')

    def test_orchestrator_falls_back_to_hostname(self):
        """When hosts from config and no orchestrator, gethostname() is used."""
        cfg = _cfg(hosts={'pve1': '10.0.0.1'})
        with mock.patch('socket.gethostname', return_value='pve1.example.com'):
            topo = discover(cfg, _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertEqual(topo.orchestrator, 'pve1')


# ── VMs ───────────────────────────────────────────────────────────────────────

class TestDiscoverVMs(unittest.TestCase):

    def test_vms_populated_from_pvesh(self):
        topo = discover(_cfg(), _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertIn('101', topo.vm_host)
        self.assertEqual(topo.vm_host['201'], 'pve1')
        self.assertEqual(topo.vm_name['211'], 'k8s-worker-1')

    def test_vm_type_populated(self):
        topo = discover(_cfg(), _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        for vmid in topo.vm_host:
            self.assertEqual(topo.vm_type[vmid], 'qemu')


# ── Kubernetes ────────────────────────────────────────────────────────────────

class TestDiscoverKubernetes(unittest.TestCase):

    def test_k8s_config_override_workers_and_cp(self):
        """workers + control_plane in config → enabled, no API call."""
        cfg = _cfg(workers=['211', '212'], control_plane=['201'])
        with mock.patch('styx.orchestrate._make_k8s_client') as mk:
            topo = discover(cfg, _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertTrue(topo.k8s_enabled)
        self.assertEqual(topo.k8s_workers, ['211', '212'])
        self.assertEqual(topo.k8s_cp, ['201'])
        mk.assert_not_called()

    def test_k8s_config_override_workers_only(self):
        """Setting only workers is enough to activate k8s."""
        cfg = _cfg(workers=['211'])
        topo = discover(cfg, _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertTrue(topo.k8s_enabled)
        self.assertEqual(topo.k8s_workers, ['211'])
        self.assertEqual(topo.k8s_cp, [])

    def test_k8s_api_discovery_success(self):
        """k8s_server + k8s_token → roles fetched via API, VMIDs matched."""
        fake_k8s = mock.MagicMock()
        fake_k8s.get_node_roles.return_value = [
            ('k8s-worker-1', 'worker'),
            ('k8s-worker-2', 'worker'),
            ('k8s-cp-1',     'control-plane'),
        ]
        cfg = _cfg(k8s_server='https://k8s.example.com', k8s_token='/var/run/token')
        with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
            topo = discover(cfg, _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertTrue(topo.k8s_enabled)
        self.assertCountEqual(topo.k8s_workers, ['211', '212'])
        self.assertCountEqual(topo.k8s_cp, ['201'])

    def test_k8s_api_unreachable_disables_k8s(self):
        """ConnectionError from _make_k8s_client → k8s_enabled=False, no crash."""
        cfg = _cfg(k8s_server='https://k8s.example.com', k8s_token='/var/run/token')
        with mock.patch('styx.orchestrate._make_k8s_client', side_effect=ConnectionError('refused')):
            topo = discover(cfg, _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertFalse(topo.k8s_enabled)

    def test_k8s_get_node_roles_raises_disables_k8s(self):
        """Exception from get_node_roles → k8s_enabled=False, no crash."""
        fake_k8s = mock.MagicMock()
        fake_k8s.get_node_roles.side_effect = TimeoutError('timed out')
        cfg = _cfg(k8s_server='https://k8s.example.com', k8s_token='/var/run/token')
        with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
            topo = discover(cfg, _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertFalse(topo.k8s_enabled)

    def test_k8s_name_mismatch_calls_on_warning_not_api_unreachable(self):
        """ValueError from match_nodes_to_vms routes to _on_warning, not 'API unreachable'."""
        fake_k8s = mock.MagicMock()
        fake_k8s.get_node_roles.return_value = [('no-such-vm-name', 'worker')]
        cfg = _cfg(k8s_server='https://k8s.example.com', k8s_token='/var/run/token')
        warnings = []
        with mock.patch('styx.orchestrate._make_k8s_client', return_value=fake_k8s):
            topo = discover(cfg, _pvesh_fn=_pvesh, _pveceph_fn=lambda: False,
                            _on_warning=warnings.append)
        self.assertFalse(topo.k8s_enabled)
        self.assertEqual(len(warnings), 1)
        self.assertIn('mismatch', warnings[0].lower())

    def test_k8s_no_credentials_skips_api(self):
        """Neither k8s_server nor k8s_token → k8s_enabled=False, no API call."""
        with mock.patch('styx.orchestrate._make_k8s_client') as mk:
            topo = discover(_cfg(), _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertFalse(topo.k8s_enabled)
        mk.assert_not_called()

    def test_k8s_server_without_token_skips_api(self):
        """k8s_server alone is insufficient — token also required."""
        with mock.patch('styx.orchestrate._make_k8s_client') as mk:
            topo = discover(
                _cfg(k8s_server='https://k8s.example.com'),
                _pvesh_fn=_pvesh, _pveceph_fn=lambda: False,
            )
        self.assertFalse(topo.k8s_enabled)
        mk.assert_not_called()


# ── Ceph ──────────────────────────────────────────────────────────────────────

class TestDiscoverCeph(unittest.TestCase):

    def test_ceph_config_override_true(self):
        """config.ceph_enabled=True → enabled without calling pveceph."""
        pveceph = mock.MagicMock(return_value=False)
        topo = discover(_cfg(ceph_enabled=True), _pvesh_fn=_pvesh, _pveceph_fn=pveceph)
        self.assertTrue(topo.ceph_enabled)
        pveceph.assert_not_called()

    def test_ceph_config_override_false(self):
        """config.ceph_enabled=False → disabled without calling pveceph."""
        pveceph = mock.MagicMock(return_value=True)
        topo = discover(_cfg(ceph_enabled=False), _pvesh_fn=_pvesh, _pveceph_fn=pveceph)
        self.assertFalse(topo.ceph_enabled)
        pveceph.assert_not_called()

    def test_ceph_from_pveceph_true(self):
        """config.ceph_enabled=None → result comes from pveceph()."""
        topo = discover(_cfg(), _pvesh_fn=_pvesh, _pveceph_fn=lambda: True)
        self.assertTrue(topo.ceph_enabled)

    def test_ceph_from_pveceph_false(self):
        topo = discover(_cfg(), _pvesh_fn=_pvesh, _pveceph_fn=lambda: False)
        self.assertFalse(topo.ceph_enabled)


# ── Discovery failure handling ────────────────────────────────────────────────

class TestDiscoverFailures(unittest.TestCase):

    def test_vm_pvesh_failure_sets_empty_maps(self):
        """/cluster/resources raises → empty VM maps + warning."""
        calls = []
        def pvesh(path, *args):
            if path == '/cluster/resources':
                raise subprocess.CalledProcessError(1, 'pvesh')
            return _pvesh(path, *args)

        warnings = []
        topo = discover(_cfg(), _pvesh_fn=pvesh, _pveceph_fn=lambda: False,
                        _on_warning=warnings.append)
        self.assertEqual(topo.vm_host, {})
        self.assertEqual(topo.vm_name, {})
        self.assertEqual(topo.vm_type, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn('VM discovery failed', warnings[0])

    def test_host_pvesh_failure_raises_runtime_error(self):
        """/cluster/status raises → RuntimeError."""
        def pvesh(path, *args):
            if path == '/cluster/status':
                raise subprocess.CalledProcessError(1, 'pvesh')
            return _pvesh(path, *args)

        with self.assertRaises(RuntimeError) as cm:
            discover(_cfg(), _pvesh_fn=pvesh, _pveceph_fn=lambda: False)
        self.assertIn('pvesh /cluster/status', str(cm.exception))

    def test_host_pvesh_not_called_when_config_hosts(self):
        """config.hosts set → pvesh never called for hosts."""
        calls = []
        def pvesh(path, *args):
            calls.append(path)
            return _pvesh(path, *args)

        cfg = _cfg(hosts={'pve1': '10.0.0.1', 'pve2': '10.0.0.2'},
                   orchestrator='pve1')
        topo = discover(cfg, _pvesh_fn=pvesh, _pveceph_fn=lambda: False)
        self.assertNotIn('/cluster/status', calls)
        self.assertEqual(set(topo.host_ips.keys()), {'pve1', 'pve2'})


if __name__ == '__main__':
    unittest.main()
