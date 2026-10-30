# Build from the repo root:  docker build -f services/_template/Dockerfile .
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY libs/common /build/libs/common
COPY services/_template /build/service
RUN pip install --prefix=/install /build/libs/common /build/service

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY --from=builder /install /usr/local
RUN useradd --create-home appuser
USER appuser
WORKDIR /home/appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
