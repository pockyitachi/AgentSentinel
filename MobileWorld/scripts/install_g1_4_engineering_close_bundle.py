"""Install the audited G1.4 engineering-close bundle with no-replace semantics."""

from __future__ import annotations

import argparse
import ctypes
import errno
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

TARGET_ROOT = Path(
    "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/g1_4_engineering_close_20260831"
)
RENAME_NOREPLACE = 1
AT_FDCWD = -100


def _load_verifier() -> ModuleType:
    path = Path(__file__).with_name("verify_g1_4_engineering_close_manifest.py")
    spec = importlib.util.spec_from_file_location("_g14_close_installer_verifier", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load engineering-close verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_exclusive(path: Path, raw: bytes, mode: int = 0o400) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("renameat2 is unavailable; no fallback is allowed")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(destination)
        raise OSError(error, os.strerror(error), destination)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--validation-receipt", required=True, type=Path)
    arguments = parser.parse_args()
    verifier = _load_verifier()
    repo_root = Path(__file__).resolve().parents[2]
    expected_manifest = repo_root / verifier.MANIFEST_RELATIVE_PATH
    expected_schema = repo_root / verifier.SCHEMA_RELATIVE_PATH
    if (
        arguments.manifest.resolve() != expected_manifest
        or arguments.schema.resolve() != expected_schema
    ):
        raise RuntimeError("installer inputs are not the pinned checked-in manifest and schema")
    if TARGET_ROOT.exists() or TARGET_ROOT.is_symlink():
        raise FileExistsError(TARGET_ROOT)

    manifest_raw, manifest = verifier._load_json(arguments.manifest)
    _, schema = verifier._load_json(arguments.schema)
    verifier.Draft202012Validator.check_schema(schema)
    verifier.Draft202012Validator(schema).validate(manifest)
    if manifest_raw != verifier._canonical_json(manifest):
        raise RuntimeError("manifest is not canonical")
    validation_raw, validation = verifier._load_json(arguments.validation_receipt, mode=0o400)
    if (
        verifier._sha256(validation_raw) != manifest["validation"]["receipt_sha256"]
        or len(validation_raw) != manifest["validation"]["receipt_byte_count"]
        or validation_raw != verifier._canonical_json(validation)
    ):
        raise RuntimeError("validation receipt does not match the manifest")

    if (
        manifest["manifest_id"]
        != f"g14smoke-{verifier.EXPECTED_ARTIFACTS['run.jsonl']['sha256'][:24]}"
        or manifest["known_config_differences"] != verifier.EXPECTED_CONFIG_DIFFERENCES
        or manifest["matched_controls"] != verifier.EXPECTED_MATCHED_CONTROLS
        or manifest["deferred_to_g1_7"] != verifier.EXPECTED_DEFERRED
    ):
        raise RuntimeError("manifest closed constants do not match the audited verifier")
    verifier._verify_source_commit(
        repo_root,
        manifest["source_commit"],
        manifest["source_bindings"],
        require_clean=False,
    )
    verifier._verify_source_tree(
        repo_root,
        manifest["source_commit"],
        manifest["source_tree_binding"],
    )
    verifier._verify_validation_receipt(
        manifest,
        repo_root,
        receipt_path=arguments.validation_receipt,
    )
    verifier._verify_artifact_declarations(manifest)

    runner = verifier._load_runner(repo_root)
    _, fixture_calls = verifier._load_fixture(repo_root)
    run_pids = verifier._verify_run(
        verifier.ORIGINAL_EVIDENCE_ROOT / "run.jsonl", fixture_calls, runner
    )
    models = {model.model_id: model for model in runner.MODELS}
    verifier._verify_log(
        verifier.ORIGINAL_EVIDENCE_ROOT / "qwen.server.log",
        model={
            "snapshot": models["qwen3vl_8b"].snapshot,
            "served_name": models["qwen3vl_8b"].served_name,
            "engine_pid": 247312,
        },
        root_pid=run_pids["qwen_root_pid"],
    )
    verifier._verify_log(
        verifier.ORIGINAL_EVIDENCE_ROOT / "mai.server.log",
        model={
            "snapshot": models["mai_ui_8b"].snapshot,
            "served_name": models["mai_ui_8b"].served_name,
            "engine_pid": 257754,
        },
        root_pid=run_pids["mai_root_pid"],
    )
    verifier._verify_runtime(
        manifest,
        repo_root,
        runner,
        run_pids,
        {
            logical_name: verifier.ORIGINAL_EVIDENCE_ROOT / logical_name
            for logical_name in verifier.EXPECTED_ARTIFACTS
        },
    )

    parent = TARGET_ROOT.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".g14-close-staging-", dir=parent))
    renamed = False
    try:
        evidence = staging / "evidence"
        sha_root = evidence / "objects" / "sha256"
        sha_root.mkdir(parents=True, mode=0o700)
        for artifact in manifest["artifacts"]:
            source = Path(artifact["original_path"])
            raw, _ = verifier._read_regular_nofollow(source)
            if len(raw) != artifact["byte_count"] or verifier._sha256(raw) != artifact["sha256"]:
                raise RuntimeError(f"artifact changed before copy: {source}")
            prefix = sha_root / artifact["sha256"][:2]
            prefix.mkdir(mode=0o700)
            _write_exclusive(prefix / artifact["sha256"], raw)
        manifest_path = evidence / f"manifest-{verifier._sha256(manifest_raw)}.json"
        _write_exclusive(manifest_path, manifest_raw)
        _write_exclusive(staging / "validation-receipt.v1.json", validation_raw)
        receipt = {
            "schema_version": "mobileworld.g1.engineering-close-installation/v1",
            "evidence_root": manifest["evidence_root"],
            "manifest_sha256": verifier._sha256(manifest_raw),
            "artifact_count": 3,
            "evidence_root_absent_before": True,
            "validation_receipt_absent_before": True,
            "installed_no_replace": True,
            "directory_fsync_completed": True,
            "final_reopen_verified": True,
            "gpu_used": False,
            "model_used": False,
            "network_used": False,
            "signal_sent": False,
        }
        receipt_raw = _canonical(receipt)
        _write_exclusive(staging / "install-receipt.v1.json", receipt_raw)
        directories = sorted(
            [path for path in staging.rglob("*") if path.is_dir()],
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            os.chmod(directory, 0o500)
            _fsync_directory(directory)
        os.chmod(staging, 0o500)
        _fsync_directory(staging)
        _rename_noreplace(staging, TARGET_ROOT)
        renamed = True
        _fsync_directory(parent)
    finally:
        if not renamed and staging.exists():
            os.chmod(staging, 0o700)
            for directory in staging.rglob("*"):
                if directory.is_dir():
                    os.chmod(directory, 0o700)
            shutil.rmtree(staging)

    for artifact in manifest["artifacts"]:
        final_path = Path(artifact["sealed_object_path"])
        verifier._verify_hash(final_path, artifact["sha256"], artifact["byte_count"])
    final_manifest = Path(manifest["evidence_root"]) / (
        f"manifest-{verifier._sha256(manifest_raw)}.json"
    )
    final_raw, _ = verifier._read_regular_nofollow(final_manifest, mode=0o400)
    if final_raw != manifest_raw:
        raise RuntimeError("installed manifest readback mismatch")
    final_validation, _ = verifier._read_regular_nofollow(
        TARGET_ROOT / "validation-receipt.v1.json", mode=0o400
    )
    final_receipt, _ = verifier._read_regular_nofollow(
        TARGET_ROOT / "install-receipt.v1.json", mode=0o400
    )
    if final_validation != validation_raw or final_receipt != receipt_raw:
        raise RuntimeError("installed receipt readback mismatch")
    print(_canonical(receipt).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
