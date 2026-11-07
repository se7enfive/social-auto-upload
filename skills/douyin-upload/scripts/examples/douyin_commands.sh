#!/usr/bin/env bash

set -euo pipefail

# account_name is user-defined. One account_name maps to one account file.
# Douyin always launches a visible local Google Chrome window; --headless is rejected.
account="account_a"
video="videos/demo.mp4"
thumbnail="videos/demo.png"

sau douyin login --account "$account"
sau douyin check --account "$account"

sau douyin upload-video \
  --account "$account" \
  --file "$video" \
  --title "Douyin video from bash" \
  --desc "Douyin video description from bash" \
  --tags "cli,video" \
  --thumbnail "$thumbnail"

sau douyin upload-note \
  --account "$account" \
  --images "videos/1.png" "videos/2.png" \
  --title "Douyin note title from bash" \
  --note "Douyin note from bash" \
  --tags "cli,note"
