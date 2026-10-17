#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  printf 'Docker is required. Install Docker Desktop or Docker Engine with Compose.\n' >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  printf 'Docker Compose is required. Install the Docker Compose plugin and try again.\n' >&2
  exit 1
fi

if [[ $# -gt 0 ]]; then
  dirs=("$@")
elif [[ -n "${PHOTO_LIBRARY:-}" ]]; then
  dirs=("$PHOTO_LIBRARY")
else
  mkdir -p -- "$script_dir/photos"
  dirs=("$script_dir/photos")
fi

resolved=()
for d in "${dirs[@]}"; do
  if [[ ! -d "$d" ]]; then
    printf 'Photo directory does not exist: %s\n' "$d" >&2
    exit 1
  fi
  resolved+=("$(cd -- "$d" && pwd)")
done

# The first directory is /photos; extra ones are /photos-2, /photos-3, ...
PHOTO_LIBRARY="${resolved[0]}"
export PHOTO_LIBRARY

cd "$script_dir"
extra=.photome-extra.yml
compose_args=()
if [[ ${#resolved[@]} -gt 1 ]]; then
  {
    printf 'services:\n  photome:\n    volumes:\n'
    for i in "${!resolved[@]}"; do
      [[ $i -eq 0 ]] && continue
      printf "      - type: bind\n        source: '%s'\n        target: /photos-%d\n        read_only: true\n" \
        "${resolved[$i]//\'/\'\'}" "$((i + 1))"
    done
  } > "$extra"
  compose_args=(-f docker-compose.yml -f "$extra")
else
  rm -f -- "$extra"
fi

exec docker compose "${compose_args[@]}" up --build
