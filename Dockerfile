FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEMO_MODE=true \
    SUPPORTOPS_DB_PATH=/app/data/supportops.sqlite3
WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md ./
COPY src ./src
COPY fixtures ./fixtures
COPY app.py ./
RUN uv sync --no-dev --no-cache

EXPOSE 5000
CMD ["uv", "run", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=5000", "--server.headless=true"]