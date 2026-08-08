"""启动 Mini-SWE web 控制台。

用法：
    python -m swe_agent.web [--project DIR] [--port N]
"""

import argparse

from swe_agent.web import server


def main() -> None:
    parser = argparse.ArgumentParser(description="Mini-SWE web 控制台")
    parser.add_argument("--project", required=True,
                        help="工作目录（必填：图生成隔离，只扫此目录及子目录）")
    parser.add_argument("--port", type=int, default=None,
                        help=f"端口（默认 {server.DEFAULT_PORT}，被占自动顺延）")
    args = parser.parse_args()
    server.main(project=args.project, port=args.port)


if __name__ == "__main__":
    main()
