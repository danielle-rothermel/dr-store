from __future__ import annotations

import importlib
import importlib.resources
import pathlib
import pkgutil
import sys

FUNCTIONAL_MODULES = (
    "dr_store.content_addressing",
    "dr_store.core",
    "dr_store.core.errors",
    "dr_store.core.filesystem",
    "dr_store.document_file",
    "dr_store.document_file.errors",
    "dr_store.document_file.file",
    "dr_store.document_directory",
    "dr_store.document_directory.directory",
    "dr_store.document_directory.sidecar",
    "dr_store.object_store",
    "dr_store.record_cache",
    "dr_store.record_cache.cache",
    "dr_store.record_cache.sqlite",
    "dr_store.storage_backends",
    "dr_store.storage_backends.contract",
    "dr_store.storage_backends.memory",
    "dr_store.storage_backends.sqlite",
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

    discovered_modules = {
        module.name
        for module in pkgutil.walk_packages(
            package.__path__, prefix=f"{package.__name__}."
        )
    }
    assert discovered_modules == set(FUNCTIONAL_MODULES), (
        "dr_store module set differs from the expected package layout: "
        f"expected={sorted(FUNCTIONAL_MODULES)!r}, "
        f"actual={sorted(discovered_modules)!r}"
    )

    for module_name in FUNCTIONAL_MODULES:
        importlib.import_module(module_name)

    typed_marker = importlib.resources.files("dr_store").joinpath("py.typed")
    assert typed_marker.is_file(), "dr_store/py.typed is absent from the wheel"

    print("built wheel package layout verified")


if __name__ == "__main__":
    main()
