"""Unit tests for orchestrate._apply_hosts_filter."""

import unittest

from styx.discover import ClusterTopology
from styx.orchestrate import _apply_hosts_filter


def _topo():
    t = ClusterTopology()
    t.host_ips     = {'pve1': '10.0.0.1', 'pve2': '10.0.0.2', 'pve3': '10.0.0.3'}
    t.orchestrator = 'pve1'
    t.vm_host      = {'101': 'pve1', '201': 'pve2', '301': 'pve3', '302': 'pve3'}
    t.vm_name      = {'101': 'pve1-vm', '201': 'k8s-worker-1',
                      '301': 'k8s-cp-1', '302': 'pve3-vm'}
    t.vm_type      = {'101': 'qemu', '201': 'qemu', '301': 'qemu', '302': 'qemu'}
    t.k8s_workers  = ['201']
    t.k8s_cp       = ['301']
    t.k8s_enabled  = True
    return t


class TestApplyHostsFilter(unittest.TestCase):

    # ── host_ips ──────────────────────────────────────────────────────────────

    def test_targeted_host_in_host_ips(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertIn('pve3', t.host_ips)

    def test_orchestrator_always_in_host_ips(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertIn('pve1', t.host_ips)   # pve1 is orchestrator

    def test_non_targeted_peer_removed_from_host_ips(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertNotIn('pve2', t.host_ips)

    def test_multiple_targeted_hosts_all_in_host_ips(self):
        t = _apply_hosts_filter(_topo(), ['pve2', 'pve3'])
        self.assertIn('pve2', t.host_ips)
        self.assertIn('pve3', t.host_ips)
        self.assertIn('pve1', t.host_ips)   # orchestrator

    # ── vm_host ───────────────────────────────────────────────────────────────

    def test_vms_on_targeted_host_in_vm_host(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertIn('301', t.vm_host)
        self.assertIn('302', t.vm_host)

    def test_vms_on_non_targeted_peer_removed(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertNotIn('201', t.vm_host)

    def test_orchestrator_vms_excluded_when_not_targeted(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        # pve1 (orchestrator) not in --hosts → its VMs not scheduled for shutdown
        self.assertNotIn('101', t.vm_host)

    def test_orchestrator_vms_included_when_explicitly_targeted(self):
        t = _apply_hosts_filter(_topo(), ['pve1', 'pve3'])
        self.assertIn('101', t.vm_host)

    # ── vm_name ───────────────────────────────────────────────────────────────

    def test_vm_name_kept_for_targeted_vms(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertIn('301', t.vm_name)
        self.assertIn('302', t.vm_name)

    def test_vm_name_removed_for_non_targeted_vms(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertNotIn('201', t.vm_name)
        self.assertNotIn('101', t.vm_name)

    # ── k8s lists ─────────────────────────────────────────────────────────────

    def test_k8s_worker_on_non_targeted_host_removed(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertNotIn('201', t.k8s_workers)   # pve2's worker

    def test_k8s_cp_on_targeted_host_kept(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertIn('301', t.k8s_cp)   # pve3's cp node

    def test_k8s_enabled_when_k8s_vms_remain(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertTrue(t.k8s_enabled)

    def test_k8s_disabled_when_no_k8s_vms_remain(self):
        # pve2 has worker, pve3 has cp — targeting only pve1 leaves no k8s VMs
        t = _apply_hosts_filter(_topo(), ['pve1'])
        self.assertFalse(t.k8s_enabled)
        self.assertEqual(t.k8s_workers, [])
        self.assertEqual(t.k8s_cp, [])

    # ── vm_type ─────────────────────────────────────────────────────────────

    def test_vm_type_filtered_to_targeted_hosts(self):
        t = _apply_hosts_filter(_topo(), ['pve3'])
        self.assertIn('301', t.vm_type)
        self.assertIn('302', t.vm_type)
        self.assertNotIn('201', t.vm_type)
        self.assertNotIn('101', t.vm_type)

    # ── edge cases ────────────────────────────────────────────────────────────

    def test_unknown_host_does_not_raise(self):
        t = _apply_hosts_filter(_topo(), ['pve99'])
        # pve99 unknown — orchestrator kept, no VMs scheduled
        self.assertIn('pve1', t.host_ips)
        self.assertNotIn('pve99', t.host_ips)
        self.assertEqual(t.vm_host, {})

    def test_returns_same_topo_object(self):
        orig = _topo()
        result = _apply_hosts_filter(orig, ['pve3'])
        self.assertIs(result, orig)


if __name__ == '__main__':
    unittest.main()
