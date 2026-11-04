#!/usr/bin/env bash
# Writes a LaunchAgent plist (the daily job, or the radar with --job radar) and prints the launchctl command.
# This script never calls launchctl.
set -euo pipefail
# bash 5.2 turns "&" in a ${var//pattern/replacement} replacement into the matched text,
# which would mangle a path that contains "&". Older bash has no such option.
shopt -u patsub_replacement 2>/dev/null || true

LABEL="net.starfield.harness-daily"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/$LABEL.plist.template"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

usage() {
	cat >&2 <<USAGE
Usage:
  install.sh --python PATH --repo DIR --logdir DIR --envfile FILE [--job daily|radar] [--hour 0-23] [--minute 0-59]
  install.sh [--job daily|radar] --uninstall

Writes $TARGET and prints the launchctl command. Defaults are hour 6, minute 0 for daily
and hour 10, minute 30 for radar.
USAGE
	exit 2
}

xml_escape() {
	local s="$1"
	s="${s//&/&amp;}"
	s="${s//</&lt;}"
	s="${s//>/&gt;}"
	printf '%s' "$s"
}

python="" repo="" logdir="" envfile="" hour="" minute="" job="daily" uninstall=0
while [ $# -gt 0 ]; do
	case "$1" in
	--python) python="${2:?}"; shift 2 ;;
	--repo) repo="${2:?}"; shift 2 ;;
	--logdir) logdir="${2:?}"; shift 2 ;;
	--envfile) envfile="${2:?}"; shift 2 ;;
	--hour) hour="${2:?}"; shift 2 ;;
	--minute) minute="${2:?}"; shift 2 ;;
	--job) job="${2:?}"; shift 2 ;;
	--uninstall) uninstall=1; shift ;;
	*) usage ;;
	esac
done

case "$job" in
daily) default_hour=6; default_minute=0 ;;
radar) LABEL="net.starfield.harness-radar"; TEMPLATE="$HERE/$LABEL.plist.template"
	TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"; default_hour=10; default_minute=30 ;;
*) echo "--job must be daily or radar" >&2; exit 2 ;;
esac
hour="${hour:-$default_hour}"
minute="${minute:-$default_minute}"

if [ "$uninstall" = 1 ]; then
	echo "Run this to stop the job:"
	echo "  launchctl bootout gui/$(id -u)/$LABEL"
	echo "Then delete the file:"
	echo "  rm \"$TARGET\""
	exit 0
fi

[ -n "$python" ] && [ -n "$repo" ] && [ -n "$logdir" ] && [ -n "$envfile" ] || usage
case "$hour" in '' | *[!0-9]*) echo "--hour must be a number from 0 to 23" >&2; exit 2 ;; esac
case "$minute" in '' | *[!0-9]*) echo "--minute must be a number from 0 to 59" >&2; exit 2 ;; esac
hour=$((10#$hour))
minute=$((10#$minute))
[ "$hour" -le 23 ] || { echo "--hour must be a number from 0 to 23" >&2; exit 2; }
[ "$minute" -le 59 ] || { echo "--minute must be a number from 0 to 59" >&2; exit 2; }
[ -d "$repo" ] || { echo "--repo is not a directory" >&2; exit 2; }

mkdir -p "$logdir" "$(dirname "$TARGET")"

plist="$(cat "$TEMPLATE")"
plist="${plist//@PYTHON@/$(xml_escape "$python")}"
plist="${plist//@REPO@/$(xml_escape "$repo")}"
plist="${plist//@LOGDIR@/$(xml_escape "$logdir")}"
plist="${plist//@ENVFILE@/$(xml_escape "$envfile")}"
plist="${plist//@HOUR@/$hour}"
plist="${plist//@MINUTE@/$minute}"
printf '%s\n' "$plist" > "$TARGET"

echo "Wrote $TARGET"
echo "Run this to load the job in your login session:"
echo "  launchctl bootstrap gui/$(id -u) \"$TARGET\""
