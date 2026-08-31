"""Run the exact CPU-only G1.4 engineering-close gates and emit one receipt."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

PYTHON = "/shared/linqiang/agent_monitor/AgentSentinel/MobileWorld/.venv/bin/python"
REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "MobileWorld/src"
SCHEMA = "mobileworld_audit_handoff/schemas/g1_4/nonformal_live_smoke_manifest.v1.schema.json"
PYTHON_FILES = [
    "MobileWorld/scripts/run_g1_gpu_smoke_simple.py",
    "MobileWorld/scripts/verify_g1_4_engineering_close_manifest.py",
    "MobileWorld/scripts/run_g1_4_engineering_close_cpu_gates.py",
    "MobileWorld/scripts/install_g1_4_engineering_close_bundle.py",
    "MobileWorld/tests/offline/test_g1_gpu_smoke_simple.py",
    "MobileWorld/tests/offline/test_g1_4_engineering_close_manifest.py",
]
BASE_ENVIRONMENT = {
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": str(SOURCE_ROOT),
}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
PACKAGE_METADATA = {
    "vllm": {
        "version": "0.19.1",
        "path": (
            "/shared/linqiang/MobileWorld/vllm_env/lib/python3.12/site-packages/"
            "vllm-0.19.1.dist-info/METADATA"
        ),
        "sha256": "f76ea566acba199ef8dd91bde6bb162795c7f8876c206a865cbb3b94fbcf7bc2",
        "byte_count": 10013,
    },
    "torch": {
        "version": "2.10.0",
        "path": (
            "/shared/linqiang/MobileWorld/vllm_env/lib/python3.12/site-packages/"
            "torch-2.10.0.dist-info/METADATA"
        ),
        "sha256": "4b0f1217de69037355b4e582905cdb303a56a488ac6fd172f148d88fed634f54",
        "byte_count": 31092,
    },
    "transformers": {
        "version": "5.6.0",
        "path": (
            "/shared/linqiang/MobileWorld/vllm_env/lib/python3.12/site-packages/"
            "transformers-5.6.0.dist-info/METADATA"
        ),
        "sha256": "cafac7e675b7e0c8610949f16f9f8b0cb6dda63193bf727e3ad27fdccc706ce9",
        "byte_count": 33162,
    },
    "flashinfer-python": {
        "version": "0.6.6",
        "path": (
            "/shared/linqiang/MobileWorld/vllm_env/lib/python3.12/site-packages/"
            "flashinfer_python-0.6.6.dist-info/METADATA"
        ),
        "sha256": "b19f7523ce21a141359cb83ab3cdcc2883ba59158ea0a03e3c95eff5a71171f1",
        "byte_count": 11016,
    },
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _commands() -> list[tuple[str, list[str]]]:
    pytest = [PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    return [
        (
            "simple_smoke_pytest",
            [*pytest, "MobileWorld/tests/offline/test_g1_gpu_smoke_simple.py"],
        ),
        (
            "manifest_verifier_pytest",
            [*pytest, "MobileWorld/tests/offline/test_g1_4_engineering_close_manifest.py"],
        ),
        (
            "history_codec_pytest",
            [*pytest, "MobileWorld/tests/offline/test_g1_history_codecs.py"],
        ),
        ("ruff_check", [PYTHON, "-m", "ruff", "check", *PYTHON_FILES]),
        ("ruff_format_check", [PYTHON, "-m", "ruff", "format", "--check", *PYTHON_FILES]),
        ("python_compile", [PYTHON, "-m", "py_compile", *PYTHON_FILES]),
        (
            "schema_meta_validation",
            [
                PYTHON,
                "-c",
                (
                    "import json,pathlib;from jsonschema import Draft202012Validator;"
                    f"s=json.loads(pathlib.Path('{SCHEMA}').read_text());"
                    "Draft202012Validator.check_schema(s);print('schema-meta-pass')"
                ),
            ],
        ),
        (
            "git_diff_check",
            [
                "/usr/bin/git",
                "-c",
                "core.fsmonitor=",
                "-c",
                "core.hooksPath=/dev/null",
                "diff",
                "--check",
            ],
        ),
    ]


def _run_command(name: str, argv: list[str]) -> dict[str, object]:
    result = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env=BASE_ENVIRONMENT,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or result.stderr:
        raise RuntimeError(f"CPU gate failed: {name}")
    return {
        "name": name,
        "argv": argv,
        "cwd": str(REPO_ROOT),
        "environment": BASE_ENVIRONMENT,
        "return_code": result.returncode,
        "stdout_base64": base64.b64encode(result.stdout).decode(),
        "stdout_sha256": _sha256(result.stdout),
        "stderr_base64": base64.b64encode(result.stderr).decode(),
        "stderr_sha256": _sha256(result.stderr),
    }


def _test_count(command: dict[str, object]) -> int:
    stdout = base64.b64decode(command["stdout_base64"], validate=True).decode()
    match = re.fullmatch(r"[.\s\[\]%0-9]+\n(\d+) passed in [0-9.]+s\n", stdout)
    if match is None:
        raise RuntimeError(f"pytest output is not recognized: {command['name']}")
    return int(match.group(1))


def _package_receipt() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name, expected in PACKAGE_METADATA.items():
        path = Path(str(expected["path"]))
        raw = path.read_bytes()
        if len(raw) != expected["byte_count"] or _sha256(raw) != expected["sha256"]:
            raise RuntimeError(f"package metadata binding changed: {name}")
        result[name] = expected
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", arguments.source_commit):
        raise RuntimeError("source commit must be a full lowercase SHA-1")
    head = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "rev-parse",
            "HEAD",
        ],
        cwd=REPO_ROOT,
        env=BASE_ENVIRONMENT,
        check=False,
        capture_output=True,
    )
    status = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        cwd=REPO_ROOT,
        env=BASE_ENVIRONMENT,
        check=False,
        capture_output=True,
    )
    if (
        head.returncode != 0
        or head.stderr
        or head.stdout != f"{arguments.source_commit}\n".encode()
        or status.returncode != 0
        or status.stderr
        or status.stdout
    ):
        raise RuntimeError("CPU gates require the exact clean source commit")
    commands = [_run_command(name, argv) for name, argv in _commands()]
    counts = [_test_count(command) for command in commands[:3]]
    if counts != [23, 32, 28]:
        raise RuntimeError(f"unexpected focused test counts: {counts}")
    receipt = {
        "schema_version": "mobileworld.g1.engineering-close-validation/v1",
        "source_commit": arguments.source_commit,
        "simple_smoke_test_count": counts[0],
        "manifest_verifier_test_count": counts[1],
        "history_codec_test_count": counts[2],
        "ruff_check_passed": True,
        "ruff_format_check_passed": True,
        "python_compile_passed": True,
        "schema_meta_validation_passed": True,
        "git_diff_check_passed": True,
        "post_hoc_runtime_packages": _package_receipt(),
        "commands": commands,
    }
    raw = (
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(json.dumps({"byte_count": len(raw), "sha256": _sha256(raw)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
