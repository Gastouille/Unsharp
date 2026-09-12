# Uniform environment: same Python, same dependencies, on any machine.
#
#   cp .env.example .env        # then fill in your XTB credentials
#   docker compose build
#   docker compose run --rm bot check
#   docker compose up           # runs with --dry-run by default
#
FROM python:3.12-slim

# tzdata is required: the trading sessions are timezone-aware.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: a code change then rebuilds only the layers below.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir --no-deps -e .

# Never run a trading bot as root.
RUN useradd --create-home --uid 1000 trader \
 && mkdir -p /app/data/logs /app/data/signals \
 && chown -R trader:trader /app
USER trader

ENTRYPOINT ["unsharp-bot"]
CMD ["check"]
