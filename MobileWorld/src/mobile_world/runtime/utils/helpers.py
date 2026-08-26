from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta

from loguru import logger
from pydantic import BaseModel

DEFAULT_ADB_TIMEOUT_SECONDS = 60.0
ADB_TERMINATION_GRACE_SECONDS = 2.0


class AdbResponse(BaseModel):
    """Response model for ADB command execution."""

    success: bool
    output: str = ""
    error: str = ""
    return_code: int = 0
    command: str = ""

    def __str__(self) -> str:
        """Return output string for backward compatibility."""
        return self.output if self.success else "ERROR"

    def __bool__(self) -> bool:
        """Allow boolean checks for success."""
        return self.success

    def __eq__(self, other: object) -> bool:
        """Support comparison with 'ERROR' string for backward compatibility."""
        if isinstance(other, str):
            if other == "ERROR":
                return not self.success
            return self.output == other
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        """Support != comparison."""
        return not self.__eq__(other)


def mask_api_key(key: str | None) -> str:
    """Mask an API key for safe logging, showing only the first 4 and last 4 characters."""
    if not key:
        return "<empty>"
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def time_within_ten_secs(time1: str | AdbResponse, time2: str | AdbResponse):
    """Compare two time strings or AdbResponse objects to check if within 10 seconds."""

    def parse_time(t: str | AdbResponse):
        if isinstance(t, AdbResponse):
            if not t.success:
                raise ValueError(f"Cannot parse time from failed command: {t.error}")
            t_str = t.output
        else:
            t_str = t

        if "+" in t_str:
            t_str = t_str.split()[1]
            t_str = t_str.split(".")[0] + "." + t_str.split(".")[1][:6]  # 仅保留到微秒
            format = "%H:%M:%S.%f"
        else:
            format = "%H:%M:%S"
        return datetime.strptime(t_str, format)

    # 解析两个时间
    time1_parsed = parse_time(time1)
    time2_parsed = parse_time(time2)

    # 计算时间差并判断
    time_difference = abs(time1_parsed - time2_parsed)

    return time_difference <= timedelta(seconds=10)


def pretty_print_messages(messages: list[dict], max_messages: int = 2) -> None:
    """
    Pretty print messages with base64 images replaced and limiting to recent messages.

    Args:
        messages: List of message dictionaries with 'role' and 'content' fields
        max_messages: Maximum number of recent messages to display (default: 2)
    """

    messages_print = copy.deepcopy(messages)

    final_str = ""

    if len(messages_print) > max_messages:
        omitted_count = len(messages_print) - max_messages
        messages_print = messages_print[-max_messages:]
        final_str += f"\n[... {omitted_count} earlier message(s) omitted ...]\n"

    for message in messages_print:
        if "content" in message and isinstance(message["content"], list):
            for content_item in message["content"]:
                if isinstance(content_item, dict):
                    if "image_url" in content_item and "url" in content_item["image_url"]:
                        url = content_item["image_url"]["url"]
                        if url.startswith("data:image/") and "base64," in url:
                            content_item["image_url"]["url"] = "[IMAGE_BASE64]"

    final_str += f"messages:\n{json.dumps(messages_print, indent=2, ensure_ascii=False)}"
    logger.info(final_str)


def execute_adb(
    adb_command: str,
    output: bool = True,
    root_required=False,
    timeout_seconds: float | None = DEFAULT_ADB_TIMEOUT_SECONDS,
) -> AdbResponse:
    if not adb_command.startswith("adb "):
        adb_command = "adb " + adb_command
    env = os.environ.copy()
    started_at = time.monotonic()

    def timeout_response() -> AdbResponse:
        timeout_description = (
            f"{timeout_seconds:g}s" if timeout_seconds is not None else "the configured deadline"
        )
        error = f"ADB command timed out after {timeout_description}"
        if output:
            logger.error(f"Command execution failed: {adb_command}")
            logger.error(error)
        return AdbResponse(
            success=False,
            error=error,
            return_code=-1,
            command=adb_command,
        )

    def terminate_process_group(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=ADB_TERMINATION_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()

    def run(command: str) -> subprocess.CompletedProcess[str] | None:
        remaining = timeout_seconds
        if timeout_seconds is not None:
            remaining = timeout_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                return None
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError:
            raise
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            terminate_process_group(process)
            return None
        return subprocess.CompletedProcess(
            args=command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    if root_required:
        whoami_check = run("adb shell whoami")
        if whoami_check is None:
            return timeout_response()
        if whoami_check.returncode == 0 and whoami_check.stdout.strip() != "root":
            root_attempt = run("adb root")
            if root_attempt is None:
                return timeout_response()
            if root_attempt.returncode != 0:
                if output:
                    logger.error("Failed to gain root access to the emulator")
                    logger.error(root_attempt.stderr)
                return AdbResponse(
                    success=False,
                    error=root_attempt.stderr or "Failed to gain root access",
                    return_code=root_attempt.returncode,
                    command=adb_command,
                )

            verify_check = run("adb shell whoami")
            if verify_check is None:
                return timeout_response()
            if verify_check.returncode != 0 or verify_check.stdout.strip() != "root":
                if output:
                    logger.error("Root permission required but not available on the emulator")
                return AdbResponse(
                    success=False,
                    error="Root permission required but not available on the emulator",
                    return_code=verify_check.returncode,
                    command=adb_command,
                )

    result = run(adb_command)
    if result is None:
        return timeout_response()
    if result.returncode == 0:
        return AdbResponse(
            success=True,
            output=result.stdout.strip(),
            return_code=result.returncode,
            command=adb_command,
        )
    if output:
        logger.error(f"Command execution failed: {adb_command}")
        logger.error(result.stderr)
    return AdbResponse(
        success=False,
        error=result.stderr or "Command execution failed",
        return_code=result.returncode,
        command=adb_command,
    )


def execute_root_sql(db_path: str, sql_query: str) -> str:
    """
    Execute a SQL query that requires root access.
    """

    adb_commands = [
        f"adb shell \"su 0 sqlite3 {db_path} '{sql_query}'\"",
        f"adb shell \"su root sqlite3 {db_path} '{sql_query}'\"",
        f'adb shell su 0 sqlite3 {db_path} "{sql_query}"',
    ]

    for adb_command in adb_commands:
        result = execute_adb(adb_command, output=False)
        if result.success and result.output and "error" not in result.output.lower():
            return result.output

    return None
