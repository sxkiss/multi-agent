# multi-agent (bt-agent) 运行镜像
# 启动方式与端口保持不变：uvicorn web_server:app --host 0.0.0.0 --port 9876
# 运行时数据全部落在容器内，不依赖外部挂载

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# git 供 update_manager 检测/拉取更新；curl 供健康检查
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 复制应用代码（.dockerignore 已排除 .venv/.git/logs 等运行时数据）
COPY . /app

EXPOSE 9876

CMD ["python", "-m", "uvicorn", "web_server:app", "--host", "0.0.0.0", "--port", "9876"]
