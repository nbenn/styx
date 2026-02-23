"""python3 -m styx <orchestrate|vm-shutdown> [args...]"""

import sys


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print('Usage: python3 -m styx <orchestrate|vm-shutdown> [args...]',
              file=sys.stderr)
        sys.exit(1)

    cmd  = sys.argv[1]
    argv = sys.argv[2:]

    if cmd == 'orchestrate':
        from styx.orchestrate import main as run
        run(argv)
    elif cmd == 'vm-shutdown':
        from styx.vm_shutdown import main as run
        run(argv)
    else:
        print(f'Unknown command: {cmd}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
