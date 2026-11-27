FROM python:3.12-slim
LABEL ai.tiktok.shop.sandbox="v1"
COPY --chmod=0444 worker.py /opt/shop-sandbox/worker.py
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["python", "-I", "-B", "/opt/shop-sandbox/worker.py"]
