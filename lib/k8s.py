#!/usr/bin/env python3
"""lib/k8s.py — Minimal Kubernetes API client for styx.

Replaces kubectl for the four operations styx needs:
  reachable   — exit 0 if the API server is reachable, 1 otherwise
  get-nodes   — print "name role" pairs (role: worker | control-plane)
  cordon      — mark a node unschedulable
  drain       — evict all non-daemonset pods from a node, wait until clear

Usage:
  python3 lib/k8s.py --server=URL --token-file=PATH [--ca-cert=PATH] <command> [args]

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
        self.token = token
        self._ctx = _ssl_context(ca_cert)

    def _request(self, method, path, body=None, timeout=10):
        url = self.server + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
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
                return 'retry'   # PDB blocking; caller may retry
            raise

    # ── drain ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _drainable(pod):
        """Return True if this pod should be evicted during a drain."""
        meta = pod['metadata']
        # Skip DaemonSet-owned pods (cannot be evicted)
        for ref in meta.get('ownerReferences', []):
            if ref.get('kind') == 'DaemonSet':
                return False
        # Skip pods already in the process of being deleted
        if meta.get('deletionTimestamp'):
            return False
        # Skip completed / failed pods
        phase = pod.get('status', {}).get('phase', '')
        return phase not in ('Succeeded', 'Failed')

    def drain(self, node, timeout=120):
        """Cordon node, evict all drainable pods, poll until clear.

        Returns True if all pods cleared within timeout, False otherwise.
        """
        self.cordon(node)

        pods = self.list_pods_on_node(node)['items']
        for pod in pods:
            if self._drainable(pod):
                self.evict(pod['metadata']['name'], pod['metadata']['namespace'])

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pods = self.list_pods_on_node(node)['items']
            if not any(self._drainable(p) for p in pods):
                return True
            time.sleep(2)

        return False


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_reachable(client, _args):
    try:
        client.list_nodes()
        return 0
    except Exception:
        return 1


def cmd_get_nodes(client, _args):
    data = client.list_nodes()
    for item in data['items']:
        name = item['metadata']['name']
        labels = item['metadata'].get('labels', {})
        role = (
            'control-plane'
            if 'node-role.kubernetes.io/control-plane' in labels
            else 'worker'
        )
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
    p.add_argument('--server',     required=True, help='API server URL')
    p.add_argument('--token-file', required=True, help='Path to bearer token file')
    p.add_argument('--ca-cert',    default=None,  help='Path to CA certificate (optional)')

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
        'reachable':  cmd_reachable,
        'get-nodes':  cmd_get_nodes,
        'cordon':     cmd_cordon,
        'drain':      cmd_drain,
    }
    sys.exit(dispatch[args.command](client, args))


if __name__ == '__main__':
    main()
