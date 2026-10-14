FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements-feed.txt ./
COPY trd_bot ./trd_bot
RUN pip install --no-cache-dir -r requirements-feed.txt . && useradd --create-home bot && mkdir /app/runtime && chown bot:bot /app/runtime
COPY config.json ./
USER bot
CMD ["python", "-m", "trd_bot", "paper", "--seconds", "0"]
