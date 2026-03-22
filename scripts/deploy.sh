#!/usr/bin/env bash
# Build and deploy to EC2.
# Usage: ./scripts/deploy.sh <ec2-host> [docker-hub-user]
#
# Prerequisites:
#   - Docker buildx for multi-arch builds
#   - SSH access to EC2 host
#   - Docker Hub login (for pushing images)
set -euo pipefail

EC2_HOST="${1:?Usage: deploy.sh <ec2-host> [docker-hub-user]}"
DOCKER_USER="${2:-shivang}"
IMAGE="${DOCKER_USER}/trading-bot-v2"
TAG="latest"

cd "$(dirname "$0")/.."

echo "Building multi-arch image..."
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --tag "${IMAGE}:${TAG}" \
    --push \
    .

echo "Deploying to ${EC2_HOST}..."
ssh "${EC2_HOST}" << 'REMOTE'
    cd ~/trading-bot-v2 || exit 1
    docker compose -f docker-compose.ec2.yml pull
    docker compose -f docker-compose.ec2.yml up -d
    echo "Deployed. Checking status..."
    docker compose -f docker-compose.ec2.yml ps
REMOTE

echo "Deploy complete."
