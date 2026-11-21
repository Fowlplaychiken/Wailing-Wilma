"""Check a selected Tesseract build and save an offline terminal-reader receipt."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from adapters.perception import tesseract
from adapters.perception.tesseract_artifacts import TesseractArtifactAdmission
from adapters.runtime_updates.file_reads import _read

# Official English tessdata_fast at tesseract.MODEL_COMMIT.
ENGLISH_SHA256 = "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sample() -> bytes:
    image = Image.new("L", (220, 100), 255)
    ImageDraw.Draw(image).text((110, 50), "42", fill=0, anchor="mm",
                               font=ImageFont.load_default(size=62))
    return b"P5\n220 100\n255\n" + image.tobytes()


async def verify(executable: Path, tessdata: Path, engine_sha: str) -> dict:
    executable = executable.resolve(strict=True)
    tessdata = tessdata.resolve(strict=True)
    if digest(_read(executable, tesseract.EXECUTABLE_BYTES_LIMIT)) != engine_sha:
        raise ValueError("The executable does not match the supplied trusted SHA-256.")
    if digest(_read(tessdata / "eng.traineddata", tesseract.MODEL_BYTES_LIMIT)) != ENGLISH_SHA256:
        raise ValueError("Use the pinned English tessdata_fast file linked in the guide.")
    pgm = sample()
    with tempfile.TemporaryDirectory(prefix="wilma-self-test-") as scratch:
        env = {"PATH": str(executable.parent), "TEMP": scratch, "TMP": scratch}
        if sys.platform == "win32":
            env["SystemRoot"] = os.environ["SystemRoot"]
        common = dict(cwd=scratch, env=env, capture_output=True, timeout=5, check=True,
                      creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        version = subprocess.run([str(executable), "--version"], **common)
        version_text = (version.stdout + version.stderr).decode("utf-8")
        first = version_text.splitlines()[0] if version_text else ""
        pins = dict(tesseract._ENGINE_PINS)
        version_name = first.removeprefix("tesseract ")
        if first != "tesseract " + version_name or version_name not in pins:
            raise ValueError("Supported Tesseract versions: " + ", ".join(pins))
        started = time.perf_counter()
        direct = subprocess.run([str(executable), "stdin", "stdout", "--tessdata-dir", str(tessdata),
                                 "-l", "eng", "--oem", "1", "--psm", "7", "-c",
                                 "tessedit_char_whitelist=0123456789.", "-c", "tessedit_create_tsv=1"],
                                input=pgm, **common)
        direct_ms = (time.perf_counter() - started) * 1000
    if " ".join(word.text for word in tesseract.parse_tsv(direct.stdout, 220, 100)) != "42":
        raise ValueError("The selected engine could not read the test image.")
    proof = {"engine_sha256": engine_sha, "model_sha256": ENGLISH_SHA256,
             "source_commit": pins[version_name], "pgm_sha256": digest(pgm),
             "stdout_sha256": digest(direct.stdout), "expected": "42",
             "exit": direct.returncode, "direct_ms": direct_ms}
    ref = "actual-pgm-self-test:" + digest(json.dumps(proof, sort_keys=True).encode())
    artifact = tesseract.TesseractArtifact(executable, tessdata, engine_sha, ENGLISH_SHA256,
        pins[version_name], tesseract.MODEL_COMMIT, ref, engine_version=version_name)

    class SelectedBuild:
        def admit(self, selected, engine_hash, model_hash):
            return selected == artifact and engine_hash == engine_sha and model_hash == ENGLISH_SHA256

    children = []

    async def spawn(*args, **kwargs):
        child = await asyncio.create_subprocess_exec(*args, **kwargs)
        children.append(child)
        return child

    reader = tesseract.TesseractRecognizer(artifact, SelectedBuild(), spawn=spawn)
    started = time.perf_counter()
    try:
        result = await reader.recognize(pgm, tesseract.Whitelist.NUMERIC)
    finally:
        await reader.aclose()
    if (result.text != "42" or result.source_version != version_name or len(children) != 1
            or children[0].returncode != 0 or reader.child_owned):
        raise ValueError("The Wilma reader did not complete its own test successfully.")
    return {"kind": "actual_native_pipe_and_original_adapter_only", "proof": proof,
            "proof_ref": ref, "version": version_text,
            "adapter": {"text": result.text, "elapsed_ms": (time.perf_counter() - started) * 1000,
                        "actual_pid": children[0].pid, "returncode": children[0].returncode,
                        "child_owned_after_close": reader.child_owned},
            "config_files": False, "runtime_profile_activated": False,
            "visual_field_or_gameplay_qualification": False,
            "probe_sha256": digest(Path(__file__).read_bytes()),
            "adapter_source_sha256": digest(Path(tesseract.__file__).read_bytes())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tesseract-executable", type=Path, required=True)
    parser.add_argument("--tessdata-directory", type=Path, required=True)
    parser.add_argument("--engine-sha256", required=True, help="SHA-256 from your trusted installation or build")
    parser.add_argument("--out", type=Path, required=True, help="new local receipt file")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Choose a new receipt filename.")
    exe, data = args.tesseract_executable.resolve(), args.tessdata_directory.resolve()
    try:
        details = asyncio.run(verify(exe, data, args.engine_sha256.lower()))
        raw = (json.dumps(details, indent=2) + "\n").encode()
        # Validate through the same admission code the running server uses before publishing.
        with tempfile.TemporaryDirectory(dir=args.out.parent) as temp:
            trial = Path(temp) / "receipt.json"
            trial.write_bytes(raw)
            TesseractArtifactAdmission(exe, data, trial.resolve(), digest(raw)).artifact()
        with args.out.open("xb") as stream:
            stream.write(raw)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Setup failed: {exc}\n")
    print(f"Receipt: {args.out.resolve()}\nReceipt SHA-256: {digest(raw)}")


if __name__ == "__main__":
    main()
