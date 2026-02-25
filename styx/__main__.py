"""python3 -m styx <orchestrate|vm-shutdown|local-shutdown> [args...]"""

import sys


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ('-v', '--version'):
        from styx import __version__
        print(__version__)
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        from styx import __version__
        print(f'''\
styx {__version__} — graceful cluster shutdown for Proxmox + Kubernetes + Ceph

Usage: styx <command> [args...]

Commands:
  orchestrate     Coordinate full cluster shutdown across all nodes
  vm-shutdown     Gracefully shut down a single VM via QMP/ACPI
  local-shutdown  Shut down local workloads and optionally power off host

Options:
  -h, --help      Show this help message
  -v, --version   Show version

Run "styx <command> --help" for command-specific options.''', file=sys.stderr)
        sys.exit(0 if sys.argv[1:] else 1)

    cmd  = sys.argv[1]
    argv = sys.argv[2:]

    if cmd == 'orchestrate':
        from styx.orchestrate import main as run
        run(argv)
    elif cmd == 'vm-shutdown':
        from styx.vm_shutdown import main as run
        run(argv)
    elif cmd == 'local-shutdown':
        from styx.local_shutdown import main as run
        run(argv)
    else:
        print(f'Unknown command: {cmd}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
