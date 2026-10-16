"""Reject direct Python imports across the outer/app ownership boundary."""
from __future__ import annotations

import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
RULES = (
    (ROOT / 'runtime-host' / 'control-plane' / 'control_plane', {'backend', 'instance_app'}),
    (ROOT / 'instance-app', {'control_plane', 'runtime_host'}),
)
SKIP = {'.venv', 'node_modules', '__pycache__', 'dist'}


def violations(root: Path, forbidden: set[str]) -> list[str]:
    failures = []
    for path in sorted(root.rglob('*.py')):
        if SKIP.intersection(path.relative_to(root).parts):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imports = [node.module]
            else:
                continue
            for module in imports:
                if module.split('.')[0] in forbidden:
                    failures.append(f'{path.relative_to(ROOT)}:{node.lineno}: forbidden import {module}')
    return failures


def main() -> int:
    failures = [item for root, forbidden in RULES for item in violations(root, forbidden)]
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    print('Ownership boundary check passed (static Python imports only).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
