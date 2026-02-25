"""Unit tests for orchestrate._refresh_vm_topology."""

import unittest

from styx.discover import ClusterTopology
from styx.orchestrate import _refresh_vm_topology


def _stale_topo():
    t = ClusterTopology()
    t.host_ips     = {'pve1': '10.0.0.1', 'pve2': '10.0.0.2'}
    t.orchestrator = 'pve1'
    t.vm_host      = {'100': 'pve1', '200': 'pve2'}
    t.vm_name      = {'100': 'vm-a', '200': 'vm-b'}
    t.vm_type      = {'100': 'qemu', '200': 'qemu'}
    t.vm_lock      = {}
    t.k8s_workers  = ['200']
    t.k8s_cp       = []
    t.k8s_enabled  = True
    t.ceph_enabled = True
    return t


# Simulated fresh API response: VM 200 migrated from pve2 → pve1
_FRESH_RESOURCES = [
    {'type': 'qemu', 'vmid': 100, 'node': 'pve1', 'name': 'vm-a',
     'status': 'running', 'template': 0},
    {'type': 'qemu', 'vmid': 200, 'node': 'pve1', 'name': 'vm-b',
     'status': 'running', 'template': 0},
]

_FRESH_RESOURCES_WITH_LOCK = [
    {'type': 'qemu', 'vmid': 100, 'node': 'pve1', 'name': 'vm-a',
     'status': 'running', 'template': 0},
    {'type': 'qemu', 'vmid': 200, 'node': 'pve1', 'name': 'vm-b',
     'status': 'running', 'template': 0, 'lock': 'backup'},
]


class TestRefreshVmTopology(unittest.TestCase):

    def test_refresh_updates_topo(self):
        topo = _stale_topo()
        pvesh = lambda *a: _FRESH_RESOURCES
        result = _refresh_vm_topology(topo, _pvesh_fn=pvesh)

        self.assertTrue(result)
        # VM 200 should now be on pve1 (migrated)
        self.assertEqual(topo.vm_host['200'], 'pve1')
        self.assertEqual(topo.vm_host['100'], 'pve1')
        self.assertEqual(topo.vm_name['200'], 'vm-b')

    def test_refresh_failure_keeps_stale(self):
        topo = _stale_topo()
        original_host = dict(topo.vm_host)
        original_name = dict(topo.vm_name)

        def failing_pvesh(*a):
            raise RuntimeError('connection refused')

        result = _refresh_vm_topology(topo, _pvesh_fn=failing_pvesh)

        self.assertFalse(result)
        self.assertEqual(topo.vm_host, original_host)
        self.assertEqual(topo.vm_name, original_name)

    def test_refresh_updates_vm_lock(self):
        topo = _stale_topo()
        pvesh = lambda *a: _FRESH_RESOURCES_WITH_LOCK
        result = _refresh_vm_topology(topo, _pvesh_fn=pvesh)

        self.assertTrue(result)
        self.assertEqual(topo.vm_lock, {'200': 'backup'})

    def test_refresh_does_not_touch_stable_fields(self):
        topo = _stale_topo()
        pvesh = lambda *a: _FRESH_RESOURCES
        _refresh_vm_topology(topo, _pvesh_fn=pvesh)

        # These fields must not change
        self.assertEqual(topo.host_ips, {'pve1': '10.0.0.1', 'pve2': '10.0.0.2'})
        self.assertEqual(topo.orchestrator, 'pve1')
        self.assertEqual(topo.k8s_workers, ['200'])
        self.assertTrue(topo.k8s_enabled)
        self.assertTrue(topo.ceph_enabled)


if __name__ == '__main__':
    unittest.main()
