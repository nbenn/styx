"""Unit tests for styx.discover"""

import json
import unittest
from pathlib import Path

from styx.discover import (
    parse_cluster_status, parse_cluster_resources,
    match_nodes_to_vms,
)

_FIXTURES = Path(__file__).parent.parent / 'fixtures'


def _pvesh(name):
    return json.loads((_FIXTURES / 'pvesh' / name).read_text())


class TestParseClusterStatus(unittest.TestCase):

    def test_extracts_hosts_and_ips(self):
        data = [
            {'type': 'node', 'name': 'pve1', 'ip': '10.0.0.1', 'local': 0},
            {'type': 'node', 'name': 'pve2', 'ip': '10.0.0.2', 'local': 0},
        ]
        host_ips, _ = parse_cluster_status(data)
        self.assertEqual(host_ips['pve1'], '10.0.0.1')
        self.assertEqual(host_ips['pve2'], '10.0.0.2')

    def test_identifies_local_node_as_orchestrator(self):
        data = [
            {'type': 'node', 'name': 'pve1', 'ip': '10.0.0.1', 'local': 0},
            {'type': 'node', 'name': 'pve2', 'ip': '10.0.0.2', 'local': 1},
        ]
        _, orchestrator = parse_cluster_status(data)
        self.assertEqual(orchestrator, 'pve2')

    def test_ignores_non_node_entries(self):
        data = [
            {'type': 'cluster', 'name': 'mycluster'},
            {'type': 'node', 'name': 'pve1', 'ip': '10.0.0.1', 'local': 1},
        ]
        host_ips, _ = parse_cluster_status(data)
        self.assertNotIn('mycluster', host_ips)
        self.assertIn('pve1', host_ips)


class TestParseClusterResources(unittest.TestCase):

    def _vm(self, vmid, name, node, status='running', template=0):
        return {'type': 'qemu', 'vmid': vmid, 'name': name,
                'node': node, 'status': status, 'template': template}

    def test_extracts_running_vms(self):
        data = [self._vm(101, 'web', 'pve1'), self._vm(102, 'db', 'pve2')]
        vm_host, vm_name, vm_type = parse_cluster_resources(data)
        self.assertEqual(vm_host['101'], 'pve1')
        self.assertEqual(vm_name['102'], 'db')

    def test_vm_type_is_qemu(self):
        data = [self._vm(101, 'web', 'pve1'), self._vm(102, 'db', 'pve2')]
        _, _, vm_type = parse_cluster_resources(data)
        self.assertEqual(vm_type['101'], 'qemu')
        self.assertEqual(vm_type['102'], 'qemu')

    def test_excludes_stopped_vms(self):
        data = [self._vm(101, 'web', 'pve1', status='stopped')]
        vm_host, _, _ = parse_cluster_resources(data)
        self.assertNotIn('101', vm_host)

    def test_excludes_templates(self):
        data = [self._vm(101, 'tmpl', 'pve1', template=1)]
        vm_host, _, _ = parse_cluster_resources(data)
        self.assertNotIn('101', vm_host)

    def test_excludes_lxc_containers(self):
        data = [{'type': 'lxc', 'vmid': 200, 'name': 'ct',
                 'node': 'pve1', 'status': 'running', 'template': 0}]
        vm_host, _, _ = parse_cluster_resources(data)
        self.assertNotIn('200', vm_host)


class TestMatchNodesToVms(unittest.TestCase):

    def test_matches_workers_and_cp(self):
        vm_name    = {'211': 'worker1', '201': 'cp1', '101': 'infra'}
        node_roles = [('worker1', 'worker'), ('cp1', 'control-plane')]
        workers, cp = match_nodes_to_vms(vm_name, node_roles)
        self.assertIn('211', workers)
        self.assertIn('201', cp)
        self.assertNotIn('101', workers)
        self.assertNotIn('101', cp)

    def test_raises_when_no_match(self):
        vm_name    = {'101': 'infra'}
        node_roles = [('worker1', 'worker')]
        with self.assertRaises(ValueError):
            match_nodes_to_vms(vm_name, node_roles)

    def test_partial_match_succeeds(self):
        vm_name    = {'211': 'worker1', '999': 'unrelated'}
        node_roles = [('worker1', 'worker')]
        workers, cp = match_nodes_to_vms(vm_name, node_roles)
        self.assertIn('211', workers)


