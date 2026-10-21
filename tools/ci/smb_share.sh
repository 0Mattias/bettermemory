#!/usr/bin/env bash
# The loopback Samba share of the membership-smb leg (design 8.7: E11,
# E15, E19, E20 and E33 on SMB, and E11 c, objects renumbered across a
# remount).
#
# Run as root on an Ubuntu runner.
#   setup:   installs samba and cifs-utils, makes USER a Samba user with
#            PASSWORD (both set in the job; neither is a secret of the
#            repository), shares a directory under /srv as "bmci" to that
#            user alone and mounts it over SMB 3.0, every file shown as
#            owned by UID and GID.
#   remount: unmounts the share and mounts it again with noserverino added,
#            so the client numbers every inode itself instead of taking the
#            server's numbers as the first mount (serverino) did: the remount
#            renumbers the objects, the case E11 c needs. It prints the
#            device and inode of one file before and after, so the log shows
#            whether the remount renumbered it.
#
# Usage: sudo bash tools/ci/smb_share.sh setup|remount UID GID USER PASSWORD
set -euo pipefail

usage="usage: smb_share.sh setup|remount UID GID USER PASSWORD"
action=${1:?$usage}
owner_uid=${2:?$usage}
owner_gid=${3:?$usage}
smb_user=${4:?$usage}
smb_password=${5:?$usage}
share_dir=/srv/bm-smb
mount_point=/mnt/smb
share=bmci

# Mounts the share; an argument adds to the options (",noserverino").
mount_share() {
  local extra=${1:-}
  mount -t cifs \
    -o "vers=3.0,username=$smb_user,password=$smb_password,uid=$owner_uid,gid=$owner_gid$extra" \
    "//localhost/$share" "$mount_point"
}

object_of() {
  stat -c 'device %d inode %i' "$1"
}

case "$action" in
setup)
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq samba cifs-utils smbclient
  # Some runner kernels carry the cifs module in the extra package only.
  if ! modprobe cifs; then
    apt-get install -y -qq "linux-modules-extra-$(uname -r)"
    modprobe cifs
  fi
  id "$smb_user" >/dev/null 2>&1 ||
    useradd --no-create-home --shell /usr/sbin/nologin "$smb_user"
  printf '%s\n%s\n' "$smb_password" "$smb_password" | smbpasswd -s -a "$smb_user"
  mkdir -p "$share_dir" "$mount_point"
  chown "$smb_user:" "$share_dir"
  cat >>/etc/samba/smb.conf <<EOF

[$share]
   path = $share_dir
   valid users = $smb_user
   read only = no
   browseable = no
EOF
  testparm -s >/dev/null
  systemctl restart smbd
  if ! mount_share; then
    echo "== the SMB mount failed; the server's view" >&2
    systemctl status smbd --no-pager >&2 || true
    smbclient -L localhost -U "$smb_user%$smb_password" -m SMB3 >&2 || true
    exit 1
  fi
  echo "mounted //localhost/$share ($share_dir) at $mount_point over SMB 3.0"
  ;;
remount)
  probe="$mount_point/remount-probe"
  [[ -e "$probe" ]] || : >"$probe"
  before=$(object_of "$probe")
  if ! umount "$mount_point"; then
    # Something still holds the share: name it, then detach it lazily so
    # the new mount is the one every later step reads.
    fuser -vm "$mount_point" >&2 || true
    umount -l "$mount_point"
  fi
  mount_share ",noserverino"
  after=$(object_of "$probe")
  echo "remount-probe before the remount: $before"
  echo "remount-probe after the remount:  $after"
  if [[ "$before" == "$after" ]]; then
    echo "the remount kept the file's object"
  else
    echo "the remount renumbered the file's object"
  fi
  ;;
*)
  echo "$usage" >&2
  exit 2
  ;;
esac
