# Vet report — strict-api

- candidate: `toolkit/skills/strict-api`
- content hash: `0bee064718cb39eadc774d232ac85f5035f730a4f2f046f6acfd558aa864e236`
- licence: MIT
- allowlists: fastapi.tiangolo.com, docs.sqlalchemy.org, alembic.sqlalchemy.org, docs.pydantic.dev, pypdf.readthedocs.io, supabase.com, nextjs.org, react.dev, tailwindcss.com, docs.z.ai, resend.com, developers.cloudflare.com, render.com, pypi.org, files.pythonhosted.org, registry.npmjs.org, fonts.googleapis.com, fonts.gstatic.com
- findings: 0

No findings. A clean scan is admission *evidence*, never admission: the item enters the index only through an owner-approved PR (F-22 AC-4).

## Provenance

- upstream: https://github.com/alirezarezvani/claude-skills (`engineering/strict-api`)
- pinned commit: `0496583603e56cdd5022dd61c823bd784a80a752` (repo HEAD at fetch, 2026-10-29)
- vendored verbatim: `SKILL.md` byte-identical to the pinned tree; the upstream repo's
  MIT `LICENSE` rides beside it (the skill dir ships no licence of its own).
- vetted: 2026-10-30, against JobFinderOS's `docs_allowlist`/`network_allowlist`
  (read from `origin/main:.factory/project.yaml` at 2026-10-29).
- admission: this report rides the owner-approved admission PR (F-22 AC-4);
  the scan is evidence, never admission.
