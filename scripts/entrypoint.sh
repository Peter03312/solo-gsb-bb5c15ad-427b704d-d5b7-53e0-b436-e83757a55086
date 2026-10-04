#!/bin/sh
# 容器入口：初始化数据库表结构后启动服务。
set -e

echo "[entrypoint] 初始化数据库（create_all，幂等）..."
python -c "from app.database import init_db; init_db(); print('[entrypoint] 数据库就绪')"

echo "[entrypoint] 启动: $*"
exec "$@"
