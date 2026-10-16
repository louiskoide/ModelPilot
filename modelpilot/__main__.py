"""ModelPilot's commands (modelpilot.commands), listed or run.

    python3 -m modelpilot                  every command with its summary; [paid: --live] marks one that can send a
                                           billed request, and only does so behind --live
    python3 -m modelpilot <name> [args]    that command's main with the arguments, as python3 -m modelpilot.<name>
"""
import sys
from . import commands


def listing():
    width = max(len(c.name) for c in commands.COMMANDS)
    lines = ['usage: python3 -m modelpilot <command> [arguments]', '', 'commands:']
    lines += [f'  {c.name:<{width}}  {c.summary}' + ('  [paid: --live]' if c.paid else '') for c in commands.COMMANDS]
    return '\n'.join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(listing())
        return 0
    if commands.find(argv[0]) is None:
        print(f'modelpilot: unknown command {argv[0]!r}; python3 -m modelpilot lists them', file=sys.stderr)
        return 2
    module = commands.load(argv[0])
    saved, sys.argv = sys.argv, [f'python3 -m modelpilot {argv[0]}', *argv[1:]]
    try:
        module.main()  # as python3 -m modelpilot.<name> runs it: a failure exits through SystemExit
    finally:
        sys.argv = saved
    return 0


if __name__ == '__main__':
    sys.exit(main())
