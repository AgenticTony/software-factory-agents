# Vet corpus (F-22 AC-3)

One seeded candidate per `factory toolkit vet` failure class, plus a
clean one that must pass. Each directory is a candidate item: every file
is scanned (prompts and scripts alike), so the seeded marker lives in
the file class it exercises.

| directory | class | marker |
|---|---|---|
| `clean/` | — | allowlisted reference only; must pass |
| `network/` | `network` | URL to a host outside the allowlist |
| `shell/` | `shell` | `subprocess`, `eval(` |
| `credential-read/` | `credential_read` | `~/.ssh/id_rsa`, `~/.aws/credentials` |
| `env-harvest/` | `env_harvest` | `process.env` dump |
| `hidden-chars/` | `hidden_chars` | zero-width space inside prose |
| `prompt-injection/` | `prompt_injection` | "ignore all previous instructions", "do not tell the user" |
| `self-modify/` | `self_modify` | `self-update`, `open(__file__, "w")` |

Not committed, by policy (the F-33 low-entropy rule, extended): the
`encoded_blob` candidate and anything credential-shaped are built at
runtime by `tests/test_f22.py` — no encoded blob or credential-shaped
string ever lives in this repo, and the tests guard that (`test_ac_3`
scans this corpus for both).

The corpus is evidence: a session must not edit what judges admission
(`toolkit/**` is under `paths.protected`).
