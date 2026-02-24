"""styx.local_shutdown — Per-host VM shutdown with optional autonomous poweroff.

Dispatched by the orchestrator after the coordinated phase (drain, HA, Ceph
flags).  Each peer shuts down its own VMs, then optionally powers off after a
deadline.  The deadline-based poweroff is a fallback: if the leader is still
alive it will send an explicit poweroff before the deadline expires.

Usage:
    styx local-shutdown <vmid>... --timeout 120 [--poweroff-delay 135] [--dry-run]
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from styx.vm_shutdown import shutdown as vm_shutdown, check as vm_check


def run(vmids, timeout_vm, poweroff_deadline=None, dry_run=False):
    """Shut down all VMIDs in parallel, optionally power off after deadline.

    Returns the worst (highest) return code from individual vm_shutdown calls.
    If poweroff_deadline is set (monotonic timestamp), sleeps until the
    deadline then powers off.
    """
    if dry_run:
        for vmid in vmids:
            vm_check(vmid)
        return 0

    worst_rc = 0
    if vmids:
        with ThreadPoolExecutor() as ex:
            futs = {ex.submit(vm_shutdown, vmid, timeout_vm): vmid
                    for vmid in vmids}
            for fut in as_completed(futs):
                vmid = futs[fut]
                try:
                    rc = fut.result()
                    if rc > worst_rc:
                        worst_rc = rc
                except Exception as e:
                    print(f'VM {vmid}: {e}', file=sys.stderr)
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
        description='Shut down local VMs and optionally power off after delay')
    p.add_argument('vmids', nargs='+', metavar='VMID')
    p.add_argument('--timeout', type=int, default=120,
                   help='Per-VM shutdown timeout in seconds (default: 120)')
    p.add_argument('--poweroff-delay', type=int, default=None,
                   help='Seconds from now until autonomous poweroff')
    p.add_argument('--dry-run', action='store_true',
                   help='Report VM status without shutting down')
    args = p.parse_args(argv)

    poweroff_deadline = None
    if args.poweroff_delay is not None:
        poweroff_deadline = time.monotonic() + args.poweroff_delay

    sys.exit(run(args.vmids, args.timeout, poweroff_deadline, args.dry_run))


if __name__ == '__main__':
    main()
