# Kubernetes API Configuration

Styx needs direct access to the Kubernetes API to cordon and drain nodes during shutdown. This guide covers creating a service account with least-privilege RBAC and configuring styx to use it.

## Overview

Styx performs four Kubernetes operations:

| Operation | API resource | Verbs |
|-----------|-------------|-------|
| Check API reachability | `/api/v1/nodes` | get |
| List node roles | `/api/v1/nodes` | list |
| Cordon (mark unschedulable) | `/api/v1/nodes` | patch |
| Drain (evict pods) | `/api/v1/pods`, `/api/v1/pods/eviction` | get, list, create |
| Check stale volumes | `storage.k8s.io/volumeattachments` | list |

## 1. Create the service account and RBAC

Apply the following manifest on any control-plane node:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: styx
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: styx
rules:
  # Cordon: patch the node's unschedulable field
  - apiGroups: [""]
    resources: [nodes]
    verbs: [get, list, patch]
  # Drain: list pods on a node, then create evictions
  - apiGroups: [""]
    resources: [pods]
    verbs: [get, list]
  - apiGroups: [""]
    resources: [pods/eviction]
    verbs: [create]
  # Post-drain check for stale volume attachments
  - apiGroups: [storage.k8s.io]
    resources: [volumeattachments]
    verbs: [list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: styx
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: styx
subjects:
  - kind: ServiceAccount
    name: styx
    namespace: kube-system
```

Save this as `styx-rbac.yaml` and apply:

```bash
kubectl apply -f styx-rbac.yaml
```

## 2. Create a bearer token

Styx authenticates with a static bearer token stored in a file on the orchestrator host.

**Option A: Long-lived token (Kubernetes 1.24+)**

```bash
kubectl create token styx -n kube-system --duration=8760h > /etc/styx/k8s-token
chmod 600 /etc/styx/k8s-token
```

**Option B: Secret-based token (all Kubernetes versions)**

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: styx-token
  namespace: kube-system
  annotations:
    kubernetes.io/service-account.name: styx
type: kubernetes.io/service-account-token
EOF

kubectl get secret styx-token -n kube-system \
  -o jsonpath='{.data.token}' | base64 -d > /etc/styx/k8s-token
chmod 600 /etc/styx/k8s-token
```

Secret-based tokens do not expire and survive API server restarts.

## 3. Export the cluster CA certificate

Styx uses the CA certificate to verify the API server's TLS identity. If omitted, TLS verification is skipped (acceptable on a trusted LAN but not recommended).

**From a control-plane node:**

```bash
cp /etc/kubernetes/pki/ca.crt /etc/styx/k8s-ca.crt
```

**From kubeconfig:**

```bash
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' \
  | base64 -d > /etc/styx/k8s-ca.crt
```

## 4. Configure styx

Add the `[kubernetes]` section to your `styx.conf`:

```ini
[kubernetes]
server        = https://10.0.0.100:6443
token         = /etc/styx/k8s-token
ca_cert       = /etc/styx/k8s-ca.crt
workers       = 211, 212, 213
control_plane = 201, 202, 203
```

| Key | Required | Description |
|-----|----------|-------------|
| `server` | Yes | Kubernetes API server URL |
| `token` | Yes | Path to file containing the bearer token |
| `ca_cert` | No | Path to cluster CA certificate (skips TLS verification if omitted) |
| `workers` | No | VM IDs of Kubernetes worker nodes (overrides auto-discovery) |
| `control_plane` | No | VM IDs of Kubernetes control-plane nodes (overrides auto-discovery) |

**When are `workers` / `control_plane` needed?** Styx auto-discovers the mapping by matching Kubernetes node names to Proxmox VM names. If your VM names match your Kubernetes node names (e.g., VM "k8s-worker-1" has hostname "k8s-worker-1"), these fields can be omitted. If they differ, list the Proxmox VM IDs explicitly.

## 5. Verify

Run a dry-run to confirm styx can reach the API and discover nodes:

```bash
styx.pyz orchestrate --mode dry-run
```

The preflight output should show:

```
[...] k8s API: reachable
[...] k8s nodes: worker1 (worker), worker2 (worker), cp1 (control-plane)
```

If the API is unreachable or the token is invalid, preflight will report the failure. In `dry-run` and `maintenance` modes this is fatal; in `emergency` mode it logs a warning and continues (skipping Kubernetes operations).
