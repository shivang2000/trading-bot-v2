#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# YouTube Transcript Downloader
# Downloads subtitles/transcripts for a list of videos and playlists
# Saves to data/research/YYYY-MM-DD/ with clean text extraction
#
# Usage: bash scripts/download_transcripts.sh
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

DATE=$(date +%Y-%m-%d)
OUTDIR="data/research/${DATE}"
DELAY=5  # seconds between downloads to avoid rate limiting

mkdir -p "$OUTDIR"
echo "═══════════════════════════════════════════════════════"
echo "YouTube Transcript Downloader"
echo "Output: $OUTDIR"
echo "═══════════════════════════════════════════════════════"
echo ""

# All URLs (deduplicated, cleaned of &t= params)
URLS=(
  # Individual videos
  "https://www.youtube.com/watch?v=ADnslyKOwFE"
  "https://www.youtube.com/watch?v=DyS79Eb92Ug"
  "https://www.youtube.com/watch?v=cHLd4HJtdIY"
  "https://www.youtube.com/watch?v=T8rBjP1SLko"
  "https://www.youtube.com/watch?v=s_3dkLvm0lM"
  "https://www.youtube.com/watch?v=QylVcZfJrK4"
  "https://www.youtube.com/watch?v=XxhON6lgs_U"
  "https://www.youtube.com/watch?v=yFPrP6nJRYQ"
  "https://www.youtube.com/watch?v=FswSkGar4Q8"
  "https://www.youtube.com/watch?v=cTecm-uk8FA"
  "https://www.youtube.com/watch?v=ZAsTgWWVv-A"
  "https://www.youtube.com/watch?v=Pz8f0wWW12M"
  "https://www.youtube.com/watch?v=G9itT05pGvc"
  "https://www.youtube.com/watch?v=QU65j9eQobc"
  "https://www.youtube.com/watch?v=kJVaVdmOTrA"
  "https://www.youtube.com/watch?v=h-Z7CEqBO3s"
  "https://www.youtube.com/watch?v=HYCdwPCLc8U"
  "https://www.youtube.com/watch?v=sclIAK3x2dw"
  "https://www.youtube.com/watch?v=I_msgqMsplg"
  "https://www.youtube.com/watch?v=iCU1EJMrEZ0"
  "https://www.youtube.com/watch?v=kFyD3H6I1I8"
  "https://www.youtube.com/watch?v=5J01qKDAziM"
  "https://www.youtube.com/watch?v=pD1vAUMbSjw"
  "https://www.youtube.com/watch?v=hJyYxKtys04"
  "https://www.youtube.com/watch?v=phphBbRex1M"
  "https://www.youtube.com/watch?v=EEpfmQo2Ars"
  "https://www.youtube.com/watch?v=ig6Z2Gbk_LE"
  "https://www.youtube.com/watch?v=j5v9OlQ_gsY"
  "https://www.youtube.com/watch?v=-QkFsIBmqSA"
  "https://www.youtube.com/watch?v=IZpcVFFGW1w"
  "https://www.youtube.com/watch?v=deymRD3kSD0"
  "https://www.youtube.com/watch?v=n5xZri8VxWs"
  "https://www.youtube.com/watch?v=NWHFPAXUt8Q"
  "https://www.youtube.com/watch?v=R9yZSDpdCGE"
  "https://www.youtube.com/watch?v=yHAC0xtBR2Q"
  "https://www.youtube.com/watch?v=62qSFeXa9z0"
  "https://www.youtube.com/watch?v=zT2hSb9IEZw"
  "https://www.youtube.com/watch?v=Y8efWZ2M1y8"
  "https://www.youtube.com/watch?v=1JiU2KJG3J8"
  "https://www.youtube.com/watch?v=8KBGqFwqWqc"
  "https://www.youtube.com/watch?v=Bb18a-K2Ygw"
  "https://www.youtube.com/watch?v=6RDhkPPDmFI"
  "https://www.youtube.com/watch?v=CLs-_2OyZUc"
  "https://www.youtube.com/watch?v=XhJg6h-8pDw"
  "https://www.youtube.com/watch?v=B5vj-4qsc5A"
  "https://www.youtube.com/watch?v=KVEgbwyXVoI"
  "https://www.youtube.com/watch?v=FBTb31IR4EE"
  "https://www.youtube.com/watch?v=BlqOTQT3V2o"
  "https://www.youtube.com/watch?v=JzJpTCdkNN0"
  "https://www.youtube.com/watch?v=P8pIiVwCEhU"
  "https://www.youtube.com/watch?v=mruUK7Xl3k8"
  "https://www.youtube.com/watch?v=fVY2U8YhWHU"
  "https://www.youtube.com/watch?v=MkvMkHDkoGI"
  "https://www.youtube.com/watch?v=erlvXg25cRc"
  # Channel — skip (too many videos, use specific video URLs instead)
  # "https://www.youtube.com/@TradewithPat"
  # Playlists
  "https://www.youtube.com/playlist?list=PL3NrHhGiBRFpd-BjOWvyoc6Xr2GQ2yKdb"
  "https://www.youtube.com/watch?v=b80QhvUHHoU&list=PL3wdfj84a2fk53i3JdpCB9WEbuXpA3Fbd"
  "https://www.youtube.com/watch?v=EtC0-585BAQ&list=PLPS2BLfwix-As7agVEnOTiyEdVRHY9pPIh"
  "https://www.youtube.com/watch?v=H4yJoCUa6IM&list=PLPS2BLfwix-CfCbSW2gN8GrqG0T-SQCKf"
  "https://www.youtube.com/watch?v=Bb18a-K2Ygw&list=PLPS2BLfwix-DXkFc-03nrJj0EHJ85uTxC"
  "https://www.youtube.com/watch?v=kP9lWHllOC0&list=PLJYxY7BQeV6a_9zcK8o-s_jJ15EqFRlaz"
  "https://www.youtube.com/watch?v=VDX4z4FOmdo&list=PLx5XjcRwuOcPXlEq-zMih74WJwY0KYddt"
  "https://www.youtube.com/watch?v=zHOG2T9Mr_M&list=PLEyYTzr_PR0LSOE6bNgR235Gue4bsVZ2j"
  "https://www.youtube.com/playlist?list=PL1gfeRckDpopSmceUW3GyBUokQLqeR6sD"
  "https://www.youtube.com/playlist?list=PL3wdfj84a2fl8TbxIpdjD7MhfSDRbdGzK"
  "https://www.youtube.com/playlist?list=PL3wdfj84a2flucy4Bif28-rrKIuYmWCkQ"
  "https://www.youtube.com/playlist?list=PLT24qNWhCCyyPeKNxrpoeV0ssD-VvIKnp"
)

