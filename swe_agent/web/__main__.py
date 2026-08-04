"""启动 Mini-SWE web 控制台。

用法：
    python -m swe_agent.web [--project DIR] [--port N]
"""

import argparse

from swe_agent.web import server


def main() -> None:
    parser = argparse.ArgumentParser(description="Mini-SWE web 控制台")
    parser.add_argument("--project", default=None,
                        help="项目目录（代码图构建根，默认用 cwd）")
    parser.add_argument("--port", type=int, default=None,
                        help=f"端口（默认 {server.DEFAULT_PORT}，被占自动顺延）")
    args = parser.parse_args()
    server.main(project=args.project, port=args.port)


if __name__ == "__main__":
    main()
