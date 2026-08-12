from __future__ import annotations

from typing import TYPE_CHECKING

MANIFEST_NAME = "record.json"

if TYPE_CHECKING:
    import os
    from collections.abc import Callable


def make_directory_descriptor_recorder(
    original_open: Callable[..., int],
) -> tuple[Callable[..., int], set[int]]:
    directory_descriptors: set[int] = set()

    def recording_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is None:
            directory_descriptors.add(descriptor)
        return descriptor

    return recording_open, directory_descriptors
