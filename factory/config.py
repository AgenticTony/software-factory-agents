"""Factory-level configuration (BUILD-ORDER F-10 AC-4).

Settings that belong to the factory, not to any one project: today, the
peak window during which ``factory loop`` starts no session, how many
reviewer sessions L2 fans out to (F-38), and how many times a failed
final full review may repeat (F-41). The Z.ai Coding Plan's quota is
shared with the owner's own interactive use, and the owner works
08:00–12:00 Mon–Fri Swedish time, so that is the default window.

The file is YAML at ``<factory_root>/config.yaml`` (absent = defaults)::

    peak_window:
      timezone: Europe/Stockholm
      days: [mon, tue, wed, thu, fri]
      start: "08:00"   # inclusive
      end: "12:00"     # exclusive
    reviewers: 2       # L2 reviewer sessions per review (F-38), >= 1
    final_review_repeats: 1   # failed final review repeats (F-41), >= 0
    ci_wait_min: 30    # F-75: minutes a run waits for its PR's CI, >= 0
                       # (0 = don't wait — the pre-F-75 behaviour)
    prices:            # F-59: per kind+model USD per MILLION tokens;
                      # input / cache_read / cache_write / output. A
                      # missing (kind, model) renders n/a in factory
                      # status, never 0. Owner-maintained.
      bedrock:
        claude-sonnet-5-5: {input: 3.0, cache_read: 0.3, cache_write: 3.75, output: 15.0}

Anything malformed is refused with :class:`ConfigError` — a window that
silently failed to parse would let the factory spend the quota at the
exact hours the owner needs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CONFIG_FILE = "config.yaml"
DEFAULT_REVIEWERS = 2
DEFAULT_FINAL_REVIEW_REPEATS = 1
DEFAULT_CI_WAIT_MIN = 30


class ConfigError(Exception):
    """The factory config is unreadable or malformed."""


@dataclass(frozen=True)
class PeakWindow:
    """A weekly window, in one timezone, during which no session starts."""

    timezone: str
    days: tuple[str, ...]
    start: str  # "HH:MM", inclusive
    end: str  # "HH:MM", exclusive


DEFAULT_PEAK_WINDOW = PeakWindow(
    "Europe/Stockholm", ("mon", "tue", "wed", "thu", "fri"), "08:00", "12:00"
)


@dataclass(frozen=True)
class FactoryConfig:
    """The parsed factory config.

    ``reviewers`` (F-38) is how many reviewer sessions run over one diff:
    a whole number >= 1, default 2. A reviewer fan-out of zero reviews
    nothing, so it is a config error, not a quiet single-review fallback.

    ``final_review_repeats`` (F-41) bounds how often a failed final full
    review sends its findings to one more implementer round and repeats:
    a whole number >= 0, default 1. Zero means the first failed final
    review escalates ``review_not_converging`` at once — nothing is
    published with an unresolved blocking or major finding either way.

    ``ci_wait_min`` (F-75) bounds how long a run waits, after a
    validated publish, for the PR's CI on the exact pushed SHA: a whole
    number >= 0, default 30. The wait also counts against the run's
    remaining wall clock (the smaller bound wins). Zero disables the
    wait — the pre-F-75 behaviour, where the publish path never read
    CI at all (the sweep's ready rule still applies).
    """

    peak_window: PeakWindow | None = field(default=DEFAULT_PEAK_WINDOW)
    reviewers: int = DEFAULT_REVIEWERS
    final_review_repeats: int = DEFAULT_FINAL_REVIEW_REPEATS
    ci_wait_min: int = DEFAULT_CI_WAIT_MIN
    # F-59 AC-4: the owner-maintained price table — kind -> model ->
    # {input, cache_read, cache_write, output} in USD per million
    # tokens. Missing entries are n/a in status, never 0; the table is
    # display-only (estimates), never gate state.
    prices: "dict[str, dict[str, dict[str, float]]]" = field(
        default_factory=dict)


def _clock(value, key: str) -> time:
    if not isinstance(value, str):
        raise ConfigError(f"peak_window.{key} must be an 'HH:MM' string, got {value!r}")
    try:
        hours, minutes = value.split(":")
        if len(hours) != 2 or len(minutes) != 2:
            raise ValueError
        return time(int(hours), int(minutes))
    except ValueError:
        raise ConfigError(f"peak_window.{key} must be 'HH:MM', got {value!r}") from None


def _zone(name) -> ZoneInfo:
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"peak_window.timezone {name!r} is not a known IANA zone") from None


def _peak_window(raw) -> PeakWindow | None:
    if raw is None:
        return None  # explicitly disabled
    if not isinstance(raw, dict) or set(raw) != {"timezone", "days", "start", "end"}:
        raise ConfigError(
            "peak_window needs exactly: timezone, days, start, end "
            f"(got {sorted(raw) if isinstance(raw, dict) else raw!r})"
        )
    _zone(raw["timezone"])
    days = raw["days"]
    if not isinstance(days, list) or not days or any(d not in DAYS for d in days):
        raise ConfigError(f"peak_window.days must be a non-empty list of {DAYS}, got {days!r}")
    if _clock(raw["start"], "start") >= _clock(raw["end"], "end"):
        raise ConfigError("peak_window.start must be before peak_window.end")
    return PeakWindow(str(raw["timezone"]), tuple(days), raw["start"], raw["end"])


def _reviewers(raw) -> int:
    """Validate the ``reviewers`` knob (F-38): a whole number >= 1.

    ``bool`` is an ``int`` subclass, so ``true``/``false`` are rejected
    explicitly — a quiet 1 from ``true`` would halve the fan-out with no
    trace of why.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"reviewers must be a whole number >= 1, got {raw!r}")
    if raw < 1:
        raise ConfigError(f"reviewers must be a whole number >= 1, got {raw!r}")
    return raw


