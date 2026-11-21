"""Explicit, receipt-pinned admission for an optional local Tesseract install."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from adapters.perception.tesseract import (
    _ENGINE_PINS,
    EXECUTABLE_BYTES_LIMIT,
    MODEL_BYTES_LIMIT,
    MODEL_COMMIT,
    TesseractArtifact,
    _is_sha256,
)
from adapters.runtime_updates.file_reads import _read

_RECEIPT_LIMIT = 16_384


class TesseractArtifactAdmission:
    """Admit only the explicitly selected binaries bound to an externally pinned receipt.

    The expected receipt hash comes from caller-owned settings. This class never
    searches for installs or treats a neighboring receipt as its own authority.
    """

    def __init__(
        self,
        executable: Path,
        tessdata_directory: Path,
        receipt_path: Path,
        expected_receipt_sha256: str,
    ) -> None:
        paths = (executable, tessdata_directory, receipt_path)
        if (any(not isinstance(path, Path) or not path.is_absolute() for path in paths)
                or not _is_sha256(expected_receipt_sha256)):
            raise ValueError(
                "absolute artifact and receipt paths with pinned receipt hash required")
        self._executable = executable
        self._tessdata = tessdata_directory
        self._receipt = receipt_path
        self._receipt_sha256 = expected_receipt_sha256

    def admit(
        self, artifact: TesseractArtifact, executable_sha256: str, model_sha256: str
    ) -> bool:
        """Recheck receipt and selected files on every call; fail closed on any error."""
        try:
            if (type(artifact) is not TesseractArtifact
                    or artifact.executable != self._executable
                    or artifact.tessdata_directory != self._tessdata
                    or (artifact.engine_version, artifact.engine_source_pin) not in _ENGINE_PINS
                    or artifact.model_source_pin != MODEL_COMMIT
                    or artifact.executable_sha256 != executable_sha256
                    or artifact.model_sha256 != model_sha256):
                return False
            executable = _read(self._executable, EXECUTABLE_BYTES_LIMIT)
            model = _read(self._tessdata / "eng.traineddata", MODEL_BYTES_LIMIT)
            receipt_raw = _read(self._receipt, _RECEIPT_LIMIT)
            if (not executable or not model
                    or _sha(executable) != executable_sha256
                    or _sha(model) != model_sha256
                    or _sha(receipt_raw) != self._receipt_sha256):
                return False
            receipt = json.loads(receipt_raw, object_pairs_hook=_unique_object,
                                  parse_constant=_reject_constant)
            proof = receipt["proof"]
            proof_ref = "actual-pgm-self-test:" + _sha(
                json.dumps(proof, sort_keys=True).encode("utf-8"))
            return self._valid_receipt(receipt, proof, proof_ref, executable_sha256,
                                       model_sha256, artifact.self_test_ref,
                                       artifact.engine_version, artifact.engine_source_pin)
        except Exception:
            return False

    def artifact(self) -> TesseractArtifact:
        """Build the current pinned artifact record, failing if receipt or files drifted."""
        executable = _read(self._executable, EXECUTABLE_BYTES_LIMIT)
        model = _read(self._tessdata / "eng.traineddata", MODEL_BYTES_LIMIT)
        raw = _read(self._receipt, _RECEIPT_LIMIT)
        if (not executable or not model or _sha(raw) != self._receipt_sha256):
            raise ValueError("selected Tesseract receipt or artifacts are unavailable")
        try:
            receipt = json.loads(raw, object_pairs_hook=_unique_object,
                                 parse_constant=_reject_constant)
            proof = receipt["proof"]
            version_line = receipt["version"].splitlines()[0]
            prefix = "tesseract "
            if not version_line.startswith(prefix):
                raise ValueError("Tesseract self-test version is invalid")
            version = version_line[len(prefix):]
            source_pin = proof["source_commit"]
            proof_ref = receipt["proof_ref"]
            artifact = TesseractArtifact(
                self._executable, self._tessdata, _sha(executable), _sha(model),
                source_pin, MODEL_COMMIT, proof_ref, engine_version=version,
            )
        except Exception as exc:
            raise ValueError("selected Tesseract receipt is invalid") from exc
        if not self.admit(artifact, artifact.executable_sha256, artifact.model_sha256):
            raise ValueError("selected Tesseract artifacts do not match the pinned receipt")
        return artifact

    @staticmethod
    def _valid_receipt(
        receipt: Any, proof: Any, proof_ref: str, executable_sha256: str,
        model_sha256: str, artifact_ref: str, engine_version: str,
        engine_source_pin: str,
    ) -> bool:
        if type(receipt) is not dict or type(proof) is not dict:
            return False
        adapter = receipt.get("adapter")
        version = receipt.get("version")
        if type(adapter) is not dict or type(version) is not str:
            return False
        direct_ms = proof.get("direct_ms")
        adapter_ms = adapter.get("elapsed_ms")
        return (
            receipt.get("kind") == "actual_native_pipe_and_original_adapter_only"
            and proof.get("engine_sha256") == executable_sha256
            and proof.get("model_sha256") == model_sha256
            and proof.get("source_commit") == engine_source_pin
            and proof.get("expected") == "42" and type(proof.get("exit")) is int
            and proof["exit"] == 0
            and _is_sha256(proof.get("pgm_sha256"))
            and _is_sha256(proof.get("stdout_sha256"))
            and receipt.get("proof_ref") == proof_ref == artifact_ref
            and version.splitlines()[0] == "tesseract " + engine_version
            and adapter.get("text") == "42" and type(adapter.get("returncode")) is int
            and adapter["returncode"] == 0
            and type(adapter.get("actual_pid")) is int and adapter["actual_pid"] > 0
            and adapter.get("child_owned_after_close") is False
            and type(direct_ms) in (int, float) and math.isfinite(direct_ms)
            and direct_ms >= 0
            and type(adapter_ms) in (int, float) and math.isfinite(adapter_ms)
            and adapter_ms >= 0
            and receipt.get("config_files") is False
            and receipt.get("runtime_profile_activated") is False
            and receipt.get("visual_field_or_gameplay_qualification") is False
            and _is_sha256(receipt.get("probe_sha256"))
            and _is_sha256(receipt.get("adapter_source_sha256"))
        )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate receipt key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite receipt number")
