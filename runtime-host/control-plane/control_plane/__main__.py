"""入口：``python -m control_plane``。"""

from __future__ import annotations

import sys

from control_plane.cli import main as cli_main


def main() -> None:
    raise SystemExit(cli_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
