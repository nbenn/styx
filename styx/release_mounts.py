"""styx.release_mounts — Unmount Proxmox network storage while its servers are up.

Proxmox mounts NFS/CIFS/CephFS storages under /mnt/pve. If their server is a
VM that shuts down with the cluster (e.g. a NAS VM) or Ceph loses its
monitors, the host's own poweroff later blocks unmounting them (hard NFS
mounts: until systemd's stop timeout). Run at dispatch time, while the
servers are still reachable, this unmounts them cleanly.

Only unmounts, never touches storage.cfg: Proxmox re-mounts enabled storages
on boot. pvestatd is stopped first, as it would re-mount them within seconds.
A plain (not lazy, not forced) umount fails with EBUSY where something still
uses the mount (e.g. a running VM's disk or ISO) — that mount is left alone.

Usage:
    styx release-mounts [--dry-run]
"""

import argparse
import os
import re
import subprocess
import sys
import time

MOUNT_ROOT = '/mnt/pve/'
NETWORK_FSTYPES = {'nfs', 'nfs4', 'cifs', 'smb3', 'ceph', 'fuse.ceph-fuse'}
UMOUNT_TIMEOUT = 10


def _unescape(field):
    """Decode octal escapes in /proc/mounts fields ('\\040' = space)."""
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), field)


def network_mounts(mounts_text, keep_paths=()):
    """Return Proxmox network storage mount points to release, deepest first.

    Skips any mount containing a path in keep_paths (e.g. the running
    styx.pyz or its log file).
    """
    result = []
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        target, fstype = _unescape(fields[1]), fields[2]
        if fstype not in NETWORK_FSTYPES or not target.startswith(MOUNT_ROOT):
            continue
        if any(p == target or p.startswith(target.rstrip('/') + '/')
               for p in keep_paths):
            continue
        result.append(target)
    return sorted(set(result), key=lambda t: (-len(t), t))


def _umount(target, timeout=UMOUNT_TIMEOUT):
    """Plain umount with a deadline. Returns a status string.

    A umount stuck in the kernel can't be killed; it is abandoned (not
    waited for) so this process keeps going.
    """
    proc = subprocess.Popen(['umount', target], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            if proc.returncode == 0:
                return 'unmounted'
            return f'left mounted ({proc.stderr.read().strip()})'
        time.sleep(0.2)
    proc.kill()
    return f'left mounted (umount timed out after {timeout}s)'


def run(dry_run=False, keep_paths=(), mounts_path='/proc/self/mounts'):
    with open(mounts_path) as f:
        targets = network_mounts(f.read(), keep_paths)
    if not targets:
        print('No network storage mounted under /mnt/pve')
        return 0
    if dry_run:
        print(f'[dry-run] would stop pvestatd and unmount: {" ".join(targets)}')
        return 0

    # Stop pvestatd so it doesn't re-mount what we unmount. It starts again
    # on boot.
    r = subprocess.run(['systemctl', 'stop', 'pvestatd'],
                       capture_output=True, text=True, timeout=30)
    print('pvestatd stopped' if r.returncode == 0
          else f'WARNING: stopping pvestatd failed: {r.stderr.strip()}')

    for target in targets:
        print(f'{target}: {_umount(target)}')
    return 0


def _own_paths():
    """Paths this styx run depends on; their filesystems are never unmounted."""
    paths = [os.path.realpath(sys.argv[0])] if sys.argv and sys.argv[0] else []
    return paths + [os.path.realpath('/var/log')]


def main(argv=None):
    p = argparse.ArgumentParser(
        description='Unmount Proxmox network storage (NFS/CIFS/CephFS) before shutdown')
    p.add_argument('--dry-run', action='store_true',
                   help='List what would be unmounted without changing anything')
    args = p.parse_args(argv)
    sys.exit(run(args.dry_run, keep_paths=_own_paths()))


if __name__ == '__main__':
    main()
