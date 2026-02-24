"""styx.local_shutdown — Per-host workload shutdown with optional autonomous poweroff.

Dispatched by the orchestrator after the coordinated phase (drain, HA, Ceph
flags).  Each peer shuts down its own workloads, then optionally powers off
after a deadline.  The deadline-based poweroff is a fallback: if the leader is
still alive it will send an explicit poweroff before the deadline expires.

Usage:
    styx local-shutdown [TYPE:]VMID... --timeout 120 [--poweroff-delay 135] [--dry-run]

TYPE defaults to 'qemu' if omitted. Example: qemu:101 lxc:200 301
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from styx.vm_shutdown import shutdown as vm_shutdown, check as vm_check


# ── dispatch maps ────────────────────────────────────────────────────────────

_SHUTDOWN = {'qemu': vm_shutdown}
_CHECK    = {'qemu': vm_check}


def _parse_workload(token):
    """Parse a workload token into (type, vmid).

    Accepts 'qemu:101' or bare '101' (defaults to 'qemu').
    """
    if ':' in token:
        wtype, vmid = token.split(':', 1)
        return wtype, vmid
    return 'qemu', token


def run(workloads, timeout_vm, poweroff_deadline=None, dry_run=False):
    """Shut down all workloads in parallel, optionally power off after deadline.

    workloads: list of (type, vmid) tuples.
    Returns the worst (highest) return code from individual shutdown calls.
    If poweroff_deadline is set (monotonic timestamp), sleeps until the
    deadline then powers off.
    """
    if dry_run:
        for wtype, vmid in workloads:
            check_fn = _CHECK.get(wtype)
            if check_fn is None:
                print(f'WARNING: unknown workload type {wtype!r} for {vmid}',
                      file=sys.stderr)
                continue
            check_fn(vmid)
        return 0

    worst_rc = 0
    if workloads:
        with ThreadPoolExecutor() as ex:
            futs = {}
            for wtype, vmid in workloads:
                shutdown_fn = _SHUTDOWN.get(wtype)
                if shutdown_fn is None:
                    print(f'WARNING: unknown workload type {wtype!r} for {vmid}',
                          file=sys.stderr)
                    worst_rc = 1
                    continue
                futs[ex.submit(shutdown_fn, vmid, timeout_vm)] = (wtype, vmid)
            for fut in as_completed(futs):
                wtype, vmid = futs[fut]
                try:
                    rc = fut.result()
                    if rc > worst_rc:
                        worst_rc = rc
                except Exception as e:
                    print(f'{wtype}:{vmid}: {e}', file=sys.stderr)
                    worst_rc = 1

    if poweroff_deadline is not None:
        remaining = poweroff_deadline - time.monotonic()
        if remaining > 0:
            print(f'Waiting {remaining:.0f}s before autonomous poweroff')
            time.sleep(remaining)
        print('Autonomous poweroff')
        os.system('poweroff')

    return worst_rc


def main(argv=None):
    p = argparse.ArgumentParser(
        description='Shut down local workloads and optionally power off after delay')
    p.add_argument('workloads', nargs='+', metavar='[TYPE:]VMID',
                   help='Workload identifiers (e.g. qemu:101 or bare 101)')
    p.add_argument('--timeout', type=int, default=120,
                   help='Per-workload shutdown timeout in seconds (default: 120)')
    p.add_argument('--poweroff-delay', type=int, default=None,
                   help='Seconds from now until autonomous poweroff')
    p.add_argument('--dry-run', action='store_true',
                   help='Report workload status without shutting down')
    args = p.parse_args(argv)

    poweroff_deadline = None
    if args.poweroff_delay is not None:
        poweroff_deadline = time.monotonic() + args.poweroff_delay

    workloads = [_parse_workload(token) for token in args.workloads]
    sys.exit(run(workloads, args.timeout, poweroff_deadline, args.dry_run))


if __name__ == '__main__':
    main()
