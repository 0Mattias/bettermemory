#!/usr/bin/env bash
# Build git from its source on GitHub and install it under a prefix
# (ENG-227): the CI legs that run the git-reading suites against the
# minimum git, the newest release and git's `next`.
#
#   tools/ci/build_git.sh REF PREFIX [SHA256]
#
# REF is a tag of github.com/git/git (v2.39.5) or a commit of it; the
# archive GitHub serves for it is downloaded, checked against SHA256 when
# one is given, unpacked into a fresh directory and built with the
# optional parts left out (no gettext, Tcl/Tk, Perl, Python, expat, curl,
# Rust or OpenSSL: the suites read local repositories only, and a build needs
# a C compiler, make and zlib's headers). Extra make variables come from
# BUILD_GIT_MAKEFLAGS, as WITH_BREAKING_CHANGES=YesPlease for git's
# `next` built as 3.0 will be. A PREFIX whose bin/git already runs is
# kept, so a cached build is not rebuilt; the caller checks its version.
# The last line printed is that git's `git version`.
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: $0 REF PREFIX [SHA256]" >&2
  exit 2
fi
ref=$1
prefix=$2
want_sha=${3:-}

case "$prefix" in
  /*) ;;
  *) echo "PREFIX must be an absolute path: $prefix" >&2; exit 2 ;;
esac

if [ -x "$prefix/bin/git" ] && "$prefix/bin/git" version >/dev/null 2>&1; then
  echo "kept the build in $prefix"
  "$prefix/bin/git" version
  exit 0
fi

jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)
work=$(mktemp -d "${TMPDIR:-/tmp}/build-git.XXXXXX")
trap 'rm -rf "$work"' EXIT

archive="$work/source.tar.gz"
curl --fail --silent --show-error --location --retry 3 \
  --output "$archive" "https://github.com/git/git/archive/$ref.tar.gz"
if [ -n "$want_sha" ]; then
  if command -v sha256sum >/dev/null 2>&1; then
    got_sha=$(sha256sum "$archive" | cut -d' ' -f1)
  else
    got_sha=$(shasum -a 256 "$archive" | cut -d' ' -f1)
  fi
  if [ "$got_sha" != "$want_sha" ]; then
    echo "the archive of $ref hashes to $got_sha, not $want_sha" >&2
    exit 1
  fi
fi

mkdir "$work/src"
tar -xzf "$archive" -C "$work/src" --strip-components=1

# shellcheck disable=SC2086 # BUILD_GIT_MAKEFLAGS is a list of words
flags=(
  "prefix=$prefix"
  # Git 2.34 uses thread_local as an identifier, a keyword in C23.
  # Keep the upstream optimization/warning defaults in GNU C11 mode.
  "CFLAGS=-g -O2 -Wall -std=gnu11"
  NO_GETTEXT=YesPlease
  NO_TCLTK=YesPlease
  NO_PERL=YesPlease
  NO_PYTHON=YesPlease
  NO_EXPAT=YesPlease
  NO_CURL=YesPlease
  NO_OPENSSL=YesPlease
  NO_RUST=YesPlease
  NO_INSTALL_HARDLINKS=YesPlease
  ${BUILD_GIT_MAKEFLAGS:-}
)
make -C "$work/src" -j"$jobs" "${flags[@]}" all >"$work/build.log" 2>&1 || {
  tail -n 60 "$work/build.log" >&2
  exit 1
}
make -C "$work/src" "${flags[@]}" install >"$work/install.log" 2>&1 || {
  tail -n 60 "$work/install.log" >&2
  exit 1
}
"$prefix/bin/git" version
