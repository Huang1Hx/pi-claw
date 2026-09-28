"""支持 ``python -m interfaces``，与 pi-claw 命令共用入口。"""

from interfaces.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