TOTAL=${#URLS[@]}
SUCCESS=0
FAILED=0
SKIPPED=0

download_single() {
  local url="$1"
  local idx="$2"

  echo "[$idx/$TOTAL] Processing: $url"

  # Get video ID for filename (or playlist ID)
  local vid_id
  vid_id=$(echo "$url" | sed -n 's/.*[?&]v=\([A-Za-z0-9_-]*\).*/\1/p' | head -1)

  if [ -z "$vid_id" ]; then
    # Playlist or channel — use yt-dlp to handle
    vid_id="playlist_$(echo "$url" | sed -n 's/.*list=\([A-Za-z0-9_-]*\).*/\1/p' | head -1)"
    if [ "$vid_id" = "playlist_" ]; then
      vid_id="channel_$(echo "$url" | sed -n 's/.*@\([A-Za-z0-9_-]*\).*/\1/p' | head -1)"
    fi
  fi

  # Skip if already downloaded
  if ls "${OUTDIR}/${vid_id}"*.srt 1>/dev/null 2>&1; then
    echo "  SKIP: Already downloaded"
    SKIPPED=$((SKIPPED + 1))
    return 0
  fi

  # Download title + description
  yt-dlp --skip-download --print title "$url" > "${OUTDIR}/${vid_id}_info.txt" 2>/dev/null || true
  yt-dlp --skip-download --print description "$url" >> "${OUTDIR}/${vid_id}_info.txt" 2>/dev/null || true

  # Download auto-generated subtitles, convert to SRT
  yt-dlp --skip-download \
    --write-auto-sub --sub-lang en --sub-format vtt --convert-subs srt \
    -o "${OUTDIR}/%(id)s" "$url" 2>/dev/null || true

  # Check if any SRT was actually created for this video
  if ls "${OUTDIR}/${vid_id}"*.srt 1>/dev/null 2>&1; then
    # Extract clean text from SRT files
    for srt in "${OUTDIR}/${vid_id}"*.srt; do
      if [ -f "$srt" ]; then
        local base
        base=$(basename "$srt" .en.srt)
        sed '/^[0-9]*$/d; /-->/d; /^$/d; s/<[^>]*>//g' "$srt" | awk '!seen[$0]++' > "${OUTDIR}/${base}_text.txt"
      fi
    done
    echo "  OK"
    SUCCESS=$((SUCCESS + 1))
  else
    echo "  FAIL: No subtitles available"
    FAILED=$((FAILED + 1))
  fi
}

# Process each URL sequentially with delay
idx=0
for url in "${URLS[@]}"; do
  idx=$((idx + 1))

  # Handle playlists — download each video in playlist
  if echo "$url" | grep -qE 'list=|playlist\?list=|/@'; then
    echo ""
    echo "[$idx/$TOTAL] PLAYLIST/CHANNEL: $url"
    echo "  Expanding playlist..."

    # Get all video URLs from playlist (timeout after 60s)
    playlist_urls=$(timeout 60 yt-dlp --flat-playlist --print url "$url" 2>/dev/null | head -30)

    if [ -z "$playlist_urls" ]; then
      echo "  FAIL: Could not expand playlist"
      FAILED=$((FAILED + 1))
    else
      pl_count=$(echo "$playlist_urls" | wc -l | tr -d ' ')
      echo "  Found $pl_count videos in playlist"

      pl_idx=0
      while IFS= read -r pl_url; do
        pl_idx=$((pl_idx + 1))
        echo "  [PL $pl_idx/$pl_count]"
        download_single "$pl_url" "$idx"
        sleep "$DELAY"
      done <<< "$playlist_urls"
    fi
  else
    download_single "$url" "$idx"
  fi

  sleep "$DELAY"
done

echo ""
echo "═══════════════════════════════════════════════════════"
echo "COMPLETE"
echo "  Output:  $OUTDIR"
echo "  Success: $SUCCESS"
echo "  Failed:  $FAILED"
echo "  Skipped: $SKIPPED"
echo "  Total files: $(ls ${OUTDIR}/*.srt 2>/dev/null | wc -l | tr -d ' ') SRT + $(ls ${OUTDIR}/*_text.txt 2>/dev/null | wc -l | tr -d ' ') text"
echo "═══════════════════════════════════════════════════════"
