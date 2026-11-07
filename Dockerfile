FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=streamable-http \
    HOST=0.0.0.0 \
    PORT=8000 \
    MCP_PATH=/mcp \
    SERVER_TZ=Asia/Bangkok \
    LOG_LEVEL=info

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY seismic_mcp_server.py .

RUN useradd --create-home --uid 10001 mcp
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["python", "seismic_mcp_server.py"]
