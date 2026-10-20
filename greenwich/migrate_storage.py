#!/usr/bin/env python3
"""Change offline Greenwich storage ownership from 10001 to 10002 on Linux.

Stop every old and new worker before use. Detach the old PVC from all workers.
Mount it in a protected directory on an offline maintenance host or container.
Run as root: python3 migrate_storage.py /mnt/greenwich --confirm-workers-stopped
This command is a manual step, not a worker startup hook or deployment action.

The command covers every directory and regular file, including state.json,
council-cache.json, saved .lock files, and any nested files. It never reads JSON,
creates files, changes modes, or replaces records. Old entries must have owner
10001:10001. Directories can also have owner 0:10001 from the old fsGroup setup.
Entries already owned by 10002:10002 are accepted for safe repeat runs.
Ancestors must be root-owned and protected against writes by other users.
Root-owned sticky directories, such as /tmp, are accepted as ancestors only.

All entries stay open through validation and migration. Every saved .lock file
gets an exclusive, nonblocking flock lease before any ownership change.
Symlinks, hard-linked files, nested mounts, special files, unexpected owners,
world-writable entries, and files with set-ID bits or capabilities are refused.
The latter restrictions prevent chown from removing other security metadata.
File contents, modes, inode identities, and access/modification times stay intact.
The filesystem changes ctime when ownership changes.

The stopped flag is an operator assertion. Advisory locks cannot prove that all
writers stopped. Keep the source detached and protected throughout this step.
Ownership changes across files are not a transaction. An I/O failure or process
interruption can leave a partial migration. Keep workers stopped and rerun after
resolving the fault. The host needs /proc, local flock support, and enough file
descriptors for the whole tree. Root needs permission to change ownership on the
mounted filesystem. No private paths, contents, or system errors are printed.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import os
import stat
import sys
from typing import NamedTuple


OLD_OWNER = (10001, 10001)
NEW_OWNER = (10002, 10002)
_FAILURE = ("Offline ownership migration refused or incomplete. "
            "Keep workers stopped and inspect the source offline.")
# Never treat a system tree or a general-purpose mount parent as state storage.
_SYSTEM_TREES = {"bin", "boot", "dev", "etc", "lib", "lib64", "proc", "run", "sbin", "sys", "usr"}
_UNSAFE_ROOTS = {"/", "/home", "/root", "/tmp", "/var", "/var/tmp", "/var/lib",
                 "/var/lib/kubelet", "/var/lib/kubelet/pods", "/mnt", "/media", "/srv", "/opt"}


class MigrationRefused(Exception):
    """A fixed message replaces all source-specific failure details."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid arguments, which can contain paths.
        raise MigrationRefused(_FAILURE)


class _Entry(NamedTuple):
    fd: int
    parent: int
    name: str
    saved: os.stat_result
    children: tuple[str, ...] | None


def _refuse() -> None:
    raise MigrationRefused(_FAILURE)


def _identity(value: os.stat_result) -> tuple:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid)


def _snapshot(value: os.stat_result) -> tuple:
    return (_identity(value), value.st_nlink, value.st_size, value.st_atime_ns,
            value.st_mtime_ns, value.st_ctime_ns)


def _parts(directory: str | os.PathLike) -> list[str]:
    source = os.fspath(directory)
    if not isinstance(source, str) or not source.startswith("/") or "\0" in source:
        _refuse()
    parts = source[1:].split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _refuse()
    if source in _UNSAFE_ROOTS or parts[0] in _SYSTEM_TREES:
        _refuse()
    return parts


def _ancestor(value: os.stat_result) -> None:
    mode = value.st_mode
    if not stat.S_ISDIR(mode) or value.st_uid != 0:
        _refuse()
    # A sticky root-owned parent prevents other users from replacing entries.
    if mode & stat.S_ISVTX:
        return
    if mode & stat.S_IWOTH or (mode & stat.S_IWGRP and value.st_gid != 0):
        _refuse()


def _mount_id(fd: int) -> int:
    # st_dev alone does not detect a bind mount of the same filesystem.
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as stream:
        for line in stream:
            if line.startswith("mnt_id:"):
                return int(line.split(":", 1)[1])
    _refuse()


def _open(stack: ExitStack, name: str, flags: int, parent: int | None = None) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOATIME,
                 dir_fd=parent)
    stack.callback(os.close, fd)
    return fd


def _metadata(value: os.stat_result) -> None:
    directory = stat.S_ISDIR(value.st_mode)
    if not directory and not stat.S_ISREG(value.st_mode):
        _refuse()
    owners = {OLD_OWNER, NEW_OWNER}
    if directory:
        owners.add((0, 10001))
    if (value.st_uid, value.st_gid) not in owners or value.st_mode & stat.S_IWOTH:
        _refuse()
    if not directory and (value.st_nlink != 1 or value.st_mode & (stat.S_ISUID | stat.S_ISGID)):
        _refuse()


