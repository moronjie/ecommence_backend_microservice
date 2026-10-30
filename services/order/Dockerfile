FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY libs/common /build/libs/common
COPY services/order /build/service
RUN pip install --prefix=/install /build/libs/common /build/service

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY --from=builder /install /usr/local
RUN useradd --create-home appuser
USER appuser
WORKDIR /home/appuser
COPY --chown=appuser services/order/alembic.ini ./alembic.ini
COPY --chown=appuser services/order/migrations ./migrations
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
