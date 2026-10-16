"""Factory CLI — the control-plane entry point.

F-01 scaffolds the command surface only. Each subcommand is a stub that
names the work order which implements it (BUILD-ORDER.md).
"""

import argparse
import sys

# subcommand -> the work order that implements it
STUBS: dict[str, str] = {
    "approve": "F-04",
    "run": "F-10",
    "loop": "F-10",
    "status": "F-11",
    "import": "F-12",
    "verify-live": "F-14",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory",
        description="Local agent software factory control plane.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, wo in STUBS.items():
        sub.add_parser(name, help=f"stub — implemented in {wo}")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parses arguments and dispatches a subcommand.

    Args:
        argv: Arguments without the program name; defaults to sys.argv[1:].

    Returns:
        Process exit code. Stubs return 2 (not implemented).
    """
    args = build_parser().parse_args(argv)
    print(
        f"factory {args.command}: not implemented yet (scheduled in {STUBS[args.command]})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
