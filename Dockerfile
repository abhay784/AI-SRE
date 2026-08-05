FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY slopsaver ./slopsaver
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home monitor \
    && mkdir -p /app/var /app/config \
    && chown -R monitor:monitor /app/var
USER monitor
CMD ["ai-sre", "run", "--config", "/app/config/customer.yaml"]
