FROM python:3.12-slim

WORKDIR /app

RUN useradd -u 10001 -U -s /usr/sbin/nologin -M appuser

COPY --chown=appuser:appuser server.py .

USER appuser

ENV HOST=0.0.0.0
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import os, sys, urllib.request; port = os.environ.get('PORT', '8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).getcode() == 200 else 1)"

CMD ["python", "server.py"]
