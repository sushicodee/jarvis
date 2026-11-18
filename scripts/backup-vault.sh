#!/bin/bash
# Daily compressed snapshot of the Obsidian vault onto the external SSD.
# Skips quietly if the SSD isn't mounted. Keeps the newest $KEEP snapshots;
# only files matching vault-*.tar.gz inside $DEST are ever pruned.
set -euo pipefail
VAULT="${VAULT:-$HOME/Documents/Obsidian Vault}"
SSD="${SSD:-/Volumes/Sushicodee}"
DEST="$SSD/Backups/obsidian-vault"
KEEP="${KEEP:-30}"

if ! /sbin/mount | grep -q " on $SSD ("; then   # full path: pm2's PATH has no /sbin
  echo "$(date '+%F %T') SSD not mounted at $SSD; skipping"
  exit 0
fi
mkdir -p "$DEST"
rm -f -- "$DEST"/vault-*.tar.gz.partial          # leftovers from an interrupted run
out="$DEST/vault-$(date +%Y-%m-%d_%H%M).tar.gz"
tmp="$out.partial"
trap 'rm -f -- "$tmp"' EXIT                     # no partial file if interrupted
tar -czf "$tmp" -C "$(dirname "$VAULT")" --exclude='.DS_Store' --exclude='.trash' "$(basename "$VAULT")"
gzip -t "$tmp"                      # verify the archive before keeping it
mv "$tmp" "$out"
echo "$(date '+%F %T') wrote $out ($(du -h "$out" | cut -f1))"

ls -1t "$DEST"/vault-*.tar.gz | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -f -- "$old" && echo "pruned $old"
done