def _unchanged(entry: _Entry) -> None:
    if _snapshot(os.fstat(entry.fd)) != _snapshot(entry.saved):
        _refuse()
    current = os.stat(entry.name, dir_fd=entry.parent, follow_symlinks=False)
    if _snapshot(current) != _snapshot(entry.saved):
        _refuse()
    if entry.children is not None and tuple(sorted(os.listdir(entry.fd))) != entry.children:
        _refuse()


def _migrate(directory: str | os.PathLike) -> int:
    parts = _parts(directory)
    with ExitStack() as stack:
        parent = _open(stack, "/", os.O_RDONLY | os.O_DIRECTORY)
        _ancestor(os.fstat(parent))
        ancestors = []
        for name in parts[:-1]:
            saved = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _ancestor(saved)
            fd = _open(stack, name, os.O_RDONLY | os.O_DIRECTORY, parent)
            if _identity(os.fstat(fd)) != _identity(saved):
                _refuse()
            ancestors.append((parent, name, fd, saved))
            parent = fd

        name = parts[-1]
        saved = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(saved.st_mode):
            _refuse()
        _metadata(saved)
        root = _open(stack, name, os.O_RDONLY | os.O_DIRECTORY, parent)
        if _snapshot(os.fstat(root)) != _snapshot(saved):
            _refuse()
        device, mount = saved.st_dev, _mount_id(root)
        entries: list[_Entry] = []
        seen = set()

        def visit(fd: int, parent_fd: int, child: str, before: os.stat_result) -> None:
            current = os.fstat(fd)
            _metadata(current)
            key = (current.st_dev, current.st_ino)
            if (_snapshot(current) != _snapshot(before) or key in seen
                    or current.st_dev != device or _mount_id(fd) != mount):
                _refuse()
            seen.add(key)
            if stat.S_ISREG(current.st_mode) and "security.capability" in os.listxattr(fd):
                _refuse()
            children = tuple(sorted(os.listdir(fd))) if stat.S_ISDIR(current.st_mode) else None
            entries.append(_Entry(fd, parent_fd, child, current, children))
            for nested in children or ():
                before = os.stat(nested, dir_fd=fd, follow_symlinks=False)
                _metadata(before)  # Reject special files before opening them.
                flags = os.O_RDONLY | os.O_NONBLOCK
                if stat.S_ISDIR(before.st_mode):
                    flags |= os.O_DIRECTORY
                nested_fd = _open(stack, nested, flags, fd)
                visit(nested_fd, fd, nested, before)

        visit(root, parent, name, saved)
        # No creation or truncation: lease the exact saved lock inodes.
        # ExitStack closes every descriptor on success or refusal, releasing leases.
        for entry in entries:
            if entry.children is None and entry.name.endswith(".lock"):
                fcntl.flock(entry.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Revalidate bindings, metadata, and directory membership after all leases.
        for ancestor_parent, ancestor_name, fd, before in ancestors:
            _ancestor(os.fstat(fd))
            if (_identity(os.fstat(fd)) != _identity(before)
                    or _identity(os.stat(ancestor_name, dir_fd=ancestor_parent,
                                         follow_symlinks=False)) != _identity(before)):
                _refuse()
        for entry in entries:
            _unchanged(entry)

        changed = 0
        # Migrate leaves first. Keep each descriptor and lease until the end.
        for entry in reversed(entries):
            _unchanged(entry)
            if (entry.saved.st_uid, entry.saved.st_gid) == NEW_OWNER:
                continue
            os.fchown(entry.fd, *NEW_OWNER)
            changed += 1
            after = os.fstat(entry.fd)
            if ((after.st_uid, after.st_gid) != NEW_OWNER
                    or after.st_mode != entry.saved.st_mode):
                _refuse()
            os.fsync(entry.fd)
        return changed


def migrate(directory: str | os.PathLike, *, confirm_workers_stopped: bool = False) -> int:
    """Return the changed-entry count. Refuse without root and explicit confirmation."""
    try:
        if confirm_workers_stopped is not True or os.geteuid() != 0 or sys.platform != "linux":
            _refuse()
        return _migrate(directory)
    except Exception:
        # Do not expose OSError filenames or other source-specific exception text.
        raise MigrationRefused(_FAILURE) from None


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="migrate_storage.py", description=(
        "Offline ownership migration to 10002:10002. Stop all workers and detach "
        "their volume before mounting it in a protected directory. Run as root."),
        allow_abbrev=False)
    parser.add_argument("directory", help="Absolute path to the protected, offline state directory.")
    parser.add_argument("--confirm-workers-stopped", action="store_true", help=(
        "Assert that every old and new worker is stopped and cannot access this volume."))
    try:
        args = parser.parse_args(argv)
        migrate(args.directory, confirm_workers_stopped=args.confirm_workers_stopped)
    except (Exception, KeyboardInterrupt):
        print(_FAILURE, file=sys.stderr)
        return 1
    print("Offline ownership migration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
