#!/usr/bin/env bash
set -euo pipefail

version="${1:-v0.4.1}"
output="${2:-dist/tesla-control-linux-arm64}"
if [[ "$output" = /* ]]; then
  output_abs="$output"
else
  output_abs="$PWD/$output"
fi

tmpdir="$(mktemp -d)"
container_id=""
cleanup() {
  rm -rf "$tmpdir"
  if [[ -n "$container_id" ]]; then
    docker rm "$container_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

mkdir -p "$(dirname "$output")"
if command -v go >/dev/null && command -v git >/dev/null; then
  git clone --quiet --depth 1 --branch "$version" \
    https://github.com/teslamotors/vehicle-command.git "$tmpdir/vehicle-command"
  (
    cd "$tmpdir/vehicle-command"
    CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build \
      -trimpath -ldflags='-s -w' -o "$output_abs" ./cmd/tesla-control
  )
elif command -v docker >/dev/null; then
  image="tesla/vehicle-command:${version#v}"
  docker pull --platform linux/arm64 "$image"
  container_id="$(docker create --platform linux/arm64 "$image")"
  docker cp "$container_id:/usr/local/bin/tesla-control" "$output_abs"
else
  echo "Errore: servono Go+git oppure Docker" >&2
  exit 1
fi
chmod 0755 "$output"
echo "Creato $output da vehicle-command $version"
