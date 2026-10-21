#!/usr/bin/env bash
# What a membership leg exercised (design 8.7): the mount's line and the
# stat of a file created on it. A "Birth: -" line, or a birth of 0 on the
# object line, means the filesystem reports no birth time, so the capture
# confirms the git directory's object by its token instead (E15).
#
# Usage: tools/ci/show_mount.sh MOUNT_POINT [FILE]
# FILE (default: a fresh file under MOUNT_POINT, removed afterwards) is
# created when missing and kept when given.
set -euo pipefail

mount_point=${1:?usage: show_mount.sh MOUNT_POINT [FILE]}
probe=${2:-}
keep=1
if [[ -z "$probe" ]]; then
  probe="$mount_point/stat-probe-$$"
  keep=0
fi

echo "== mount | grep $mount_point"
mount | grep " $mount_point " || {
  echo "nothing is mounted at $mount_point" >&2
  exit 1
}
[[ -e "$probe" ]] || : >"$probe"
echo "== stat $probe"
stat "$probe"
stat -c 'object: device %d inode %i birth %W' "$probe"
if [[ "$keep" == 0 ]]; then
  rm -f "$probe"
fi
