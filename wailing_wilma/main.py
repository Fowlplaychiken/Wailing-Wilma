"""Load the installed Wilma version through Wingman's custom skill loader."""

import importlib
import json
import re
from pathlib import Path

version = json.loads(Path(__file__).with_name("manifest.json").read_bytes())["version"]
if not isinstance(version, str) or not re.fullmatch(
    r"[0-9]+\.[0-9]+\.[0-9]+", version
):
    raise ValueError("Invalid Wilma package version")
namespace = "wailing_wilma_skill_v" + version.replace(".", "_")
importlib.invalidate_caches()
WailingWilma = importlib.import_module(namespace + ".skill").WailingWilma
