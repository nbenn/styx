"""styx.discover — Cluster topology discovery.

Pure parsing functions; no external calls or side effects.
"""

from dataclasses import dataclass, field


@dataclass
class ClusterTopology:
    host_ips:    dict = field(default_factory=dict)   # hostname -> IP
    orchestrator: str = ''
    vm_host:     dict = field(default_factory=dict)   # vmid -> hostname
    vm_name:     dict = field(default_factory=dict)   # vmid -> VM name
    vm_type:     dict = field(default_factory=dict)   # vmid -> 'qemu' | 'lxc'
    k8s_workers: list = field(default_factory=list)   # VMIDs
    k8s_cp:      list = field(default_factory=list)   # VMIDs
    vm_lock:     dict = field(default_factory=dict)   # vmid -> lock string
    k8s_enabled: bool = False
    ceph_enabled: bool = False


def parse_cluster_status(data):
    """Parse /cluster/status JSON list. Returns (host_ips, orchestrator)."""
    host_ips = {}
    orchestrator = ''
    for node in data:
        if node.get('type') != 'node':
            continue
        name = node.get('name', '')
        host_ips[name] = node.get('ip', '')
        if node.get('local', 0):
            orchestrator = name
    return host_ips, orchestrator


def parse_cluster_resources(data):
    """Parse /cluster/resources JSON list. Returns (vm_host, vm_name, vm_type, vm_lock).

    Filters to running non-template QEMU VMs only.
    vm_lock maps VMID to lock string, only for VMs that have a lock set.
    """
    vm_host = {}
    vm_name = {}
    vm_type = {}
    vm_lock = {}
    for vm in data:
        if vm.get('type') != 'qemu':
            continue
        if vm.get('template', 0):
            continue
        if vm.get('status') != 'running':
            continue
        vmid = str(vm['vmid'])
        vm_host[vmid] = vm.get('node', '')
        vm_name[vmid] = vm.get('name', '')
        vm_type[vmid] = 'qemu'
        lock = vm.get('lock')
        if lock:
            vm_lock[vmid] = lock
    return vm_host, vm_name, vm_type, vm_lock


def match_nodes_to_vms(vm_name, node_roles):
    """Match k8s node names to Proxmox VMIDs by name.

    node_roles: list of (node_name, role) where role is 'worker' or 'control-plane'.
    Returns (workers, cp) VMID lists. Raises ValueError if no matches found.
    """
    role_map = {name: role for name, role in node_roles}
    workers = []
    cp = []
    for vmid, name in vm_name.items():
        if name in role_map:
            if role_map[name] == 'control-plane':
                cp.append(vmid)
            else:
                workers.append(vmid)
    if not workers and not cp:
        raise ValueError(
            'No Kubernetes node names match any Proxmox VM name. '
            'Provide workers/control_plane VMIDs in [kubernetes] config.'
        )
    return workers, cp
