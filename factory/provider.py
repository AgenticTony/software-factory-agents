"""The ONE provider resolver (F-59).

Every seat today reached one provider: ``session_env`` hard-coded the
Z.ai base URL and token, so client source would have gone to Z.ai — no
DPA, processing outside the EU, a consumer plan. This module makes the
provider a per-project, owner-approved choice (the profile's
``data_owner`` / ``provider`` / ``providers_allowed`` / ``seat_providers``
blocks are F-63 security groups — a client repo cannot flip itself to
``self``, change its provider or widen its allowed list without the
owner re-approving), records it per run, and refuses a client repo that
has not made the choice (at profile load, in :mod:`factory.contract`).

The attack this module exists to defeat: client source or a credential
reaching a provider or host the owner did not approve — through a
client-written ``project.yaml``, a hostile session, an ambient host
environment, or an error path that quietly falls back to the Z.ai
default. Hence:

* ONE resolver — :func:`resolve_provider`. No call site keeps a
  ``DEFAULT_BASE_URL``/``seat_token`` path of its own; every session
  construction (run, import, eval, rehearsal stubs' profile) resolves
  here. ``ZAI_BASE_URL`` survives only as the zai kind's constant.
* Literal env per kind — :meth:`ProviderSpec.env` returns EXACTLY the
  variable names that provider needs, values built only from the
  resolver's own inputs. Nothing ambient is read on the env path.
* One credential per kind — :func:`provider_token`, the keychain item
  ``factory-<kind>`` (``factory-zai`` unchanged) with a per-kind ambient
  fallback that refuses a token exported for ANOTHER provider (the
  F-06 ambient-URL rule, generalised). Bedrock uses the scoped bearer
  token the pinned CLI reads (``AWS_BEARER_TOKEN_BEDROCK`` — set, the
  CLI skips the AWS credential chain: profiles, files and IMDS are
  never consulted). Vertex cannot run without a credentials FILE or an
  ambient ADC lookup, so it refuses at resolve time rather than weaken
  that rule (spec mismatch, owner's call).
* No fallback, anywhere: a missing credential, an error or a quota is a
  named refusal. Nothing re-resolves a client project to Z.ai.

Env names and hosts were verified against the pinned CLI binary
(claude-agent-sdk 2.1.301, ``_bundled/claude``; ``_cli_version.py``
names the pin) — see the F-59 BOOTSTRAP entry for the exact fragments.
What could not be verified on the wire is on the AC-7 runbook's
checklist.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

__all__ = [
    "ANTHROPIC_BASE_URL_DEFAULT",
    "ProviderError",
    "ProviderSpec",
    "PROVIDER_KINDS",
    "ZAI_BASE_URL",
    "anthropic_family_env",
    "bedrock_env",
    "bedrock_runtime_host",
    "is_model_host",
    "keychain_service",
    "model_host_owner",
    "provider_token",
    "resolve_provider",
    "run_secrets",
]


class ProviderError(Exception):
    """The profile's provider choice cannot be honoured. The message is
    for humans and names the kind, field or credential at fault."""


# The zai kind's base URL — the constant ``session.DEFAULT_BASE_URL``
# was, now owned by the resolver alone (F-59: it may survive ONLY here).
ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
# The first-party endpoint the pinned CLI uses when ANTHROPIC_BASE_URL
# is unset; the anthropic kind sets it explicitly so the env is literal
# and the ambient-URL rule has an exact value to compare against.
ANTHROPIC_BASE_URL_DEFAULT = "https://api.anthropic.com"

PROVIDER_KINDS = ("zai", "anthropic", "bedrock", "vertex",
                  "anthropic_compatible")

# The seat model roles (project.schema.json's provider.models keys).
# F-72 adds ``arbiter`` — the review-dispute seat. It has NO required
# model of its own: it defaults to the reviewer's provider and model
# (resolve_provider routes an un-overridden arbiter seat at the reviewer
# role), so an existing profile needs no change to run one, and the
# owner may override it with ``seat_providers.arbiter`` under the same
# providers_allowed rule as every seat (an F-63 security group, so the
# override re-approval-gates the profile).
SEAT_ROLES = ("implementer", "reviewer", "verifier", "importer", "arbiter")

# Seats whose default resolution is ANOTHER seat's: the arbiter runs the
# reviewer's provider and model unless the owner overrode it (F-72 AC-2).
SEAT_DEFAULTS = {"arbiter": "reviewer"}

# One keychain item per kind: ``factory-<kind>``. ``factory-zai`` is the
# production item DESIGN D2 named, unchanged (keychain_service("zai") is
# exactly session.KEYCHAIN_SERVICE's value).
KEYCHAIN_PREFIX = "factory-"


def keychain_service(kind: str) -> str:
    """The keychain item name holding ``kind``'s credential."""
    return f"{KEYCHAIN_PREFIX}{kind}"


