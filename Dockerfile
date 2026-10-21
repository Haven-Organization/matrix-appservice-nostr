FROM python:3.14-slim AS base

# Pinned rather than left to whatever --system would pick: a bind-mounted
# host directory (for storage.data_dir) needs a stable, documented UID/GID
# to be pre-chowned to (`chown -R 999:999`).
RUN groupadd --gid 999 bridge \
    && useradd --system --uid 999 --gid 999 --create-home --shell /usr/sbin/nologin bridge

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge/ ./bridge/
COPY main.py .

# The mounted config.yaml must set storage.data_dir to /data: the default
# "./data" resolves against /app, which the "bridge" user can't write.
RUN mkdir -p /data && chown bridge:bridge /data
VOLUME ["/data"]

USER bridge
ENV BRIDGE_CONFIG=/config/config.yaml

# To run a one-off command in this image (generating the AppService
# registration, say), override the entrypoint:
#   docker run --rm --entrypoint python -v ...:/config/config.yaml:ro \
#     -v bridge-data:/data <image> -m bridge.appservice \
#     /config/config.yaml /data/appservice-registration.yaml

# The mounted config.yaml must set bridge.listen_host to 0.0.0.0, or nothing
# outside the container can reach the bridge. EXPOSE assumes the default port.
EXPOSE 8092

ENTRYPOINT ["python", "main.py"]
