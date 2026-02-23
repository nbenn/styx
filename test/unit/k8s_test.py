#!/usr/bin/env python3
"""Unit tests for lib/k8s.py"""

import importlib.util
import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

# Load lib/k8s.py without requiring a package __init__
_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    'k8s', os.path.join(_here, '../../lib/k8s.py'))
k8s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(k8s)


# ── helpers ───────────────────────────────────────────────────────────────────

def _resp(data):
    """Build a mock that works as urlopen's context-manager return value."""
    m = MagicMock()
    m.read.return_value = json.dumps(data).encode()
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


def _client():
    return k8s.K8sClient('https://k8s.example:6443', 'test-token')


def _pod(name, owner_kind=None, deletion_ts=None, phase=None, namespace='default'):
    meta = {'name': name, 'namespace': namespace}
    if owner_kind:
        meta['ownerReferences'] = [{'kind': owner_kind, 'name': 'owner'}]
    if deletion_ts:
        meta['deletionTimestamp'] = deletion_ts
    return {
        'metadata': meta,
        'status': ({'phase': phase} if phase else {}),
    }


# ── reachable ─────────────────────────────────────────────────────────────────

class TestReachable(unittest.TestCase):

    def test_returns_0_when_api_responds(self):
        with patch.object(k8s.urllib.request, 'urlopen', return_value=_resp({'items': []})):
            self.assertEqual(k8s.cmd_reachable(_client(), None), 0)

    def test_returns_1_on_network_error(self):
        with patch.object(k8s.urllib.request, 'urlopen', side_effect=OSError('refused')):
            self.assertEqual(k8s.cmd_reachable(_client(), None), 1)

    def test_returns_1_on_http_error(self):
        err = urllib.error.HTTPError(None, 403, 'Forbidden', {}, None)
        with patch.object(k8s.urllib.request, 'urlopen', side_effect=err):
            self.assertEqual(k8s.cmd_reachable(_client(), None), 1)


# ── get-nodes ─────────────────────────────────────────────────────────────────

class TestGetNodes(unittest.TestCase):

    def _nodes(self, *items):
        return {'items': list(items)}

    def _node(self, name, labels=None):
        return {'metadata': {'name': name, 'labels': labels or {}}}

    def test_node_without_cp_label_is_worker(self):
        resp = _resp(self._nodes(self._node('worker1')))
        with patch.object(k8s.urllib.request, 'urlopen', return_value=resp):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                k8s.cmd_get_nodes(_client(), None)
        self.assertIn('worker1 worker', out.getvalue())

    def test_node_with_cp_label_is_control_plane(self):
        resp = _resp(self._nodes(
            self._node('cp1', {'node-role.kubernetes.io/control-plane': ''})))
        with patch.object(k8s.urllib.request, 'urlopen', return_value=resp):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                k8s.cmd_get_nodes(_client(), None)
        self.assertIn('cp1 control-plane', out.getvalue())

    def test_mixed_cluster_outputs_correct_roles(self):
        resp = _resp(self._nodes(
            self._node('cp1',     {'node-role.kubernetes.io/control-plane': ''}),
            self._node('worker1'),
            self._node('worker2'),
        ))
        with patch.object(k8s.urllib.request, 'urlopen', return_value=resp):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                k8s.cmd_get_nodes(_client(), None)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        roles = dict(line.split() for line in lines)
        self.assertEqual(roles['cp1'],     'control-plane')
        self.assertEqual(roles['worker1'], 'worker')
        self.assertEqual(roles['worker2'], 'worker')


# ── cordon ────────────────────────────────────────────────────────────────────

class TestCordon(unittest.TestCase):

    def test_sends_patch_to_correct_node_path(self):
        with patch.object(k8s.urllib.request, 'urlopen', return_value=_resp({})) as m:
            _client().cordon('worker1')
        req = m.call_args[0][0]
        self.assertEqual(req.method, 'PATCH')
        self.assertIn('/api/v1/nodes/worker1', req.full_url)

    def test_patch_body_sets_unschedulable_true(self):
        with patch.object(k8s.urllib.request, 'urlopen', return_value=_resp({})) as m:
            _client().cordon('worker1')
        req = m.call_args[0][0]
        body = json.loads(req.data)
        self.assertTrue(body['spec']['unschedulable'])

    def test_uses_strategic_merge_patch_content_type(self):
        with patch.object(k8s.urllib.request, 'urlopen', return_value=_resp({})) as m:
            _client().cordon('worker1')
        req = m.call_args[0][0]
        self.assertIn('strategic-merge-patch', req.get_header('Content-type'))