# ------------------------------------------------------------- endpoints --

# Verified against the pinned CLI (2.1.301): ``ANTHROPIC_BEDROCK_BASE_URL
# || `https://bedrock-runtime.${region}.amazonaws.com``` — region from
# ``AWS_REGION`` then ``AWS_DEFAULT_REGION`` (``En()``), then the AWS
# shared config (never read here: the resolver always sets AWS_REGION).
def bedrock_runtime_host(region: str) -> str:
    """The Bedrock runtime inference host for ``region`` (pattern-checked
    at load — ``region`` is never free text reaching a host)."""
    return f"bedrock-runtime.{region}.amazonaws.com"


def vertex_hosts(region: str) -> tuple[str, ...]:
    """The Vertex inference hosts for ``region`` (the CLI's LSt switch:
    global → aiplatform.googleapis.com; us/eu → the rep host; otherwise
    the regional ``{region}-aiplatform`` form). The kind refuses at
    resolve time today; the list exists so the egress allowlist rule
    (another provider's model host) still recognises Vertex hosts."""
    if region == "global":
        return ("aiplatform.googleapis.com",)
    if region in ("us", "eu"):
        return (f"aiplatform.{region}.rep.googleapis.com",)
    return (f"{region}-aiplatform.googleapis.com",)


# The zai inference host (the endpoint ZAI_BASE_URL names).
ZAI_HOST = "api.z.ai"

# The model-provider host FAMILIES (R1-1/R1-4): an egress allowlist
# entry or an anthropic_compatible base_url naming a host one of these
# families owns refuses the profile at load unless it is exactly the
# project's own inference host (F-59 AC-3) — the kind label is not the
# control; the host is.
#
# Verified where: the Bedrock forms are the pinned CLI's own URL
# builders (bedrock-runtime.${region}.amazonaws.com plus the SDK's FIPS
# and dual-stack variants bedrock-runtime-fips.*.amazonaws.com /
# *.api.aws in the bundled binary's aws-sdk rules) and the public AWS
# partition shapes (amazonaws.com.cn). The Vertex forms are the CLI's
# LSt switch. The Z.ai OPERATOR family — z.ai and bigmodel.cn plus
# their subdomains — is NOT in the CLI binary (third-party hosts the
# factory supports): its basis is docs/provider-options-2026-11-05.md
# (GLM = Zhipu, the Z.ai plan) plus the round-1 review probe naming
# open.bigmodel.cn as Zhipu's other Anthropic endpoint; whether Zhipu
# exposes further endpoints is an UNVERIFIED runbook item (the family
# rule covers new subdomains structurally).
def model_host_owner(host: str) -> str | None:
    """Which provider kind owns ``host`` as a model endpoint, or None
    when it is not a known model host.

    Family-level, matched on labels so a subdomain cannot smuggle a
    suffix (``bedrock-runtime.amazonaws.com.evil.com`` is not the AWS
    host; ``notz.ai`` is not the Z.ai operator): the Z.ai operator is
    any ``z.ai``/``bigmodel.cn`` domain; Anthropic any
    ``anthropic.com`` domain; Bedrock ANY ``bedrock*`` label under the
    AWS partitions (amazonaws.com, amazonaws.com.cn, api.aws — a
    PrivateLink endpoint name or a subdomain-prefixed host included);
    Vertex the aiplatform forms under googleapis.com."""
    lowered = (host or "").strip().lower().rstrip(".")
    if not lowered or "." not in lowered:
        return None
    labels = lowered.split(".")
    last_two = tuple(labels[-2:])
    if last_two == ("z", "ai") or last_two == ("bigmodel", "cn"):
        return "zai"
    if last_two == ("anthropic", "com"):
        return "anthropic"
    under_aws_partition = (
        last_two == ("amazonaws", "com")
        or last_two == ("api", "aws")
        or tuple(labels[-3:]) == ("amazonaws", "com", "cn"))
    if under_aws_partition and any(
            label.startswith("bedrock") for label in labels[:-2]):
        return "bedrock"
    if last_two == ("googleapis", "com") and (
            labels[0] == "aiplatform"
            or (len(labels[0]) > 11 and labels[0].endswith("-aiplatform"))
            or (len(labels) == 4 and labels[1] == "aiplatform"
                and labels[2] in ("us", "eu", "rep"))):
        return "vertex"
    return None


