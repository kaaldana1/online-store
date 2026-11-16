# Poshmark auto-delist worker. Runs a normal (not headless) Chromium on a virtual screen (Xvfb),
# so Poshmark sees the same kind of browser as when you run it yourself.
# Debian 12 (bookworm): a release Playwright officially supports.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp
WORKDIR /app

COPY automation/requirements.txt automation/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 xvfb xauth fonts-liberation \
 && pip install --no-cache-dir -r automation/requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /ms-playwright

COPY automation/ automation/

# data/ (database + screenshots) and browser-profiles/ (your saved login) are mounted from
# the host by docker-compose.yml. The start script runs the worker with --idle-if-off: if
# AUTO_DELIST is off it waits quietly instead of exiting, so Docker doesn't restart it in a loop.
CMD ["sh", "automation/docker-start.sh"]
