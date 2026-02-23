"""Unit tests for styx.k8s"""

import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from styx.k8s import K8sClient, cmd_reachable, cmd_get_nodes


# ── helpers ───────────────────────────────────────────────────────────────────

def _resp(data):
    m = MagicMock()
    m.read.return_value = json.dumps(data).encode()
    m.__enter__ = lambda s: s
    m.__exit__  = MagicMock(return_value=False)
    return m


def _client():
    return K8sClient('https://k8s.example:6443', 'test-token')


def _pod(name, owner_kind=None, deletion_ts=None, phase=None,
         namespace='default', mirror=False):
    meta = {'name': name, 'namespace': namespace, 'annotations': {}}
    if mirror:
        meta['annotations']['kubernetes.io/config.mirror'] = name
    if owner_kind:
        meta['ownerReferences'] = [{'kind': owner_kind, 'name': 'owner'}]
    if deletion_ts:
        meta['deletionTimestamp'] = deletion_ts
    return {
        'metadata': meta,
        'status': ({'phase': phase} if phase else {}),
    }


def _node(name, labels=None):
    return {'metadata': {'name': name, 'labels': labels or {}}}


# ── reachable ─────────────────────────────────────────────────────────────────

class TestReachable(unittest.TestCase):

    def test_returns_0_when_api_responds(self):
        with patch('urllib.request.urlopen', return_value=_resp({'items': []})):
            self.assertEqual(cmd_reachable(_client(), None), 0)

    def test_returns_1_on_network_error(self):
        with patch('urllib.request.urlopen', side_effect=OSError('refused')):
            self.assertEqual(cmd_reachable(_client(), None), 1)

    def test_returns_1_on_http_error(self):
        err = urllib.error.HTTPError(None, 403, 'Forbidden', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            self.assertEqual(cmd_reachable(_client(), None), 1)


# ── get_node_roles ────────────────────────────────────────────────────────────

class TestGetNodeRoles(unittest.TestCase):

    def test_node_without_cp_label_is_worker(self):
        resp = _resp({'items': [_node('worker1')]})
        with patch('urllib.request.urlopen', return_value=resp):
            roles = _client().get_node_roles()
        self.assertIn(('worker1', 'worker'), roles)

    def test_node_with_cp_label_is_control_plane(self):
        resp = _resp({'items': [
            _node('cp1', {'node-role.kubernetes.io/control-plane': ''})
        ]})
        with patch('urllib.request.urlopen', return_value=resp):
            roles = _client().get_node_roles()
        self.assertIn(('cp1', 'control-plane'), roles)

    def test_mixed_cluster(self):
        resp = _resp({'items': [
            _node('cp1', {'node-role.kubernetes.io/control-plane': ''}),
            _node('worker1'),
            _node('worker2'),
        ]})
        with patch('urllib.request.urlopen', return_value=resp):
            roles = dict(_client().get_node_roles())
        self.assertEqual(roles['cp1'],     'control-plane')
        self.assertEqual(roles['worker1'], 'worker')
        self.assertEqual(roles['worker2'], 'worker')

    def test_cmd_get_nodes_prints_name_role_pairs(self):
        resp = _resp({'items': [
            _node('cp1', {'node-role.kubernetes.io/control-plane': ''}),
            _node('worker1'),
        ]})
        with patch('urllib.request.urlopen', return_value=resp):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                cmd_get_nodes(_client(), None)
        lines = out.getvalue().splitlines()
        role_map = dict(l.split() for l in lines)
        self.assertEqual(role_map['cp1'],     'control-plane')
        self.assertEqual(role_map['worker1'], 'worker')


# ── cordon ────────────────────────────────────────────────────────────────────

class TestCordon(unittest.TestCase):

    def test_sends_patch_to_correct_path(self):
        with patch('urllib.request.urlopen', return_value=_resp({})) as m:
            _client().cordon('worker1')
        req = m.call_args[0][0]
        self.assertEqual(req.method, 'PATCH')
        self.assertIn('/api/v1/nodes/worker1', req.full_url)

    def test_sets_unschedulable_true(self):
        with patch('urllib.request.urlopen', return_value=_resp({})) as m:
            _client().cordon('worker1')
        self.assertTrue(json.loads(m.call_args[0][0].data)['spec']['unschedulable'])

    def test_uses_strategic_merge_patch(self):
        with patch('urllib.request.urlopen', return_value=_resp({})) as m:
            _client().cordon('worker1')
        self.assertIn('strategic-merge-patch', m.call_args[0][0].get_header('Content-type'))


# ── _drainable ────────────────────────────────────────────────────────────────

class TestDrainable(unittest.TestCase):

    def test_plain_pod_is_drainable(self):
        self.assertTrue(K8sClient._drainable(_pod('app')))

    def test_daemonset_pod_not_drainable(self):
        self.assertFalse(K8sClient._drainable(_pod('ds', owner_kind='DaemonSet')))

    def test_replicaset_pod_is_drainable(self):
        self.assertTrue(K8sClient._drainable(_pod('app', owner_kind='ReplicaSet')))

    def test_statefulset_pod_is_drainable(self):
        self.assertTrue(K8sClient._drainable(_pod('db', owner_kind='StatefulSet')))

    def test_terminating_pod_not_drainable(self):
        self.assertFalse(
            K8sClient._drainable(_pod('app', deletion_ts='2024-01-01T00:00:00Z')))

    def test_succeeded_pod_not_drainable(self):
        self.assertFalse(K8sClient._drainable(_pod('job', phase='Succeeded')))

    def test_failed_pod_not_drainable(self):
        self.assertFalse(K8sClient._drainable(_pod('job', phase='Failed')))

    def test_running_pod_is_drainable(self):
        self.assertTrue(K8sClient._drainable(_pod('app', phase='Running')))

    def test_mirror_pod_not_drainable(self):
        """Static pod mirror (kubernetes.io/config.mirror annotation) must be skipped."""
        self.assertFalse(K8sClient._drainable(_pod('kube-apiserver', mirror=True)))

    def test_mirror_pod_with_daemonset_owner_not_drainable(self):
        """Belt and braces: mirror annotation takes priority."""
        self.assertFalse(
            K8sClient._drainable(_pod('etcd', mirror=True, owner_kind='ReplicaSet')))


# ── drain ─────────────────────────────────────────────────────────────────────

class TestDrain(unittest.TestCase):

    def test_evicts_regular_pod_returns_true(self):
        pods_before = [_pod('app'), _pod('ds', owner_kind='DaemonSet')]
        pods_after  = [_pod('ds', owner_kind='DaemonSet')]
        responses   = [
            _resp({}),                     # cordon PATCH
            _resp({'items': pods_before}), # list pods
            _resp({}),                     # evict 'app'
            _resp({'items': pods_after}),  # poll: only DS → done
        ]
        import styx.k8s as k8s_mod
        with patch('urllib.request.urlopen', side_effect=responses):
            with patch.object(k8s_mod.time, 'sleep'):
                result = _client().drain('worker1', timeout=60)
        self.assertTrue(result)

    def test_mirror_pod_not_evicted(self):
        pods = [_pod('kube-apiserver', mirror=True)]
        responses = [
            _resp({}),              # cordon
            _resp({'items': pods}), # list: only mirror pod — skip
            _resp({'items': []}),   # poll: no drainable pods → done
        ]
        import styx.k8s as k8s_mod
        with patch('urllib.request.urlopen', side_effect=responses) as m:
            with patch.object(k8s_mod.time, 'sleep'):
                _client().drain('cp1', timeout=60)
        # cordon + list + poll = 3 calls; no eviction POST
        self.assertEqual(m.call_count, 3)

    def test_daemonset_pods_not_evicted(self):
        pods      = [_pod('ds', owner_kind='DaemonSet')]
        responses = [
            _resp({}),              # cordon
            _resp({'items': pods}), # list
            _resp({'items': []}),   # poll → done
        ]
        import styx.k8s as k8s_mod
        with patch('urllib.request.urlopen', side_effect=responses) as m:
            with patch.object(k8s_mod.time, 'sleep'):
                _client().drain('worker1', timeout=60)
        self.assertEqual(m.call_count, 3)

    def test_returns_true_when_already_empty(self):
        responses = [
            _resp({}),            # cordon
            _resp({'items': []}), # list: nothing
            _resp({'items': []}), # poll → done
        ]
        import styx.k8s as k8s_mod
        with patch('urllib.request.urlopen', side_effect=responses):
            with patch.object(k8s_mod.time, 'sleep'):
                self.assertTrue(_client().drain('worker1', timeout=60))

    def test_returns_false_on_timeout(self):
        pod       = _pod('stuck')
        responses = [
            _resp({}),               # cordon
            _resp({'items': [pod]}), # list
            _resp({}),               # evict
        ]
        import styx.k8s as k8s_mod
        with patch('urllib.request.urlopen', side_effect=responses):
            with patch.object(k8s_mod.time, 'monotonic', side_effect=[100, 102]):
                self.assertFalse(_client().drain('worker1', timeout=1))

    def test_polls_until_clear(self):
        pod       = _pod('slow')
        responses = [
            _resp({}),               # cordon
            _resp({'items': [pod]}), # list
            _resp({}),               # evict
            _resp({'items': [pod]}), # poll 1: still there
            _resp({'items': []}),    # poll 2: gone
        ]
        import styx.k8s as k8s_mod
        with patch('urllib.request.urlopen', side_effect=responses):
            with patch.object(k8s_mod.time, 'sleep'):
                self.assertTrue(_client().drain('worker1', timeout=60))


if __name__ == '__main__':
    unittest.main()
