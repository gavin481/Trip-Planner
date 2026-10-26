"""Copy the Japan & Bali files from the ema volume onto the trip service volume.

Run on this PC with the Railway CLI signed in:

    python scripts/copy_trip_data.py --env development --dry-run
    python scripts/copy_trip_data.py --env development
    python scripts/copy_trip_data.py --env production

Safety rules, by design:
- The ema volume is only read. Nothing on ema is changed or deleted.
- On the trip volume a file is only written when it does not exist yet. An existing
  file is never overwritten, truncated, or deleted (it is reported as "kept").
- Only the trip files are copied: trip_itinerary.json, trip_password.json,
  trip_gate_secret, gmail_oauth_token.json, gmail_oauth_client.json,
  trip_files/*, and trip_photos/*.
- Every written file is checked against the source SHA-256.

The Gmail token files are sealed with DLM_TOKEN_KEY. Give the trip service the
same DLM_TOKEN_KEY value as ema in that environment, or they cannot be opened.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

FILES = [
    "trip_itinerary.json",
    "trip_password.json",
    "trip_gate_secret",
    "gmail_oauth_token.json",
    "gmail_oauth_client.json",
]
DIRS = ["trip_files", "trip_photos"]

# Runs on the ema container. Read-only: lists the trip files and returns them as one
# base64 tar.gz between markers.
SOURCE_CODE = r"""
import base64, hashlib, io, json, sys, tarfile
from pathlib import Path
root = Path(sys.argv[1])
files = FILES_LIST; dirs = DIRS_LIST; want = sys.argv[2] == "data"
names = [n for n in files if (root / n).is_file()]
for d in dirs:
    folder = root / d
    if folder.is_dir():
        names += [d + "/" + p.name for p in sorted(folder.iterdir()) if p.is_file()]
listing = {n: [(root / n).stat().st_size, hashlib.sha256((root / n).read_bytes()).hexdigest()] for n in names}
out = {"listing": listing}
if want:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for n in names:
            tar.add(str(root / n), arcname=n, recursive=False)
    out["tar"] = base64.b64encode(buf.getvalue()).decode("ascii")
print("<<TRIPCOPY>>" + json.dumps(out) + "<<END>>")
"""

# Runs on the trip container. Writes only files that are missing. Never overwrites.
TARGET_CODE = r"""
import base64, hashlib, io, json, sys, tarfile
from pathlib import Path
root = Path(sys.argv[1]); dry = sys.argv[2] == "1"
allowed_files = set(FILES_LIST); allowed_dirs = set(DIRS_LIST)
payload = json.loads(sys.stdin.read())
expected = payload["listing"]
report = {"written": [], "kept": [], "conflict": [], "rejected": []}
def ok_name(name):
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts) or name.startswith("/") or "\\" in name:
        return False
    return (len(parts) == 1 and parts[0] in allowed_files) or (len(parts) == 2 and parts[0] in allowed_dirs)
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload["tar"])), mode="r:gz") as tar:
    for member in tar.getmembers():
        name = member.name
        if not member.isfile() or not ok_name(name) or name not in expected:
            report["rejected"].append(name); continue
        data = tar.extractfile(member).read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected[name][1]:
            report["rejected"].append(name); continue
        target = root / name
        if target.exists():
            same = target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest
            report["kept" if same else "conflict"].append(name); continue
        if dry:
            report["written"].append(name); continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(data)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            report["rejected"].append(name + " (verify failed)"); continue
        if name in ("trip_password.json", "trip_gate_secret", "gmail_oauth_token.json", "gmail_oauth_client.json"):
            try:
                target.chmod(0o600)
            except OSError:
                pass
        report["written"].append(name)
print("<<TRIPCOPY>>" + json.dumps(report) + "<<END>>")
"""


def _code(template: str) -> str:
    # The lists are written into the code so no JSON has to survive shell quoting.
    return template.replace("FILES_LIST", repr(FILES)).replace("DIRS_LIST", repr(DIRS))


def railway_exe() -> str:
    local = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if local:
        exe = Path(local) / "railway-cli" / "railway.exe"
        if exe.is_file():
            return str(exe)
    found = shutil.which("railway")
    if found:
        return found
    raise SystemExit("The Railway CLI is not on this PC.")


def run_ssh(service: str, env: str, code: str, args: list[str], stdin: bytes | None = None) -> dict:
    proc = subprocess.run(
        [railway_exe(), "ssh", "-s", service, "-e", env, "--", "python", "-c", code, *args],
        input=stdin,
        capture_output=True,
    )
    out = proc.stdout.decode("utf-8", "replace")
    match = re.search(r"<<TRIPCOPY>>(.*)<<END>>", out, re.S)
    if proc.returncode != 0 or not match:
        detail = proc.stderr.decode("utf-8", "replace").strip() or out.strip()
        raise SystemExit(f"railway ssh to {service} ({env}) failed: {detail[-600:]}")
    return json.loads(match.group(1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=["development", "production"])
    parser.add_argument("--source-service", default="ema")
    parser.add_argument("--source-dir", default="/data/ema-railway-data")
    parser.add_argument("--target-service", default="trip")
    parser.add_argument("--target-dir", default="/data")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written. Writes nothing.")
    args = parser.parse_args()
    if args.source_service == args.target_service:
        raise SystemExit("Source and target service must differ.")

    print(f"Reading trip files from {args.source_service}:{args.source_dir} ({args.env}) ...")
    source = run_ssh(args.source_service, args.env, _code(SOURCE_CODE),
                     [args.source_dir, "data"])
    listing = source["listing"]
    if not listing:
        raise SystemExit("No trip files found on the source volume. Nothing to copy.")
    for name, (size, _digest) in sorted(listing.items()):
        print(f"  {name}  ({size} bytes)")

    print(f"{'Checking' if args.dry_run else 'Writing missing files to'} {args.target_service}:{args.target_dir} ...")
    body = json.dumps({"listing": listing, "tar": source["tar"]}).encode("utf-8")
    report = run_ssh(args.target_service, args.env, _code(TARGET_CODE),
                     [args.target_dir, "1" if args.dry_run else "0"],
                     stdin=body)
    label = "would write" if args.dry_run else "written"
    for key, title in (("written", label), ("kept", "already there, identical"),
                       ("conflict", "already there, DIFFERENT - left untouched"), ("rejected", "rejected")):
        for name in report.get(key, []):
            print(f"  [{title}] {name}")
    if report.get("conflict"):
        print("Some files already exist on the trip volume with different contents. They were not changed.")
    print("Done. Nothing on the ema volume was changed.")


if __name__ == "__main__":
    sys.exit(main())
