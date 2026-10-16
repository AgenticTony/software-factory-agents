"""Factory CLI — the control-plane entry point.

``approve`` is real (F-04). Every other subcommand is a stub naming the
work order that implements it (BUILD-ORDER.md).
"""

import argparse
import sys

from factory.approve import (
    ApprovalError,
    approve_task,
    default_approver,
    reconcile_bootstrap,
)
from factory.contract import ContractError
from factory.state import TaskQueue

# subcommand -> the work order that implements it
STUBS: dict[str, str] = {
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

    approve = sub.add_parser("approve", help="approve a task contract (F-04)")
    approve.add_argument("task_id", nargs="?", help="task id to approve, e.g. F-04")
    approve.add_argument(
        "--yes",
        action="store_true",
        help="non-interactive confirmation (refused for risk-flagged tasks)",
    )
    approve.add_argument(
        "--reconcile-bootstrap",
        action="store_true",
        help="approve and advance every entry in the bootstrap ledger",
    )
    approve.add_argument(
        "--queue",
        default=".factory/tasks.json",
        help="queue path (default: %(default)s)",
    )
    approve.add_argument(
        "--ledger",
        default=".factory/BOOTSTRAP.md",
        help="bootstrap ledger path for --reconcile-bootstrap (default: %(default)s)",
    )
    approve.add_argument(
        "--approved-by", default=None, help="approver name (default: git user.name)"
    )

    for name, wo in STUBS.items():
        sub.add_parser(name, help=f"stub — implemented in {wo}")
    return parser


def _run_approve(args: argparse.Namespace) -> int:
    """Dispatch the approve subcommand; refusals exit 1, never a traceback."""
    try:
        approver = args.approved_by or default_approver()
        queue = TaskQueue(args.queue)
        if args.reconcile_bootstrap:
            results = reconcile_bootstrap(
                queue, args.ledger, approved_by=approver, assume_yes=args.yes
            )
            for result in results:
                print(f"{result.task_id}: {result.action} — {result.detail}")
            return 1 if any(r.action == "failed" for r in results) else 0
        status = approve_task(
            queue, args.task_id, approved_by=approver, assume_yes=args.yes
        )
        print(f"{args.task_id}: approved -> {status}")
        return 0
    except (ApprovalError, ContractError) as exc:
        print(f"factory approve: {exc}", file=sys.stderr)
        return 1


def main(argv: list | None = None) -> int:
    """Parses arguments and dispatches a subcommand.

    Args:
        argv: Arguments without the program name; defaults to sys.argv[1:].

    Returns:
        Process exit code. Stubs return 2 (not implemented); approve
        returns 0 on success and 1 on any refusal or decline.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "approve":
        if bool(args.task_id) == bool(args.reconcile_bootstrap):
            parser.error(
                "approve takes exactly one of: a task id, or --reconcile-bootstrap"
            )
        return _run_approve(args)
    print(
        f"factory {args.command}: not implemented yet (scheduled in {STUBS[args.command]})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
