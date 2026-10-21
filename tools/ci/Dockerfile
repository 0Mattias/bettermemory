# The container of the membership-docker leg (design 8.7, E10): Python
# 3.13 on Alpine with git and nothing else. The base is pinned by the
# digest of its multi-arch index, so an amd64 runner and an arm64 host
# build from the same release. No bettermemory is installed: the leg
# mounts the source tree read-only and imports the hook client's modules
# from it (tests/test_membership_container.py).
FROM python:3.13.16-alpine3.24@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a

# /work holds the clones and copies made inside a container, at paths the
# host cannot see; /workspaces holds the bind-mounted repository. Both are
# open to any user, since the leg runs as the host user's uid on Linux.
RUN apk add --no-cache git \
    && mkdir -m 1777 /work /workspaces