# ── fixture-based tests ───────────────────────────────────────────────────────

class TestClusterStatusFixture(unittest.TestCase):
    """parse_cluster_status against realistic pvesh /cluster/status output."""

    def setUp(self):
        self.data = _pvesh('cluster_status.json')

    def test_all_hosts_extracted(self):
        host_ips, _ = parse_cluster_status(self.data)
        self.assertEqual(set(host_ips), {'pve1', 'pve2', 'pve3'})

    def test_ips_correct(self):
        host_ips, _ = parse_cluster_status(self.data)
        self.assertEqual(host_ips['pve1'], '10.0.1.1')
        self.assertEqual(host_ips['pve3'], '10.0.1.3')

    def test_orchestrator_is_local_node(self):
        _, orchestrator = parse_cluster_status(self.data)
        self.assertEqual(orchestrator, 'pve1')

    def test_cluster_entry_not_in_hosts(self):
        host_ips, _ = parse_cluster_status(self.data)
        self.assertNotIn('proxmox', host_ips)


class TestClusterStatusOfflineNodeFixture(unittest.TestCase):
    """Offline node (online=0) must still appear in host_ips.

    The polling loop needs to track it; SSH failure is handled gracefully.
    """

    def test_offline_node_still_in_host_ips(self):
        data = _pvesh('cluster_status_offline_node.json')
        host_ips, _ = parse_cluster_status(data)
        self.assertIn('pve3', host_ips)
        self.assertEqual(len(host_ips), 3)


class TestClusterResourcesFixture(unittest.TestCase):
    """parse_cluster_resources against realistic pvesh /cluster/resources output."""

    def setUp(self):
        data = _pvesh('cluster_resources.json')
        self.vm_host, self.vm_name, self.vm_type = parse_cluster_resources(data)

    def test_running_qemu_vms_included(self):
        for vmid in ('101', '102', '201', '211', '212', '213'):
            self.assertIn(vmid, self.vm_host)

    def test_stopped_vm_excluded(self):
        self.assertNotIn('104', self.vm_host)

    def test_template_excluded(self):
        self.assertNotIn('900', self.vm_host)

    def test_lxc_excluded(self):
        self.assertNotIn('300', self.vm_host)

    def test_host_assignments_correct(self):
        self.assertEqual(self.vm_host['201'], 'pve1')
        self.assertEqual(self.vm_host['211'], 'pve2')
        self.assertEqual(self.vm_host['212'], 'pve2')
        self.assertEqual(self.vm_host['213'], 'pve3')

    def test_names_correct(self):
        self.assertEqual(self.vm_name['201'], 'k8s-cp-1')
        self.assertEqual(self.vm_name['211'], 'k8s-worker-1')

    def test_all_types_are_qemu(self):
        for vmid in self.vm_host:
            self.assertEqual(self.vm_type[vmid], 'qemu')


class TestClusterResourcesMigrationFixture(unittest.TestCase):
    """VM with lock=migrate is still 'running' and must be included.

    The Known Limitations section advises against running styx during
    migration; if it happens anyway we should still attempt shutdown.
    """

    def test_migrating_vm_included(self):
        data = _pvesh('cluster_resources_migration.json')
        vm_host, _, _ = parse_cluster_resources(data)
        self.assertIn('212', vm_host)

    def test_migrating_vm_node_is_current_location(self):
        data = _pvesh('cluster_resources_migration.json')
        vm_host, _, _ = parse_cluster_resources(data)
        # VM 212 migrated to pve3 — shutdown command must go to its new host
        self.assertEqual(vm_host['212'], 'pve3')


if __name__ == '__main__':
    unittest.main()
