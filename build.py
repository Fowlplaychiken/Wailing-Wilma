"""Build the installable Wailing Wilma ZIP using only the standard library."""

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "wailing_wilma"
DEPENDENCIES = "dependencies/wailing_wilma_skill"
SKILL_FILES = ("__init__.py", "main.py", "default_config.yaml", "logo.png")
DEPENDENCY_FILES = (
    "__init__.py", "skill.py", "foreground.py", "image_encoding.py",
    "policy.py", "preferences.py", "reader.py",
)
DOCUMENTATION = ("README.md", "LICENSE", "NOTICE.md", "docs/wilma-warning-flow.svg")


def build(destination: Path) -> dict:
    destination = destination.resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError("Use a new output folder outside the source checkout")
    files = {name: (SKILL / name).read_bytes() for name in SKILL_FILES}
    for name in DEPENDENCY_FILES:
        path = f"{DEPENDENCIES}/{name}"
        files[path] = (SKILL / path).read_bytes()
    for name in DOCUMENTATION:
        files[name] = (ROOT / name).read_bytes()

    config = files["default_config.yaml"]
    version = re.search(rb"(?m)^version: ([0-9]+\.[0-9]+\.[0-9]+)\r?$", config)
    if version is None:
        raise ValueError("The skill configuration needs an explicit version")
    manifest = {
        "schema_version": 1,
        "name": "WailingWilma",
        "version": version.group(1).decode("ascii"),
        "game_input": False,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    destination.mkdir(parents=True, exist_ok=False)
    archive = destination / f"wailing_wilma-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, "x") as bundle:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(f"wailing_wilma/{name}", (1980, 1, 1, 0, 0, 0))
            entry.create_system, entry.external_attr = 3, 0o100644 << 16
            bundle.writestr(entry, data)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None or any(
            bundle.read(f"wailing_wilma/{name}") != data for name, data in files.items()
        ):
            raise ValueError("The archive readback differs from the source")
    return {"archive": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(json.dumps(build(parser.parse_args().out)))
