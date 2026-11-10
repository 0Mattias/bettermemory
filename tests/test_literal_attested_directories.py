from pathlib import Path
import pytest
from bettermemory.verify import (
    ClaimAnchor,
    detect_path_drift,
    unverifiable_attestations,
)


@pytest.mark.parametrize("rel", ["src/routes/blog/[slug]", "app/[locale]", "docs/v1."])
@pytest.mark.parametrize("absolute", [False, True])
def test_literal_attested_directory(tmp_path: Path, rel: str, absolute: bool) -> None:
    target = tmp_path / rel
    target.mkdir(parents=True)
    if target.name not in {entry.name for entry in target.parent.iterdir()}:
        pytest.skip("this filesystem normalizes the literal trailing dot")
    raw = str(target) if absolute else rel
    anchor = ClaimAnchor(root=str(tmp_path), rule="home_checkout")
    assert target.is_dir()
    assert unverifiable_attestations([raw], anchor=anchor) == []
    report = detect_path_drift("", verified_paths=[raw], anchor=anchor)
    assert report.missing == ()
    assert report.claim_anchored_missing == ()
    assert report.verified == (str(target),)
