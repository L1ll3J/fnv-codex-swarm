"""Create the updater's manifest for one built application wheel."""

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--repository", required=True)
parser.add_argument("--package", required=True)
parser.add_argument("--wheel", type=Path, required=True)
args = parser.parse_args()
with zipfile.ZipFile(args.wheel) as archive:
    names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
    if len(names) != 1:
        raise SystemExit("Expected one distribution metadata file")
    metadata = BytesParser().parsebytes(archive.read(names[0]))
if metadata["Name"] != args.package:
    raise SystemExit("Wheel distribution differs from release package")
if (
    os.environ.get("GITHUB_REF_TYPE") == "tag"
    and os.environ.get("GITHUB_REF_NAME") != "v" + metadata["Version"]
):
    raise SystemExit("Release tag differs from wheel version")
manifest = {
    "schema": 1,
    "repository": args.repository,
    "package": args.package,
    "version": metadata["Version"],
    "wheel": args.wheel.name,
    "sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
}
(args.wheel.parent / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
