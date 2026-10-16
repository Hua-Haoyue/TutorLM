"""入口：``python -m control_plane``。"""

from __future__ import annotations

import argparse
import logging


def main() -> None:
    parser = argparse.ArgumentParser(description="TutorLM 多实例控制面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--no-startup-tasks",
        action="store_true",
        help="跳过启动对账与后台 GC（仅调试用）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    import uvicorn

    from control_plane.api.app import create_app

    app = create_app(run_startup_tasks=not args.no_startup_tasks)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
