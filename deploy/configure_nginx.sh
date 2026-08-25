#!/usr/bin/env bash
# Configure nginx and obtain the first Let's Encrypt certificate.
# Usage: sudo ./deploy/configure_nginx.sh suporte.seudominio.com.br email@dominio.com.br

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET=/etc/nginx/sites-available/zz2

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run this script with sudo"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || fail "invalid domain"
[[ "$EMAIL" == *@* ]] || fail "invalid email"

for command in nginx certbot curl sed; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is not installed"
done

curl -fsS --max-time 5 http://127.0.0.1:8080/healthz >/dev/null \
  || fail "the application is not healthy on 127.0.0.1:8080"

install -d -m 0755 /etc/nginx/sites-available /etc/nginx/sites-enabled /var/www/html

# Start with HTTP only: the final template cannot be loaded until the certificate
# exists. The ACME path stays on disk; everything else reaches the application.
cat >"$TARGET" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

ln -sfn "$TARGET" /etc/nginx/sites-enabled/zz2
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

certbot certonly --webroot -w /var/www/html \
  --non-interactive --agree-tos --email "$EMAIL" -d "$DOMAIN"

sed "s/suporte\.example\.com\.br/$DOMAIN/g" \
  "$ROOT_DIR/deploy/nginx/zz2.conf" >"$TARGET"
nginx -t
systemctl reload nginx

printf 'HTTPS configured: https://%s\n' "$DOMAIN"
printf 'Check renewal with: sudo certbot renew --dry-run\n'
