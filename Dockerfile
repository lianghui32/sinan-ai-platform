FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 先装依赖再拷代码：依赖层不变时利用 Docker 缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# 非 root 运行：容器被攻破时降低影响面（数据卷在宿主侧需保证 appuser 可写）
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# SQLite 数据落在这个卷上，容器重建不丢数据
VOLUME ["/app/data"]
EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:8005/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8005"]