def is_model_host(host: str) -> bool:
    """Whether ``host`` is a known model-provider host (any kind)."""
    return model_host_owner(host) is not None


# ------------------------------------------------------------------ spec --

# The literal env builders. Each returns EXACTLY the names its provider
# needs — nothing read, nothing inherited (the F-06 session_env rule,
# one builder per shape).


def anthropic_family_env(base_url: str, token: str,
                         timeout_ms: int) -> dict[str, str]:
    """zai / anthropic / anthropic_compatible: the three Anthropic-API
    seat names (the old ``session_env``, unchanged shape)."""
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": token,
        "API_TIMEOUT_MS": str(int(timeout_ms)),
    }


def bedrock_env(bearer_token: str, region: str,
                timeout_ms: int) -> dict[str, str]:
    """bedrock: the pinned CLI's native Bedrock shape — the use flag,
    the scoped bearer token (set, the CLI's AWS credential chain never
    runs: no ~/.aws, no profiles, no IMDS), the region, the timeout."""
    return {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_BEARER_TOKEN_BEDROCK": bearer_token,
        "AWS_REGION": region,
        "API_TIMEOUT_MS": str(int(timeout_ms)),
    }


@dataclass(frozen=True)
class ProviderSpec:
    """One seat's resolved provider (F-59): where inference goes, which
    model, which env names, which credential — and nothing else.

    NEVER holds a credential VALUE (``repr`` is safe): the token is
    looked up per session by :func:`provider_token` or passed by a
    caller that already holds it.
    """

    kind: str
    model: str | None
    endpoint_host: str
    egress_hosts: tuple[str, ...]
    env_keys: tuple[str, ...]
    credential_key: str
    region: str | None = None
    base_url: str | None = None

    def env(self, credential: str, timeout_ms: int) -> dict[str, str]:
        """The literal session env for this provider (exactly the names
        in ``env_keys``; asserts the builder agrees)."""
        if self.kind == "bedrock":
            built = bedrock_env(credential, self.region or "", timeout_ms)
        else:
            built = anthropic_family_env(
                self.base_url or "", credential, timeout_ms)
        if tuple(built) != self.env_keys:
            raise ProviderError(
                f"internal provider inconsistency: the {self.kind} env "
                f"builder produced {sorted(built)} but the spec names "
                f"{sorted(self.env_keys)}"
            )
        return built

    def provider_record(self) -> dict[str, Any]:
        """The run-record block: kind, model, region or endpoint host.
        NEVER a credential (asserted by tests/test_f59.py::test_ac_4)."""
        record: dict[str, Any] = {"kind": self.kind}
        if self.model is not None:
            record["model"] = self.model
        if self.region is not None:
            record["region"] = self.region
        else:
            record["endpoint_host"] = self.endpoint_host
        return record


# ------------------------------------------------------------ resolution --

def _own_specs(profile: Mapping[str, Any]) -> "list[ProviderSpec]":
    """Every provider spec this project's seats resolve to: the
    project's own (``seat=None``) plus one per seat role. The ONE
    source for both the egress-allowlist rule and
    :func:`run_secrets` — the scrub set and the session credentials
    come from one resolution by construction (R1-3). A role whose
    override cannot resolve (vertex today) is skipped here: it has no
    literal credential to hold, and the session seam refuses it by
    name."""
    specs: "list[ProviderSpec]" = []
    for role in (None, *SEAT_ROLES):
        try:
            specs.append(resolve_provider(profile, role))
        except ProviderError:
            continue
    return specs


