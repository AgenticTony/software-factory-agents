"""Factory CLI — the control-plane entry point.

``approve`` (F-04), ``run`` and ``loop`` (F-10) are real. Every other
subcommand is a stub naming the work order that implements it
(BUILD-ORDER.md).
"""

import argparse
import sys
from pathlib import Path

from factory.approve import (
    ApprovalError,
    approve_task,
    default_approver,
    reconcile_bootstrap,
)
from factory.config import ConfigError, load_config
from factory.contract import ContractError
from factory.gates import GateError
from factory.orchestrator import run_loop, run_once
from factory.prbody import PRBodyError
from factory.progress import ProgressLogError
from factory.publish import PublishError, PushRefused
from factory.runs import RunError
from factory.sandbox import SandboxError
from factory.session import SessionConfigError
from factory.state import TaskQueue

# run/loop exit codes: 0 = a PR opened, nothing to do, or the peak window;
# 1 = an escalation needs you; 3 = the quota stop; 2 = the orchestrator
# itself failed (the task was re-queued, every run dir removed).
_EXIT = {"pr_opened": 0, "idle": 0, "peak_window": 0, "queue_empty": 0,
         "needs_human": 1, "escalation": 1, "quota_exhausted": 3}
_RUN_ERRORS = (ConfigError, ContractError, GateError, PRBodyError, ProgressLogError,
               PublishError, PushRefused, RunError, SandboxError, SessionConfigError)

# subcommand -> the work order that implements it
STUBS: dict[str, str] = {
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

    for name, text in (
        ("run", "pick the next ready task and take it to a PR (F-10)"),
        ("loop", "run until the queue is empty, quota, peak window or escalation (F-10)"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--queue", default=".factory/tasks.json",
                         help="queue path (default: %(default)s)")
        cmd.add_argument("--profile", default=None,
                         help="project.yaml path (default: next to the queue)")
        cmd.add_argument("--factory-root", default=None,
                         help="factory root (default: ~/factory)")
        cmd.add_argument("--config", default=None,
                         help="factory config (default: <factory-root>/config.yaml)")

    for name, wo in STUBS.items():
        sub.add_parser(name, help=f"stub — implemented in {wo}")
    return parser


def _run_approve(args: argparse.Namespace) -> int:
    """Dispatch the approve subcommand; refusals exit 1, never a traceback."""
    try:
        approver = args.approved_by or default_approver()
        queue = TaskQueue(args.queue)
        if args.reconcile_bootstrap:
            # No --yes here by design (see main): every entry is confirmed
            # interactively.
            results = reconcile_bootstrap(queue, args.ledger, approved_by=approver)
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
    except KeyboardInterrupt:
        print("factory approve: interrupted", file=sys.stderr)
        return 130


def _run_orchestrator(args: argparse.Namespace) -> int:
    """Dispatch run/loop; known failures exit 2 with a message, not a traceback."""
    try:
        kwargs = {"factory_root": Path(args.factory_root) if args.factory_root else None}
        if args.config:
            kwargs["config"] = load_config(args.config)
        if args.command == "run":
            result = run_once(Path(args.queue), profile_path=args.profile, **kwargs)
            detail = f" {result.task_id}" if result.task_id else ""
            extra = result.pr or result.escalation or ""
            print(f"factory run: {result.outcome}{detail} {extra}".rstrip())
            return _EXIT[result.outcome]
        loop = run_loop(Path(args.queue), profile_path=args.profile, **kwargs)
        for result in loop.results:
            extra = result.pr or result.escalation or ""
            print(f"  {result.task_id or '-'}: {result.outcome} {extra}".rstrip())
        print(f"factory loop: stopped ({loop.stop_reason}) after {len(loop.results)} run(s)")
        return _EXIT[loop.stop_reason]
    except _RUN_ERRORS as exc:
        print(f"factory {args.command}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"factory {args.command}: interrupted", file=sys.stderr)
        return 130


def main(argv: list | None = None) -> int:
    """Parses arguments and dispatches a subcommand.

    Args:
        argv: Arguments without the program name; defaults to sys.argv[1:].

    Returns:
        Process exit code. Stubs return 2 (not implemented); approve
        returns 0 on success and 1 on any refusal or decline; run/loop
        per ``_EXIT`` (2 when the orchestrator itself failed).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "approve":
        if bool(args.task_id) == bool(args.reconcile_bootstrap):
            parser.error(
                "approve takes exactly one of: a task id, or --reconcile-bootstrap"
            )
        if args.reconcile_bootstrap and args.yes:
            parser.error(
                "--yes is not accepted with --reconcile-bootstrap: the ledger is "
                "plain markdown, so reconcile confirms every entry interactively"
            )
        return _run_approve(args)
    if args.command in ("run", "loop"):
        return _run_orchestrator(args)
    print(
        f"factory {args.command}: not implemented yet (scheduled in {STUBS[args.command]})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
