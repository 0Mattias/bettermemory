#!/usr/bin/env bash
# The loopback NFSv3 mount of the membership-nfs leg (design 8.7: E11,
# E15, E19, E20 and E33 on a filesystem that reports no birth time).
#
# Run as root on an Ubuntu runner. Installs the kernel NFS server, exports
# a directory under /srv to this host only (rw, sync, no_subtree_check,
# no_root_squash), gives that directory to the uid and gid named, so their
# pytest owns what it writes there, and mounts the export over NFSv3.
# proto=tcp and mountproto=tcp keep both protocols on IPv4, so "localhost"
# reaches the server as the 127.0.0.1 the export names.
#
# Usage: sudo bash tools/ci/nfs_export.sh UID GID [EXPORT_DIR] [MOUNT_POINT]
set -euo pipefail

owner_uid=${1:?usage: nfs_export.sh UID GID [EXPORT_DIR] [MOUNT_POINT]}
owner_gid=${2:?usage: nfs_export.sh UID GID [EXPORT_DIR] [MOUNT_POINT]}
export_dir=${3:-/srv/bm-nfs}
mount_point=${4:-/mnt/nfs}
options="rw,sync,no_subtree_check,no_root_squash"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nfs-kernel-server nfs-common
# Some runner kernels carry the NFS modules in the extra package only.
if ! { modprobe nfsd && modprobe nfs; }; then
  apt-get install -y -qq "linux-modules-extra-$(uname -r)"
  modprobe nfsd
  modprobe nfs
fi

mkdir -p "$export_dir" "$mount_point"
chown "$owner_uid:$owner_gid" "$export_dir"
printf '%s 127.0.0.1(%s)\n' "$export_dir" "$options" >>/etc/exports
systemctl restart nfs-kernel-server
exportfs -ra
echo "== exportfs -v"
exportfs -v

if ! mount -t nfs -o vers=3,proto=tcp,mountproto=tcp \
  "localhost:$export_dir" "$mount_point"; then
  echo "== the NFSv3 mount failed; the server's view" >&2
  rpcinfo -p localhost >&2 || true
  showmount -e localhost >&2 || true
  exit 1
fi
echo "mounted localhost:$export_dir at $mount_point over NFSv3"
