# One image for all three fake-restaurant services; APP selects the module.
FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi uvicorn "psycopg[binary]"
WORKDIR /srv
COPY apps/ /srv/apps/
ENV APP=gateway
CMD ["sh", "-c", "uvicorn apps.${APP}:app --host 0.0.0.0 --port 8000"]