def _final_review_repeats(raw) -> int:
    """Validate the ``final_review_repeats`` knob (F-41): a whole
    number >= 0. Same bool refusal as ``reviewers`` — a quiet 1 from
    ``true`` would silently spend a repeat round no owner granted."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(
            f"final_review_repeats must be a whole number >= 0, got {raw!r}")
    if raw < 0:
        raise ConfigError(
            f"final_review_repeats must be a whole number >= 0, got {raw!r}")
    return raw


def _ci_wait_min(raw) -> int:
    """Validate the ``ci_wait_min`` knob (F-75): a whole number >= 0.
    Same bool refusal as ``reviewers`` — ``true`` must not read as a
    silent 1-minute wait, and a negative wait is no wait at all."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"ci_wait_min must be a whole number >= 0, got {raw!r}")
    if raw < 0:
        raise ConfigError(f"ci_wait_min must be a whole number >= 0, got {raw!r}")
    return raw


_PRICE_FIELDS = ("input", "cache_read", "cache_write", "output")


def _prices(raw) -> "dict[str, dict[str, dict[str, float]]]":
    """Validate the ``prices`` table (F-59): kind -> model -> the four
    per-million rates, each a finite number >= 0. A malformed table is a
    ConfigError — a wrong price silently applied to every status page is
    worse than a loud refusal."""
    if not isinstance(raw, dict):
        raise ConfigError(f"prices must be a mapping, got {raw!r}")
    table: "dict[str, dict[str, dict[str, float]]]" = {}
    for kind, models in raw.items():
        if not isinstance(models, dict):
            raise ConfigError(f"prices.{kind} must be a mapping, got {models!r}")
        for model, rates in models.items():
            if not isinstance(rates, dict) or set(rates) != set(_PRICE_FIELDS):
                raise ConfigError(
                    f"prices.{kind}.{model} needs exactly "
                    f"{list(_PRICE_FIELDS)}, got "
                    f"{sorted(rates) if isinstance(rates, dict) else rates!r}"
                )
            for field_name, value in rates.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or value < 0:
                    raise ConfigError(
                        f"prices.{kind}.{model}.{field_name} must be a "
                        f"number >= 0, got {value!r}"
                    )
            table.setdefault(str(kind), {})[str(model)] = {
                name: float(rates[name]) for name in _PRICE_FIELDS
            }
    return table


def load_config(path: Path | str | None) -> FactoryConfig:
    """Load the factory config.

    Args:
        path: Config file path; None or a missing file yields the defaults.

    Returns:
        The parsed :class:`FactoryConfig`.

    Raises:
        ConfigError: The file is unreadable, not a mapping, has unknown
            keys, or carries a malformed ``peak_window``, ``reviewers``,
            ``final_review_repeats`` or ``ci_wait_min``.
    """
    if path is None or not Path(path).exists():
        return FactoryConfig()
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read factory config {path}: {exc}") from exc
    if raw is None:
        return FactoryConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"factory config {path} must be a mapping")
    unknown = sorted(set(raw) - {"peak_window", "reviewers",
                                  "final_review_repeats", "ci_wait_min",
                                  "prices"})
    if unknown:
        raise ConfigError(f"factory config {path}: unknown keys {unknown}")
    return FactoryConfig(
        peak_window=_peak_window(raw["peak_window"]) if "peak_window" in raw
        else DEFAULT_PEAK_WINDOW,
        reviewers=_reviewers(raw["reviewers"]) if "reviewers" in raw
        else DEFAULT_REVIEWERS,
        final_review_repeats=(
            _final_review_repeats(raw["final_review_repeats"])
            if "final_review_repeats" in raw else DEFAULT_FINAL_REVIEW_REPEATS
        ),
        ci_wait_min=(
            _ci_wait_min(raw["ci_wait_min"])
            if "ci_wait_min" in raw else DEFAULT_CI_WAIT_MIN
        ),
        prices=_prices(raw["prices"]) if "prices" in raw else {},
    )


def in_peak_window(now: datetime, window: PeakWindow | None) -> bool:
    """True when ``now`` falls inside the window, judged in its timezone.

    Args:
        now: An aware datetime (any zone; converted, so DST is honoured).
        window: The window, or None (disabled: never in it).

    Raises:
        ConfigError: ``now`` is naive — a naive clock reading has no
            defined Swedish time.
    """
    if window is None:
        return False
    if now.tzinfo is None or now.utcoffset() is None:
        raise ConfigError("in_peak_window needs an aware datetime, not a naive one")
    local = now.astimezone(_zone(window.timezone))
    if DAYS[local.weekday()] not in window.days:
        return False
    return _clock(window.start, "start") <= local.time() < _clock(window.end, "end")