# ── _drainable ────────────────────────────────────────────────────────────────

class TestDrainable(unittest.TestCase):

    def test_plain_pod_is_drainable(self):
        self.assertTrue(k8s.K8sClient._drainable(_pod('app')))

    def test_daemonset_pod_is_not_drainable(self):
        self.assertFalse(k8s.K8sClient._drainable(_pod('ds', owner_kind='DaemonSet')))

    def test_replicaset_pod_is_drainable(self):
        self.assertTrue(k8s.K8sClient._drainable(_pod('app', owner_kind='ReplicaSet')))

    def test_statefulset_pod_is_drainable(self):
        self.assertTrue(k8s.K8sClient._drainable(_pod('db', owner_kind='StatefulSet')))

    def test_terminating_pod_is_not_drainable(self):
        self.assertFalse(
            k8s.K8sClient._drainable(_pod('app', deletion_ts='2024-01-01T00:00:00Z')))

    def test_succeeded_pod_is_not_drainable(self):
        self.assertFalse(k8s.K8sClient._drainable(_pod('job', phase='Succeeded')))

    def test_failed_pod_is_not_drainable(self):
        self.assertFalse(k8s.K8sClient._drainable(_pod('job', phase='Failed')))

    def test_running_pod_is_drainable(self):
        self.assertTrue(k8s.K8sClient._drainable(_pod('app', phase='Running')))


# ── drain ─────────────────────────────────────────────────────────────────────

class TestDrain(unittest.TestCase):

    def test_evicts_regular_pod_and_returns_true_when_cleared(self):
        pods_before = [_pod('app'), _pod('ds', owner_kind='DaemonSet')]
        pods_after  = [_pod('ds', owner_kind='DaemonSet')]
        responses = [
            _resp({}),                     # cordon PATCH
            _resp({'items': pods_before}), # list pods
            _resp({}),                     # evict 'app'
            _resp({'items': pods_after}),  # poll: only DS pod left → done
        ]
        with patch.object(k8s.urllib.request, 'urlopen', side_effect=responses):
            with patch.object(k8s.time, 'sleep'):
                result = _client().drain('worker1', timeout=60)
        self.assertTrue(result)

    def test_daemonset_pods_are_not_evicted(self):
        pods = [_pod('ds', owner_kind='DaemonSet')]
        responses = [
            _resp({}),              # cordon
            _resp({'items': pods}), # list (only DS pod — nothing to evict)
            _resp({'items': []}),   # poll: no drainable pods → done
        ]
        with patch.object(k8s.urllib.request, 'urlopen', side_effect=responses) as m:
            with patch.object(k8s.time, 'sleep'):
                _client().drain('worker1', timeout=60)
        # cordon + list + poll = 3 calls; no eviction POST
        self.assertEqual(m.call_count, 3)

    def test_returns_true_when_node_already_empty(self):
        responses = [
            _resp({}),             # cordon
            _resp({'items': []}),  # list: nothing to evict
            _resp({'items': []}),  # poll: empty → done
        ]
        with patch.object(k8s.urllib.request, 'urlopen', side_effect=responses):
            with patch.object(k8s.time, 'sleep'):
                result = _client().drain('worker1', timeout=60)
        self.assertTrue(result)

    def test_returns_false_when_pods_remain_after_timeout(self):
        # monotonic: t=100 to set deadline, t=102 on first while-check → expired
        pod = _pod('stuck')
        responses = [
            _resp({}),              # cordon
            _resp({'items': [pod]}),# list: 'stuck' to evict
            _resp({}),              # evict
            # while loop never entered (deadline already past)
        ]
        with patch.object(k8s.urllib.request, 'urlopen', side_effect=responses):
            with patch.object(k8s.time, 'monotonic', side_effect=[100, 102]):
                result = _client().drain('worker1', timeout=1)
        self.assertFalse(result)

    def test_polls_until_pods_clear(self):
        pod = _pod('slow')
        responses = [
            _resp({}),               # cordon
            _resp({'items': [pod]}), # list: 'slow' to evict
            _resp({}),               # evict
            _resp({'items': [pod]}), # poll 1: still present
            _resp({'items': []}),    # poll 2: gone → done
        ]
        with patch.object(k8s.urllib.request, 'urlopen', side_effect=responses):
            with patch.object(k8s.time, 'sleep'):
                result = _client().drain('worker1', timeout=60)
        self.assertTrue(result)


if __name__ == '__main__':
    unittest.main()
