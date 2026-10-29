"""Whether one recorded path lies at or below another, read the one way.

Recorded paths cross machines: a record written on Windows is read on
macOS and the reverse, and a view recorded on one machine is compared with
a path recorded on another. So whether a path lies at or below a root is
read off the two spellings, never off the platform this runs on: either
slash separates, whichever separator the two are spelled with. `inside`
is that one rule (`norm` the trailing-separator trim it compares under).

The membership family reads it (`membership_records`), and so does
verify's claim anchoring (`verify_anchoring.Anchoring.translate`), which
asks it whether a path lies under a recorded view before the model
translates the path: the two must agree on what "below the root" means on
every host (ENG-166). It lives apart from both so that the verify family,
on the hook path, reads the rule without importing the membership family
and the store behind it. It imports nothing.
"""

from __future__ import annotations


def norm(path: str) -> str:
    """`path` without its trailing separators of either slash; a path of
    separators alone is kept as spelled."""
    stripped = path.rstrip("/\\")
    return stripped or path


def inside(path: str, root: str) -> bool:
    """`path` is `root` or below it, whichever separator the two are
    spelled with: recorded paths cross machines.

    Compared as spelled, after `norm`: no case is folded, no ``..`` is
    resolved and no link is followed, since a recorded path may name a
    tree this machine has never had. One spelling of a path in mixed
    separators (``C:\\w/app``) does not meet its uniform twin
    (``C:\\w\\app``)."""
    path, root = norm(path), norm(root)
    return path == root or any(path.startswith(root + sep) for sep in ("/", "\\"))
