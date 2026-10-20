#!/usr/bin/env bash
set -euo pipefail
: "${OMNI_REPO:?}"
: "${OMNI_COMMIT:?}"
: "${TEST_IMAGE:?}"
: "${VLLM_ASCEND_IMAGE:?}"
: "${VLLM_ASCEND_VERSION:?}"
: "${BUILDKITD_ADDR:?}"
[[ "$OMNI_COMMIT" =~ ^[0-9a-f]{40}$ ]]
# Match the upstream BuildKit client release; keep it local to this job.
if ! command -v buildctl >/dev/null 2>&1; then
  client_dir=$(mktemp -d)
  case "$(uname -m)" in
    aarch64|arm64) client_arch=arm64 ;;
    x86_64) client_arch=amd64 ;;
    *) echo "Unsupported BuildKit client architecture" >&2; exit 1 ;;
  esac
  curl -fsSL "https://github.com/moby/buildkit/releases/download/v0.29.0/buildkit-v0.29.0.linux-${client_arch}.tar.gz" -o "$client_dir/buildkit.tar.gz"
  tar -xzf "$client_dir/buildkit.tar.gz" -C "$client_dir" bin/buildctl
  export PATH="$client_dir/bin:$PATH"
fi
export DOCKER_CONFIG=${DOCKER_CONFIG:-/home/user/.docker}
for credential in ca.pem cert.pem key.pem config.json; do
  test -r "$DOCKER_CONFIG/$credential"
done
checkout=$(mktemp -d)
trap 'rm -rf "$checkout"; if [[ -n ${client_dir:-} ]]; then rm -rf "$client_dir"; fi' EXIT
git -C "$checkout" init
git -C "$checkout" remote add origin "$OMNI_REPO"
git -C "$checkout" fetch --depth 1 origin "$OMNI_COMMIT"
git -C "$checkout" checkout --detach FETCH_HEAD
test "$(git -C "$checkout" rev-parse HEAD)" = "$OMNI_COMMIT"
cd "$checkout"
buildctl \
  --addr="$BUILDKITD_ADDR" \
  --tlscacert="$DOCKER_CONFIG/ca.pem" \
  --tlscert="$DOCKER_CONFIG/cert.pem" \
  --tlskey="$DOCKER_CONFIG/key.pem" \
  build \
  --frontend dockerfile.v0 \
  --local context=. \
  --local dockerfile=./docker \
  --opt filename=Dockerfile.npu.ci \
  --opt "build-arg:VLLM_ASCEND_IMAGE=$VLLM_ASCEND_IMAGE" \
  --opt "build-arg:VLLM_ASCEND_VERSION=$VLLM_ASCEND_VERSION" \
  --opt build-arg:VLLM_ASCEND_DEVICE_SUFFIX=-a3 \
  --secret "id=dockerconfig,src=$DOCKER_CONFIG/config.json" \
  --output "type=image,name=$TEST_IMAGE,push=true" \
  --progress=plain
