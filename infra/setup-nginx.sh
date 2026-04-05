#!/bin/bash
# WattLab Phase 6 — nginx + certbot setup
# Run as: sudo bash infra/setup-nginx.sh
# From: /home/gos/wattlab/

set -e

echo "=== Step 1: Move Nextcloud to port 8080 ==="
snap set nextcloud ports.http=8080
echo "Nextcloud moved. Now at http://192.168.1.62:8080/"

echo ""
echo "=== Step 2: Install nginx + certbot ==="
apt-get install -y nginx certbot python3-certbot-nginx

echo ""
echo "=== Step 3: Create ACME challenge directory ==="
mkdir -p /var/www/certbot

echo ""
echo "=== Step 4: Install nginx config ==="
cp /home/gos/wattlab/infra/wattlab.nginx.conf /etc/nginx/sites-available/wattlab
ln -sf /etc/nginx/sites-available/wattlab /etc/nginx/sites-enabled/wattlab
# Disable default site if present
rm -f /etc/nginx/sites-enabled/default

echo ""
echo "=== Step 5: Test and start nginx ==="
nginx -t
systemctl enable nginx
systemctl restart nginx

echo ""
echo "=== Done! ==="
echo ""
echo "nginx is running. WattLab will be reachable on HTTP as soon as"
echo "the BouyguesBox forwards ports 80+443 to 192.168.1.62."
echo ""
echo "Once DNS for wattlab.greeningofstreaming.org has propagated, run:"
echo "  sudo certbot --nginx -d wattlab.greeningofstreaming.org"
echo ""
echo "Nextcloud is now at: http://192.168.1.62:8080/"
