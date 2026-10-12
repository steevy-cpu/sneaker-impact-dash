#!/usr/bin/env python3
"""
Check Cloudflare Images quota and optionally delete old temp uploads (tmp*.png).

Usage:
  python scripts/cloudflare_images_quota.py              # stats only
  python scripts/cloudflare_images_quota.py --dry-run    # show what would be deleted
  python scripts/cloudflare_images_quota.py --delete 500 # delete up to 500 oldest tmp*.png
"""

import argparse
import os
import sys

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "2184c4b5c9a9696f676879b737f97744")
API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/images/v1"


def headers():
    return {"Authorization": f"Bearer {API_TOKEN}"}


def get_stats():
    r = requests.get(f"{BASE}/stats", headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()["result"]["count"]


def list_images(page=1, per_page=100):
    r = requests.get(
        BASE, headers=headers(), params={"page": page, "per_page": per_page}, timeout=30
    )
    r.raise_for_status()
    return r.json()["result"]["images"]


def delete_image(image_id: str) -> bool:
    r = requests.delete(f"{BASE}/{image_id}", headers=headers(), timeout=30)
    if r.status_code == 200 and r.json().get("success"):
        return True
    print(f"  failed to delete {image_id}: {r.status_code} {r.text[:120]}")
    return False


def collect_tmp_images(max_count: int):
    """Oldest tmp*.png uploads first (paginate from end is not supported; scan pages)."""
    candidates = []
    page = 1
    while len(candidates) < max_count:
        batch = list_images(page=page)
        if not batch:
            break
        for img in batch:
            name = (img.get("filename") or "").lower()
            if name.startswith("tmp") and name.endswith(".png"):
                candidates.append(img)
        page += 1
        if page > 2000:  # safety cap
            break
    candidates.sort(key=lambda x: x.get("uploaded", ""))
    return candidates[:max_count]


def main():
    parser = argparse.ArgumentParser(description="Cloudflare Images quota helper")
    parser.add_argument("--delete", type=int, metavar="N", help="Delete up to N oldest tmp*.png images")
    parser.add_argument("--dry-run", action="store_true", help="With --delete, only print IDs")
    args = parser.parse_args()

    if not API_TOKEN:
        print("CLOUDFLARE_API_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    count = get_stats()
    current, allowed = count["current"], count["allowed"]
    over = max(0, current - allowed)
    print(f"Images: {current:,} / {allowed:,} allowed", end="")
    if over:
        print(f"  ({over:,} OVER LIMIT — uploads will return 403)")
    else:
        print()

    if args.delete:
        targets = collect_tmp_images(args.delete)
        print(f"Found {len(targets)} tmp*.png candidate(s) to delete")
        deleted = 0
        for img in targets:
            iid = img["id"]
            fname = img.get("filename", "?")
            if args.dry_run:
                print(f"  would delete {iid} ({fname})")
                deleted += 1
            elif delete_image(iid):
                deleted += 1
                if deleted % 50 == 0:
                    print(f"  deleted {deleted}...")
        print(f"{'Would delete' if args.dry_run else 'Deleted'}: {deleted}")
        if not args.dry_run and deleted:
            count = get_stats()
            print(f"After cleanup: {count['current']:,} / {count['allowed']:,}")


if __name__ == "__main__":
    main()
