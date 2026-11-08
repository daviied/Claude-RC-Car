FROM python:3.12-slim

WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ .

RUN useradd -r -u 10001 app && mkdir -p /data && chown app /data
USER app
ENV DATA_DIR=/data
VOLUME /data

EXPOSE 5000
# One worker (Socket.IO state lives in memory), many threads for WebSockets
CMD ["gunicorn", "-w", "1", "--threads", "100", "-b", "0.0.0.0:5000", "app:app"]
