FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 安装依赖（PuLP wheel 自带 CBC 求解器二进制，无需系统包）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用
COPY app ./app
COPY scripts ./scripts
RUN chmod +x scripts/entrypoint.sh

# SQLite 数据目录（Compose 中以卷挂载持久化）
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
