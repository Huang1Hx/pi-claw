#!/bin/sh
set -eu

: "${PI_CLAW_CWD_RELATIVE:?缺少受信宿主提供的工作目录}"
case "${PI_CLAW_CWD_RELATIVE}" in
    /* | *../* | ../* | *"/.." | *"
"*)
        echo "拒绝不安全的容器工作目录" >&2
        exit 125
        ;;
esac

# /input 是唯一宿主挂载且只读；所有命令副作用只发生在有容量上限的 tmpfs。
cp -R /input/. /workspace/
# 快照保持只读便于安全挂载；只放宽容器私有副本，产品文件不会回写宿主。
chmod -R u+rwX /workspace
cd "/workspace/${PI_CLAW_CWD_RELATIVE}"
exec "$@"
