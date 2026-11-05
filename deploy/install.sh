#!/usr/bin/env bash
# Runs as root on the server, at its first boot and on every deploy.sh: installs what the server needs,
# writes the cloudflared and Litestream settings from SSM ($PARAMS), restores the database from R2 on a
# fresh box, and (re)starts the three services.
set -euo pipefail
cd "$(dirname "$0")/.."
PARAMS=${PARAMS:?}
DATA=/var/lib/bar-rewind
CLOUDFLARED_VERSION=2026.10.7
CLOUDFLARED_SHA256=d9407eb6e70ac31f54978a6b408a0320b51b664a6d0b498248692d7ac56bf836
LITESTREAM_VERSION=0.5.18
LITESTREAM_SHA256=4a19d2f19e65e1e9b99d47708d2e6135c10ab64fce973d35745a87c72656720e

install_deb() {
  local name=$1 version=$2 url=$3 sha256=$4
  [ "$(dpkg-query -W -f '${Version}' "$name" 2>/dev/null)" = "$version" ] && return
  curl -sSfLo "/tmp/$name.deb" "$url"
  echo "$sha256  /tmp/$name.deb" | sha256sum -c -
  apt-get install -y -q -o Dpkg::Options::=--force-confold "/tmp/$name.deb"
  rm "/tmp/$name.deb"
}

if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap -q /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi
swapon --show=NAME --noheadings | grep -qx /swapfile || swapon /swapfile

if ! python3 -c "import boto3" 2>/dev/null; then
  apt-get update -q
  apt-get install -y -q python3 python3-boto3
fi
install_deb cloudflared "$CLOUDFLARED_VERSION" \
  "https://github.com/cloudflare/cloudflared/releases/download/$CLOUDFLARED_VERSION/cloudflared-linux-arm64.deb" "$CLOUDFLARED_SHA256"
install_deb litestream "$LITESTREAM_VERSION" \
  "https://github.com/benbjohnson/litestream/releases/download/v$LITESTREAM_VERSION/litestream-$LITESTREAM_VERSION-linux-arm64.deb" "$LITESTREAM_SHA256"

id bar-rewind >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin bar-rewind
install -d -o bar-rewind -g bar-rewind -m 700 "$DATA"
install -d -m 700 /etc/bar-rewind
umask 077
# the package ships its own /etc/litestream.yml, world-readable: a new file gets this umask instead
rm -f /etc/litestream.yml /etc/bar-rewind/cloudflared.env
PARAMS=$PARAMS python3 - <<'EOF'
import os
from server import settings

s = settings.from_ssm(os.environ["PARAMS"])
with open("/etc/bar-rewind/cloudflared.env", "w") as f:
    f.write(f"TUNNEL_TOKEN={s['cloudflared-token']}\n")
with open("/etc/litestream.yml", "w") as f:
    f.write(f"""dbs:
  - path: /var/lib/bar-rewind/bar-rewind.db
    replica:
      type: s3
      bucket: {s['r2-bucket']}
      path: db
      endpoint: {s['r2-endpoint']}
      region: auto
      force-path-style: true
      access-key-id: {s['r2-access-key-id']}
      secret-access-key: {s['r2-secret-access-key']}
""")
EOF
umask 022
# Litestream shares the database's WAL files with the server, so it runs as the server's user
chown bar-rewind:bar-rewind /etc/litestream.yml
install -d /etc/systemd/system/litestream.service.d
printf '[Service]\nUser=bar-rewind\nGroup=bar-rewind\n' > /etc/systemd/system/litestream.service.d/user.conf
systemctl daemon-reload

if [ ! -f "$DATA/bar-rewind.db" ]; then
  systemctl stop litestream 2>/dev/null || true
  runuser -u bar-rewind -- litestream restore -config /etc/litestream.yml -if-replica-exists "$DATA/bar-rewind.db"
fi

sed "s|@PARAMS@|$PARAMS|; s|@REGION@|${AWS_DEFAULT_REGION:?}|; s|@CHECKOUT@|$PWD|" deploy/bar-rewind-server.service > /etc/systemd/system/bar-rewind-server.service
cp deploy/cloudflared.service /etc/systemd/system/bar-rewind-cloudflared.service
systemctl daemon-reload
systemctl enable -q bar-rewind-server bar-rewind-cloudflared litestream
systemctl restart bar-rewind-server
systemctl restart bar-rewind-cloudflared litestream
systemctl --no-pager --lines=0 status bar-rewind-server bar-rewind-cloudflared litestream
