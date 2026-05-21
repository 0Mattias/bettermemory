# YYYY-MM-DD — short-slug

**Reported by:** issue link or "private report"
**bettermemory version at time of report:** vX.Y.Z
**Fixed in:** vX.Y.Z (or "open — see Status")
**Status:** open / fixed / wontfix-with-rationale

## Symptom

What the user observed. The reply that referenced the stale claim, the verdict the retrieval returned, and why the verdict misled the model. Include the exact memory body if it can be shared.

## Root cause

Which of the verification signals failed, and why:

- **Calendar age** — was `last_verified_at` reasonable, or was the threshold tuned wrong?
- **Path drift** — did the extractor miss a path? Misclassify a URL route as a file? Tokenize incorrectly?
- **Commit drift** — was the origin field missing, the verified-paths attestation stale, or the commit attribution wrong?
- **Threshold rule** — did `v1_top1_high` (silent-miss probe) fire on noise or miss real signal?

Cite the file and line where the bug lives.

## Fix

The change that landed and why it's the right shape. Link the PR. Note any backwards-compat or migration consequences.

## Verification

Tests added or updated. The regression case should be reproducible from the test name. If the fix changes the verdict for existing memories on disk, document the upgrade path.

## What the surface should do differently

The lesson generalised. Did the bug suggest a new signal (a fifth drift axis), a wider threshold, a better extractor grammar, or a documentation gap? This is the section that compounds — read together, the "differently" notes are the roadmap for the verification surface.

## References

- Issue: #N
- PR: #N
- Related incidents: `YYYY-MM-DD-other-slug.md`
- Related code: `src/bettermemory/<module>.py:<line>`
