#!/usr/bin/env bash
# Starts PhotoMe with your photo folders mounted read-only inside the container.
#
# Usage: start.sh [--remap] [--sync-into NAME] [--writable NAME] [--rename-mount OLD=NEW]
#                 [[name=]directory ...]
#
#   start.sh photos=/data/pictures archive=/mnt/archive   each folder gets the mount name you give
#   start.sh /data/pictures /mnt/archive                   no names: /photos, /photos-2, ... in order
#
# The mount name is what the database stores in every file path ("/archive/old/a.jpg"), so a
# folder must keep the same name for as long as its records exist. The script remembers the
# name -> folder pairs in .photome-mounts and stops if a name would point at another folder
# (pass --remap if you really moved a folder).
#
# Every mount is read-only. To let PhotoMe's Sync panel copy files INTO a mount:
#   --sync-into NAME   (recommended) the mount itself stays read-only; only the folders
#                      <mount>/_from-<other mount> become writable. PhotoMe can add files there and
#                      nowhere else in that mount.
#   --writable NAME    the whole mount becomes writable (use only if --sync-into does not work).
# PhotoMe never overwrites or deletes anything in either case.
#
# --rename-mount OLD=NEW renames a mount point that already has records: the database paths
# /OLD/... become /NEW/... (a backup of the database is made first), then NEW is mounted as usual.
# Give the folder its new name too:   start.sh --rename-mount photos-2=archive archive=/mnt/archive
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

fail() { printf '%s\n' "$*" >&2; exit 1; }

remap=0
writable=()
sync_into=()
rename_spec=""
args=()
add_names() {  # add_names ARRAY_NAME "a,b,/c"
  local target="$1" item
  local listed
  IFS=, read -r -a listed <<< "$2"
  for item in "${listed[@]}"; do eval "$target+=(\"\${item#/}\")"; done
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remap) remap=1 ;;
    --writable|--sync-into)
      flag="$1"
      shift
      [[ $# -gt 0 ]] || fail "$flag needs the name of a mount (for example: $flag archive)."
      if [[ "$flag" == "--writable" ]]; then add_names writable "$1"; else add_names sync_into "$1"; fi ;;
    --writable=*) add_names writable "${1#--writable=}" ;;
    --sync-into=*) add_names sync_into "${1#--sync-into=}" ;;
    --rename-mount)
      shift
      [[ $# -gt 0 ]] || fail '--rename-mount needs OLD=NEW (for example: --rename-mount photos-2=archive).'
      rename_spec="$1" ;;
    --rename-mount=*) rename_spec="${1#--rename-mount=}" ;;
    *) args+=("$1") ;;
  esac
  shift
done

if ! command -v docker >/dev/null 2>&1; then
  fail 'Docker is required. Install Docker Desktop or Docker Engine with Compose.'
fi
if ! docker compose version >/dev/null 2>&1; then
  fail 'Docker Compose is required. Install the Docker Compose plugin and try again.'
fi

# Names the container already uses for itself; a photo mount there would break the app.
reserved=" bin boot data dev etc home lib lib32 lib64 libx32 media mnt opt proc root run sbin srv sys tmp usr var workspace "

has_name() {  # has_name NAME: is NAME already taken?
  local n
  for n in ${names[@]+"${names[@]}"}; do
    if [[ "$n" == "$1" ]]; then return 0; fi
  done
  return 1
}

names=()
dirs=()
plain=()
for arg in ${args[@]+"${args[@]}"}; do
  if [[ "$arg" =~ ^/?([A-Za-z0-9][A-Za-z0-9._-]{0,39})=(.+)$ ]]; then
    name="${BASH_REMATCH[1]}"
    dir="${BASH_REMATCH[2]}"
    if [[ "$reserved" == *" $name "* ]]; then fail "The mount name '$name' is reserved by the container. Choose another."; fi
    if has_name "$name"; then fail "The mount name '$name' is used twice."; fi
    names+=("$name")
    dirs+=("$dir")
  else
    plain+=("$arg")
  fi
done

# Folders given without a name take the first unused default name: photos, photos-2, photos-3, ...
next=1
for dir in ${plain[@]+"${plain[@]}"}; do
  while :; do
    candidate=photos
    if [[ $next -gt 1 ]]; then candidate="photos-$next"; fi
    next=$((next + 1))
    if ! has_name "$candidate"; then break; fi
  done
  names+=("$candidate")
  dirs+=("$dir")
done

