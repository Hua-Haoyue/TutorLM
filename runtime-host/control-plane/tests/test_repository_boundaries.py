"""Verify that the lightweight ownership check detects forbidden imports."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture()
def boundary_checker():
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location('check_boundaries', root / 'scripts' / 'check-boundaries.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('source,module', [
    ('import backend.host', 'backend.host'),
    ('from backend.domain import models', 'backend.domain'),
    ('import control_plane.store', 'control_plane.store'),
])
def test_forbidden_import_is_reported(tmp_path, boundary_checker, source, module):
    path = tmp_path / 'service.py'
    path.write_text(source, encoding='utf-8')
    boundary_checker.ROOT = tmp_path
    failures = boundary_checker.violations(tmp_path, {'backend', 'control_plane'})
    assert failures == [f'service.py:1: forbidden import {module}']


def test_relative_and_unrelated_imports_are_allowed(tmp_path, boundary_checker):
    path = tmp_path / 'service.py'
    path.write_text('from .backend import models\nimport json\n', encoding='utf-8')
    assert boundary_checker.violations(tmp_path, {'backend'}) == []


def test_repository_has_no_direct_cross_boundary_imports(boundary_checker):
    assert boundary_checker.main() == 0
