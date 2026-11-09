"""Node-run srt stand-ins, shared by every suite that injects one (F-62).

The launcher exec's srt as ``[node, <cli.js>, ...]`` (F-62 AC-1), so a
stand-in must be JavaScript a node process can run — with the real
cli.js shebang on line 1, so a PATH-resolved launch (the pre-fix shape,
exercised by F-62's mutation checks) would exec it exactly the way the
real one is exec'd. This module is not collected and imports no test
module; it deliberately imports nothing from ``factory`` either — the
pin string arrives as an argument.
"""

from pathlib import Path

STANDIN_SHEBANG = "#!/usr/bin/env node\n"

_TEMPLATE = """{shebang}
// Generated srt stand-in (tests/_support/srt.py) — do not edit by hand.
const args = process.argv.slice(2);
if (args[0] === "--version") {{
  console.log("srt " + {version!r});
  process.exit({version_exit});
}}
{body}\
"""


def _run_c_js() -> str:
    """JS that runs the ``-c`` command unsandboxed, like the shims did
    with ``exec /bin/sh -c "$4"`` — faithful to the srt surface the
    drain/pump tests need (the command runs as srt's own child)."""
    return (
        "const cp = require(\"child_process\");\n"
        "const i = args.indexOf(\"-c\");\n"
        "if (i >= 0) {\n"
        "  const r = cp.spawnSync(\"/bin/sh\", [\"-c\", args[i + 1]],\n"
        "                         { stdio: \"inherit\" });\n"
        "  process.exit(r.status === null ? 127 : r.status);\n"
        "}\n"
    )


def write_srt(
    path: Path,
    *,
    version: str,
    body: str | None = None,
    run_c: bool = False,
    version_exit: int = 0,
) -> Path:
    """Write one stand-in srt at ``path`` (executable) and return it.

    Args:
        version: What ``--version`` prints after "srt " (the pin check
            reads the last whitespace token of the last line).
        body: JS replacing everything after the ``--version`` guard —
            full control (async output, sleeps, exits). Default: the
            ``run_c`` handling when asked, else exit 0.
        run_c: With no explicit ``body``: run the ``-c`` command through
            /bin/sh, exiting with its status.
        version_exit: Exit code of the --version branch (a broken pin).
    """
    if body is None:
        body = _run_c_js() if run_c else "process.exit(0);\n"
    path = Path(path)
    path.write_text(
        _TEMPLATE.format(
            shebang=STANDIN_SHEBANG, version=version,
            version_exit=version_exit, body=body,
        )
    )
    path.chmod(0o755)
    return path
