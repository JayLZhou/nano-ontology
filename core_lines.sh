#!/usr/bin/env bash
# Count non-blank, non-comment lines in the nano_onto package.
# "core" is what every deployment runs; "optional" modules are only imported when used.
cd "$(dirname "$0")"
optional="http_server.py migrate.py tables.py webhooks.py"
core=0; extra=0
for f in nano_onto/*.py; do
  n=$(grep -cvE '^\s*(#|$)' "$f")
  if [[ " $optional " == *" $(basename "$f") "* ]]; then
    extra=$((extra + n)); tag="optional"
  else
    core=$((core + n)); tag="core"
  fi
  printf "  %-28s %5d  %s\n" "$f" "$n" "$tag"
done
echo "  ----------------------------------------------"
printf "  %-28s %5d\n" "core total" "$core"
printf "  %-28s %5d\n" "optional total" "$extra"
