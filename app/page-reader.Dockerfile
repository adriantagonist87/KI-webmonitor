FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils libreoffice-writer libreoffice-calc libreoffice-impress fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o 1000 -g 1000 -m 0700 /run/kiwm-page-reader
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --chmod=0644 app/document_page_reader.py /usr/local/bin/document_page_reader.py
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "/usr/local/bin/document_page_reader.py", "--health"]
CMD ["python", "/usr/local/bin/document_page_reader.py"]
