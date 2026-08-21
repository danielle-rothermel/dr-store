"""POSIX local-filesystem primitives for private durable file trees.

``PrivateDirectory`` is an fd-anchored handle: every child operation uses
``dir_fd`` with ``O_NOFOLLOW`` and ``O_CLOEXEC``, so a path is resolved once
and symlink races are excluded. Ownership, file type, mode (``0o600`` /
``0o700`` with repair-on-drift), and regular-file hard-link count are
verified on every descriptor. Newly created directories fsync the child and
then the parent.

``FileLock`` composes that handle. Entering opens the parent as a private
directory, creates the lock file under that handle, and takes an advisory
``flock`` (shared or exclusive). ``lock.directory`` is the held parent and
is valid only while the lock is held. Two ``FileLock`` instances on the
same path exclude each other even in one process because each open has its
own file description.

The primitive is POSIX-only and assumes a local filesystem. ``flock`` over
NFS is unreliable and is excluded rather than handled. Lock-file naming
conventions stay caller-owned; the lock takes a path.
"""

from __future__ import annotations

from dr_store.localfs._fs import (
    FileLock,
    PrivateDirectory,
    ensure_private_directory,
    fsync_file,
    fsync_parent_directory,
    open_private_directory,
    open_private_regular_file,
)
from dr_store.localfs.errors import (
    LocalFsError,
    LockNotHeldError,
    PrivateDirectoryClosedError,
    PrivatePathReason,
    PrivatePathViolationError,
)

__all__ = [
    "FileLock",
    "LocalFsError",
    "LockNotHeldError",
    "PrivateDirectory",
    "PrivateDirectoryClosedError",
    "PrivatePathReason",
    "PrivatePathViolationError",
    "ensure_private_directory",
    "fsync_file",
    "fsync_parent_directory",
    "open_private_directory",
    "open_private_regular_file",
]
