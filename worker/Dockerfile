FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-boto3 p7zip-full curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# BAR-Rewind's Linux package (zig build dist in BAR-Rewind, or a release); deploy.sh puts it here
COPY bar-rewind-linux.tar.gz /tmp/
RUN tar -xzf /tmp/bar-rewind-linux.tar.gz -C /opt && rm /tmp/bar-rewind-linux.tar.gz
COPY worker.py /opt/worker.py
ENTRYPOINT ["python3", "/opt/worker.py"]
