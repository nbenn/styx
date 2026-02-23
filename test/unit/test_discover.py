"""Unit tests for styx.discover"""

import unittest

from styx.discover import (
    parse_cluster_status, parse_cluster_resources,
    classify_by_tags, match_nodes_to_vms,
    WORKER_TAG, CP_TAG,
)


class TestParseClusterStatus(unittest.TestCase):

    def _data(self, nodes):
        return nodes

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

    def _vm(self, vmid, name, node, status='running', template=0, tags=''):
        return {'type': 'qemu', 'vmid': vmid, 'name': name,
                'node': node, 'status': status, 'template': template, 'tags': tags}

    def test_extracts_running_vms(self):
        data = [self._vm(101, 'web', 'pve1'), self._vm(102, 'db', 'pve2')]
        vm_host, vm_name, _ = parse_cluster_resources(data)
        self.assertEqual(vm_host['101'], 'pve1')
        self.assertEqual(vm_name['102'], 'db')

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

    def test_parses_semicolon_separated_tags(self):
        data = [self._vm(101, 'k8s', 'pve1', tags='styx:k8s-worker;production')]
        _, _, vm_tags = parse_cluster_resources(data)
        self.assertIn('styx:k8s-worker', vm_tags['101'])
        self.assertIn('production', vm_tags['101'])


class TestClassifyByTags(unittest.TestCase):

    def test_identifies_workers(self):
        vm_tags = {'101': [WORKER_TAG], '102': ['other']}
        workers, cp = classify_by_tags(vm_tags)
        self.assertIn('101', workers)
        self.assertNotIn('101', cp)

    def test_identifies_cp(self):
        vm_tags = {'201': [CP_TAG]}
        workers, cp = classify_by_tags(vm_tags)
        self.assertIn('201', cp)
        self.assertNotIn('201', workers)

    def test_untagged_vms_not_included(self):
        vm_tags = {'301': ['unrelated']}
        workers, cp = classify_by_tags(vm_tags)
        self.assertNotIn('301', workers)
        self.assertNotIn('301', cp)


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


if __name__ == '__main__':
    unittest.main()