def _own_kinds(profile: Mapping[str, Any]) -> set[str]:
    """The provider kinds this project may reach: its own provider plus
    every seat override's kind (each already validated against
    providers_allowed at load). Used by the egress-allowlist rule."""
    kinds = set()
    block = profile.get("provider")
    if isinstance(block, Mapping) and block.get("kind"):
        kinds.add(str(block["kind"]))
    seats = profile.get("seat_providers")
    if isinstance(seats, Mapping):
        for entry in seats.values():
            if isinstance(entry, Mapping) and entry.get("kind"):
                kinds.add(str(entry["kind"]))
    return kinds


def resolve_provider(profile: Mapping[str, Any],
                     seat: str | None = None) -> ProviderSpec:
    """Resolve the provider for ``profile`` (and one seat model role).

    Args:
        profile: The loaded, defaulted profile mapping — the SAME dict
            the run builds settings from (hash-once, never re-read).
        seat: The seat model role (implementer/reviewer/verifier/
            importer); None resolves the project's own provider (the
            gate runner's settings and the SDK probe — no seat model).

    Returns:
        The :class:`ProviderSpec`. No credential is looked up here.

    Raises:
        ProviderError: The seat names an override outside
            ``providers_allowed`` (load also refuses it — this is the
            backstop for hand-built dicts), or the kind is ``vertex``
            (no literal-credential path exists: it needs a credentials
            FILE or ambient ADC, which this factory never passes —
            refusing is the rule, not a gap to patch around).
    """
    block = profile.get("provider")
    kind = str(block.get("kind")) if isinstance(block, Mapping) else "zai"
    model = None
    region = None
    base_url = None
    if isinstance(block, Mapping):
        models = block.get("models") or {}
        region = block.get("region")
        base_url = block.get("base_url")
    else:
        models = {}

    if seat is not None:
        override = (profile.get("seat_providers") or {}).get(seat)
        if isinstance(override, Mapping) and override:
            allowed = profile.get("providers_allowed") or []
            if override.get("kind") not in allowed:
                raise ProviderError(
                    f"seat {seat!r} names provider {override.get('kind')!r} "
                    f"which is not in the project's providers_allowed "
                    f"{allowed!r} — a seat runs the project's provider or "
                    "one the owner allowed (F-59 AC-5)"
                )
            kind = str(override.get("kind"))
            model = override.get("model")
            region = override.get("region")
            base_url = override.get("base_url")
        elif seat in SEAT_DEFAULTS:
            # F-72 AC-2: an un-overridden arbiter IS the reviewer's seat —
            # the reviewer's provider override (if any) and the reviewer's
            # model. The owner's pending decision is the override hook;
            # the default picks nothing new.
            return resolve_provider(profile, SEAT_DEFAULTS[seat])
        else:
            model = models.get(seat)

    if kind == "vertex":
        # The spec mismatch, named (see the module docstring): Vertex
        # authenticates through google-auth-library ADC — a credentials
        # file or an ambient lookup — and the CLI exposes no literal
        # token env for it. The factory never passes credentials files
        # or ambient lookups, so this kind refuses until a
        # literal-credential path exists (an owner decision, not a
        # silent weakening of the rule).
        raise ProviderError(
            "the vertex provider cannot run in this factory: it has no "
            "literal-credential path (the pinned CLI authenticates "
            "Vertex through Application Default Credentials — a file or "
            "an ambient lookup this factory never passes). Refusing at "
            "resolve time rather than weaken the no-ambient-credentials "
            "rule; see the F-59 spec mismatch in BOOTSTRAP.md"
        )

    if kind == "bedrock":
        # region is load-validated for bedrock; a hand-built dict that
        # skipped validation gets the same refusal here.
        if not region:
            raise ProviderError(
                "the bedrock provider requires a region (pattern-checked "
                "at profile load); refusing to guess one"
            )
        host = bedrock_runtime_host(str(region))
        return ProviderSpec(
            kind=kind, model=model, region=str(region),
            endpoint_host=host, egress_hosts=(host,),
            env_keys=("CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK",
                      "AWS_REGION", "API_TIMEOUT_MS"),
            credential_key="AWS_BEARER_TOKEN_BEDROCK",
        )

    if kind == "anthropic_compatible":
        if not base_url:
            raise ProviderError(
                "the anthropic_compatible provider requires base_url "
                "(https only, exact host — validated at profile load)"
            )
        return ProviderSpec(
            kind=kind, model=model, base_url=str(base_url),
            endpoint_host=urlsplit(str(base_url)).hostname or "",
            egress_hosts=(urlsplit(str(base_url)).hostname or "",),
            env_keys=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                      "API_TIMEOUT_MS"),
            credential_key="ANTHROPIC_AUTH_TOKEN",
        )

    if kind == "anthropic":
        return ProviderSpec(
            kind=kind, model=model, base_url=ANTHROPIC_BASE_URL_DEFAULT,
            endpoint_host="api.anthropic.com",
            egress_hosts=("api.anthropic.com",),
            env_keys=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                      "API_TIMEOUT_MS"),
            credential_key="ANTHROPIC_AUTH_TOKEN",
        )

    if kind == "zai":
        return ProviderSpec(
            kind=kind, model=model, base_url=ZAI_BASE_URL,
            endpoint_host="api.z.ai", egress_hosts=("api.z.ai",),
            env_keys=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                      "API_TIMEOUT_MS"),
            credential_key="ANTHROPIC_AUTH_TOKEN",
        )

    raise ProviderError(
        f"unknown provider kind {kind!r}: expected one of "
        f"{list(PROVIDER_KINDS)}"
    )


