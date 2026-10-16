"""Packaging/entrypoint contract for the instance application image.

These are static checks plus one import check: they never build, pull or run
Docker, never touch the network and never install anything. They exist because
two acceptance failures were packaging-only:

* ``backend/requirements.txt`` declared no third-party packages even though the
  HTTP layer imports fastapi/starlette/pydantic (and uvicorn serves it);
* the image ENTRYPOINT must resolve to a module that actually exists, with the
  ``backend`` package importable (WORKDIR + PYTHONPATH), so the container does
  not die on an import of a missing ``backend.host.app``.
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
DEPLOY = BACKEND.parent / "deploy"
REQUIREMENTS = BACKEND / "requirements.txt"
REQUIREMENTS_DEV = BACKEND / "requirements-dev.txt"
DOCKERFILE = DEPLOY / "Dockerfile"


def _requirement_names(path: Path) -> set[str]:
    """Distribution names declared in a requirements file (comments/options skipped)."""
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("-"):
            continue
        # name[extras]specifier  ->  normalised name
        name = re.split(r"[<>=!~;\[\s]", entry, maxsplit=1)[0].strip().lower()
        names.add(name.replace("_", "-"))
    return names


# -- requirements --------------------------------------------------------
def test_runtime_requirements_declare_the_http_stack():
    names = _requirement_names(REQUIREMENTS)
    # The Host/adapters import these at runtime; uvicorn serves the ASGI app.
    for required in ("fastapi", "starlette", "pydantic", "uvicorn"):
        assert required in names, f"{required} missing from requirements.txt"


def test_runtime_requirements_use_real_version_ranges():
    text = REQUIREMENTS.read_text(encoding="utf-8")
    # Every declared dependency carries a version specifier, not a bare pin-free name.
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("-"):
            continue
        assert re.search(r"[<>=!~]", entry), f"no version range declared for {entry!r}"


def test_dev_requirements_add_the_test_driver():
    names = _requirement_names(REQUIREMENTS_DEV)
    assert "pytest" in names
    # starlette/fastapi TestClient needs httpx to drive the ASGI app in tests.
    assert "httpx" in names
    # The dev file builds on the runtime file.
    assert "-r requirements.txt" in REQUIREMENTS_DEV.read_text(encoding="utf-8")


# -- Dockerfile ----------------------------------------------------------
def _dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_dockerfile_sets_workdir_and_pythonpath_for_the_backend_package():
    text = _dockerfile_text()
    # The ``backend`` package is imported as ``backend.*``; /app must be both the
    # working directory and on sys.path, otherwise nothing imports.
    assert re.search(r"^WORKDIR\s+/app\s*$", text, re.MULTILINE), "WORKDIR /app is missing"
    assert re.search(r"PYTHONPATH=/app", text), "PYTHONPATH=/app is missing"
    assert re.search(r"^COPY\s+instance-app/backend\s+/app/backend\s*$", text, re.MULTILINE)


def test_dockerfile_entrypoint_targets_an_importable_module():
    text = _dockerfile_text()
    match = re.search(r"^ENTRYPOINT\s+(\[.*\])\s*$", text, re.MULTILINE)
    assert match, "ENTRYPOINT is missing"

    argv = json.loads(match.group(1))
    assert argv[:3] == ["python", "-m", "uvicorn"], argv
    target = next((a for a in argv if ":" in a and "." in a and not a.startswith("-")), None)
    assert target, f"no module:factory target in ENTRYPOINT: {argv}"
    module_name, factory = target.split(":", 1)
    assert factory.isidentifier(), factory

    # The target must be importable *and* expose the factory, so the container's
    # first import cannot hit a missing module such as the non-existent
    # ``backend.host.app``.
    module = importlib.import_module(module_name)
    assert callable(getattr(module, factory)), f"{module_name}.{factory} is not callable"


def test_the_package_runner_and_the_image_target_the_same_factory_module():
    """``python -m backend`` and the image ENTRYPOINT must agree on one factory.

    The runner (``backend/__main__.py``) and the Dockerfile both point at the real
    application factory ``backend.adapters.http.app:create_app``. ``backend.host.app``
    never existed -- the Host layer holds the composition root
    (``backend.host.state``) and the HTTP assembly lives in ``backend.adapters.http.app``
    -- so the runner must not name it again.
    """
    # Guard: the module a stale runner used to import genuinely does not exist.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.host.app")

    main_text = (BACKEND / "__main__.py").read_text(encoding="utf-8")
    match = re.search(
        r"from\s+\.([\w.]+)\s+import\s+([A-Za-z_]\w*)", main_text
    )
    assert match, "backend/__main__.py no longer imports a factory in a recognisable form"
    runner_module = f"backend.{match.group(1)}"
    runner_factory = match.group(2)

    # The ENTRYPOINT target must be the exact same module and factory.
    entry_match = re.search(r"^ENTRYPOINT\s+(\[.*\])\s*$", _dockerfile_text(), re.MULTILINE)
    assert entry_match, "ENTRYPOINT is missing"
    argv = json.loads(entry_match.group(1))
    entry_target = next((a for a in argv if ":" in a and "." in a and not a.startswith("-")), None)
    assert entry_target, f"no module:factory target in ENTRYPOINT: {argv}"
    entry_module, entry_factory = entry_target.split(":", 1)

    assert (runner_module, runner_factory) == (entry_module, entry_factory), (
        f"runner targets {runner_module}:{runner_factory} but ENTRYPOINT targets "
        f"{entry_module}:{entry_factory}"
    )
    assert runner_module == "backend.adapters.http.app"

    # Importing it here proves the named factory exists and is callable, so neither
    # the container nor ``python -m backend`` dies on a missing module at import time.
    module = importlib.import_module(runner_module)
    assert callable(getattr(module, runner_factory)), f"{runner_module}.{runner_factory} is not callable"