if [[ ${#names[@]} -eq 0 ]]; then
  if [[ -n "${PHOTO_LIBRARY:-}" ]]; then
    names=(photos)
    dirs=("$PHOTO_LIBRARY")
  else
    mkdir -p -- "$script_dir/photos"
    names=(photos)
    dirs=("$script_dir/photos")
  fi
fi

resolved=()
for i in "${!dirs[@]}"; do
  [[ -d "${dirs[$i]}" ]] || fail "Photo directory does not exist: ${dirs[$i]}"
  resolved+=("$(cd -- "${dirs[$i]}" && pwd)")
done

folder_of() {  # folder_of NAME: the folder on this computer behind mount NAME
  local i
  for i in "${!names[@]}"; do
    if [[ "${names[$i]}" == "$1" ]]; then printf '%s' "${resolved[$i]}"; return 0; fi
  done
  return 1
}
in_list() {  # in_list NAME LISTNAME
  local item
  eval "for item in \${$2[@]+\"\${$2[@]}\"}; do if [[ \"\$item\" == \"\$1\" ]]; then return 0; fi; done"
  return 1
}

for item in ${writable[@]+"${writable[@]}"} ${sync_into[@]+"${sync_into[@]}"}; do
  has_name "$item" || fail "'$item' is not one of the mounts (the mounts are: ${names[*]})."
done
for item in ${sync_into[@]+"${sync_into[@]}"}; do
  if in_list "$item" writable; then fail "'$item' is named in both --sync-into and --writable; choose one."; fi
done

rename_old=""
rename_new=""
if [[ -n "$rename_spec" ]]; then
  [[ "$rename_spec" == *=* ]] || fail "--rename-mount needs OLD=NEW, for example --rename-mount photos-2=archive."
  rename_old="${rename_spec%%=*}"; rename_old="${rename_old#/}"
  rename_new="${rename_spec#*=}"; rename_new="${rename_new#/}"
  [[ "$rename_old" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$ ]] || fail "--rename-mount: '$rename_old' is not a valid mount name."
  [[ "$rename_old" != "$rename_new" ]] || fail "--rename-mount: the old and the new name are the same."
  has_name "$rename_new" || fail "--rename-mount: '$rename_new' must be one of the mounts you pass (name the folder $rename_new=...), so the renamed mount is mounted under its new name."
  if has_name "$rename_old"; then fail "--rename-mount: '$rename_old' is still passed as a mount; remove it, because it is being renamed to '$rename_new'."; fi
fi

# ---- keep names and folders consistent between runs ------------------------------------
state="$script_dir/.photome-mounts"
remembered() {  # remembered NAME: the folder NAME pointed to last time, if any
  if [[ -f "$state" ]]; then
    awk -F= -v n="$1" '$1 == n { sub(/^[^=]*=/, ""); print; exit }' "$state"
  fi
}

conflicts=""
for i in "${!names[@]}"; do
  before="$(remembered "${names[$i]}")"
  if [[ -n "$before" && "$before" != "${resolved[$i]}" ]]; then
    conflicts+="  /${names[$i]} was $before and would now be ${resolved[$i]}"$'\n'
  fi
done
if [[ -n "$rename_old" ]]; then
  before="$(remembered "$rename_old")"
  if [[ -n "$before" && "$before" != "$(folder_of "$rename_new")" ]]; then
    conflicts+="  /$rename_old was $before and /$rename_new would be $(folder_of "$rename_new")"$'\n'
  fi
fi
if [[ -n "$conflicts" && $remap -eq 0 ]]; then
  {
    printf 'A mount name would point at a different folder than last time:\n%s\n' "$conflicts"
    printf 'The records already in the database under that name belong to the old folder, so this\n'
    printf 'would make their paths wrong. Give the folders the names they had before, or, if you\n'
    printf 'really moved the folder, run again with --remap.\n'
  } >&2
  exit 1
fi

# ---- the environment the app (and docker compose) needs ------------------------------------
# /photos always comes from PHOTO_LIBRARY (docker-compose.yml); the others are added below.
library=""
host_paths=""
for i in "${!names[@]}"; do
  if [[ "${names[$i]}" == photos ]]; then library="${resolved[$i]}"; fi
  host_paths+="${host_paths:+|}/${names[$i]}=${resolved[$i]}"
done
placeholder=0
if [[ -z "$library" ]]; then
  mkdir -p -- "$script_dir/photos"
  library="$script_dir/photos"
  placeholder=1
fi
PHOTO_LIBRARY="$library"
export PHOTO_LIBRARY
export PHOTOME_HOST_PATHS="$host_paths"
cd "$script_dir"

# ---- rename a mount point in the database (before anything is started) -------------------------
if [[ -n "$rename_old" ]]; then
  printf 'Renaming mount /%s to /%s in the database (a backup is made first)...\n' "$rename_old" "$rename_new"
  docker compose stop >/dev/null 2>&1 || true   # nothing may be writing to the database meanwhile
  if ! docker compose run --rm --no-deps -T photome python app.py --rename-mount "$rename_old" "$rename_new"; then
    fail "The rename failed, so nothing was changed and the app was not started. See the message above."
  fi
fi

# ---- remember the current pairs, and keep the old ones that are not mounted this time ----------
unmounted=""
new_state="$(mktemp)"
for i in "${!names[@]}"; do printf '%s=%s\n' "${names[$i]}" "${resolved[$i]}"; done > "$new_state"
if [[ -f "$state" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    old="${line%%=*}"
    if [[ -n "$old" && "$old" != "$rename_old" ]] && ! has_name "$old"; then
      printf '%s\n' "$line" >> "$new_state"
      unmounted+="  /$old  (${line#*=})"$'\n'
    fi
  done < "$state"
fi
mv -- "$new_state" "$state"

if [[ $placeholder -eq 1 ]]; then printf "Note: no mount is named 'photos', so /photos is an empty folder.\n"; fi
printf 'Mount points (the name is what the database stores):\n'
for i in "${!names[@]}"; do
  mode=""
  if in_list "${names[$i]}" writable; then mode="   [READ-WRITE]"; fi
  if in_list "${names[$i]}" sync_into; then mode="   [read-only; add-only _from-* folders]"; fi
  printf '  /%-12s <- %s%s\n' "${names[$i]}" "${resolved[$i]}" "$mode"
done
for item in ${writable[@]+"${writable[@]}"}; do
  printf 'WRITABLE: /%s. PhotoMe may add files anywhere in it, but only inside _from-<source> folders; it never overwrites or deletes.\n' "$item"
done
for item in ${sync_into[@]+"${sync_into[@]}"}; do
  printf 'SYNC INTO: /%s stays read-only; only its _from-<source> folders are writable. PhotoMe never overwrites or deletes.\n' "$item"
done
if [[ -n "$unmounted" ]]; then
  printf 'Mounted last time but not now (their records stay in the database):\n%s' "$unmounted"
fi

# ---- the compose override: extra mounts, and the writable folders --------------------------------
extra=.photome-extra.yml
compose_args=()
needs_extra=0
for name in "${names[@]}"; do
  if [[ "$name" != photos ]] || in_list "$name" writable; then needs_extra=1; fi
done
if [[ ${#sync_into[@]} -gt 0 ]]; then needs_extra=1; fi
if [[ $needs_extra -eq 1 ]]; then
  # the folders that receive copies must exist on the computer before they can be mounted
  for target in ${sync_into[@]+"${sync_into[@]}"}; do
    for other in "${names[@]}"; do
      if [[ "$other" == "$target" ]]; then continue; fi
      mkdir -p -- "$(folder_of "$target")/_from-$other" || fail "Could not create $(folder_of "$target")/_from-$other"
    done
  done
  {
    printf 'services:\n  photome:\n    volumes:\n'
    for i in "${!names[@]}"; do
      # /photos comes from docker-compose.yml (read-only) unless it was made writable here
      if [[ "${names[$i]}" == photos ]] && ! in_list photos writable; then continue; fi
      read_only=true
      if in_list "${names[$i]}" writable; then read_only=false; fi
      printf "      - type: bind\n        source: '%s'\n        target: /%s\n        read_only: %s\n" \
        "${resolved[$i]//\'/\'\'}" "${names[$i]}" "$read_only"
    done
    for target in ${sync_into[@]+"${sync_into[@]}"}; do
      for other in "${names[@]}"; do
        if [[ "$other" == "$target" ]]; then continue; fi
        folder="$(folder_of "$target")/_from-$other"
        printf "      - type: bind\n        source: '%s'\n        target: /%s/_from-%s\n        read_only: false\n" \
          "${folder//\'/\'\'}" "$target" "$other"
      done
    done
  } > "$extra"
  compose_args=(-f docker-compose.yml -f "$extra")
else
  rm -f -- "$extra"
fi

exec docker compose ${compose_args[@]+"${compose_args[@]}"} up --build
