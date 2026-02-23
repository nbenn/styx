"""styx.vm_shutdown — Quorum-free VM shutdown via QMP + PID escalation.

Replaces bin/styx-vm-shutdown (bash + socat) with pure Python.
Escalation: ACPI system_powerdown → SIGTERM → SIGKILL.
"""

import argparse
import json
import os
import signal
import socket
import sys
import time

_QMP_SOCKET = '/var/run/qemu-server/{vmid}.qmp'
_PID_FILE   = '/var/run/qemu-server/{vmid}.pid'


def _read_pid(vmid):
    try:
        with open(_PID_FILE.format(vmid=vmid)) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal
    # Treat zombie (Z) as dead — process has exited but not yet been reaped
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat = f.read()
        state = stat.split(')')[1].split()[0]
        return state != 'Z'
    except OSError:
        pass
    return True


def _poll_dead(pid, deadline, interval=1):
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(interval)
    return not _alive(pid)


def _qmp_powerdown(vmid):
    """Send ACPI system_powerdown via QMP Unix socket. Returns True if sent."""
    path = _QMP_SOCKET.format(vmid=vmid)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(path)
            s.recv(4096)  # greeting
            s.sendall(json.dumps({'execute': 'qmp_capabilities'}).encode())
            s.recv(4096)  # ack
            s.sendall(json.dumps({'execute': 'system_powerdown'}).encode())
            s.recv(4096)  # response
        return True
    except OSError:
        return False


def shutdown(vmid, timeout=120):
    """Gracefully shut down a VM. Returns 0 on success, 1 if it could not be killed."""
    pid = _read_pid(vmid)
    if pid is None or not _alive(pid):
        print(f'VM {vmid} is not running')
        return 0

    if _qmp_powerdown(vmid):
        print(f'VM {vmid}: sent ACPI powerdown')
        if _poll_dead(pid, time.monotonic() + timeout):
            print(f'VM {vmid} stopped gracefully')
            return 0

    print(f'VM {vmid}: timeout after {timeout}s, sending SIGTERM')
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 0
    if _poll_dead(pid, time.monotonic() + 10):
        print(f'VM {vmid} stopped after SIGTERM')
        return 0

    print(f'VM {vmid}: still running, sending SIGKILL')
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return 0
    _poll_dead(pid, time.monotonic() + 5)
    if not _alive(pid):
        print(f'VM {vmid} force-killed')
        return 0

    print(f'VM {vmid}: could not be killed', file=sys.stderr)
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(description='Graceful VM shutdown')
    p.add_argument('vmid')
    p.add_argument('timeout', type=int, nargs='?', default=120)
    args = p.parse_args(argv)
    sys.exit(shutdown(args.vmid, args.timeout))


if __name__ == '__main__':
    main()
