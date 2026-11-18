#!/usr/bin/env bash
# site-check.sh: request every URL the website uses and require each one to answer correctly.
#
# Usage: site-check.sh [--site <site base>] [--full] [--list] [<API base> [<ranges>]]
#   <API base>  default https://atlas.shifter.io
#   <ranges>    space-separated, default "24h 7d 30d"
#   --site URL  also request the website's pages on URL (for example https://shifter.io)
#   --full      list every URL in the table, not only the failures and the redirects
#   --list      print the URL list (after reading /v1/networks) and request nothing else
#
# The URLs come from site-check.urls next to this script; the networks from /v1/networks.
# Every URL is requested with curl, following at most 5 redirects. An API URL passes with a
# final 200, valid JSON, at most one redirect hop and no redirect to itself; a page passes the
# same way, with the text "temporarily unavailable" absent instead of the JSON rule. Prints a
# summary line and a Markdown table to paste into a release request. Exit 0 only when every URL
# passes, 1 on any failure, 2 on a usage error. The release rule: docs/release-checklist.md.
set -u

usage() { sed -n '4,9p' "$0" >&2; exit 2; }

site="" full=0 list=0
while [ $# -gt 0 ]; do
  case $1 in
    --site) [ $# -ge 2 ] || usage; site=${2%/}; shift 2 ;;
    --full) full=1; shift ;;
    --list) list=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    -*) usage ;;
    *) break ;;
  esac
done
[ $# -le 2 ] || usage
base=${1:-https://atlas.shifter.io}; base=${base%/}
ranges=${2:-24h 7d 30d}
urls_file="$(cd "$(dirname "$0")" && pwd)/site-check.urls"
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT

# Expands site-check.urls into "<kind> <full URL>" lines in argv[6]; prints the network count.
expand_py='
import json, sys
urls_file, networks, ranges, base, site, out = sys.argv[1:]
try:
    d = json.load(open(networks))
except (OSError, ValueError):
    d = None
rows = d.get("networks", d) if isinstance(d, dict) else d
rows = rows if isinstance(rows, list) else []
slugs = sorted(x["slug"] for x in rows if isinstance(x, dict) and x.get("slug"))
if not slugs:
    sys.exit(f"FAIL: {base}/v1/networks gave no network list")
roots = {"api": base, "site": site}
lines = []
for n, line in enumerate(open(urls_file), 1):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    kind, _, path = line.partition(" ")
    if kind not in roots or not path.strip():
        sys.exit(f"{urls_file}:{n}: expected \"api <path>\" or \"site <path>\"")
    paths = [path.strip().replace("{first-slug}", slugs[0])]
    for key, values in (("{slug}", slugs), ("{range}", ranges.split())):
        if key in paths[0]:
            paths = [p.replace(key, v) for p in paths for v in values]
    if roots[kind]:
        lines += [f"{kind} {roots[kind]}{p}" for p in paths]
open(out, "w").write("".join(x + "\n" for x in lines))
print(len(slugs))
'
curl -sfL --max-redirs 5 -m 30 -o "$W/networks.json" "$base/v1/networks"
nets=$(python3 -c "$expand_py" "$urls_file" "$W/networks.json" "$ranges" "$base" "$site" "$W/urls") || exit 1
if [ "$list" = 1 ]; then cat "$W/urls"; exit 0; fi

# check <workdir> "<kind> <url>": one tab-separated result line: verdict, code, hops, secs, url.
check() {
  local kind=${2%% *} url=${2#* } body res code hops secs final
  body=$(mktemp "$1/body.XXXXXX")
  res=$(curl -s -L --max-redirs 5 -m 30 -o "$body" \
    -w '%{http_code} %{num_redirects} %{time_total} %{url_effective}' "$url")
  read -r code hops secs final <<< "$res"
  printf '%s\t%s\t%s\t%s\t%s\n' "$(verdict)" "$code" "${hops:-0}" "$secs" "$url"
  rm -f "$body"
}
# verdict: the rule of the header above, on check's variables.
verdict() {
  if [ "$code" != 200 ]; then
    if [ "${hops:-0}" -ge 5 ]; then echo "FAIL redirect loop"; else echo "FAIL HTTP $code"; fi
  elif [ "$hops" -gt 1 ]; then echo "FAIL $hops redirect hops"
  elif [ "$hops" = 1 ] && [ "$final" = "$url" ]; then echo "FAIL redirect to itself"
  elif [ "$kind" = api ] && ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$body" 2>/dev/null; then
    echo "FAIL not JSON"
  elif [ "$kind" = site ] && grep -qi 'temporarily unavailable' "$body"; then echo "FAIL unavailable page"
  elif [ "$hops" = 1 ]; then echo "pass, 1 redirect"
  else echo pass; fi
}
export -f check verdict
tr '\n' '\0' < "$W/urls" | xargs -0 -n 1 -P 8 "$BASH" -c 'check "$0" "$1"' "$W" > "$W/res"

n=$(wc -l < "$W/urls" | tr -d ' '); pages=$(grep -c '^site ' "$W/urls")
ok=$(grep -c '^pass' "$W/res"); red=$(awk -F'\t' '$3 > 0' "$W/res" | wc -l | tr -d ' ')
echo "site-check $base${site:+ + $site} $(date -u '+%Y-%m-%d %H:%M:%SZ'): $nets networks," \
  "$n URLs ($((n - pages)) API, $pages pages): $ok pass, $((n - ok)) FAIL, $red redirected"
echo
echo "| result | code | hops | secs | url |"
echo "|---|---|---|---|---|"
sort -t "$(printf '\t')" -k1,1 -k5,5 "$W/res" \
  | awk -F'\t' -v full="$full" 'full || $1 != "pass" { printf "| %s | %s | %s | %s | %s |\n", $1, $2, $3, $4, $5 }'
[ "$full" = 1 ] || [ "$ok" != "$n" ] || [ "$red" != 0 ] || echo "| every URL: pass, no redirect | | | | |"
[ "$ok" = "$n" ]
