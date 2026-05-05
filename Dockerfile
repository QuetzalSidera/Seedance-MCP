FROM python:3.12-slim

WORKDIR /app

COPY server.py /app/server.py
COPY config.example.toml /app/config.example.toml

RUN mkdir -p /app/outputs

EXPOSE 8765

CMD ["python", "/app/server.py", "--config", "/app/config.toml"]
