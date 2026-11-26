FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Install from pyproject rather than a second hard-coded dependency list: the
# two lists drifted the first time a dependency was added.
COPY pyproject.toml ./
COPY app ./app
# The console script is the break-glass path when nobody can log in, so fail the
# build if it cannot import. A flat `packages = ["app"]` shipped app/*.py without
# app/repo for several releases: the server worked off /app while `shelfmark`
# died with ModuleNotFoundError.
RUN pip install --no-cache-dir . \
 && python -c "import app.repo, app.web" \
 && shelfmark --help > /dev/null

# The migrations live inside the package, and the console script imports the
# installed package rather than the source tree. Prove from a scratch directory
# that an installed copy can still create a schema: this is the assertion that
# would have caught `shelfmark restore` dying on a fresh volume.
RUN cd /tmp \
 && SHELFMARK_DATA_DIR=/tmp/migrations-smoke python -c "from app import db; assert db.migrate(), db.MIGRATIONS" \
 && rm -rf /tmp/migrations-smoke

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin shelfmark \
 && mkdir -p /data && chown shelfmark:shelfmark /data
USER 10001
ENV SHELFMARK_DATA_DIR=/data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
