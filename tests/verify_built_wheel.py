"""Verify the installed wheel without importing from the repository."""

from __future__ import annotations

import importlib
import importlib.resources
import importlib.util
import pathlib
import sys

PUBLIC_MODULES = (
    "dr_store",
    "dr_store.document_directory",
    "dr_store.storage_backends",
)

FUNCTIONAL_MODULES = (
    "dr_store.content_addressing",
    "dr_store.core",
    "dr_store.core.errors",
    "dr_store.core.filesystem",
    "dr_store.document_directory",
    "dr_store.document_directory.directory",
    "dr_store.document_directory.sidecar",
    "dr_store.object_store",
    "dr_store.storage_backends",
    "dr_store.storage_backends.contract",
    "dr_store.storage_backends.memory",
    "dr_store.storage_backends.sqlite",
)

REMOVED_MODULES = (
    "dr_store.backends",
    "dr_store.docdir",
    "dr_store.references",
    "dr_store.store",
)


def _assert_repository_is_not_importable(repository: pathlib.Path) -> None:
    for entry in sys.path:
        path = pathlib.Path(entry or pathlib.Path.cwd()).resolve()
        assert not path.is_relative_to(repository), (
            f"repository path leaked onto sys.path: {path}"
        )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_built_wheel.py REPOSITORY_ROOT")

    repository = pathlib.Path(sys.argv[1]).resolve()
    _assert_repository_is_not_importable(repository)

    package = importlib.import_module("dr_store")
    package_path = pathlib.Path(package.__file__).resolve()
    assert package_path.is_relative_to(pathlib.Path(sys.prefix).resolve()), (
        "dr_store was not imported from the isolated environment: "
        f"{package_path}"
    )

    for module_name in FUNCTIONAL_MODULES:
        importlib.import_module(module_name)

    for module_name in PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        for export in module.__all__:
            assert getattr(module, export) is not None, (
                f"{module_name}.{export} is not importable"
            )

    typed_marker = importlib.resources.files("dr_store").joinpath("py.typed")
    assert typed_marker.is_file(), "dr_store/py.typed is absent from the wheel"

    for module_name in REMOVED_MODULES:
        assert importlib.util.find_spec(module_name) is None, (
            f"removed module remains importable: {module_name}"
        )

    print("built wheel public surface verified")


if __name__ == "__main__":
    main()