# ------------------------------------------------------------ credentials --

def _keychain_token(service: str) -> str | None:
    """The ``service`` keychain item's secret, or None where absent.

    Generalised from session._keychain_token (same behaviour, per-kind
    service name); its own function so tests stub the keychain step and
    exercise the env-fallback rules hermetically on a Mac that has the
    item. Never called for a kind whose item the owner has not created —
    a missing item is the no-credential state callers refuse on."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # no macOS keychain here (or it hung) — try the env
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def provider_token(kind: str, *, base_url: str | None = None) -> str | None:
    """The credential for ``kind``, or None where no credential exists.

    Lookup order: the macOS keychain item ``factory-<kind>``, then a
    per-kind ambient fallback that refuses a token exported for ANOTHER
    provider — the F-06 ambient-URL rule, generalised per kind:

    * zai / anthropic / anthropic_compatible read ``ANTHROPIC_AUTH_TOKEN``
      only when the ambient ``ANTHROPIC_BASE_URL`` names THAT kind's
      endpoint (zai's URL, api.anthropic.com, or the project's
      compatible base_url). A token exported with no URL belongs to
      api.anthropic.com: accepted for ``anthropic``, refused for the
      others — exactly the F-06 reading.
    * bedrock reads ``AWS_BEARER_TOKEN_BEDROCK`` — a name that can only
      ever mean Bedrock, so no URL check applies; the ANTHROPIC_* names
      are never read for it.

    On the Linux CI runner neither exists and this returns None, which
    callers must treat as "no session can authenticate here".
    """
    keychain = _keychain_token(keychain_service(kind))
    if keychain is not None:
        return keychain
    if kind == "bedrock":
        return os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or None
    if kind not in ("zai", "anthropic", "anthropic_compatible"):
        return None
    expected = {
        "zai": ZAI_BASE_URL,
        "anthropic": ANTHROPIC_BASE_URL_DEFAULT,
        "anthropic_compatible": base_url or "",
    }[kind]
    ambient_url = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if kind == "anthropic" and not ambient_url:
        ambient_url = ANTHROPIC_BASE_URL_DEFAULT  # no URL = first-party
    if ambient_url != expected.rstrip("/"):
        return None
    return os.environ.get("ANTHROPIC_AUTH_TOKEN") or None


def run_secrets(profile: Mapping[str, Any]) -> tuple[str, ...]:
    """Every provider token this run holds, deduplicated, empty strings
    dropped: one lookup per provider SPEC the seats resolve to (R1-3 —
    each with its own base_url, so an anthropic_compatible seat override
    on the ambient path contributes the token its own resolution would
    send, not a lookup keyed to the project block's URL).

    The ONE source for ``egress_secrets``/``extra_secrets`` tuples (the
    orchestrator, the importer, the CLI's keep path), so a Bedrock token
    is scrubbed exactly as the Z.ai token is today — one helper, not
    five copies. A spec with no credential contributes nothing (the run
    will refuse at its own session seam with the kind named)."""
    tokens: list[str] = []
    for spec in _own_specs(profile):
        token = provider_token(spec.kind, base_url=spec.base_url)
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)
