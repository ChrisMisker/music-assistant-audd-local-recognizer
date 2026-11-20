#!/usr/bin/env bash
# Send AudD debug samples to AudD again at other speed corrections (a dashboard test, scripted).
#
#   scripts/audd-test.sh --speeds -1,-2,-3 debug-samples/<sample>.wav [more.wav ...]
#   scripts/audd-test.sh --speeds -2 --max 30 debug-samples/*_19eeb3a0_*.wav
#
# Each WAV saved by "Save AudD samples for debugging" (scripts/fetch-samples.sh) has a .json next to
# it with the speed correction it was sent at; the sample is re-timed to every speed in --speeds
# (total correction relative to the turntable, like the setting) on this machine and sent to AudD
# from INSIDE the Music Assistant container, with the token from MA's own (encrypted) provider
# settings. The token never leaves the container and is never printed. Every sample costs one AudD
# request on the user's account: the run refuses more than --max requests (default 20).

source "$(dirname "$0")/_common.sh"

SPEEDS=""
MAX=20
FILES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --speeds) SPEEDS="$2"; shift 2 ;;
    --max) MAX="$2"; shift 2 ;;
    -*) die "unknown option $1" ;;
    *) FILES+=("$1"); shift ;;
  esac
done
[[ -n "$SPEEDS" && ${#FILES[@]} -gt 0 ]] || die "usage: $0 --speeds -1,-2 [--max N] sample.wav..."
[[ "$MAX" =~ ^[0-9]+$ ]] || die "--max needs a number"

PAYLOAD="$(mktemp)"
trap 'rm -f "$PAYLOAD"' EXIT
uv run python - "$SPEEDS" "$MAX" "${FILES[@]}" >"$PAYLOAD" <<'PY' || exit 1
import base64, io, json, sys, wave
from pathlib import Path

import numpy as np

sys.path.insert(0, ".")
from audd_local_recognizer.fingerprints import change_speed

speeds = [float(s) for s in sys.argv[1].split(",") if s.strip()]
limit = int(sys.argv[2])
jobs = []
for name in sys.argv[3:]:
    path = Path(name)
    meta = path.with_suffix(".json")
    sent = json.loads(meta.read_text())["speed_correction_percent"] if meta.exists() else 0.0
    with wave.open(str(path)) as reader:
        rate = reader.getframerate()
        data = np.frombuffer(reader.readframes(reader.getnframes()), "<i2")
    for total in speeds:
        out = change_speed(data, ((1 + total / 100) / (1 + sent / 100) - 1) * 100)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(rate)
            writer.writeframes(out.tobytes())
        label = f"{path.stem[:40]} at {total:+.1f}%"
        jobs.append({"label": label, "wav": base64.b64encode(buf.getvalue()).decode()})
if len(jobs) > limit:
    sys.exit(f"{len(jobs)} AudD requests needed, more than --max {limit}; nothing sent")
print(f"{len(jobs)} AudD request(s)…", file=sys.stderr)
json.dump(jobs, sys.stdout)
PY

resolve_container
resolve_runtime
SETTINGS="$MA_DATA_DIR/settings.json"
{
  printf 'JOBS = r"""'; cat "$PAYLOAD"; printf '"""\n'
  cat <<PY
import asyncio, base64, json
import aiohttp
from cryptography.fernet import Fernet
from music_assistant.providers.audd_local_recognizer.audd_client import (
    AudDClient, AudDError, AudDNoMatch,
)

def token() -> str:
    settings = json.load(open("$SETTINGS"))
    for provider in (settings.get("providers") or {}).values():
        if provider.get("domain") != "$PACKAGE":
            continue
        value = (provider.get("values") or {}).get("audd_api_token") or ""
        if value.startswith("_encrypted_"):
            key = settings.get("encryption_key") or ""
            value = Fernet(key.encode()).decrypt(value.removeprefix("_encrypted_").encode()).decode()
        if value:
            return value.strip()
    raise SystemExit("no AudD token found in the provider settings")

async def main() -> None:
    client_token = token()
    async with aiohttp.ClientSession() as session:
        client = AudDClient(session, client_token)
        for job in json.loads(JOBS):
            try:
                result = await client.recognize(base64.b64decode(job["wav"]))
            except AudDError as err:
                print(f"{job['label']}: ERROR {err}")
                continue
            if isinstance(result, AudDNoMatch):
                print(f"{job['label']}: no match ({result.reason})")
                continue
            print(
                f"{job['label']}: HIT {result.get('artist')} - {result.get('title')} "
                f"[{result.get('album')}] timecode {result.get('audd_timecode')} "
                f"length {result.get('duration_seconds')}"
            )

asyncio.run(main())
PY
} | remote "$DOCKER exec -i '$MA_CONTAINER' '$CONTAINER_PY' -"
