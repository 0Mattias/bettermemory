#!/bin/bash
# Stand up, or re-pin, the public AML endpoint on Amazon Linux 2023 (arm64).
#
# Run on the host as root, with the adapter token on stdin:
#
#   sudo bash deploy.sh <commit> <hostname> [<hostname>...] < token-file
#
# Idempotent: a second run fetches, checks out <commit>, re-syncs the venv,
# rewrites the units and the Caddyfile, and restarts both services. The
# stores under /var/lib/bm-aml are left alone. The adapter
# (bench/aml/server.py) listens on 127.0.0.1:8080 only; Caddy terminates TLS
# for every <hostname>, passes the API through, and answers a browser's GET
# with the static pages in bench/aml/site/.
set -euo pipefail
COMMIT="$1"; shift
HOSTS="$*"
TOKEN="$(cat)"
[ -n "$TOKEN" ] || { echo "token on stdin required" >&2; exit 1; }

dnf -y -q install git >/dev/null

id bmaml >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/bm-aml --shell /sbin/nologin bmaml
id caddy >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/caddy --shell /sbin/nologin caddy

if [ ! -x /usr/local/bin/uv ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi

if [ ! -d /opt/bm-aml/src/.git ]; then
  sudo -u bmaml git clone -q https://github.com/0Mattias/bettermemory.git /opt/bm-aml/src
fi
sudo -u bmaml git -C /opt/bm-aml/src fetch -q origin
sudo -u bmaml git -C /opt/bm-aml/src checkout -q --detach "$COMMIT"
cd /opt/bm-aml/src
sudo -u bmaml env UV_CACHE_DIR=/opt/bm-aml/.uv-cache UV_PYTHON_INSTALL_DIR=/opt/bm-aml/.python \
  /usr/local/bin/uv sync -q --frozen --no-dev --python 3.11

install -d -o bmaml -g bmaml -m 700 /var/lib/bm-aml
umask 077
printf 'AML_ADAPTER_TOKEN=%s\nAML_STORE_ROOT=/var/lib/bm-aml\nBETTERMEMORY_DIR=/var/lib/bm-aml/.unused\n' "$TOKEN" > /etc/bm-aml.env
chown root:bmaml /etc/bm-aml.env; chmod 640 /etc/bm-aml.env
umask 022

# The pages a browser sees. Copied out of the checkout because bmaml's home
# is not readable by the caddy user.
install -d -m 755 /var/www/bm-aml
install -m 644 /opt/bm-aml/src/bench/aml/site/index.html /opt/bm-aml/src/bench/aml/site/404.html /var/www/bm-aml/

cat > /etc/systemd/system/bm-aml.service <<'UNIT'
[Unit]
Description=bettermemory AML adapter (Add/Search/health)
After=network-online.target
Wants=network-online.target

[Service]
User=bmaml
Group=bmaml
EnvironmentFile=/etc/bm-aml.env
WorkingDirectory=/opt/bm-aml/src
ExecStart=/opt/bm-aml/src/.venv/bin/python bench/aml/server.py --host 127.0.0.1 --port 8080
Restart=always
RestartSec=2
LimitNOFILE=65536
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/bm-aml
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

if [ ! -x /usr/local/bin/caddy ]; then
  curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=arm64" -o /usr/local/bin/caddy
  chmod 755 /usr/local/bin/caddy
fi
setcap cap_net_bind_service=+ep /usr/local/bin/caddy
install -d -m 755 /etc/caddy
SITES=""
for h in $HOSTS; do SITES="${SITES:+$SITES, }$h"; done
# A browser's GET (anything but /health) gets the static pages, with the
# site's 404 for an unknown path; every other request, which is all of
# AML's, goes to the adapter unchanged. The pages inline their styles, font,
# favicon and art, so the policy allows inline style and data: only.
cat > /etc/caddy/Caddyfile <<CADDY
{
	admin off
}

(page_headers) {
	header {
		Content-Security-Policy "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		Cache-Control "public, max-age=300"
	}
}

$SITES {
	request_body {
		max_size 64MB
	}
	@page {
		method GET HEAD
		not path /health
	}
	handle @page {
		import page_headers
		root * /var/www/bm-aml
		file_server
	}
	handle {
		reverse_proxy 127.0.0.1:8080 {
			transport http {
				read_timeout 35m
				write_timeout 35m
			}
		}
	}
	handle_errors 404 {
		import page_headers
		root * /var/www/bm-aml
		rewrite * /404.html
		file_server
	}
	log {
		output discard
	}
}
CADDY
/usr/local/bin/caddy validate --config /etc/caddy/Caddyfile >/dev/null

cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy (TLS front for the AML adapter)
After=network-online.target
Wants=network-online.target

[Service]
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
Restart=always
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable -q --now bm-aml
systemctl restart bm-aml
systemctl enable -q --now caddy
systemctl restart caddy
sleep 3
systemctl is-active bm-aml caddy
curl -s -o /dev/null -w 'local health %{http_code}\n' http://127.0.0.1:8080/health
sudo -u bmaml git -C /opt/bm-aml/src rev-parse --short HEAD
