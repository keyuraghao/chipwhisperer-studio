#!/usr/bin/env bash
# Real-hardware test session for ChipWhisperer Studio (a test tool).
# Usage: tools/hw_session.sh [--prepare] [--sim] [extra pytest args]
#   --prepare  set up the session data folder (private copy of the firmware sources, linked toolchains) and pre-build the target firmware, then exit
#   --sim      run the stages against the simulator instead of a Husky
# Env passed through: CWSTUDIO_HW_SN, CWSTUDIO_HW_PLATFORM (default CWHUSKY), CWSTUDIO_HW_PROGRAMMER, CWSTUDIO_HW_SKIP_PROGRAM=1, CWSTUDIO_HW_MANUAL=1, CWSTUDIO_HW_DATA
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(dirname "$REPO")"
VENV="$ROOT/.venv"
SRC_DATA="${CWSTUDIO_FW_DATA:-$ROOT/.studio-dev/data}"
export CWSTUDIO_HW_DATA="${CWSTUDIO_HW_DATA:-$ROOT/.studio-dev/hw-data}"
export CWSTUDIO_HW_PLATFORM="${CWSTUDIO_HW_PLATFORM:-CWHUSKY}"
MODE=husky
PREPARE=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --prepare) PREPARE=1 ;;
    --sim) MODE=sim ;;
    *) ARGS+=("$a") ;;
  esac
done
# shellcheck disable=SC1091
source "$VENV/bin/activate"
export PYTHONPATH="$REPO/src"
cd "$REPO" || exit 1

prepare() {
  echo "== preparing $CWSTUDIO_HW_DATA from $SRC_DATA"
  mkdir -p "$CWSTUDIO_HW_DATA/firmware"
  if [ ! -f "$CWSTUDIO_HW_DATA/firmware/chipwhisperer/Makefile.inc" ]; then
    rsync -a --exclude 'objdir*' --exclude '*.elf' --exclude '*.hex' --exclude '*.bin' --exclude '*.eep' --exclude '*.lss' --exclude '*.map' --exclude '*.sym' --exclude '*.o' --exclude '*.d' "$SRC_DATA/firmware/chipwhisperer/" "$CWSTUDIO_HW_DATA/firmware/chipwhisperer/" || return 1
  fi
  [ -e "$CWSTUDIO_HW_DATA/toolchains" ] || ln -s "$SRC_DATA/toolchains" "$CWSTUDIO_HW_DATA/toolchains"
  python - <<EOF || return 1
import os
from cwstudio.toolchains import ToolchainManager
from cwstudio.firmware import FirmwareManager
d = os.environ["CWSTUDIO_HW_DATA"]
fm = FirmwareManager(d, ToolchainManager(d))
r = fm.build({"project": "simpleserial-aes", "platform": os.environ["CWSTUDIO_HW_PLATFORM"], "ss_ver": "SS_VER_2_1", "crypto_target": "TINYAES128C", "compiler": "gcc"}, wait=True)
if r["state"] != "ok":
    print("\n".join(fm.build_log()["lines"][-20:]))
    raise SystemExit("build failed: %s" % r.get("error"))
print("pre-built:", r["hex"], "programmer:", r["programmer"])
EOF
}

if [ "$PREPARE" = 1 ]; then
  prepare
  exit $?
fi

echo "== prerequisites ($MODE)"
ok=1
if [ "$MODE" = husky ]; then
  if lsusb | grep -qi '2b3e:ace5'; then echo "ok: Husky on USB (2b3e:ace5)"; else echo "MISSING: no Husky on USB (lsusb has no 2b3e:ace5)"; ok=0; fi
  if id -nG | tr ' ' '\n' | grep -qx -e chipwhisperer -e plugdev; then echo "ok: user in chipwhisperer/plugdev group"; else echo "MISSING: user not in chipwhisperer or plugdev group"; ok=0; fi
  if ls /etc/udev/rules.d/ 2>/dev/null | grep -qi -e newae -e chipwhisperer; then echo "ok: udev rule present"; else echo "MISSING: no NewAE udev rule in /etc/udev/rules.d"; ok=0; fi
fi
[ -f "$CWSTUDIO_HW_DATA/firmware/chipwhisperer/Makefile.inc" ] || { echo "data folder not prepared: running --prepare"; prepare || ok=0; }
if [ "$ok" != 1 ]; then echo "fix the items above, then run again"; exit 2; fi

echo "== running stages (platform $CWSTUDIO_HW_PLATFORM)"
CWSTUDIO_HW="$MODE" python -m pytest tests/hardware -v -s -p no:cacheprovider "${ARGS[@]}"
rc=$?
echo "== summary: $REPO/tests/hardware/last_run.json"
exit $rc
