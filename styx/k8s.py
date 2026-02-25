"""styx.k8s — Minimal Kubernetes API client.

Covers the four operations styx needs:
  reachable        — check if the API server responds
  get_node_roles() — list of (name, role) tuples
  cordon()         — mark a node unschedulable
  drain()          — evict all drainable pods and wait until clear

CLI entry point preserved for standalone use / debugging.

Dependencies: Python 3 stdlib only (urllib, ssl, json, argparse).
"""

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ── SSL ───────────────────────────────────────────────────────────────────────

def _ssl_context(ca_cert):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if ca_cert:
        ctx.load_verify_locations(ca_cert)
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── Client ────────────────────────────────────────────────────────────────────

class K8sClient:
    def __init__(self, server, token, ca_cert=None):
        self.server = server.rstrip('/')
        self.token  = token
        self._ctx   = _ssl_context(ca_cert)

    def _request(self, method, path, body=None, timeout=10):
        url  = self.server + path
        data = json.dumps(body).encode() if body is not None else None
        req  = urllib.request.Request(url, data=data, method=method)
        req.add_header('Authorization', f'Bearer {self.token}')
        req.add_header('Accept', 'application/json')
        if data is not None:
            content_type = (
                'application/strategic-merge-patch+json'
                if method == 'PATCH'
                else 'application/json'
            )
            req.add_header('Content-Type', content_type)
        with urllib.request.urlopen(req, context=self._ctx, timeout=timeout) as r:
            return json.loads(r.read())

    # ── nodes ─────────────────────────────────────────────────────────────────

    def list_nodes(self):
        return self._request('GET', '/api/v1/nodes')

    def get_node_roles(self):
        """Return list of (name, role) tuples.

        role is 'control-plane' or 'worker'.
        """
        data = self.list_nodes()
        result = []
        for item in data['items']:
            name   = item['metadata']['name']
            labels = item['metadata'].get('labels', {})
            role   = (
                'control-plane'
                if 'node-role.kubernetes.io/control-plane' in labels
                else 'worker'
            )
            result.append((name, role))
        return result

    def cordon(self, node):
        self._request('PATCH', f'/api/v1/nodes/{node}',
                      {'spec': {'unschedulable': True}})

    # ── pods ──────────────────────────────────────────────────────────────────

    def list_pods_on_node(self, node):
        qs = urllib.parse.urlencode({'fieldSelector': f'spec.nodeName={node}'})
        return self._request('GET', f'/api/v1/pods?{qs}')

    def evict(self, name, namespace):
        body = {
            'apiVersion': 'policy/v1',
            'kind': 'Eviction',
            'metadata': {'name': name, 'namespace': namespace},
        }
        try:
            self._request(
                'POST',
                f'/api/v1/namespaces/{namespace}/pods/{name}/eviction',
                body,
            )
            return 'evicted'
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 'gone'    # already deleted
            if e.code in (422, 429):
                return 'retry'   # PDB blocking or rate-limited
            raise

    # ── volume attachments ────────────────────────────────────────────────────

    def list_volume_attachments(self):
        """Return list of (name, nodeName) tuples for all VolumeAttachments."""
        data = self._request('GET', '/apis/storage.k8s.io/v1/volumeattachments')
        return [
            (item['metadata']['name'], item.get('spec', {}).get('nodeName', ''))
            for item in data.get('items', [])
        ]

    # ── drain ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _drainable(pod):
        """Return True if this pod should be evicted during a drain."""
        meta = pod['metadata']
        # Skip mirror pods (static pods managed via kubelet staticPodPath;
        # not evictable via the API)
        if 'kubernetes.io/config.mirror' in meta.get('annotations', {}):
            return False
        # Skip DaemonSet-owned pods
        for ref in meta.get('ownerReferences', []):
            if ref.get('kind') == 'DaemonSet':
                return False
        # Skip pods already being deleted
        if meta.get('deletionTimestamp'):
            return False
        # Skip completed / failed pods
        phase = pod.get('status', {}).get('phase', '')
        return phase not in ('Succeeded', 'Failed')

    def drain(self, node, timeout=120):
        """Cordon node, evict all drainable pods, poll until clear.

        Re-issues evictions on every poll so PDB-blocked pods are retried
        once the budget allows.  Returns True if all pods cleared within
        timeout, False otherwise.
        """
        self.cordon(node)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pods = self.list_pods_on_node(node)['items']
            pending = [p for p in pods if self._drainable(p)]
            if not pending:
                return True
            for pod in pending:
                self.evict(pod['metadata']['name'], pod['metadata']['namespace'])
            time.sleep(5)

        return False


# ── CLI commands ──────────────────────────────────────────────────────────────

def cmd_reachable(client, _args):
    try:
        client.list_nodes()
        return 0
    except Exception:
        return 1


def cmd_get_nodes(client, _args):
    for name, role in client.get_node_roles():
        print(name, role)
    return 0


def cmd_cordon(client, args):
    client.cordon(args.node)
    return 0


def cmd_drain(client, args):
    if not client.drain(args.node, timeout=args.timeout):
        print(f'drain timed out for {args.node}', file=sys.stderr)
        return 1
    return 0


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description='Minimal k8s API client for styx')
    p.add_argument('--server',     required=True)
    p.add_argument('--token-file', required=True)
    p.add_argument('--ca-cert',    default=None)

    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('reachable')
    sub.add_parser('get-nodes')

    cordon_p = sub.add_parser('cordon')
    cordon_p.add_argument('node')

    drain_p = sub.add_parser('drain')
    drain_p.add_argument('node')
    drain_p.add_argument('--timeout', type=int, default=120)

    args = p.parse_args()

    with open(args.token_file) as f:
        token = f.read().strip()

    client = K8sClient(args.server, token, args.ca_cert)

    dispatch = {
        'reachable': cmd_reachable,
        'get-nodes': cmd_get_nodes,
        'cordon':    cmd_cordon,
        'drain':     cmd_drain,
    }
    sys.exit(dispatch[args.command](client, args))


if __name__ == '__main__':
    main()
