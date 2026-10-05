#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
IMAGE_TAG=${1:-pi-claw-sandbox:local}
OUTPUT_DIR=${2:-${REPOSITORY_ROOT}/runs/sandbox-build}

mkdir -p "${OUTPUT_DIR}"
docker build --pull --file "${REPOSITORY_ROOT}/docker/sandbox/Dockerfile" \
    --tag "${IMAGE_TAG}" "${REPOSITORY_ROOT}"

IMAGE_ID=$(docker image inspect "${IMAGE_TAG}" --format '{{.Id}}')
case "${IMAGE_ID}" in
    sha256:????????????????????????????????????????????????????????????????) ;;
    *)
        echo "无法解析本地镜像 ID" >&2
        exit 1
        ;;
esac
printf '%s\n' "${IMAGE_ID}" >"${OUTPUT_DIR}/image-id.txt"

docker scout sbom "local://${IMAGE_TAG}" --format spdx \
    --output "${OUTPUT_DIR}/sbom.spdx.json"
printf '镜像：%s\nID：%s\nSBOM：%s\n' \
    "${IMAGE_TAG}" "${IMAGE_ID}" "${OUTPUT_DIR}/sbom.spdx.json"
