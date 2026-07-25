FROM python:3.12-slim

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app (includes app/web/index.html, served at /)
COPY app/ .

# persistent config + SQLite db live here; mount a volume at /config
RUN mkdir -p /config
ENV SOLARR_DB=/config/solarr.db
ENV PYTHONUNBUFFERED=1
VOLUME ["/config"]
EXPOSE 5000

# container health = the API health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/api/health').status==200 else 1)"

# gunicorn serves Flask; the in-process background worker thread (download
# polling / import) starts automatically once setup is complete. One worker
# keeps the worker thread and SQLite access single-process and simple.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "8", "--timeout", "120", "main:app"]
