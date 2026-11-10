"""Whether one recorded path lies at or below another, read the one way.

Recorded paths cross machines: a record written on Windows is read on
macOS and the reverse, and a view recorded on one machine is compared with
a path recorded on another. So separators are read off the spelling,
never off the host: a POSIX root uses `/`, while a Windows spelling can
use either slash. A backslash in a POSIX name is data (ENG-254). `inside`
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
    """Trim the spelling's trailing separators, keeping filesystem roots."""
    separators = "/" if path.startswith("/") else "/\\"
    stripped = path.rstrip(separators)
    if (
        len(stripped) == 2
        and stripped[0].isalpha()
        and stripped[1] == ":"
        and len(path) > 2
        and path[2] in "/\\"
    ):
        return path[:3]
    return stripped or path


def inside(path: str, root: str) -> bool:
    """`path` is `root` or below it, in the root spelling's separators.

    Compared as spelled, after `norm`: no case is folded, no ``..`` is
    resolved and no link is followed, since a recorded path may name a
    tree this machine has never had. One spelling of a path in mixed
    separators (``C:\\w/app``) does not meet its uniform twin
    (``C:\\w\\app``)."""
    path, root = norm(path), norm(root)
    separators = "/" if root.startswith("/") else "/\\"
    return path == root or any(
        path.startswith(root if root.endswith(sep) else root + sep)
        for sep in separators
    )


def basename(path: str) -> str:
    """The last recorded component, preserving POSIX backslashes."""
    spelled = norm(path)
    if not spelled.startswith("/"):
        spelled = spelled.replace("\\", "/")
    return spelled.rstrip("/").rsplit("/", 1)[-1] or path
