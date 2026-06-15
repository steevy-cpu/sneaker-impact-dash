#!/usr/bin/env bash
# backup_dash.sh — nightly backup of the dash's data.
#
# What it protects: sneakers.db (every photo/pair record + sync state) and the
# image stores (table photos, pair crops, curated label_data training set).
#
# DB: consistent online snapshot via sqlite's backup API (safe while the app
#     runs, WAL-aware), gzipped, keep the most recent 30.
# Images/label_data: rsync mirror (incremental — only new files copied).
#
# NOTE: this machine has ONE physical disk, so these backups guard against
# corruption, bad deploys, and accidental deletion — NOT disk failure. Point
# BACKUP_ROOT at an external drive or add an rclone push for real DR.
set -euo pipefail

DASH_DIR="/home/sneakerai/Documents/ShoeSort-main/sneaker-impact-dash"
BACKUP_ROOT="${BACKUP_ROOT:-/home/sneakerai/Backups/sneaker-dash}"
KEEP_DB=30

STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_ROOT/db" "$BACKUP_ROOT/images" "$BACKUP_ROOT/label_data"

# --- 1. SQLite online snapshot (the sqlite3 CLI is not installed; python is) --
python3 - <<EOF
import sqlite3
src = sqlite3.connect("$DASH_DIR/sneakers.db")
dst = sqlite3.connect("$BACKUP_ROOT/db/sneakers-$STAMP.db")
src.backup(dst)          # consistent even while the app is writing
dst.close(); src.close()
EOF
gzip "$BACKUP_ROOT/db/sneakers-$STAMP.db"

# --- 2. prune old DB snapshots (keep newest $KEEP_DB) ------------------------
ls -1t "$BACKUP_ROOT/db"/sneakers-*.db.gz 2>/dev/null | tail -n +$((KEEP_DB + 1)) | xargs -r rm --

# --- 3. mirror image stores (incremental) ------------------------------------
rsync -a --delete "$DASH_DIR/images/"     "$BACKUP_ROOT/images/"
rsync -a --delete "$DASH_DIR/sneaker_impact_training/label_data/" "$BACKUP_ROOT/label_data/"

echo "[backup] $STAMP ok — $(ls "$BACKUP_ROOT/db" | wc -l) db snapshots, $(du -sh "$BACKUP_ROOT" | cut -f1) total"
