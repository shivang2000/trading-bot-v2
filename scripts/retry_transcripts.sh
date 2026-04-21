#!/bin/bash
# Retry script for rate-limited YouTube transcript downloads
# Run this after 30+ minutes cooldown from the main download script
# Usage: bash scripts/retry_transcripts.sh

set -euo pipefail

OUTDIR="data/research/2026-04-12"
DELAY=30  # 30 seconds between downloads to avoid 429

echo "═══════════════════════════════════════════════════════"
echo "YouTube Transcript Retry (with 30s delays)"
echo "═══════════════════════════════════════════════════════"

# Rate-limited individual videos
RETRY_VIDEOS=(
  B5vj-4qsc5A BlqOTQT3V2o CLs-_2OyZUc FBTb31IR4EE FswSkGar4Q8
  JzJpTCdkNN0 MkvMkHDkoGI P8pIiVwCEhU XhJg6h-8pDw erlvXg25cRc fVY2U8YhWHU
)

# Playlists not yet processed
PLAYLISTS=(
  "PL3NrHhGiBRFpd-BjOWvyoc6Xr2GQ2yKdb"
  "PL3wdfj84a2fk53i3JdpCB9WEbuXpA3Fbd"
  "PLPS2BLfwix-As7agVEnOTiyEdVRHY9pPIh"
  "PLPS2BLfwix-CfCbSW2gN8GrqG0T-SQCKf"
  "PLPS2BLfwix-DXkFc-03nrJj0EHJ85uTxC"
  "PLJYxY7BQeV6a_9zcK8o-s_jJ15EqFRlaz"
  "PLx5XjcRwuOcPXlEq-zMih74WJwY0KYddt"
  "PLEyYTzr_PR0LSOE6bNgR235Gue4bsVZ2j"
  "PL1gfeRckDpopSmceUW3GyBUokQLqeR6sD"
  "PL3wdfj84a2fl8TbxIpdjD7MhfSDRbdGzK"
  "PL3wdfj84a2flucy4Bif28-rrKIuYmWCkQ"
  "PLT24qNWhCCyyPeKNxrpoeV0ssD-VvIKnp"
)

SUCCESS=0
FAILED=0

# Retry individual videos
echo ""
echo "--- Retrying ${#RETRY_VIDEOS[@]} individual videos ---"
for vid in "${RETRY_VIDEOS[@]}"; do
  if ls "${OUTDIR}/${vid}"*.srt 1>/dev/null 2>&1; then
    echo "  $vid: already done, skip"
    continue
  fi
  echo -n "  $vid: "
  yt-dlp --skip-download --write-auto-sub --sub-lang en --sub-format vtt --convert-subs srt \
    -o "${OUTDIR}/${vid}" "https://www.youtube.com/watch?v=${vid}" 2>&1 | tail -1

  if ls "${OUTDIR}/${vid}"*.srt 1>/dev/null 2>&1; then
    sed '/^[0-9]*$/d; /-->/d; /^$/d; s/<[^>]*>//g' "${OUTDIR}/${vid}.en.srt" | awk '!seen[$0]++' > "${OUTDIR}/${vid}_text.txt"
    echo "    -> OK"
    SUCCESS=$((SUCCESS + 1))
  else
    FAILED=$((FAILED + 1))
  fi
  sleep "$DELAY"
done

# Process playlists
echo ""
echo "--- Processing ${#PLAYLISTS[@]} playlists ---"
for pl_id in "${PLAYLISTS[@]}"; do
  echo ""
  echo "  Playlist: $pl_id"
  pl_urls=$(timeout 60 yt-dlp --flat-playlist --print url \
    "https://www.youtube.com/playlist?list=${pl_id}" 2>/dev/null | head -20)

  if [ -z "$pl_urls" ]; then
    echo "    FAIL: could not expand playlist"
    FAILED=$((FAILED + 1))
    sleep "$DELAY"
    continue
  fi

  pl_count=$(echo "$pl_urls" | wc -l | tr -d ' ')
  echo "    Found $pl_count videos"

  while IFS= read -r pl_url; do
    vid=$(echo "$pl_url" | sed -n 's/.*[?&]v=\([A-Za-z0-9_-]*\).*/\1/p' | head -1)
    if [ -z "$vid" ]; then
      vid=$(echo "$pl_url" | sed -n 's|.*/\([A-Za-z0-9_-]*\)$|\1|p' | head -1)
    fi
    if [ -z "$vid" ]; then continue; fi

    if ls "${OUTDIR}/${vid}"*.srt 1>/dev/null 2>&1; then
      echo -n "."  # already done
      continue
    fi

    echo -n "  $vid: "
    # Get title
    yt-dlp --skip-download --print title "$pl_url" > "${OUTDIR}/${vid}_info.txt" 2>/dev/null || true

    yt-dlp --skip-download --write-auto-sub --sub-lang en --sub-format vtt --convert-subs srt \
      -o "${OUTDIR}/${vid}" "$pl_url" 2>&1 | tail -1

    if ls "${OUTDIR}/${vid}"*.srt 1>/dev/null 2>&1; then
      sed '/^[0-9]*$/d; /-->/d; /^$/d; s/<[^>]*>//g' "${OUTDIR}/${vid}.en.srt" | awk '!seen[$0]++' > "${OUTDIR}/${vid}_text.txt"
      echo " OK"
      SUCCESS=$((SUCCESS + 1))
    else
      FAILED=$((FAILED + 1))
    fi
    sleep "$DELAY"
  done <<< "$pl_urls"
done

echo ""
echo "═══════════════════════════════════════════════════════"
echo "COMPLETE"
echo "  New downloads: $SUCCESS"
echo "  Failed: $FAILED"
echo "  Total SRT files: $(ls ${OUTDIR}/*.srt 2>/dev/null | wc -l | tr -d ' ')"
echo "  Total text files: $(ls ${OUTDIR}/*_text.txt 2>/dev/null | wc -l | tr -d ' ')"
echo "═══════════════════════════════════════════════════════"
