"""`[policy]`: the write gates' escape hatches, taken away from the agent.

Every write gate has a flag that passes it, and the caller sets the flag:
`force` skips memory_write's two duplicate gates, `acknowledge_credential`
the secret check, and `acknowledge_transient`, `acknowledge_user_claim`,
`acknowledge_scope_mismatch` and memory_update's `acknowledge_truncation`
theirs. The caller is the agent, so a gate it can wave through is advice
to the agent rather than a rule of the store. Before this module only the
capture path took the flags away, in code (the capture block at the top
of `handlers.write.memory_write`), and no setting could say "secrets are
never stored here, whatever the agent passes".

`[policy] forbid_force` and `[policy] forbid_acknowledge` say it, for
every call. They live in the user's config.toml like any other key, and
an organisation pins them in the managed file (`managed.py`), where the
user's config cannot loosen them.

A call that passes a forbidden flag as true is refused whole, before
payload validation and before any gate runs: nothing is written, no write
or update event is recorded (as for the handlers' other ValueError
refusals, an unknown scope or a cap), and no gate answers. Refusing the
call, rather than dropping
the flag and letting the gate decide, is deliberate. A dropped flag on a
call its gate would not have refused (a clean body sent with
`acknowledge_credential=True`) commits exactly as if the flag had been
honoured, so the agent never learns the flag is gone and keeps reaching
for it; a refusal teaches the rule on the first call. It is also the
simpler contract to state: the flag is not available here.

The refusal is a `PolicyRefusal`, a ValueError, the form every other
handler refusal takes; the ToolHandlers boundary re-raises it as the
SDK's tool error with the same words (`_handlers.RefusedError`; on the
wire the SDK puts its own `Error executing tool <name>:` in front). The
refusal's text starts with `policy_refused:` so a programmatic caller can
find it, and names each switch and whether the organisation's managed
file pins it: the agent can then tell the user which file to change, or
that no file the user owns can change it.

With both switches at their defaults `check_overrides` returns at once,
so the two tools are byte-for-byte what they were: the same schema, the
same answers, the same events.

A capture write is checked after the capture path has cleared its flags
(`memory_write`), so a capture is unchanged by any policy: its flags were
never in force.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .config import ACKNOWLEDGE_NAMES

if TYPE_CHECKING:
    from .config import Config

#: The first word of every refusal's text, for a caller that branches on it.
REFUSAL_STATUS = "policy_refused"

_FORCE_KEY = "policy.forbid_force"
_ACKNOWLEDGE_KEY = "policy.forbid_acknowledge"


def _label(key: str) -> str:
    """`policy.forbid_force` as the config file spells it."""
    section, name = key.split(".", 1)
    return f"[{section}] {name}"


class PolicyRefusal(ValueError):
    """A call refused because it passed a flag `[policy]` forbids.

    `flags` are the parameters as the call passed them (`force`,
    `acknowledge_credential`), `keys` the switches that forbid them as
    "section.key", and `managed` the subset of `keys` the organisation's
    managed file pins. `str()` is the text the agent reads."""

    def __init__(
        self,
        *,
        tool: str,
        flags: tuple[str, ...],
        keys: tuple[str, ...],
        managed: frozenset[str],
    ) -> None:
        self.tool = tool
        self.flags = flags
        self.keys = keys
        self.managed = managed
        super().__init__(_message(tool, flags, keys, managed))


def _message(
    tool: str, flags: tuple[str, ...], keys: tuple[str, ...], managed: frozenset[str]
) -> str:
    passed = " and ".join(f"{flag}=True" for flag in flags)
    switches = "; ".join(
        f"{_label(key)} "
        + (
            "(managed: pinned by the organisation's managed config, which the "
            "user's own config cannot change)"
            if key in managed
            else "(set in the user's config)"
        )
        for key in keys
    )
    outcome = (
        "Nothing was written." if tool == "memory_write" else "Nothing was changed."
    )
    if len(flags) == 1:
        remedy = (
            "Re-send the call without that flag. The gate it skips then "
            "answers as usual, and what it refuses has to be fixed in the "
            "content itself."
        )
    else:
        remedy = (
            "Re-send the call without those flags. The gates they skip then "
            "answer as usual, and what they refuse has to be fixed in the "
            "content itself."
        )
    if "force" in flags:
        remedy += (
            " A duplicate stays refused as one: update the existing memory "
            "with memory_update instead."
        )
    return (
        f"{REFUSAL_STATUS}: {tool} was called with {passed}, which this "
        f"install forbids: {switches}. {outcome} {remedy}"
    )


def check_overrides(
    config: Config,
    tool: str,
    *,
    force: bool = False,
    acknowledge: Mapping[str, bool],
) -> None:
    """Raise `PolicyRefusal` when the call passes a flag `[policy]` forbids.

    `acknowledge` maps each of the tool's acknowledge flags, by its bare
    name (`ACKNOWLEDGE_NAMES`), to the value the call passed. Every
    forbidden flag the call passed is named in one refusal, so the agent
    fixes the call in one retry rather than one flag at a time. A flag
    passed as false is never refused, and neither is a flag this policy
    does not name."""
    policy = config.policy
    if not policy.forbid_force and not policy.forbid_acknowledge:
        return
    flags: list[str] = []
    keys: list[str] = []
    if force and policy.forbid_force:
        flags.append("force")
        keys.append(_FORCE_KEY)
    forbidden = set(policy.forbid_acknowledge)
    acknowledged = [
        name
        for name in ACKNOWLEDGE_NAMES
        if acknowledge.get(name, False) and name in forbidden
    ]
    if acknowledged:
        flags.extend(f"acknowledge_{name}" for name in acknowledged)
        keys.append(_ACKNOWLEDGE_KEY)
    if not flags:
        return
    raise PolicyRefusal(
        tool=tool,
        flags=tuple(flags),
        keys=tuple(keys),
        managed=frozenset(key for key in keys if key in config.managed_keys),
    )


__all__ = ["REFUSAL_STATUS", "PolicyRefusal", "check_overrides"]
