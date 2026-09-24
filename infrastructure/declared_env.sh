#!/bin/bash
# declared_env.sh — apply infrastructure/declared-flags.env to the box's env files
# (metron-ops-I340, metron-ops-I281). Called by deploy-on-merge.sh on every deploy.
#
# Usage: declared_env.sh DECLARED_FILE TARGET_ENV [OTHER_ENV ...]
#
#   TARGET_ENV   gets the managed "declared-flags" block (every Metron unit reads it).
#   OTHER_ENV    is only scanned: hand-set lines for declared or retired names are removed.
#
# For every file, a line setting a declared NAME outside the managed block is removed and
# logged with its value (declared values are non-secret by contract), and a line setting a
# retired NAME is removed and logged WITHOUT its value. The managed block is then rewritten
# at the end of TARGET_ENV. Idempotent: a second run leaves every file byte-identical.
#
# Exits non-zero on a malformed declaration, so a bad edit to the declared file fails the
# deploy instead of silently dropping a compliance flag.
set -euo pipefail

declared="${1:?usage: declared_env.sh DECLARED_FILE TARGET_ENV [OTHER_ENV ...]}"
target="${2:?usage: declared_env.sh DECLARED_FILE TARGET_ENV [OTHER_ENV ...]}"
shift 2
others=("$@")

BEGIN="# >>> declared-flags (managed by deploy-on-merge.sh from infrastructure/declared-flags.env — do not edit) >>>"
END="# <<< declared-flags <<<"
NAME_RE='^[A-Z][A-Z0-9_]*$'

set_names=()
set_lines=()
retired=()
lineno=0
while IFS= read -r raw || [ -n "$raw" ]; do
  lineno=$((lineno + 1))
  line="${raw#"${raw%%[![:space:]]*}"}"   # strip leading whitespace
  line="${line%"${line##*[![:space:]]}"}"  # strip trailing whitespace
  case "$line" in
    ""|"#"*) continue ;;
    -*)
      name="${line#-}"
      [[ "$name" =~ $NAME_RE ]] || { echo "declared_env: ${declared}:${lineno}: bad retired name '${name}'"; exit 2; }
      retired+=("$name")
      ;;
    *=*)
      name="${line%%=*}"
      [[ "$name" =~ $NAME_RE ]] || { echo "declared_env: ${declared}:${lineno}: bad name '${name}'"; exit 2; }
      set_names+=("$name")
      set_lines+=("$line")
      ;;
    *)
      echo "declared_env: ${declared}:${lineno}: expected NAME=value or -NAME"
      exit 2
      ;;
  esac
done < "$declared"

if [ "${#set_names[@]}" -eq 0 ] && [ "${#retired[@]}" -eq 0 ]; then
  echo "declared_env: ${declared} declares nothing — refusing to apply an empty declaration"
  exit 2
fi

# Remove unmanaged lines for declared/retired names from one file, logging each removal.
# The managed block itself is stripped from every file too (it only belongs in TARGET, and
# is re-appended there below).
scrub() {
  local file="$1" tmp name found
  [ -e "$file" ] || { echo "  ${file}: absent, nothing to scan"; return 0; }
  tmp=$(mktemp)
  # Drop any previous managed block wholesale.
  awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' "$file" > "$tmp"
  for name in ${set_names[@]+"${set_names[@]}"}; do
    found=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$tmp" || true)
    if [ -n "$found" ]; then
      while IFS= read -r l; do
        echo "  ${file}: removed hand-set line '${l}' (declared value is managed)"
      done <<< "$found"
      grep -vE "^[[:space:]]*(export[[:space:]]+)?${name}=" "$tmp" > "${tmp}.n" || true
      mv "${tmp}.n" "$tmp"
    fi
  done
  for name in ${retired[@]+"${retired[@]}"}; do
    if grep -qE "^[[:space:]]*(export[[:space:]]+)?${name}=" "$tmp"; then
      grep -vE "^[[:space:]]*(export[[:space:]]+)?${name}=" "$tmp" > "${tmp}.n" || true
      mv "${tmp}.n" "$tmp"
      echo "  ${file}: removed retired ${name} (value not logged)"
    else
      echo "  ${file}: retired ${name} absent"
    fi
  done
  # Rewrite in place (cat >, not mv) so the file keeps its owner and mode.
  if ! cmp -s "$tmp" "$file"; then
    cat "$tmp" > "$file"
  fi
  rm -f "$tmp"
}

touch "$target"
scrub "$target"
for f in ${others[@]+"${others[@]}"}; do
  scrub "$f"
done

if [ "${#set_lines[@]}" -gt 0 ]; then
  { echo "$BEGIN"
    printf '%s\n' "${set_lines[@]}"
    echo "$END"; } >> "$target"
  for l in ${set_lines[@]+"${set_lines[@]}"}; do
    echo "  ${target}: declared ${l}"
  done
fi
echo "  declared_env: ${#set_lines[@]} declared, ${#retired[@]} retired, applied to ${target}"
