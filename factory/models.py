"""The model registry and the ONE role resolver (F-100).

Models change every two to three months. Before F-100 every project set
its seats' models in its own profile (F-59 ``provider.models``), both
reviewer seats shared one model, and moving the factory to a new GLM
meant editing and re-approving every profile. This module is the one
owner-maintained registry — the ``models`` block of the factory config
(:mod:`factory.config`) — plus the ONE resolver every seat goes through:

* :func:`resolve_role` — ``(profile, config, role)`` → the seat's
  :class:`~factory.provider.ProviderSpec` (kind, model with the alias
  expanded, region/base_url, and the record facts ``role``/``stack``).
  Every session construction resolves here; no call site keeps a model
  id or a provider default of its own (the AC-2 grep test enforces it).
* :func:`check_profile_stack` — the load-time sweep: every role the
  stack names resolves, inside what the project approved.
* :func:`stack_for` — the onboarding answer → restriction + stack map.

The attack this module exists to defeat: a client project silently
running a model or provider it did not approve because a SHARED stack
was edited. So the project's own approved ``providers_allowed`` (F-63
security group) and its recorded ``model_restrictions`` answer stay the
authority — a stack edit changes models freely inside them and can never
move a project outside them: that refuses at load, named
(project, role, kind), and never falls back to another provider.

F-59's blanket refusal of a client project on ``zai`` is REPLACED by the
recorded restriction answer (owner decision 2026-11-18: clients are open
by default). The F-59 ``provider.models`` block still works for one
release as a project-level override, with a deprecation note for the health-check
command F-66 adds (:func:`deprecation_notes`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from factory.provider import (
    PROVIDER_KINDS,
    ProviderError,
    ProviderSpec,
    _build_spec,
)

__all__ = [
    "DEFAULT_REGISTRY",
    "DEFAULT_STACK_NAME",
    "MODEL_ID_RE",
    "MODEL_ROLES",
    "REQUIRED_STACK_ROLES",
    "RESTRICTIONS",
    "ROLE_DEFAULTS",
    "RegistryError",
    "RoleTarget",
    "ModelStack",
    "ModelRegistry",
    "StackChoice",
    "check_profile_stack",
    "deprecation_notes",
    "expand_alias",
    "model_family",
    "model_ids_outside_registry",
    "registry_from_raw",
    "resolve_role",
    "reviewer_seat_role",
    "seat_role",
    "stack_for",
]


class RegistryError(Exception):
    """The model registry is malformed. The message is for humans and
    names the alias, role, provider or stack at fault. :func:`factory.
    config.load_config` wraps it in :class:`factory.config.ConfigError`;
    alias expansion at resolve time raises it directly."""


# ------------------------------------------------------------------ roles --

# The role names (F-100 AC-2). Fine enough that any area of the factory
# swaps on its own: the two reviewer lenses are separate roles, so seat 2
# (the defect hunt) can run another family, and F-97's final-review
# tiers read ``final_review.fix``/``final_review.strong`` from the same
# stack. ``arbiter`` and ``contract_reviewer`` have no required entry of
# their own (F-72/F-80): a stack that leaves them out runs them on
# ``reviewer.conformance`` (ROLE_DEFAULTS).
MODEL_ROLES = (
    "implementer",
    "reviewer.conformance",
    "reviewer.hunt",
    "verifier",
    "arbiter",
    "contract_reviewer",
    "importer",
    "final_review.fix",
    "final_review.strong",
)

ROLE_DEFAULTS = {
    "arbiter": "reviewer.conformance",
    "contract_reviewer": "reviewer.conformance",
}

# Every role a stack MUST name (the AC-1 "missing required role"
# refusal): the AC-2 list minus the two that default to conformance.
REQUIRED_STACK_ROLES = tuple(
    role for role in MODEL_ROLES if role not in ROLE_DEFAULTS)

RESTRICTIONS = ("none", "no_china", "contained")
DEFAULT_STACK_NAME = "standard"

# The onboarding answer → (restriction, stack) table (AC-6). One pure
# mapping; F-69's interview calls it and shows the proposal.
RESTRICTION_STACKS = {
    "none": "standard",
    "no_china": "no-china",
    "contained": "contained",
}

# A role name maps back to the F-59 SEAT whose seat_providers entry pins
# it: both reviewer lenses are the reviewer seat, F-97's final-review
# tiers are the final_review seat (schema-admitted), everything else is
# itself.
def seat_for_role(role: str) -> str:
    if role.startswith("reviewer."):
        return "reviewer"
    if role.startswith("final_review."):
        return "final_review"
    return role


def seat_role(seat: str) -> str:
    """The default ROLE a seat name resolves as. ``reviewer`` without
    its lens named is the conformance review (the base prompt IS the
    conformance lens, F-94); every fan-out passes each seat's own role
    explicitly."""
    if seat == "reviewer":
        return "reviewer.conformance"
    return seat


def reviewer_seat_role(seat_no: int, reviewers: int) -> str:
    """Reviewer seat ``seat_no`` (1-based) of an N-seat fan-out, as a
    registry role: the lens :func:`factory.prompts.reviewer_seat_lens`
    assigns the seat, so seat 1 and seat 2 can run different models
    exactly as they run different prompts."""
    from factory.prompts import reviewer_seat_lens

    lens = reviewer_seat_lens(seat_no, reviewers)
    return "reviewer.conformance" if lens in ("conformance", "both") \
        else "reviewer.hunt"


# ------------------------------------------------------------ model ids --

# The model-id families the factory knows (the AC-2 grep covers exactly
# these shapes): GLM ids belong to the zai family, Claude ids to the
# anthropic one (Bedrock serves Claude ids under its own kind). A model
# of another shape is a literal id the registry takes as written.
_MODEL_ID_RE = re.compile(
    r"\b(glm|claude)-[a-z0-9][a-z0-9._-]*\b", re.IGNORECASE)
MODEL_ID_RE = _MODEL_ID_RE

# The AC-2 grep's own, stricter shape: a model id always carries a
# version digit somewhere (the factory's convention — aliases are
# words, ids are versions), so package and product names the looser
# family shape would catch (claude-agent-sdk, claude-code) do not fire
# the grep.
_GREP_ID_RE = re.compile(
    r"\b(glm|claude)-[a-z0-9][a-z0-9._-]*[0-9][a-z0-9._-]*\b",
    re.IGNORECASE)


def model_family(model_id: str) -> "str | None":
    """Which provider family ``model_id`` belongs to by its id shape
    (``"zai"`` for glm-*, ``"anthropic"`` for claude-*), or None for an
    unrecognised shape.

    The registry's cross-check reads this: an alias that would point a
    kind's role at ANOTHER family's model (the hostile "alias pointing
    at a model of another provider") refuses at parse, named. Bedrock
    runs Claude ids, so the anthropic family is legal there;
    anthropic_compatible is a BYO endpoint (the self-hosted GLM
    variant) and takes any family."""
    match = _MODEL_ID_RE.fullmatch(str(model_id or "").strip())
    if match is None:
        return None
    return "zai" if match.group(1).lower() == "glm" else "anthropic"


# Per kind, which model-id families may fill its roles. zai runs GLM;
# anthropic and bedrock run Claude; vertex refuses at resolve anyway
# (F-59) and takes either shape at parse; anthropic_compatible is BYO.
_FAMILY_FOR_KIND = {
    "zai": {"zai"},
    "anthropic": {"anthropic"},
    "bedrock": {"anthropic"},
    "vertex": {"zai", "anthropic"},
    "anthropic_compatible": {"zai", "anthropic", None},
}


# ------------------------------------------------------------- registry --

@dataclass(frozen=True)
class RoleTarget:
    """One stack's entry for one role: the provider kind, the model (an
    id or an alias — expanded at resolve, never at parse, so an alias
    bump is one line with no re-approval), and the region a bedrock
    entry may pin (absent: the project's own provider region — the
    client's region — applies)."""
    kind: str
    model: str
    region: "str | None" = None


@dataclass(frozen=True)
class ModelStack:
    """A named stack: every required role's target, and whether the
    stack is contained (the ``contained`` restriction reads this)."""
    name: str
    contained: bool
    roles: Mapping[str, RoleTarget]


@dataclass(frozen=True)
class ModelRegistry:
    """The parsed ``models`` block: aliases, stacks, provider origins,
    the kinds marked contained, and prices (the F-59 price table, moved
    here)."""
    aliases: Mapping[str, str]
    stacks: Mapping[str, ModelStack]
    providers: Mapping[str, str]
    contained_kinds: "frozenset[str]"
    prices: "dict[str, dict[str, dict[str, float]]]"

    def model_ids(self) -> "set[str]":
        """Every model id and alias the registry names — the allowed
        set for the AC-2 grep over factory sources."""
        ids = set(self.aliases)
        ids.update(self.aliases.values())
        for stack in self.stacks.values():
            for target in stack.roles.values():
                ids.add(target.model)
        for models in self.prices.values():
            ids.update(models)
        return ids


def _string(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{what} must be a non-empty string, got {value!r}")
    return value.strip()


def registry_from_raw(raw: Any) -> ModelRegistry:
    """Parse and validate the ``models`` block.

    Raises:
        RegistryError: an unknown role, an alias cycle, a dangling
            alias (a stack model no alias declares and no known family
            shapes), an alias chain that never ends at a model id, an
            unknown provider kind, a stack missing a required role, or
            an alias pointing a kind's role at another provider's model
            family — each naming the offender. :func:`factory.config.
            load_config` wraps this in :class:`ConfigError`.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise RegistryError(f"models must be a mapping, got {raw!r}")
    unknown = sorted(set(raw) - {"aliases", "stacks", "providers", "prices"})
    if unknown:
        raise RegistryError(f"models has unknown keys {unknown}")

    aliases_raw = raw.get("aliases") or {}
    if not isinstance(aliases_raw, Mapping):
        raise RegistryError(f"models.aliases must be a mapping, got "
                            f"{aliases_raw!r}")
    aliases: dict[str, str] = {}
    for name, value in aliases_raw.items():
        aliases[_string(name, "an alias name")] = _string(
            value, f"alias {name!r}")

    providers_raw = raw.get("providers") or {}
    if not isinstance(providers_raw, Mapping):
        raise RegistryError(f"models.providers must be a mapping, got "
                            f"{providers_raw!r}")
    providers: dict[str, str] = {}
    contained_kinds: set[str] = set()
    for kind, block in providers_raw.items():
        kind = _string(kind, "a provider kind")
        if kind not in PROVIDER_KINDS:
            raise RegistryError(
                f"models.providers names unknown provider kind {kind!r}: "
                f"expected one of {list(PROVIDER_KINDS)}")
        if not isinstance(block, Mapping):
            raise RegistryError(f"models.providers.{kind} must be a "
                                f"mapping, got {block!r}")
        origin = block.get("origin")
        providers[kind] = _string(origin, f"models.providers.{kind}.origin")
        # F-100 R2: only an explicit contained: true marks a kind as one
        # the ``contained`` restriction may run (the fail-closed default
        # is NOT contained — an unmarked kind never carries contained
        # code, whatever it is called).
        if block.get("contained"):
            if not isinstance(block.get("contained"), bool):
                raise RegistryError(
                    f"models.providers.{kind}.contained must be true or "
                    f"false, got {block['contained']!r}")
            contained_kinds.add(kind)

    def check_target(where: str, entry: Any) -> RoleTarget:
        if not isinstance(entry, Mapping) or set(entry) - {
                "kind", "model", "region"}:
            raise RegistryError(
                f"{where} must name exactly kind, model (and region for a "
                f"pinned bedrock role), got {entry!r}")
        kind = _string(entry.get("kind"), f"{where}.kind")
        if kind not in PROVIDER_KINDS:
            raise RegistryError(
                f"{where} names unknown provider kind {kind!r}: expected "
                f"one of {list(PROVIDER_KINDS)}")
        model = _string(entry.get("model"), f"{where}.model")
        family = model_family(model)
        if model not in aliases:
            # A family-shaped name with no version digit is an alias
            # reference (the factory's convention: aliases are words,
            # ids are versions); one no alias declares is dangling.
            if family is not None and not _is_literal_model_id(model):
                raise RegistryError(
                    f"{where} names {model!r}, a family-shaped alias name "
                    "no alias declares — declare it under models.aliases "
                    "or write the literal model id (dangling alias)")
            if family is None and not any(
                    character.isdigit() for character in model):
                raise RegistryError(
                    f"{where} names {model!r}, which no alias declares and "
                    "no known model-id family shapes — a dangling alias "
                    "(declare it under models.aliases or write the model id)")
        else:
            family = model_family(expand_chain(aliases, model, where))
        legal = _FAMILY_FOR_KIND.get(kind, set())
        if family is not None and family not in legal:
            raise RegistryError(
                f"{where} is kind {kind!r} but model {model!r} belongs to "
                f"the {family} family — an alias must not point a role at "
                "another provider's model")
        region = entry.get("region")
        if region is not None:
            region = _string(region, f"{where}.region")
        return RoleTarget(kind=kind, model=model, region=region)

    stacks_raw = raw.get("stacks") or {}
    if not isinstance(stacks_raw, Mapping) or not stacks_raw:
        raise RegistryError(f"models.stacks must be a non-empty mapping, "
                            f"got {stacks_raw!r}")
    stacks: dict[str, ModelStack] = {}
    for name, block in stacks_raw.items():
        name = _string(name, "a stack name")
        if not isinstance(block, Mapping) or set(block) != {
                "contained", "roles"}:
            raise RegistryError(
                f"models.stacks.{name} needs exactly: contained, roles "
                f"(got {sorted(block) if isinstance(block, Mapping) else block!r})")
        contained = block.get("contained")
        if not isinstance(contained, bool):
            raise RegistryError(
                f"models.stacks.{name}.contained must be true or false, "
                f"got {contained!r}")
        roles_raw = block.get("roles") or {}
        if not isinstance(roles_raw, Mapping):
            raise RegistryError(f"models.stacks.{name}.roles must be a "
                                f"mapping, got {roles_raw!r}")
        roles: dict[str, RoleTarget] = {}
        for role, entry in roles_raw.items():
            role = _string(role, "a role name")
            if role not in MODEL_ROLES:
                raise RegistryError(
                    f"models.stacks.{name}.roles names unknown role "
                    f"{role!r}: expected one of {list(MODEL_ROLES)} "
                    "(reviewer.hunter, say, is not a role)")
            roles[role] = check_target(
                f"models.stacks.{name}.roles.{role}", entry)
        missing = [role for role in REQUIRED_STACK_ROLES if role not in roles]
        if missing:
            raise RegistryError(
                f"models.stacks.{name} is missing required role(s) "
                f"{missing} — a stack names every role but arbiter and "
                "contract_reviewer (they default to reviewer.conformance)")
        stacks[name] = ModelStack(name=name, contained=contained, roles=roles)

    prices = prices_from_raw(raw.get("prices")) if "prices" in raw else {}
    return ModelRegistry(aliases=dict(aliases), stacks=dict(stacks),
                         providers=dict(providers),
                         contained_kinds=frozenset(contained_kinds),
                         prices=prices)


_PRICE_FIELDS = ("input", "cache_read", "cache_write", "output")


def prices_from_raw(raw: Any) -> "dict[str, dict[str, dict[str, float]]]":
    """Validate a price table: kind → model → the four per-million
    rates, each a finite number >= 0. A malformed table is a
    RegistryError — a wrong price silently applied to every status page
    is worse than a loud refusal. (``factory.config._prices`` delegates
    here, keeping the dependency one-directional: config imports
    models, never the reverse at import time.)"""
    if not isinstance(raw, Mapping):
        raise RegistryError(f"prices must be a mapping, got {raw!r}")
    table: "dict[str, dict[str, dict[str, float]]]" = {}
    for kind, models in raw.items():
        if not isinstance(models, Mapping):
            raise RegistryError(f"prices.{kind} must be a mapping, "
                                f"got {models!r}")
        for model, rates in models.items():
            if not isinstance(rates, Mapping) or set(rates) != set(
                    _PRICE_FIELDS):
                raise RegistryError(
                    f"prices.{kind}.{model} needs exactly "
                    f"{list(_PRICE_FIELDS)}, got "
                    f"{sorted(rates) if isinstance(rates, Mapping) else rates!r}"
                )
            for field_name, value in rates.items():
                if isinstance(value, bool) \
                        or not isinstance(value, (int, float)) or value < 0:
                    raise RegistryError(
                        f"prices.{kind}.{model}.{field_name} must be a "
                        f"number >= 0, got {value!r}"
                    )
            table.setdefault(str(kind), {})[str(model)] = {
                name: float(rates[name]) for name in _PRICE_FIELDS
            }
    return table


def _is_literal_model_id(model: str) -> bool:
    """Whether ``model`` reads as a literal model id: a known family
    shape (glm-…/claude-…) that carries a version digit somewhere. The
    factory's convention is that aliases are words (glm-latest,
    glm-newest) and ids are versions (glm-5.3, claude-sonnet-5-5) — so
    a family-shaped name with no digit is an ALIAS reference, and one
    that no alias declares is dangling."""
    if model_family(model) is None:
        return False
    return any(character.isdigit() for character in model)


def expand_chain(aliases: Mapping[str, str], name: str, where: str) -> str:
    """Follow ``name`` through the alias table to a literal model id.

    Raises:
        RegistryError: the chain cycles or does not end at a literal
            model id (a glm-/claude- family shape carrying a version
            digit)."""
    seen: list[str] = []
    current = name
    while current in aliases:
        if current in seen:
            raise RegistryError(
                f"{where}: the alias chain through {seen + [current]} "
                "cycles and never reaches a model id")
        seen.append(current)
        current = aliases[current]
    if seen and not _is_literal_model_id(current):
        raise RegistryError(
            f"{where}: alias {name!r} ends at {current!r}, which is not a "
            "literal model id (a glm-…/claude-… family shape with a "
            "version digit, like glm-5.3)")
    return current


def expand_alias(registry: ModelRegistry, name: str) -> str:
    """Expand ``name`` through the registry's aliases to the model id
    (a full chain, not one level; a plain id passes through).

    Raises:
        RegistryError: A family-shaped name with no version digit that
            no alias declares (a dangling alias — the words-are-aliases
            convention), or a chain that cycles or never ends at a
            literal id."""
    expanded = expand_chain(registry.aliases, name, f"alias {name!r}")
    if expanded == name and model_family(name) is not None \
            and not _is_literal_model_id(name):
        raise RegistryError(
            f"alias {name!r}: a family-shaped name with no version digit "
            "that no alias declares — declare it under models.aliases or "
            "write the literal model id (dangling alias)")
    return expanded


# The documented example (README "Model stacks" and the config
# docstring): the owner's three stacks, shipped as the DEFAULTS a
# config-less factory (and every test fixture) resolves through. The
# owner's real config.yaml is theirs — the factory never writes it.
_DEFAULT_RAW = {
    "aliases": {"glm-latest": "glm-5.3"},
    "providers": {
        "zai": {"origin": "cn"},
        "anthropic": {"origin": "us"},
        "bedrock": {"origin": "us", "contained": True},
        "vertex": {"origin": "us"},
        "anthropic_compatible": {"origin": "unspecified"},
    },
    "stacks": {
        # Owner decision 2026-11-19 (review round 2): standard is
        # zai-ONLY — every role on the workhorse, so an existing zai
        # profile loads and runs with no new credential. The
        # cross-family seats live in standard-plus, the opt-in a
        # project reaches only through model_stack (re-approval) plus
        # an allowed anthropic.
        "standard": {
            "contained": False,
            "roles": {
                "implementer": {"kind": "zai", "model": "glm-latest"},
                "reviewer.conformance": {"kind": "zai",
                                         "model": "glm-latest"},
                "reviewer.hunt": {"kind": "zai", "model": "glm-latest"},
                "verifier": {"kind": "zai", "model": "glm-latest"},
                "importer": {"kind": "zai", "model": "glm-latest"},
                "final_review.fix": {"kind": "zai", "model": "glm-latest"},
                "final_review.strong": {"kind": "zai",
                                        "model": "glm-latest"},
            },
        },
        "standard-plus": {
            "contained": False,
            "roles": {
                "implementer": {"kind": "zai", "model": "glm-latest"},
                "reviewer.conformance": {"kind": "zai",
                                         "model": "glm-latest"},
                "reviewer.hunt": {"kind": "anthropic",
                                  "model": "claude-sonnet-5-5"},
                "verifier": {"kind": "zai", "model": "glm-latest"},
                "importer": {"kind": "zai", "model": "glm-latest"},
                "final_review.fix": {"kind": "anthropic",
                                     "model": "claude-opus-5-5"},
                "final_review.strong": {"kind": "anthropic",
                                        "model": "claude-fable-5-2"},
            },
        },
        "no-china": {
            "contained": False,
            "roles": {
                "implementer": {"kind": "anthropic",
                                "model": "claude-opus-5-5"},
                "reviewer.conformance": {"kind": "anthropic",
                                         "model": "claude-sonnet-5-5"},
                "reviewer.hunt": {"kind": "anthropic",
                                  "model": "claude-opus-5-5"},
                "verifier": {"kind": "anthropic",
                             "model": "claude-sonnet-5-5"},
                "importer": {"kind": "anthropic",
                             "model": "claude-opus-5-5"},
                "final_review.fix": {"kind": "anthropic",
                                     "model": "claude-opus-5-5"},
                "final_review.strong": {"kind": "anthropic",
                                        "model": "claude-fable-5-2"},
            },
        },
        "contained": {
            "contained": True,
            "roles": {
                "implementer": {"kind": "bedrock",
                                "model": "claude-opus-5-5"},
                "reviewer.conformance": {"kind": "bedrock",
                                         "model": "claude-sonnet-5-5"},
                "reviewer.hunt": {"kind": "bedrock",
                                  "model": "claude-opus-5-5"},
                "verifier": {"kind": "bedrock",
                             "model": "claude-sonnet-5-5"},
                "importer": {"kind": "bedrock",
                             "model": "claude-sonnet-5-5"},
                "final_review.fix": {"kind": "bedrock",
                                     "model": "claude-opus-5-5"},
                "final_review.strong": {"kind": "bedrock",
                                        "model": "claude-fable-5-2"},
            },
        },
    },
    "prices": {
        "anthropic": {
            "claude-sonnet-5-5": {"input": 3.0, "cache_read": 0.3,
                                  "cache_write": 3.75, "output": 15.0},
        },
        "bedrock": {
            "claude-sonnet-5-5": {"input": 3.0, "cache_read": 0.3,
                                  "cache_write": 3.75, "output": 15.0},
        },
    },
}
DEFAULT_REGISTRY = registry_from_raw(_DEFAULT_RAW)


def model_ids_outside_registry(root: Path,
                               registry: ModelRegistry = DEFAULT_REGISTRY,
                               ) -> "list[str]":
    """The AC-2 grep: every model id (glm-*/claude-* shape) that appears
    in a ``.py`` file under ``root`` but is NOT named by the registry.

    The registry defaults are the allowed set, so a hard-coded model id
    anywhere in factory code — the silent swap that bypasses the
    registry — fails the test that calls this over ``factory/``."""
    allowed = registry.model_ids()
    found: list[str] = []
    for path in sorted(Path(root).rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _GREP_ID_RE.finditer(text):
            model = match.group(0)
            if model not in allowed:
                found.append(model)
    return sorted(set(found))


# -------------------------------------------------------------- onboarding --

@dataclass(frozen=True)
class StackChoice:
    """One onboarding answer's mapping (AC-6): the restriction value,
    the stack it picks, and the reason line the confirm card shows.
    A skipped question maps to None/None — never to a default stack."""
    restriction: "str | None"
    stack: "str | None"
    reason: str


def stack_for(answers: Mapping[str, Any]) -> StackChoice:
    """Map the onboarding restriction answers to ``model_restrictions``
    and a stack.

    Args:
        answers: The interview's answers, keyed by restriction name
            (``none`` / ``no_china`` / ``contained``), truthy when given.
            The question is always asked; an answer set that carries
            nothing is a SKIPPED question, never an assumed ``none``.

    Raises:
        ValueError: An answer key that is not a restriction — refusing
            beats guessing at a typo'd key.
    """
    unknown = sorted(set(answers) - set(RESTRICTIONS))
    if unknown:
        raise ValueError(
            f"stack_for got unknown answer keys {unknown}: expected "
            f"{list(RESTRICTIONS)}")
    given = {name for name in RESTRICTIONS if answers.get(name)}
    if not given:
        return StackChoice(
            None, None,
            "the model-restriction question was not answered — the "
            "interview cannot finish without it, and the default answer "
            "is 'no restriction', never assumed")
    # contained wins when both restrictions are given: it is the
    # strictly stronger claim (where the code may run subsumes who may
    # serve it).
    if "contained" in given:
        return StackChoice(
            "contained", RESTRICTION_STACKS["contained"],
            "contained in a named environment — the strongest "
            "restriction wins when both are given")
    if "no_china" in given:
        return StackChoice(
            "no_china", RESTRICTION_STACKS["no_china"],
            "no models from Chinese providers — the no-china stack")
    return StackChoice(
        "none", RESTRICTION_STACKS["none"],
        "no restriction — the standard stack (GLM workhorse)")


# -------------------------------------------------------------- resolution --

def _profile_restriction(profile: Mapping[str, Any]) -> "str | None":
    block = profile.get("model_restrictions")
    if not isinstance(block, Mapping):
        return None
    value = block.get("value")
    return value if value in RESTRICTIONS else None


def _stack_name(profile: Mapping[str, Any]) -> str:
    """The stack this project runs: its own ``model_stack``, else the
    stack its restriction answer picks (a client's onboarding lands the
    name in the profile; the derived default keeps a hand-migrated
    client loadable), else the default stack."""
    named = profile.get("model_stack")
    if isinstance(named, str) and named.strip():
        return named.strip()
    restriction = _profile_restriction(profile)
    if restriction is not None:
        return RESTRICTION_STACKS[restriction]
    return DEFAULT_STACK_NAME


def _guard(profile: Mapping[str, Any], role: str, kind: str) -> None:
    """The AC-3 guard: ``kind`` must be the project's own provider kind
    or in its owner-approved ``providers_allowed`` — named, refusing,
    never falling back."""
    block = profile.get("provider")
    own = str(block.get("kind")) if isinstance(block, Mapping) else "zai"
    allowed = list(profile.get("providers_allowed") or [])
    if kind != own and kind not in allowed:
        name = profile.get("name") or "<unnamed project>"
        raise ProviderError(
            f"project {name!r} role {role!r} resolves to provider "
            f"{kind!r}, which is neither the project's own kind "
            f"{own!r} nor in its providers_allowed {allowed!r} — a stack "
            "edit can never move a project outside what it approved "
            "(F-100 AC-3); widen providers_allowed and re-approve the "
            "profile, or pin the role with seat_providers"
        )


def _restriction_check(profile: Mapping[str, Any],
                       registry: ModelRegistry, stack: ModelStack,
                       role: str, kind: str,
                       base_url: "str | None" = None) -> None:
    """The recorded restriction answers, enforced per role, FAIL CLOSED.
    no_china refuses any provider the registry does not mark with an
    explicitly non-cn origin ("cn", "unspecified" and a missing entry
    all refuse — an unknown origin is not proof of non-cn) and any
    base_url on a Z.ai-family host, whatever the kind. contained
    refuses a stack not marked contained AND any role whose resolved
    kind the registry does not mark contained (only an explicit
    ``contained: true`` marks one). A client with no recorded answer
    refuses — the question is always asked, never assumed."""
    if profile.get("data_owner") == "client":
        restriction = _profile_restriction(profile)
        if restriction is None:
            raise ProviderError(
                f"project {profile.get('name') or '<unnamed project>'!r} "
                "is a client project with no recorded model_restrictions "
                "answer — the onboarding question (does the client "
                "restrict which AI models may process the code, or where "
                "it runs?) is always asked, never assumed; record the "
                "answer (the default is none) and re-approve the profile"
            )
        if restriction == "no_china":
            origin = registry.providers.get(kind)
            if origin is None or origin in ("cn", "unspecified"):
                raise ProviderError(
                    f"project {profile.get('name')!r} recorded "
                    f"model_restrictions no_china, but role {role!r} "
                    f"resolves to provider {kind!r} whose registry origin "
                    f"is {origin!r} — the client forbids models from "
                    "Chinese providers, and a kind the registry does not "
                    "mark explicitly non-cn fails closed (F-100 AC-3, "
                    "review R1)")
            if base_url:
                from urllib.parse import urlsplit

                from factory.provider import model_host_owner

                host = urlsplit(str(base_url)).hostname or ""
                if model_host_owner(host) == "zai":
                    raise ProviderError(
                        f"project {profile.get('name')!r} recorded "
                        f"model_restrictions no_china, but role {role!r} "
                        f"resolves to a base_url on {host!r}, a Z.ai-"
                        "family host — the kind label does not move "
                        "client code off a Chinese endpoint (F-100 AC-3, "
                        "review R1)")
        if restriction == "contained":
            if not stack.contained:
                raise ProviderError(
                    f"project {profile.get('name')!r} recorded "
                    f"model_restrictions contained, but stack "
                    f"{stack.name!r} is not marked contained — the code "
                    "may not leave the named environment (F-100 AC-3)")
            if kind not in registry.contained_kinds:
                raise ProviderError(
                    f"project {profile.get('name')!r} recorded "
                    f"model_restrictions contained, but role {role!r} "
                    f"resolves to provider {kind!r}, which the registry "
                    "does not mark contained — only an explicit "
                    "contained: true admits a kind into a contained "
                    "project, and a seat pin never moves code out of "
                    "the named environment (F-100 AC-3, review R2)")


def resolve_role(profile: Mapping[str, Any], config: Any, role: str,
                 ) -> ProviderSpec:
    """Resolve one seat role's provider and model (the ONE resolver).

    Args:
        profile: The loaded, defaulted profile mapping.
        config: The factory config (:class:`factory.config.FactoryConfig`)
            carrying the model registry (and, for the eval only, its
            in-memory ``role_overrides``).
        role: A name from :data:`MODEL_ROLES` (a seat name works for
            every seat but the reviewer, whose lenses are separate
            roles — pass the lens role at every fan-out).

    Returns:
        The :class:`~factory.provider.ProviderSpec`, with ``role`` and
        ``stack`` set for the record (AC-4) and the model alias
        expanded.

    Raises:
        ProviderError: The role is unknown; the resolved kind is outside
            what the project approved (the AC-3 guard — no fallback);
            the project's recorded restriction refuses the kind or the
            stack; or the kind cannot run here (vertex, a bedrock
            region that is missing).
    """
    if config is None:
        # R3 (review round 4): no silent default — a call site that
        # misses the config is a bug (the seat would resolve against
        # DEFAULT_REGISTRY while its siblings resolve against the
        # owner's registry), so it refuses here, loudly.
        raise ProviderError(
            "resolve_role needs the factory config (the registry the "
            "run carries) — a None config is a missed call site, not "
            "the default registry")
    registry = getattr(config, "models", None) or DEFAULT_REGISTRY
    overrides = getattr(config, "role_overrides", None) or {}

    if role not in MODEL_ROLES:
        raise ProviderError(
            f"unknown model role {role!r}: expected one of "
            f"{list(MODEL_ROLES)} (a seat lens is reviewer.conformance "
            "or reviewer.hunt — reviewer.hunter, say, is not a role)")

    stack_name = _stack_name(profile)
    stack = registry.stacks.get(stack_name)
    if stack is None:
        raise ProviderError(
            f"project {profile.get('name')!r} names model_stack "
            f"{stack_name!r}, which the registry does not define "
            f"(known: {sorted(registry.stacks)})")

    seat = seat_for_role(role)

    # Precedence (F-100 AC-3): the project's own seat pin, then the
    # F-59 provider.models block (one release, deprecated), then the
    # stack. A stack edit therefore moves nothing a project pinned, and
    # nothing a project still carries from F-59 — only what it left to
    # the stack.
    override = (profile.get("seat_providers") or {}).get(seat)
    block = profile.get("provider")
    kind = model = region = base_url = None
    if isinstance(override, Mapping) and override:
        kind = str(override.get("kind"))
        model = override.get("model")
        region = override.get("region")
        base_url = override.get("base_url")
    elif (isinstance(block, Mapping)
          and isinstance(block.get("models"), Mapping)
          and seat in block["models"]):
        kind = str(block.get("kind") or "zai")
        model = block["models"].get(seat)
        region = block.get("region")
        base_url = block.get("base_url")
    elif role in ROLE_DEFAULTS and role not in stack.roles:
        return resolve_role(profile, config, ROLE_DEFAULTS[role])
    elif role in stack.roles:
        target = stack.roles[role]
        kind = target.kind
        model = target.model
        region = target.region
        if region is None and kind == "bedrock":
            region = (block.get("region")
                      if isinstance(block, Mapping) else None)
        base_url = (block.get("base_url")
                    if isinstance(block, Mapping) else None) \
            if kind == "anthropic_compatible" else None
    else:  # pragma: no cover — the registry requires every other role
        raise ProviderError(
            f"stack {stack_name!r} names no role {role!r} and no default "
            f"covers it (registry gap)")

    if isinstance(model, str) and model.strip():
        model = model.strip()
    else:
        raise ProviderError(
            f"role {role!r} resolves to no model — the stack, the seat "
            "pin or provider.models must name one")

    # The eval's per-run override (AC-5): applied AFTER the stack pick,
    # still through the family rule and the guard — an override can
    # never move a seat outside what the project (the eval fixture)
    # allows, and it is never written anywhere. A glm-*/claude-* id
    # carries its provider with it (zai / anthropic; bedrock has no id
    # shape of its own, so an override cannot name it); any other shape
    # keeps the stack's kind (a custom inference-profile id, say).
    if role in overrides:
        model = str(overrides[role]).strip()
        family = model_family(model)
        if family is not None:
            kind = family
        model = expand_alias(registry, model)

    _restriction_check(profile, registry, stack, role, str(kind),
                       base_url=base_url if isinstance(base_url, str)
                       else None)
    _guard(profile, role, str(kind))

    spec = _build_spec(kind=str(kind), model=expand_alias(registry, model),
                       region=region if isinstance(region, str) else None,
                       base_url=base_url if isinstance(base_url, str) else None,
                       role=role, stack=stack_name)
    return spec


def check_profile_stack(profile: Mapping[str, Any], config: Any) -> None:
    """The load-time sweep: EVERY role the stack names resolves for this
    profile — the guard, the restrictions and the missing-answer rule
    over the whole stack, not one seat. Called wherever the registry is
    known (the run, import and eval loaders pass the config), so a
    stack edit that would move a project outside what it approved
    refuses at load, named — before any session or spend.

    Raises:
        ProviderError: As :func:`resolve_role`, for the first refusing
            role (the message names project, role and kind).
    """
    if config is None:
        raise ProviderError(
            "check_profile_stack needs the factory config — a None "
            "config is a missed call site, not the default registry")
    registry = getattr(config, "models", None) or DEFAULT_REGISTRY
    stack_name = _stack_name(profile)
    stack = registry.stacks.get(stack_name)
    if stack is None:
        raise ProviderError(
            f"project {profile.get('name')!r} names model_stack "
            f"{stack_name!r}, which the registry does not define "
            f"(known: {sorted(registry.stacks)})")
    # EVERY role, not only the stack's own entries: a seat pin on any
    # role (review R1/R2) resolves through the same precedence and meets
    # the same restriction checks at load, before any session.
    for role in MODEL_ROLES:
        resolve_role(profile, config, role)


def deprecation_notes(profile: Mapping[str, Any]) -> "list[str]":
    """The F-100 deprecation notes the health-check command shows
    (behind this helper until F-66 wires it): an F-59
    ``provider.models`` block still works for one release — move the
    project to ``model_stack`` so a registry edit can reach it."""
    notes: list[str] = []
    block = profile.get("provider")
    if isinstance(block, Mapping) and block.get("models"):
        notes.append(
            "provider.models is deprecated (F-100): it still overrides "
            "the stack for one release, but a model bump in the "
            "registry cannot reach a seat pinned here — move the "
            "project to model_stack and re-approve")
    return notes
