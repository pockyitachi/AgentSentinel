from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path


def _script() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "install_r2_4_openai_secret.py"


def _run(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(_script()),
            "--source-env",
            str(source),
            "--environment-key",
            "OPENAI_API_KEY",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_installer_writes_fresh_raw_owner_only_secret_without_reporting_value(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "source.env"
    secret = "sk-test-this-is-not-a-real-key"
    source.write_text(f"IGNORED=value\nexport OPENAI_API_KEY='{secret}'\n", encoding="utf-8")
    source.chmod(0o600)
    output = tmp_path / "openai-key.raw"

    result = _run(source, output)

    assert result.returncode == 0
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert result.stdout.strip() == '{"ok": true, "secret_installed": true}'
    assert result.stderr == ""
    assert output.read_bytes() == secret.encode()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_installer_rejects_duplicate_key_without_creating_output(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    source = tmp_path / "source.env"
    source.write_text("OPENAI_API_KEY=first\nOPENAI_API_KEY=second\n", encoding="utf-8")
    source.chmod(0o600)
    output = tmp_path / "openai-key.raw"

    result = _run(source, output)

    assert result.returncode == 2
    assert "SOURCE_SECRET_KEY_MISSING_OR_DUPLICATE" in result.stderr
    assert not output.exists()
