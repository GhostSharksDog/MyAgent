"""冻结程序入口分流；目录选择 worker 不导入服务或初始化托盘。"""

import sys

PICKER_ARGUMENT = "--legacy-pick-directory"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    if len(args) > 1:
        if args[1] != PICKER_ARGUMENT or len(args) not in (3, 4):
            # 不把脚本路径、无效 worker 参数误当成一次普通启动。
            return 2
        from app.core.picker_worker import main as pick_main

        return pick_main([args[0], *args[2:]])

    from app.desktop.launcher import run

    run()
    return 0
