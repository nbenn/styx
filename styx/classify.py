"""styx.classify — VM role classification. Pure functions."""


def classify_vmid(vmid, workers, cp):
    """Return 'k8s-worker', 'k8s-cp', or 'other'."""
    if vmid in workers:
        return 'k8s-worker'
    if vmid in cp:
        return 'k8s-cp'
    return 'other'


def other_vmids(all_vmids, workers, cp):
    """Return VMIDs that are neither k8s-worker nor k8s-cp."""
    k8s = set(workers) | set(cp)
    return [v for v in all_vmids if v not in k8s]
