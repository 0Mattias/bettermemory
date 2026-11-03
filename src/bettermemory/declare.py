"""The membership model a declaration runs on (design 8.8): this
process's machine, its row written first, with a token writer, so a
declaration that separates a checkout re-mints its checkout token
(design 1.1). The CLI's declarations (`cli/project_cmd.py`,
`cli/memory_move_cmd.py`) and the agent's (`handlers/questions.py`)
open the same model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .membership import Checkout, Membership
    from .store import Store


def declaring_model(
    store: Store, config: Config, *, session: str | None = None
) -> Membership:
    """`Membership.open` for a declaration: the process's machine identity
    and a token writer under the ``[membership] write_token`` policy that
    writes only in this machine's checkouts, each token naming the one it
    replaces, so a declaration cut off between the write and its commit
    leaves a token the store knows for the checkout's (ENG-205). Having
    read the store, not the file, it replaces any token there but another
    re-mint of the same one (`checkout_token.remint`, `only_siblings`)."""
    from . import checkout_token, location
    from .membership import BARE, Membership

    identity = location.process_machine()
    policy = config.membership.write_token

    def write_token(k: Checkout) -> tuple[str | None, str]:
        if k.git_dir is None or k.machine != identity.id:
            return None, "skipped:other_machine"
        wrote = checkout_token.remint(
            k.git_dir,
            policy=policy,
            bare=k.kind == BARE,
            replacing=k.lineage,
            only_siblings=True,
        )
        return wrote.token, wrote.status

    return Membership.open(store, identity, token_writer=write_token, session=session)


__all__ = ["declaring_model"]
