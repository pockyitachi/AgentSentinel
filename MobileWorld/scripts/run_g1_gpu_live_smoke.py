#!/usr/bin/env python3
"""Validate or explicitly execute the D-034 synthetic GPU live-smoke proof."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import hashlib
import json
import os
import resource
import select
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

# The client environment is editable-installed against the main checkout.  Pin
# imports to this exact CLI's sibling source tree before importing MobileWorld;
# ``-I`` then cannot drift back to an unrelated editable checkout.
_SCRIPT_PATH = Path(__file__).resolve()
_SOURCE_ROOT = _SCRIPT_PATH.parents[1] / "src"

JsonValue = Any

_EXECUTE_CONFIRMATION = "EXECUTE-D034-SYNTHETIC-22-CALL-SMOKE"
_FD_CLOSE_UPPER_BOUND_EXCLUSIVE = 1_048_576
_FORBIDDEN_PRE_UNSHARE_MODULE_ROOTS = ("mobile_world", "loguru", "PIL", "openai")
_STAGE1_RECEIPT_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-stage1-preexec/v1"
_OWNED_COMMAND_GATE_CODE = (
    "import base64,json,os,sys;"
    "fd=int(sys.argv[1]);"
    "env=json.loads(base64.urlsafe_b64decode(sys.argv[2]).decode('utf-8'));"
    "argv=sys.argv[3:];"
    "token=os.read(fd,1);os.close(fd);"
    "os.execve(argv[0],argv,env) if token==b'G' else os._exit(125)"
)
_SCRATCH_DIRECTORY_NAMES = {
    "HOME": "home",
    "HF_HOME": "hf-home",
    "XDG_CACHE_HOME": "xdg-cache",
    "TORCH_HOME": "torch-home",
    "TRITON_CACHE_DIR": "triton-cache",
    "VLLM_CACHE_ROOT": "vllm-cache",
    "TMPDIR": "tmp",
}
_OUTER_STDLIB_BOOTSTRAP_CODE = """\
import hashlib,json,os,stat,sys
def read_exact(path,limit):
    if not path.startswith('/') or os.path.normpath(path)!=path or '..' in path.split('/'):
        raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
    parts=path.split('/')[1:];directory_fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=directory_fd);os.close(directory_fd);directory_fd=following
        fd=os.open(parts[-1],os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=limit:
            raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
        chunks=[]
        left=before.st_size
        while left:
            chunk=os.read(fd,min(left,1048576))
            if not chunk:
                raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
            chunks.append(chunk);left-=len(chunk)
        after=os.fstat(fd)
    finally:
        os.close(fd)
    identity=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
    if identity(before)!=identity(after):
        raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
    return b''.join(chunks),before
if not (sys.flags.isolated==1 and sys.flags.ignore_environment==1 and sys.flags.no_site==1 and sys.flags.dont_write_bytecode==1 and sys.pycache_prefix=='/dev/null'):
    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
stage,authority_path,authority_sha,bootstrap_sha,cli_path,stage_payload,*cli_args=sys.argv[1:]
internal_flags={'--inside-network-namespace-stage1','--inside-network-namespace','--namespace-sandboxed','--stage1-receipt-b64','--stage0-bootstrap-sha256','--pinned-bootstrap-stage'}
if stage not in {'STAGE0','STAGE1','STAGE2'} or any(any(item==flag or item.startswith(flag+'=') for flag in internal_flags) for item in cli_args):
    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
authority_bytes,authority_meta=read_exact(authority_path,131072)
if hashlib.sha256(authority_bytes).hexdigest()!=authority_sha:
    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
def closed_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate')
        result[key]=value
    return result
authority=json.loads(authority_bytes,object_pairs_hook=closed_pairs)
canonical=json.dumps(authority,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
namespace=authority['network_namespace'];source=authority['source']
expected_environment={'LC_CTYPE':'C.UTF-8'} if stage in {'STAGE0','STAGE1'} else namespace['launcher_environment']
expected_uid=namespace['host_owner_uid'] if stage=='STAGE0' else namespace['inside_owner_uid']
expected_gid=namespace['host_owner_gid'] if stage=='STAGE0' else namespace['inside_owner_gid']
if not (canonical==authority_bytes and dict(os.environ)==expected_environment and authority_meta.st_uid==expected_uid and authority_meta.st_gid==expected_gid and stat.S_IMODE(authority_meta.st_mode)==0o600 and authority_meta.st_nlink==1 and bootstrap_sha==source['outer_bootstrap_code_sha256']):
    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
binding=source['critical_files']['runner_cli']
expected_cli=source['worktree_root']+'/'+binding['relative_path']
cli_bytes,cli_meta=read_exact(cli_path,1048576)
if not (cli_path==expected_cli and hashlib.sha256(cli_bytes).hexdigest()==binding['sha256'] and cli_meta.st_uid==expected_uid and cli_meta.st_gid==expected_gid and cli_meta.st_nlink==1 and stat.S_IMODE(cli_meta.st_mode)&0o022==0):
    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
stage_args={'STAGE0':[],'STAGE1':['--inside-network-namespace-stage1'],'STAGE2':['--inside-network-namespace','--namespace-sandboxed','--stage1-receipt-b64',stage_payload]}[stage]
if (stage in {'STAGE0','STAGE1'} and stage_payload!='-') or (stage=='STAGE2' and not 0<len(stage_payload)<=262144):
    raise SystemExit('GPU_SMOKE_SOURCE_BINDING_INVALID')
sys.argv=[cli_path,*cli_args,'--stage0-bootstrap-sha256',bootstrap_sha,'--pinned-bootstrap-stage',stage,*stage_args]
scope={'__name__':'__main__','__file__':cli_path,'__package__':None,'__cached__':None,'__builtins__':__builtins__,'_PINNED_BOOTSTRAP':{'authority_sha256':authority_sha,'cli_sha256':binding['sha256'],'bootstrap_sha256':bootstrap_sha,'stage':stage,'cli_opened_nofollow':True,'cli_compiled_from_verified_bytes':True}}
exec(compile(cli_bytes,cli_path,'exec'),scope,scope)
"""
_OUTER_AUTHORITY_KEYS = {
    "schema_version",
    "authority_id",
    "decision_id",
    "authorized_scope",
    "authorized",
    "issued_at_utc",
    "expires_at_utc",
    "owner_uid",
    "gpu",
    "endpoint",
    "model_order",
    "models",
    "outer_runtime",
    "private_runtime",
    "client_runtime",
    "server_runtime",
    "bindings",
    "matrix",
    "policies",
    "source",
    "network_namespace",
    "evidence_root",
    "runtime_scratch_root",
}
_OUTER_NETWORK_NAMESPACE_KEYS = {
    "required",
    "implementation",
    "host_owner_uid",
    "host_owner_gid",
    "inside_owner_uid",
    "inside_owner_gid",
    "uid_map_line",
    "gid_map_line",
    "env_path",
    "env_sha256",
    "unshare_path",
    "unshare_sha256",
    "ip_path",
    "ip_sha256",
    "setpriv_path",
    "setpriv_sha256",
    "nvidia_smi_path",
    "nvidia_smi_sha256",
    "nvidia_smi_byte_count",
    "pre_namespace_environment",
    "launcher_environment",
    "expected_interfaces",
    "loopback_up_required",
    "default_route_allowed",
    "external_network_allowed",
    "python_pycache_prefix",
    "fd_close_upper_bound_exclusive",
    "outer_fd_closure_receipt_sha256",
}
_OUTER_LAUNCHER_ENVIRONMENT_KEYS = {
    "PATH",
    "CUDA_VISIBLE_DEVICES",
    "LD_LIBRARY_PATH",
    "HOME",
    "HF_HOME",
    "XDG_CACHE_HOME",
    "TORCH_HOME",
    "TRITON_CACHE_DIR",
    "VLLM_CACHE_ROOT",
    "TMPDIR",
    "PYTHONNOUSERSITE",
    "LC_ALL",
    "LANG",
    "GPU_SMOKE_OUTER_FD_CLOSURE_SHA256",
}
_OUTER_PRE_NAMESPACE_ENVIRONMENT_KEYS = {"LC_CTYPE"}
_OUTER_RUNTIME_KEYS = {
    "python_path",
    "python_resolved_path",
    "python_sha256",
    "python_byte_count",
    "python_version",
    "python_flags",
    "stdlib_root",
    "stdlib_tree_sha256",
    "stdlib_tree_entry_count",
    "stdlib_tree_byte_count",
    "required_owner_uid",
    "required_owner_gid",
    "directory_mode",
    "regular_mode",
    "executable_mode",
    "symlinks_allowed",
    "hardlinks_allowed",
}
_OUTER_PRIVATE_RUNTIME_KEYS = {
    "root",
    "python_path",
    "python_resolved_path",
    "python_sha256",
    "python_byte_count",
    "python_version",
    "python_flags",
    "stdlib_root",
    "tree_sha256",
    "tree_entry_count",
    "tree_byte_count",
    "owner_uid",
    "owner_gid",
    "directory_mode",
    "regular_mode",
    "executable_mode",
    "symlinks_allowed",
    "hardlinks_allowed",
}
_OUTER_CLIENT_RUNTIME_KEYS = {
    "python_path",
    "python_resolved_path",
    "python_sha256",
    "site_packages_path",
    "openai_version",
    "site_packages_tree_sha256",
    "site_packages_tree_entry_count",
    "site_packages_tree_byte_count",
    "site_packages_owner_uid",
    "site_packages_owner_gid",
    "site_packages_directory_mode",
    "site_packages_regular_mode",
    "site_packages_executable_mode",
    "site_packages_symlinks_allowed",
    "site_packages_hardlinks_allowed",
}
_OUTER_SERVER_RUNTIME_KEYS = _OUTER_CLIENT_RUNTIME_KEYS | {"vllm_version", "torch_version"}
_OUTER_SOURCE_KEYS = {
    "worktree_root",
    "source_root",
    "git_path",
    "git_sha256",
    "head_commit",
    "critical_files",
    "bootstrap_manifest_sha256",
    "source_tree_sha256",
    "source_tree_entry_count",
    "source_tree_byte_count",
    "outer_bootstrap_code_sha256",
    "outer_bootstrap_code_byte_count",
}
_CRITICAL_SOURCE_FILES = {
    "mobile_world_init": "MobileWorld/src/mobile_world/__init__.py",
    "gpu_live_smoke": "MobileWorld/src/mobile_world/offline/gpu_live_smoke.py",
    "causal_replay_contracts": "MobileWorld/src/mobile_world/offline/causal_replay/contracts.py",
    "causal_replay_core": "MobileWorld/src/mobile_world/offline/causal_replay/core.py",
    "causal_replay_registry": "MobileWorld/src/mobile_world/offline/causal_replay/registry.py",
    "live_preparation": (
        "MobileWorld/src/mobile_world/offline/causal_replay_runner/live_preparation.py"
    ),
    "history_codecs": "MobileWorld/src/mobile_world/offline/g1_history_codecs/codecs.py",
    "runner_cli": "MobileWorld/scripts/run_g1_gpu_live_smoke.py",
}
_PREIMPORT_RUNTIME_RECEIPT: dict[str, object] | None = None
_STAGE1_PREEXEC_RECEIPT: dict[str, object] | None = None


def _canonical_json_bytes_stdlib(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _read_authority_stdlib(args: argparse.Namespace) -> dict[str, object]:
    authority_path = Path(args.authority)
    if not (
        authority_path.is_absolute()
        and str(authority_path) == args.authority
        and ".." not in authority_path.parts
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    try:
        fd = os.open(authority_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 128 * 1024:
                raise ValueError("authority size/type invalid")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    raise ValueError("authority truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        data = b"".join(chunks)
        if (
            before_identity != after_identity
            or hashlib.sha256(data).hexdigest() != args.authority_sha256
        ):
            raise ValueError("authority digest/identity differs")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate authority key")
                result[key] = value
            return result

        value = json.loads(data, object_pairs_hook=reject_duplicates)
        if type(value) is not dict or data != _canonical_json_bytes_stdlib(value):
            raise ValueError("authority is not canonical")
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID") from exc


def _hash_regular_nofollow_stdlib(path: str, expected_sha256: object) -> int:
    if not (
        path.startswith("/")
        and str(Path(path)) == path
        and ".." not in Path(path).parts
        and type(expected_sha256) is str
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("bound tool is not regular")
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("bound tool truncated")
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)

        def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if identity(before) != identity(after) or digest.hexdigest() != expected_sha256:
            raise ValueError("bound tool digest differs")
        return before.st_size
    except (OSError, ValueError) as exc:
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID") from exc


def _digest_regular_nofollow_stdlib(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if identity(before) != identity(after):
            raise RuntimeError("GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE")
        return digest.hexdigest()
    finally:
        os.close(fd)


class _OwnedCommandStdlibError(RuntimeError):
    def __init__(self, code: str, receipt: dict[str, object]) -> None:
        super().__init__(code)
        self.receipt = receipt


def _owned_pidfd_open_stdlib(pid: int) -> int:
    if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
        raise RuntimeError("GPU_SMOKE_RUNTIME_INVALID")
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    result = syscall(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0))
    if result < 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ESRCH:
            return -1
        raise OSError(error_number, os.strerror(error_number))
    return int(result)


def _owned_pidfd_send_signal_stdlib(pidfd: int, sig: signal.Signals) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    result = syscall(
        ctypes.c_long(424),
        ctypes.c_int(pidfd),
        ctypes.c_int(int(sig)),
        ctypes.c_void_p(),
        ctypes.c_uint(0),
    )
    if result < 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ESRCH:
            return False
        raise OSError(error_number, os.strerror(error_number))
    return True


def _owned_pidfd_live_stdlib(pidfd: int) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    return not poller.poll(0)


def _read_proc_at_stdlib(procdir_fd: int, name: str, maximum_bytes: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=procdir_fd)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise RuntimeError("GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _minimal_proc_identity_stdlib(procdir_fd: int, pid: int) -> dict[str, int]:
    text = _read_proc_at_stdlib(procdir_fd, "stat", 64 * 1024).decode("utf-8")
    close = text.rfind(")")
    fields = text[close + 2 :].split()
    if close < 1 or len(fields) < 20:
        raise RuntimeError("GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE")
    return {
        "uid": os.fstat(procdir_fd).st_uid,
        "pid": pid,
        "ppid": int(fields[1]),
        "pgid": int(fields[2]),
        "sid": int(fields[3]),
        "starttime_ticks": int(fields[19]),
    }


def _full_proc_identity_stdlib(
    procdir_fd: int,
    minimal: dict[str, int],
) -> dict[str, object]:
    raw_argv = _read_proc_at_stdlib(procdir_fd, "cmdline", 4 * 1024 * 1024)
    argv = [
        item.decode("utf-8", errors="surrogateescape") for item in raw_argv.split(b"\0") if item
    ]
    if not argv:
        raise RuntimeError("GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE")
    executable_path = os.readlink("exe", dir_fd=procdir_fd)
    executable_fd = os.open("exe", os.O_RDONLY | os.O_CLOEXEC, dir_fd=procdir_fd)
    try:
        metadata = os.fstat(executable_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE")
        digest = hashlib.sha256()
        os.lseek(executable_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(executable_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(executable_fd)
    identity: dict[str, object] = dict(minimal)
    identity.update(
        {
            "executable_path": executable_path,
            "executable_sha256": digest.hexdigest(),
            "argv": argv,
            "argv_sha256": hashlib.sha256(_canonical_json_bytes_stdlib(argv)).hexdigest(),
        }
    )
    return identity


def _minimal_matches_stdlib(
    actual: dict[str, int],
    expected: dict[str, object],
    *,
    allow_ppid_change: bool,
) -> bool:
    return all(
        actual[key] == expected[key] for key in ("uid", "pid", "pgid", "sid", "starttime_ticks")
    ) and (allow_ppid_change or actual["ppid"] == expected["ppid"])


def _revalidate_owned_member_stdlib(
    member: dict[str, object],
    *,
    full: bool,
    allow_ppid_change: bool,
) -> dict[str, object] | None:
    pidfd = cast(int, member["pidfd"])
    procdir_fd = cast(int, member["procdir_fd"])
    identity = cast(dict[str, object], member["identity"])
    if not _owned_pidfd_live_stdlib(pidfd):
        return None
    if os.fstat(procdir_fd).st_uid != identity["uid"]:
        raise RuntimeError("GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH")
    before = _minimal_proc_identity_stdlib(procdir_fd, cast(int, identity["pid"]))
    if not _minimal_matches_stdlib(before, identity, allow_ppid_change=allow_ppid_change):
        raise RuntimeError("GPU_SMOKE_PROCESS_TREE_DRIFT")
    if _minimal_proc_identity_stdlib(procdir_fd, cast(int, identity["pid"])) != before:
        raise RuntimeError("GPU_SMOKE_PROCESS_TREE_DRIFT")
    if not full:
        return cast(dict[str, object], before)
    current = _full_proc_identity_stdlib(procdir_fd, before)
    original_matches = all(current[key] == identity[key] for key in identity) or (
        allow_ppid_change
        and all(current[key] == identity[key] for key in identity if key != "ppid")
    )
    allowed_matches = (
        type(member.get("allowed_exec_argv")) is list
        and current["argv"] == member["allowed_exec_argv"]
        and current["executable_path"] == member.get("allowed_exec_path")
        and current["executable_sha256"] == member.get("allowed_exec_sha256")
        and all(
            current[key] == identity[key]
            for key in ("uid", "pid", "pgid", "sid", "starttime_ticks")
        )
        and (allow_ppid_change or current["ppid"] == identity["ppid"])
    )
    if _minimal_proc_identity_stdlib(procdir_fd, cast(int, identity["pid"])) != before or not (
        original_matches or allowed_matches
    ):
        raise RuntimeError("GPU_SMOKE_PROCESS_TREE_DRIFT")
    return current


def _pinned_children_stdlib(member: dict[str, object]) -> tuple[int, ...]:
    if _revalidate_owned_member_stdlib(member, full=False, allow_ppid_change=False) is None:
        return ()
    procdir_fd = cast(int, member["procdir_fd"])
    task_fd = os.open(
        "task",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=procdir_fd,
    )
    try:
        children: set[int] = set()
        for task_name in sorted(os.listdir(task_fd)):
            if not task_name.isdecimal():
                continue
            try:
                thread_fd = os.open(
                    task_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=task_fd,
                )
            except FileNotFoundError:
                continue
            try:
                raw = _read_proc_at_stdlib(thread_fd, "children", 1024 * 1024).decode("ascii")
            finally:
                os.close(thread_fd)
            children.update(int(item) for item in raw.split())
        if any(pid <= 1 for pid in children):
            raise RuntimeError("GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE")
        _revalidate_owned_member_stdlib(member, full=False, allow_ppid_change=False)
        return tuple(sorted(children))
    finally:
        os.close(task_fd)


def _capture_owned_child_stdlib(
    child_pid: int,
    parent: dict[str, object],
    root: dict[str, object],
) -> dict[str, object] | None:
    pidfd = _owned_pidfd_open_stdlib(child_pid)
    if pidfd < 0:
        return None
    try:
        procdir_fd = os.open(
            f"/proc/{child_pid}",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        os.close(pidfd)
        return None
    try:
        root_identity = cast(dict[str, object], root["identity"])
        parent_identity = cast(dict[str, object], parent["identity"])
        if os.fstat(procdir_fd).st_uid != root_identity["uid"]:
            raise RuntimeError("GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH")
        minimal = _minimal_proc_identity_stdlib(procdir_fd, child_pid)
        if not (
            minimal["uid"] == root_identity["uid"]
            and minimal["ppid"] == parent_identity["pid"]
            and minimal["sid"] == root_identity["sid"]
            and minimal["starttime_ticks"] >= root_identity["starttime_ticks"]
            and _minimal_proc_identity_stdlib(procdir_fd, child_pid) == minimal
        ):
            raise RuntimeError("GPU_SMOKE_PROCESS_TREE_DRIFT")
        identity = _full_proc_identity_stdlib(procdir_fd, minimal)
        if _minimal_proc_identity_stdlib(procdir_fd, child_pid) != minimal:
            raise RuntimeError("GPU_SMOKE_PROCESS_TREE_DRIFT")
        return {
            "identity": identity,
            "pidfd": pidfd,
            "procdir_fd": procdir_fd,
            "depth": cast(int, parent["depth"]) + 1,
        }
    except BaseException:
        os.close(procdir_fd)
        os.close(pidfd)
        raise


def _observe_owned_descendants_stdlib(members: dict[int, dict[str, object]]) -> bool:
    root = min(members.values(), key=lambda item: cast(int, item["depth"]))
    discovered = False
    queue = sorted(members.values(), key=lambda item: cast(int, item["depth"]))
    while queue:
        parent = queue.pop(0)
        for child_pid in _pinned_children_stdlib(parent):
            if child_pid in members:
                continue
            child = _capture_owned_child_stdlib(child_pid, parent, root)
            if child is None:
                continue
            members[child_pid] = child
            queue.append(child)
            discovered = True
    return discovered


def _terminate_owned_members_stdlib(
    members: dict[int, dict[str, object]],
    trace: list[dict[str, object]],
    reason: str,
) -> bool:
    for member in sorted(
        members.values(),
        key=lambda item: (
            cast(int, item["depth"]),
            cast(dict[str, object], item["identity"])["pid"],
        ),
        reverse=True,
    ):
        identity = cast(dict[str, object], member["identity"])
        base = {
            "pid": identity["pid"],
            "starttime_ticks": identity["starttime_ticks"],
            "signal": "SIGKILL",
            "signal_api": "PIDFD",
            "ownership": "RECORDED_OWN_ACQUISITION",
            "reason": reason,
            "scope": "OWNED_AUXILIARY_COMMAND",
        }
        trace.append({"sequence": len(trace) + 1, **base, "state": "INTENDED"})
        # Auxiliary descendants may exec after their ancestry and retained
        # pidfd/procdir were proven.  Cleanup therefore revalidates only the
        # stable minimal tuple; argv/executable drift cannot retarget pidfd.
        if _revalidate_owned_member_stdlib(member, full=False, allow_ppid_change=True) is None:
            trace.append({"sequence": len(trace) + 1, **base, "state": "EXITED_BEFORE_SIGNAL"})
            continue
        trace.append({"sequence": len(trace) + 1, **base, "state": "IDENTITY_REVALIDATED"})
        state = (
            "SENT"
            if _owned_pidfd_send_signal_stdlib(cast(int, member["pidfd"]), signal.SIGKILL)
            else "EXITED_BEFORE_SIGNAL"
        )
        trace.append({"sequence": len(trace) + 1, **base, "state": state})
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if all(not _owned_pidfd_live_stdlib(cast(int, item["pidfd"])) for item in members.values()):
            return True
        time.sleep(0.005)
    return all(not _owned_pidfd_live_stdlib(cast(int, item["pidfd"])) for item in members.values())


def _owned_command_receipt_stdlib(
    command: tuple[str, ...],
    timeout_seconds: float,
    stdout_cap: int,
    stderr_cap: int,
    stdout: bytes,
    stderr: bytes,
    root: dict[str, object] | None,
    members: dict[int, dict[str, object]],
    initial_pidfd: bool,
    reason: str,
    returncode: int | None,
    trace: list[dict[str, object]],
    release_proven: bool,
) -> dict[str, object]:
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-owned-command/v1",
        "command": list(command),
        "command_sha256": hashlib.sha256(_canonical_json_bytes_stdlib(list(command))).hexdigest(),
        "executable_path": command[0],
        "timeout_milliseconds": int(timeout_seconds * 1000),
        "stdout_byte_cap": stdout_cap,
        "stderr_byte_cap": stderr_cap,
        "stdout_byte_count": len(stdout),
        "stderr_byte_count": len(stderr),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "initial_pidfd_acquired": initial_pidfd,
        "launch_gate_identity": (
            cast(dict[str, object], root["identity"]) if root is not None else None
        ),
        "launch_gate_code_sha256": hashlib.sha256(
            _OWNED_COMMAND_GATE_CODE.encode("utf-8")
        ).hexdigest(),
        "launch_gate_released_after_identity_proof": (
            root is not None and root.get("launch_gate_released") is True
        ),
        "target_executable_path": (
            root.get("allowed_exec_path") if root is not None else os.path.realpath(command[0])
        ),
        "target_executable_sha256": (root.get("allowed_exec_sha256") if root is not None else None),
        "descendant_policy": "FORBIDDEN",
        "observed_descendant_count": max(0, len(members) - (1 if root is not None else 0)),
        "observed_descendant_identities": [
            cast(dict[str, object], item["identity"])
            for item in sorted(members.values(), key=lambda value: cast(int, value["depth"]))
            if cast(int, item["depth"]) > 0
        ],
        "descendant_census_max_interval_milliseconds": 10,
        "completion_reason": reason,
        "returncode": returncode,
        "signal_trace": trace,
        "release_proven": release_proven,
        "numeric_pid_signal_count": 0,
        "popen_kill_count": 0,
        "popen_send_signal_count": 0,
        "communicate_timeout_count": 0,
    }


def _run_owned_command_stdlib(
    command: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
    error_code: str,
    stdout_byte_cap: int = 4 * 1024 * 1024,
    stderr_byte_cap: int = 4 * 1024 * 1024,
) -> dict[str, object]:
    frozen = tuple(command)
    if not (
        frozen
        and Path(frozen[0]).is_absolute()
        and all(type(item) is str and item for item in frozen)
        and 0 < timeout_seconds <= 600
        and 0 < stdout_byte_cap <= 64 * 1024 * 1024
        and 0 < stderr_byte_cap <= 64 * 1024 * 1024
    ):
        raise RuntimeError(error_code)
    target_environment_b64 = base64.urlsafe_b64encode(_canonical_json_bytes_stdlib(env)).decode(
        "ascii"
    )
    gate_read_fd, gate_write_fd = os.pipe2(os.O_CLOEXEC)
    gate_command = (
        os.path.abspath(sys.executable),
        "-I",
        "-S",
        "-B",
        "-X",
        "pycache_prefix=/dev/null",
        "-c",
        _OWNED_COMMAND_GATE_CODE,
        str(gate_read_fd),
        target_environment_b64,
        *frozen,
    )
    try:
        process = subprocess.Popen(
            list(gate_command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"LC_CTYPE": "C.UTF-8"},
            close_fds=True,
            start_new_session=True,
            shell=False,
            pass_fds=(gate_read_fd,),
        )
    except OSError as exc:
        os.close(gate_read_fd)
        os.close(gate_write_fd)
        raise _OwnedCommandStdlibError(
            error_code,
            _owned_command_receipt_stdlib(
                frozen,
                timeout_seconds,
                stdout_byte_cap,
                stderr_byte_cap,
                b"",
                b"",
                None,
                {},
                False,
                "POPEN_FAILED",
                None,
                [],
                False,
            ),
        ) from exc
    os.close(gate_read_fd)
    pidfd = -1
    root: dict[str, object] | None = None
    members: dict[int, dict[str, object]] = {}
    trace: list[dict[str, object]] = []
    stdout = bytearray()
    stderr = bytearray()
    returncode: int | None = None
    reason = "INTERNAL_FAILURE"
    release_proven = False
    poller = select.poll()
    streams: dict[int, tuple[str, object]] = {}
    try:
        try:
            pidfd = _owned_pidfd_open_stdlib(process.pid)
        except BaseException as exc:
            receipt = _owned_command_receipt_stdlib(
                frozen,
                timeout_seconds,
                stdout_byte_cap,
                stderr_byte_cap,
                b"",
                b"",
                None,
                {},
                False,
                "INITIAL_PIDFD_UNAVAILABLE",
                None,
                [],
                False,
            )
            raise _OwnedCommandStdlibError(error_code, receipt) from exc
        if pidfd < 0:
            receipt = _owned_command_receipt_stdlib(
                frozen,
                timeout_seconds,
                stdout_byte_cap,
                stderr_byte_cap,
                b"",
                b"",
                None,
                {},
                False,
                "EXITED_BEFORE_PIDFD_OPEN",
                None,
                [],
                True,
            )
            raise _OwnedCommandStdlibError(error_code, receipt)
        try:
            procdir_fd = os.open(
                f"/proc/{process.pid}",
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            if os.fstat(procdir_fd).st_uid != os.getuid():
                raise RuntimeError("GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH")
            minimal = _minimal_proc_identity_stdlib(procdir_fd, process.pid)
            if not (
                minimal["uid"] == os.getuid()
                and minimal["ppid"] == os.getpid()
                and minimal["pgid"] == process.pid
                and minimal["sid"] == process.pid
                and _minimal_proc_identity_stdlib(procdir_fd, process.pid) == minimal
            ):
                raise RuntimeError("GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH")
            identity = _full_proc_identity_stdlib(procdir_fd, minimal)
            if not (
                _minimal_proc_identity_stdlib(procdir_fd, process.pid) == minimal
                and identity["argv"] == list(gate_command)
                and identity["executable_path"] == os.path.realpath(gate_command[0])
            ):
                raise RuntimeError("GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH")
            target_executable = os.path.realpath(frozen[0])
            root = {
                "identity": identity,
                "pidfd": pidfd,
                "procdir_fd": procdir_fd,
                "depth": 0,
                "allowed_exec_argv": list(frozen),
                "allowed_exec_path": target_executable,
                "allowed_exec_sha256": _digest_regular_nofollow_stdlib(target_executable),
                "launch_gate_released": False,
            }
            members[process.pid] = root
            if os.write(gate_write_fd, b"G") != 1:
                raise RuntimeError(error_code)
            root["launch_gate_released"] = True
            os.close(gate_write_fd)
            gate_write_fd = -1
        except BaseException as exc:
            release_proven = False
            if (
                root is not None
                and root.get("launch_gate_released") is True
                and _owned_pidfd_live_stdlib(cast(int, root["pidfd"]))
            ):
                release_proven = _terminate_owned_members_stdlib(
                    members,
                    trace,
                    "ACQUISITION_FAILURE_AFTER_GATE_RELEASE",
                )
            elif root is not None and not _owned_pidfd_live_stdlib(cast(int, root["pidfd"])):
                release_proven = True
            elif root is None:
                release_proven = not _owned_pidfd_live_stdlib(pidfd)
            receipt = _owned_command_receipt_stdlib(
                frozen,
                timeout_seconds,
                stdout_byte_cap,
                stderr_byte_cap,
                b"",
                b"",
                root,
                members,
                True,
                "ACQUISITION_FAILURE",
                None,
                trace,
                release_proven,
            )
            raise _OwnedCommandStdlibError(error_code, receipt) from exc
        assert process.stdout is not None and process.stderr is not None
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            poller.register(stream.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR)
            streams[stream.fileno()] = (name, stream)
        deadline = time.monotonic() + timeout_seconds
        failure: str | None = None
        while streams or _owned_pidfd_live_stdlib(pidfd):
            if _owned_pidfd_live_stdlib(pidfd) and _observe_owned_descendants_stdlib(members):
                failure = "UNEXPECTED_DESCENDANT"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "TIMEOUT"
                break
            for fd, _events in poller.poll(max(1, min(5, int(remaining * 1000)))):
                if fd not in streams:
                    continue
                name, stream = streams[fd]
                destination = stdout if name == "stdout" else stderr
                cap = stdout_byte_cap if name == "stdout" else stderr_byte_cap
                try:
                    chunk = os.read(fd, min(65_536, cap - len(destination) + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    poller.unregister(fd)
                    streams.pop(fd)
                    continue
                destination.extend(chunk)
                if len(destination) > cap:
                    del destination[cap:]
                    failure = "STDOUT_LIMIT" if name == "stdout" else "STDERR_LIMIT"
                    break
            if failure is not None:
                break
        if failure is not None:
            release_proven = _terminate_owned_members_stdlib(members, trace, failure)
            reason = failure
        else:
            release_proven = all(
                not _owned_pidfd_live_stdlib(cast(int, item["pidfd"])) for item in members.values()
            )
            reason = "EXITED"
        if not _owned_pidfd_live_stdlib(pidfd):
            returncode = process.wait()
        stdout_bytes = bytes(stdout)
        stderr_bytes = bytes(stderr)
        receipt = _owned_command_receipt_stdlib(
            frozen,
            timeout_seconds,
            stdout_byte_cap,
            stderr_byte_cap,
            stdout_bytes,
            stderr_bytes,
            root,
            members,
            True,
            reason,
            returncode,
            trace,
            release_proven,
        )
        if failure is not None or not release_proven or returncode is None:
            raise _OwnedCommandStdlibError(error_code, receipt)
        return {
            "returncode": returncode,
            "stdout": stdout_bytes,
            "stderr": stderr_bytes,
            "receipt": receipt,
        }
    except _OwnedCommandStdlibError:
        raise
    except BaseException as exc:
        if root is not None and _owned_pidfd_live_stdlib(cast(int, root["pidfd"])):
            release_proven = _terminate_owned_members_stdlib(
                members,
                trace,
                "INTERNAL_FAILURE",
            )
        elif root is not None:
            release_proven = True
        else:
            release_proven = pidfd >= 0 and not _owned_pidfd_live_stdlib(pidfd)
        receipt = _owned_command_receipt_stdlib(
            frozen,
            timeout_seconds,
            stdout_byte_cap,
            stderr_byte_cap,
            bytes(stdout),
            bytes(stderr),
            root,
            members,
            pidfd >= 0,
            "INTERNAL_FAILURE",
            returncode,
            trace,
            release_proven,
        )
        raise _OwnedCommandStdlibError(error_code, receipt) from exc
    finally:
        if gate_write_fd >= 0:
            try:
                os.close(gate_write_fd)
            except OSError:
                pass
        for _name, stream in streams.values():
            try:
                cast(Any, stream).close()
            except OSError:
                pass
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if process.stderr is not None and not process.stderr.closed:
            process.stderr.close()
        for member in members.values():
            try:
                os.close(cast(int, member["procdir_fd"]))
            except OSError:
                pass
            try:
                os.close(cast(int, member["pidfd"]))
            except OSError:
                pass
        if pidfd >= 0 and (root is None or pidfd != root["pidfd"]):
            try:
                os.close(pidfd)
            except OSError:
                pass


def _tree_identity_stdlib(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _hash_open_file_stdlib(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = os.read(fd, min(8 * 1024 * 1024, remaining))
        if not chunk:
            raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _enumerate_bound_tree_stdlib(
    root: str,
    *,
    owner_uid: int,
    owner_gid: int,
    recorded_owner_uid: int,
    recorded_owner_gid: int,
    directory_mode: int,
    regular_mode: int,
    executable_mode: int,
    symlinks_allowed: bool,
    hardlinks_allowed: bool,
    forbid_bytecode_and_pth: bool = False,
) -> dict[str, object]:
    """Mirror the production no-follow tree aggregate before any site import."""

    if not root.startswith("/") or str(Path(root)) != root or root == "/":
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID") from exc
    entries: list[dict[str, object]] = []

    def require_owner_mode(metadata: os.stat_result, mode: int) -> None:
        if not (
            metadata.st_uid == owner_uid
            and metadata.st_gid == owner_gid
            and stat.S_IMODE(metadata.st_mode) == mode
        ):
            raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")

    def walk(directory_fd: int, relative_directory: str, absolute_directory: str) -> None:
        directory_before = os.fstat(directory_fd)
        require_owner_mode(directory_before, directory_mode)
        entries.append(
            {
                "path": relative_directory or ".",
                "entry_type": "DIRECTORY",
                "mode": stat.S_IMODE(directory_before.st_mode),
                "owner_role": "AUTHORIZED_OWNER",
                "byte_count": 0,
                "sha256": None,
                "symlink_target": None,
                "resolved_target": absolute_directory,
            }
        )
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID") from exc
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
            if forbid_bytecode_and_pth and (
                name == "__pycache__" or name.endswith((".pyc", ".pyo", ".pth"))
            ):
                raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
            relative = f"{relative_directory}/{name}" if relative_directory else name
            absolute = f"{absolute_directory}/{name}"
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID") from exc
            if stat.S_ISDIR(before.st_mode):
                require_owner_mode(before, directory_mode)
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    if _tree_identity_stdlib(before) != _tree_identity_stdlib(os.fstat(child_fd)):
                        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                    walk(child_fd, relative, absolute)
                    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if _tree_identity_stdlib(before) != _tree_identity_stdlib(after):
                        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISREG(before.st_mode):
                executable = bool(stat.S_IMODE(before.st_mode) & 0o111)
                require_owner_mode(before, executable_mode if executable else regular_mode)
                if not hardlinks_allowed and before.st_nlink != 1:
                    raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                file_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(file_fd)
                    if _tree_identity_stdlib(before) != _tree_identity_stdlib(opened):
                        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                    digest = _hash_open_file_stdlib(file_fd, opened.st_size)
                    after = os.fstat(file_fd)
                finally:
                    os.close(file_fd)
                if _tree_identity_stdlib(opened) != _tree_identity_stdlib(after):
                    raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                entries.append(
                    {
                        "path": relative,
                        "entry_type": "REGULAR_FILE",
                        "mode": stat.S_IMODE(before.st_mode),
                        "owner_role": "AUTHORIZED_OWNER",
                        "byte_count": before.st_size,
                        "sha256": digest,
                        "symlink_target": None,
                        "resolved_target": absolute,
                    }
                )
                continue
            if stat.S_ISLNK(before.st_mode):
                if not symlinks_allowed or not (
                    before.st_uid == owner_uid and before.st_gid == owner_gid
                ):
                    raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                try:
                    target = os.readlink(name, dir_fd=directory_fd)
                    resolved = os.path.realpath(absolute)
                    resolved_metadata = os.stat(resolved, follow_symlinks=False)
                    if not stat.S_ISREG(resolved_metadata.st_mode):
                        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                    resolved_executable = bool(stat.S_IMODE(resolved_metadata.st_mode) & 0o111)
                    require_owner_mode(
                        resolved_metadata,
                        executable_mode if resolved_executable else regular_mode,
                    )
                    resolved_fd = os.open(
                        resolved,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    )
                    try:
                        opened = os.fstat(resolved_fd)
                        if _tree_identity_stdlib(resolved_metadata) != _tree_identity_stdlib(
                            opened
                        ):
                            raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                        digest = _hash_open_file_stdlib(resolved_fd, opened.st_size)
                        resolved_after = os.fstat(resolved_fd)
                    finally:
                        os.close(resolved_fd)
                    after_target = os.readlink(name, dir_fd=directory_fd)
                    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID") from exc
                if not (
                    target == after_target
                    and _tree_identity_stdlib(before) == _tree_identity_stdlib(after)
                    and _tree_identity_stdlib(opened) == _tree_identity_stdlib(resolved_after)
                ):
                    raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
                entries.append(
                    {
                        "path": relative,
                        "entry_type": "SYMLINK",
                        "mode": stat.S_IMODE(before.st_mode),
                        "owner_role": "AUTHORIZED_OWNER",
                        "byte_count": opened.st_size,
                        "sha256": digest,
                        "symlink_target": target,
                        "resolved_target": resolved,
                    }
                )
                continue
            raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
        if _tree_identity_stdlib(directory_before) != _tree_identity_stdlib(os.fstat(directory_fd)):
            raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")

    try:
        walk(root_fd, "", root)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda item: cast(str, item["path"]))
    return {
        "root": root,
        "tree_sha256": hashlib.sha256(_canonical_json_bytes_stdlib(entries)).hexdigest(),
        "tree_entry_count": len(entries),
        "tree_byte_count": sum(cast(int, item["byte_count"]) for item in entries),
        "owner_uid": recorded_owner_uid,
        "owner_gid": recorded_owner_gid,
        "aggregate_owner_identity": "AUTHORIZED_OWNER",
        "numeric_owner_excluded_from_tree_digest": True,
        "directory_mode": directory_mode,
        "regular_mode": regular_mode,
        "executable_mode": executable_mode,
        "symlinks_allowed": symlinks_allowed,
        "hardlinks_allowed": hardlinks_allowed,
        "forbidden_bytecode_or_pth_count": 0 if forbid_bytecode_and_pth else None,
        "all_entries_nofollow_revalidated": True,
    }


def _enumerate_source_tree_stdlib(source_root: str) -> dict[str, object]:
    root = Path(source_root)
    if not (root.is_absolute() and str(root) == source_root and ".." not in root.parts):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID") from exc
    entries: list[dict[str, object]] = []

    def walk(directory_fd: int, relative_directory: str) -> None:
        before = os.fstat(directory_fd)
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID") from exc
        for name in names:
            if name == "__pycache__" or name.endswith((".pyc", ".pyo")):
                continue
            relative = f"{relative_directory}/{name}" if relative_directory else name
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    if (
                        os.fstat(child_fd).st_dev,
                        os.fstat(child_fd).st_ino,
                    ) != (metadata.st_dev, metadata.st_ino):
                        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
                    entries.append({"path": relative, "entry_type": "DIRECTORY", "byte_count": 0})
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
            fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(fd)
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                ):
                    raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
                digest = hashlib.sha256()
                remaining = opened.st_size
                while remaining:
                    chunk = os.read(fd, min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
                    digest.update(chunk)
                    remaining -= len(chunk)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
            entries.append(
                {
                    "path": relative,
                    "entry_type": "REGULAR_FILE",
                    "byte_count": opened.st_size,
                    "sha256": digest.hexdigest(),
                }
            )
        after_directory = os.fstat(directory_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after_directory.st_dev,
            after_directory.st_ino,
            after_directory.st_mtime_ns,
            after_directory.st_ctime_ns,
        ):
            raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")

    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    entries.sort(key=lambda item: cast(str, item["path"]))
    return {
        "root": source_root,
        "tree_sha256": hashlib.sha256(_canonical_json_bytes_stdlib(entries)).hexdigest(),
        "tree_entry_count": len(entries),
        "tree_byte_count": sum(cast(int, item["byte_count"]) for item in entries),
        "ignored_bytecode_cache_entries_not_importable": True,
        "symlink_count": 0,
        "hardlink_count": 0,
        "all_files_nofollow_revalidated": True,
    }


def _verify_source_closure_stdlib(authority: dict[str, object]) -> dict[str, object]:
    source = authority.get("source")
    namespace = authority.get("network_namespace")
    if not (type(source) is dict and set(source) == _OUTER_SOURCE_KEYS and type(namespace) is dict):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    source = cast(dict[str, object], source)
    namespace = cast(dict[str, object], namespace)
    git_path = source.get("git_path")
    if git_path != "/usr/bin/git":
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    _hash_regular_nofollow_stdlib(cast(str, git_path), source.get("git_sha256"))
    inventory = _enumerate_source_tree_stdlib(cast(str, source["source_root"]))
    if not (
        inventory["tree_sha256"] == source.get("source_tree_sha256")
        and inventory["tree_entry_count"] == source.get("source_tree_entry_count")
        and inventory["tree_byte_count"] == source.get("source_tree_byte_count")
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    launcher_environment = cast(dict[str, object], namespace["launcher_environment"])
    git_environment = {
        "HOME": cast(str, launcher_environment["HOME"]),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }
    revision = _run_owned_command_stdlib(
        [
            cast(str, git_path),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            cast(str, source["worktree_root"]),
            "rev-parse",
            "HEAD",
        ],
        env=git_environment,
        timeout_seconds=10,
        error_code="GPU_SMOKE_SOURCE_BINDING_INVALID",
    )
    status = _run_owned_command_stdlib(
        [
            cast(str, git_path),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            cast(str, source["worktree_root"]),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        env=git_environment,
        timeout_seconds=30,
        error_code="GPU_SMOKE_SOURCE_BINDING_INVALID",
    )
    head = cast(bytes, revision["stdout"]).decode("ascii", errors="strict").strip()
    if not (
        revision["returncode"] == 0
        and status["returncode"] == 0
        and status["stdout"] == b""
        and head == source.get("head_commit") == authority["bindings"]["source_git_commit"]
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-preimport-source-closure/v1",
        "git_path": git_path,
        "git_sha256": source["git_sha256"],
        "head_commit": head,
        "porcelain_v1_empty": True,
        "untracked_files_all_empty": True,
        "source_tree": inventory,
        "project_or_third_party_module_imported_before_closure": False,
        "owned_commands": [revision["receipt"], status["receipt"]],
    }


def _expected_outer_fd_closure_receipt() -> dict[str, object]:
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-outer-fd-closure/v1",
        "strategy": "PYTHON_OS_CLOSERANGE_THEN_PROC_SELF_FD_REVALIDATION",
        "close_lower_bound": 3,
        "close_upper_bound_exclusive": _FD_CLOSE_UPPER_BOUND_EXCLUSIVE,
        "standard_fd_numbers": [0, 1, 2],
        "standard_fd_socket_count": 0,
        "standard_fds_non_inet": True,
        "remaining_fd_count_above_stderr": 0,
        "all_inherited_fds_closed": True,
        "forbidden_pre_unshare_module_count": 0,
        "foreign_process_fds_read": 0,
    }


def _outer_authority_namespace_values(
    args: argparse.Namespace,
) -> tuple[dict[str, object], dict[str, object], dict[str, str], dict[str, str]]:
    authority = _read_authority_stdlib(args)
    if set(authority) != _OUTER_AUTHORITY_KEYS:
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    namespace = authority.get("network_namespace")
    environment = namespace.get("launcher_environment") if type(namespace) is dict else None
    pre_namespace_environment = (
        namespace.get("pre_namespace_environment") if type(namespace) is dict else None
    )
    if not (
        type(namespace) is dict
        and type(environment) is dict
        and type(pre_namespace_environment) is dict
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    return (
        authority,
        cast(dict[str, object], namespace),
        cast(dict[str, str], environment),
        cast(dict[str, str], pre_namespace_environment),
    )


def _runtime_census_stdlib(
    authority: dict[str, object],
    *,
    inside_network_namespace: bool,
    phase: str,
    require_current_private_interpreter: bool,
) -> dict[str, object]:
    if phase not in {"PRE_PRIVATE_EXEC", "PRE_IMPORT"}:
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
    private = authority.get("private_runtime")
    client = authority.get("client_runtime")
    server = authority.get("server_runtime")
    namespace = authority.get("network_namespace")
    if not (
        type(private) is dict
        and set(private) == _OUTER_PRIVATE_RUNTIME_KEYS
        and type(client) is dict
        and set(client) == _OUTER_CLIENT_RUNTIME_KEYS
        and type(server) is dict
        and set(server) == _OUTER_SERVER_RUNTIME_KEYS
        and type(namespace) is dict
    ):
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
    private = cast(dict[str, object], private)
    client = cast(dict[str, object], client)
    server = cast(dict[str, object], server)
    namespace = cast(dict[str, object], namespace)
    actual_owner_uid = (
        cast(int, namespace["inside_owner_uid"])
        if inside_network_namespace
        else cast(int, namespace["host_owner_uid"])
    )
    actual_owner_gid = (
        cast(int, namespace["inside_owner_gid"])
        if inside_network_namespace
        else cast(int, namespace["host_owner_gid"])
    )
    root = cast(str, private["root"])
    python_path = cast(str, private["python_path"])
    if not (
        python_path == private["python_resolved_path"] == f"{root}/bin/python3.12"
        and private["stdlib_root"] == f"{root}/lib/python3.12"
        and private["python_version"] == "3.12.12"
        and private["python_flags"] == ["-I", "-S", "-B", "-X", "pycache_prefix=/dev/null"]
        and private["owner_uid"] == 0
        and private["owner_gid"] == 0
        and private["directory_mode"] == 0o500
        and private["regular_mode"] == 0o400
        and private["executable_mode"] == 0o500
        and private["symlinks_allowed"] is False
        and private["hardlinks_allowed"] is False
        and client["python_path"] == server["python_path"] == python_path
        and client["site_packages_path"] == f"{root}/site-packages/client"
        and server["site_packages_path"] == f"{root}/site-packages/server"
        and (
            not require_current_private_interpreter
            or str(Path(sys.executable).absolute()) == python_path
        )
    ):
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
    python_bytes = _hash_regular_nofollow_stdlib(python_path, private["python_sha256"])
    if python_bytes != private["python_byte_count"]:
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH")
    private_inventory = _enumerate_bound_tree_stdlib(
        root,
        owner_uid=actual_owner_uid,
        owner_gid=actual_owner_gid,
        recorded_owner_uid=0,
        recorded_owner_gid=0,
        directory_mode=0o500,
        regular_mode=0o400,
        executable_mode=0o500,
        symlinks_allowed=False,
        hardlinks_allowed=False,
        forbid_bytecode_and_pth=True,
    )
    if not (
        private_inventory["tree_sha256"] == private["tree_sha256"]
        and private_inventory["tree_entry_count"] == private["tree_entry_count"]
        and private_inventory["tree_byte_count"] == private["tree_byte_count"]
    ):
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH")
    site_inventories: dict[str, object] = {}
    for role, runtime in (("client_runtime", client), ("server_runtime", server)):
        if not (
            runtime["site_packages_owner_uid"] == 0
            and runtime["site_packages_owner_gid"] == 0
            and runtime["site_packages_directory_mode"] == 0o500
            and runtime["site_packages_regular_mode"] == 0o400
            and runtime["site_packages_executable_mode"] == 0o500
            and runtime["site_packages_symlinks_allowed"] is False
            and runtime["site_packages_hardlinks_allowed"] is False
        ):
            raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
        inventory = _enumerate_bound_tree_stdlib(
            cast(str, runtime["site_packages_path"]),
            owner_uid=actual_owner_uid,
            owner_gid=actual_owner_gid,
            recorded_owner_uid=0,
            recorded_owner_gid=0,
            directory_mode=0o500,
            regular_mode=0o400,
            executable_mode=0o500,
            symlinks_allowed=False,
            hardlinks_allowed=False,
            forbid_bytecode_and_pth=True,
        )
        if not (
            inventory["tree_sha256"] == runtime["site_packages_tree_sha256"]
            and inventory["tree_entry_count"] == runtime["site_packages_tree_entry_count"]
            and inventory["tree_byte_count"] == runtime["site_packages_tree_byte_count"]
        ):
            raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH")
        site_inventories[role] = inventory
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-preimport-runtime-census/v1",
        "phase": phase,
        "private_runtime": private_inventory,
        "private_python": {
            "path": python_path,
            "sha256": private["python_sha256"],
            "byte_count": python_bytes,
        },
        "site_packages": site_inventories,
        "actual_owner_view": "INSIDE_NAMESPACE" if inside_network_namespace else "HOST_OWNER",
        "aggregate_owner_identity": "AUTHORIZED_OWNER",
        "forbidden_bytecode_or_pth_count": 0,
        "private_interpreter_started_before_census": require_current_private_interpreter,
        "third_party_modules_imported_before_census": False,
        "toctou_free_runtime_binding_proven": False,
        "native_dt_needed_dependency_closure_proven": False,
        "formal_execution_closure_proven": False,
    }


def _preexec_private_runtime_census_stdlib(
    authority: dict[str, object],
) -> dict[str, object]:
    return _runtime_census_stdlib(
        authority,
        inside_network_namespace=True,
        phase="PRE_PRIVATE_EXEC",
        require_current_private_interpreter=False,
    )


def _preimport_runtime_census_stdlib(
    authority: dict[str, object],
    *,
    inside_network_namespace: bool,
) -> dict[str, object]:
    return _runtime_census_stdlib(
        authority,
        inside_network_namespace=inside_network_namespace,
        phase="PRE_IMPORT",
        require_current_private_interpreter=True,
    )


def _outer_execute_context(
    args: argparse.Namespace,
) -> tuple[dict[str, object], dict[str, object], dict[str, str], dict[str, str]]:
    authority, namespace, environment, pre_namespace_environment = (
        _outer_authority_namespace_values(args)
    )
    if dict(os.environ) != pre_namespace_environment:
        raise RuntimeError("GPU_SMOKE_NETWORK_NAMESPACE_INVALID")
    source = authority.get("source")
    client = authority.get("client_runtime")
    server = authority.get("server_runtime")
    outer_runtime = authority.get("outer_runtime")
    private_runtime = authority.get("private_runtime")
    gpu = authority.get("gpu")
    if not (
        authority.get("schema_version") == "mobileworld.g1.gpu-live-smoke-authority/v1"
        and authority.get("decision_id") == "D-034"
        and authority.get("authorized_scope") == "SYNTHETIC_NON_CASE_GPU_LIVE_SMOKE_22_CALLS"
        and authority.get("authorized") is True
        and type(namespace) is dict
        and set(namespace) == _OUTER_NETWORK_NAMESPACE_KEYS
        and type(environment) is dict
        and set(environment) == _OUTER_LAUNCHER_ENVIRONMENT_KEYS
        and type(pre_namespace_environment) is dict
        and set(pre_namespace_environment) == _OUTER_PRE_NAMESPACE_ENVIRONMENT_KEYS
        and type(source) is dict
        and set(source) == _OUTER_SOURCE_KEYS
        and type(client) is dict
        and set(client) == _OUTER_CLIENT_RUNTIME_KEYS
        and type(server) is dict
        and set(server) == _OUTER_SERVER_RUNTIME_KEYS
        and type(outer_runtime) is dict
        and set(outer_runtime) == _OUTER_RUNTIME_KEYS
        and type(private_runtime) is dict
        and set(private_runtime) == _OUTER_PRIVATE_RUNTIME_KEYS
        and type(gpu) is dict
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    namespace = cast(dict[str, object], namespace)
    environment = cast(dict[str, object], environment)
    source = cast(dict[str, object], source)
    client = cast(dict[str, object], client)
    server = cast(dict[str, object], server)
    outer_runtime = cast(dict[str, object], outer_runtime)
    private_runtime = cast(dict[str, object], private_runtime)
    pre_namespace_environment = cast(dict[str, object], pre_namespace_environment)
    gpu = cast(dict[str, object], gpu)
    scratch_root = authority.get("runtime_scratch_root")
    if not (
        namespace.get("required") is True
        and namespace.get("implementation") == "LINUX_USER_NETNS_MAP_ROOT_V1"
        and namespace.get("host_owner_uid") == os.getuid()
        and namespace.get("host_owner_gid") == os.getgid()
        and namespace.get("inside_owner_uid") == 0
        and namespace.get("inside_owner_gid") == 0
        and namespace.get("uid_map_line") == f"0 {os.getuid()} 1"
        and namespace.get("gid_map_line") == f"0 {os.getgid()} 1"
        and namespace.get("fd_close_upper_bound_exclusive") == _FD_CLOSE_UPPER_BOUND_EXCLUSIVE
        and namespace.get("python_pycache_prefix") == "/dev/null"
        and namespace.get("external_network_allowed") is False
        and namespace.get("expected_interfaces") == ["lo"]
        and gpu.get("uuid") == "GPU-991ac45f-e9e9-1c25-590c-fb49ca752965"
        and client.get("python_path") == client.get("python_resolved_path")
        and client.get("python_path") == private_runtime.get("python_path")
        and server.get("python_path") == private_runtime.get("python_path")
        and outer_runtime.get("python_path") == outer_runtime.get("python_resolved_path")
        and outer_runtime.get("python_path") == "/usr/bin/python3.10"
        and str(Path(sys.executable).absolute()) == "/usr/bin/python3.10"
        and outer_runtime.get("python_version") == "3.10.12"
        and sys.version.split()[0] == "3.10.12"
        and outer_runtime.get("python_flags")
        == ["-I", "-S", "-B", "-X", "pycache_prefix=/dev/null"]
        and private_runtime.get("python_version") == "3.12.12"
        and private_runtime.get("python_flags")
        == ["-I", "-S", "-B", "-X", "pycache_prefix=/dev/null"]
        and type(scratch_root) is str
        and cast(str, scratch_root).startswith("/")
        and str(Path(cast(str, scratch_root))) == scratch_root
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    outer_python_size = _hash_regular_nofollow_stdlib(
        "/usr/bin/python3.10",
        outer_runtime.get("python_sha256"),
    )
    if not (
        outer_python_size == outer_runtime.get("python_byte_count")
        and outer_runtime.get("stdlib_root") == "/usr/lib/python3.10"
        and outer_runtime.get("required_owner_uid") == 0
        and outer_runtime.get("required_owner_gid") == 0
        and outer_runtime.get("directory_mode") == 0o755
        and outer_runtime.get("regular_mode") == 0o644
        and outer_runtime.get("executable_mode") == 0o755
        and outer_runtime.get("symlinks_allowed") is True
        and outer_runtime.get("hardlinks_allowed") is True
    ):
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
    outer_stdlib = _enumerate_bound_tree_stdlib(
        "/usr/lib/python3.10",
        owner_uid=0,
        owner_gid=0,
        recorded_owner_uid=0,
        recorded_owner_gid=0,
        directory_mode=0o755,
        regular_mode=0o644,
        executable_mode=0o755,
        symlinks_allowed=True,
        hardlinks_allowed=True,
    )
    if not (
        outer_stdlib["tree_sha256"] == outer_runtime.get("stdlib_tree_sha256")
        and outer_stdlib["tree_entry_count"] == outer_runtime.get("stdlib_tree_entry_count")
        and outer_stdlib["tree_byte_count"] == outer_runtime.get("stdlib_tree_byte_count")
    ):
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH")
    closure_sha256 = hashlib.sha256(
        _canonical_json_bytes_stdlib(_expected_outer_fd_closure_receipt())
    ).hexdigest()
    expected_environment: dict[str, object] = {
        "PATH": "/usr/bin:/bin",
        "CUDA_VISIBLE_DEVICES": gpu["uuid"],
        "LD_LIBRARY_PATH": "",
        "HOME": f"{scratch_root}/namespace-launcher/home",
        "HF_HOME": f"{scratch_root}/namespace-launcher/hf-home",
        "XDG_CACHE_HOME": f"{scratch_root}/namespace-launcher/xdg-cache",
        "TORCH_HOME": f"{scratch_root}/namespace-launcher/torch-home",
        "TRITON_CACHE_DIR": f"{scratch_root}/namespace-launcher/triton-cache",
        "VLLM_CACHE_ROOT": f"{scratch_root}/namespace-launcher/vllm-cache",
        "TMPDIR": f"{scratch_root}/namespace-launcher/tmp",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "GPU_SMOKE_OUTER_FD_CLOSURE_SHA256": closure_sha256,
    }
    if not (
        environment == expected_environment
        and pre_namespace_environment == {"LC_CTYPE": "C.UTF-8"}
        and namespace.get("outer_fd_closure_receipt_sha256") == closure_sha256
        and namespace.get("env_path") == "/usr/bin/env"
        and namespace.get("unshare_path") == "/usr/bin/unshare"
        and source.get("worktree_root") == str(_SCRIPT_PATH.parents[2])
        and source.get("source_root") == str(_SOURCE_ROOT)
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    if environment.get("LD_LIBRARY_PATH") != "":
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    if not (
        namespace.get("nvidia_smi_path") == "/usr/bin/nvidia-smi"
        and type(namespace.get("nvidia_smi_byte_count")) is int
        and cast(int, namespace["nvidia_smi_byte_count"]) > 0
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    for role in ("env", "unshare", "nvidia_smi"):
        byte_count = _hash_regular_nofollow_stdlib(
            cast(str, namespace[f"{role}_path"]),
            namespace[f"{role}_sha256"],
        )
        if role == "nvidia_smi" and byte_count != namespace["nvidia_smi_byte_count"]:
            raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    critical = source.get("critical_files")
    if type(critical) is not dict or set(critical) != set(_CRITICAL_SOURCE_FILES):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    runner_binding = cast(dict[str, object], critical["runner_cli"])
    if runner_binding.get("relative_path") != _CRITICAL_SOURCE_FILES["runner_cli"]:
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    _hash_regular_nofollow_stdlib(str(_SCRIPT_PATH), runner_binding.get("sha256"))
    bootstrap_bytes = _OUTER_STDLIB_BOOTSTRAP_CODE.encode("utf-8")
    bootstrap_sha256 = hashlib.sha256(bootstrap_bytes).hexdigest()
    pinned_bootstrap = globals().get("_PINNED_BOOTSTRAP")
    if not (
        args.stage0_bootstrap_sha256 == bootstrap_sha256
        and args.pinned_bootstrap_stage == "STAGE0"
        and source.get("outer_bootstrap_code_sha256") == bootstrap_sha256
        and source.get("outer_bootstrap_code_byte_count") == len(bootstrap_bytes)
        and type(pinned_bootstrap) is dict
        and pinned_bootstrap
        == {
            "authority_sha256": args.authority_sha256,
            "cli_sha256": runner_binding.get("sha256"),
            "bootstrap_sha256": bootstrap_sha256,
            "stage": "STAGE0",
            "cli_opened_nofollow": True,
            "cli_compiled_from_verified_bytes": True,
        }
    ):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    return (
        authority,
        namespace,
        cast(dict[str, str], environment),
        cast(dict[str, str], pre_namespace_environment),
    )


def _normalized_id_map_stdlib(path: str) -> str:
    try:
        fields = Path(path).read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("GPU_SMOKE_NETWORK_NAMESPACE_INVALID") from exc
    if len(fields) != 3 or not all(item.isdecimal() for item in fields):
        raise RuntimeError("GPU_SMOKE_NETWORK_NAMESPACE_INVALID")
    return " ".join(fields)


def _ensure_owner_only_directory_stdlib(path: str, *, exclusive: bool) -> None:
    lexical = Path(path)
    if not (lexical.is_absolute() and str(lexical) == path and ".." not in lexical.parts):
        raise RuntimeError("GPU_SMOKE_RUNTIME_SCRATCH_INVALID")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current_fd = os.open("/", flags)
    try:
        parts = lexical.parts[1:]
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
                existed = True
            except FileNotFoundError:
                if not final:
                    raise RuntimeError("GPU_SMOKE_RUNTIME_SCRATCH_INVALID") from None
                os.mkdir(part, 0o700, dir_fd=current_fd)
                child_fd = os.open(part, flags, dir_fd=current_fd)
                existed = False
            if final and exclusive and existed:
                os.close(child_fd)
                raise RuntimeError("GPU_SMOKE_RUNTIME_SCRATCH_COLLISION")
            os.close(current_fd)
            current_fd = child_fd
        metadata = os.fstat(current_fd)
        if not (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.getuid() == 0
            and metadata.st_gid == os.getgid() == 0
            and stat.S_IMODE(metadata.st_mode) == 0o700
        ):
            raise RuntimeError("GPU_SMOKE_RUNTIME_SCRATCH_INVALID")
        os.fsync(current_fd)
    except OSError as exc:
        raise RuntimeError("GPU_SMOKE_RUNTIME_SCRATCH_INVALID") from exc
    finally:
        os.close(current_fd)


def _prepare_stage1_launcher_scratch_stdlib(
    authority: dict[str, object],
    environment: dict[str, str],
) -> dict[str, object]:
    scratch_root = cast(str, authority["runtime_scratch_root"])
    _ensure_owner_only_directory_stdlib(scratch_root, exclusive=False)
    launcher_root = f"{scratch_root}/namespace-launcher"
    _ensure_owner_only_directory_stdlib(launcher_root, exclusive=True)
    leaves: dict[str, str] = {}
    for key, name in _SCRATCH_DIRECTORY_NAMES.items():
        leaf = f"{launcher_root}/{name}"
        if environment.get(key) != leaf:
            raise RuntimeError("GPU_SMOKE_NETWORK_NAMESPACE_INVALID")
        _ensure_owner_only_directory_stdlib(leaf, exclusive=True)
        leaves[key] = leaf
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-stage1-scratch/v1",
        "root": launcher_root,
        "directories": leaves,
        "directory_count": len(leaves),
        "regular_file_count": 0,
        "owner_uid": 0,
        "owner_gid": 0,
        "mode": 0o700,
        "created_exclusively": True,
        "symlink_followed": False,
    }


def _stage1_preexec_context(
    args: argparse.Namespace,
) -> tuple[dict[str, object], dict[str, object], dict[str, str], dict[str, object]]:
    authority, namespace, environment, pre_namespace_environment = (
        _outer_authority_namespace_values(args)
    )
    outer_runtime = cast(dict[str, object], authority["outer_runtime"])
    source = cast(dict[str, object], authority["source"])
    bootstrap_bytes = _OUTER_STDLIB_BOOTSTRAP_CODE.encode("utf-8")
    pinned_bootstrap = globals().get("_PINNED_BOOTSTRAP")
    if not (
        dict(os.environ) == pre_namespace_environment == {"LC_CTYPE": "C.UTF-8"}
        and str(Path(sys.executable).absolute()) == "/usr/bin/python3.10"
        and outer_runtime.get("python_path") == "/usr/bin/python3.10"
        and os.getuid() == namespace.get("inside_owner_uid") == 0
        and os.getgid() == namespace.get("inside_owner_gid") == 0
        and _normalized_id_map_stdlib("/proc/self/uid_map") == namespace.get("uid_map_line")
        and _normalized_id_map_stdlib("/proc/self/gid_map") == namespace.get("gid_map_line")
        and args.stage0_bootstrap_sha256 == hashlib.sha256(bootstrap_bytes).hexdigest()
        and source.get("outer_bootstrap_code_sha256") == args.stage0_bootstrap_sha256
        and source.get("outer_bootstrap_code_byte_count") == len(bootstrap_bytes)
        and args.pinned_bootstrap_stage == "STAGE1"
        and type(pinned_bootstrap) is dict
        and pinned_bootstrap.get("stage") == "STAGE1"
        and pinned_bootstrap.get("authority_sha256") == args.authority_sha256
        and pinned_bootstrap.get("bootstrap_sha256") == args.stage0_bootstrap_sha256
        and pinned_bootstrap.get("cli_opened_nofollow") is True
        and pinned_bootstrap.get("cli_compiled_from_verified_bytes") is True
    ):
        raise RuntimeError("GPU_SMOKE_NETWORK_NAMESPACE_INVALID")
    for role in ("env", "ip", "setpriv"):
        _hash_regular_nofollow_stdlib(
            cast(str, namespace[f"{role}_path"]),
            namespace[f"{role}_sha256"],
        )
    runtime_preexec = _preexec_private_runtime_census_stdlib(authority)
    scratch = _prepare_stage1_launcher_scratch_stdlib(authority, environment)
    completed = _run_owned_command_stdlib(
        [cast(str, namespace["ip_path"]), "link", "set", "dev", "lo", "up"],
        env=cast(dict[str, str], pre_namespace_environment),
        timeout_seconds=10,
        error_code="GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
    )
    if completed["returncode"] != 0:
        raise RuntimeError("GPU_SMOKE_NETWORK_NAMESPACE_INVALID")
    receipt: dict[str, object] = {
        "schema_version": _STAGE1_RECEIPT_SCHEMA_VERSION,
        "authority_sha256": args.authority_sha256,
        "stage": "INSIDE_NETNS_ROOT_STDLIB_PRE_PRIVATE_EXEC",
        "outer_python_path": "/usr/bin/python3.10",
        "outer_python_sha256": outer_runtime["python_sha256"],
        "uid_map_line": namespace["uid_map_line"],
        "gid_map_line": namespace["gid_map_line"],
        "inside_owner_uid": 0,
        "inside_owner_gid": 0,
        "private_runtime_preexec": runtime_preexec,
        "launcher_scratch": scratch,
        "loopback_setup": {
            "tool_path": namespace["ip_path"],
            "tool_sha256": namespace["ip_sha256"],
            "interface": "lo",
            "set_up_succeeded": True,
            "owned_command": completed["receipt"],
        },
        "outer_fd_closure_receipt_sha256": namespace["outer_fd_closure_receipt_sha256"],
        "private_interpreter_started_before_runtime_census": False,
        "third_party_or_project_module_imported": False,
    }
    return authority, namespace, environment, receipt


def _encode_stage1_receipt(receipt: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(_canonical_json_bytes_stdlib(receipt)).decode("ascii")


def _decode_stage1_receipt(args: argparse.Namespace) -> dict[str, object]:
    encoded = args.stage1_receipt_b64
    if type(encoded) is not str or not 0 < len(encoded) <= 256 * 1024:
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
    try:
        data = base64.b64decode(encoded, altchars=b"-_", validate=True)
        value = json.loads(data)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID") from exc
    if not (
        type(value) is dict
        and data == _canonical_json_bytes_stdlib(value)
        and value.get("schema_version") == _STAGE1_RECEIPT_SCHEMA_VERSION
        and value.get("authority_sha256") == args.authority_sha256
        and value.get("private_interpreter_started_before_runtime_census") is False
        and value.get("third_party_or_project_module_imported") is False
    ):
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_INVALID")
    return cast(dict[str, object], value)


def _bootstrap_import_paths(args: argparse.Namespace) -> None:
    global _PREIMPORT_RUNTIME_RECEIPT, _STAGE1_PREEXEC_RECEIPT
    if args.command in {"prepare", "execute"}:
        try:
            authority = _read_authority_stdlib(args)
            if args.command == "execute":
                pinned_bootstrap = globals().get("_PINNED_BOOTSTRAP")
                if not (
                    args.inside_network_namespace
                    and args.namespace_sandboxed
                    and not args.inside_network_namespace_stage1
                    and args.pinned_bootstrap_stage == "STAGE2"
                    and type(pinned_bootstrap) is dict
                    and pinned_bootstrap.get("stage") == "STAGE2"
                    and pinned_bootstrap.get("authority_sha256") == args.authority_sha256
                    and pinned_bootstrap.get("bootstrap_sha256") == args.stage0_bootstrap_sha256
                    and pinned_bootstrap.get("cli_opened_nofollow") is True
                    and pinned_bootstrap.get("cli_compiled_from_verified_bytes") is True
                ):
                    raise RuntimeError("GPU_SMOKE_NETWORK_NAMESPACE_INVALID")
                _STAGE1_PREEXEC_RECEIPT = _decode_stage1_receipt(args)
                source_closure = _verify_source_closure_stdlib(authority)
            else:
                source_closure = None
            runtime_census = _preimport_runtime_census_stdlib(
                authority,
                inside_network_namespace=(
                    args.command == "execute" and args.inside_network_namespace
                ),
            )
            if args.command == "execute":
                stage1_runtime = cast(
                    dict[str, object],
                    cast(dict[str, object], _STAGE1_PREEXEC_RECEIPT)["private_runtime_preexec"],
                )
                if not (
                    stage1_runtime.get("phase") == "PRE_PRIVATE_EXEC"
                    and cast(dict[str, object], stage1_runtime["private_runtime"])["tree_sha256"]
                    == cast(dict[str, object], runtime_census["private_runtime"])["tree_sha256"]
                    and cast(dict[str, object], stage1_runtime["private_python"])["sha256"]
                    == cast(dict[str, object], runtime_census["private_python"])["sha256"]
                ):
                    raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH")
            runtime_census["stage1_preexec_receipt_sha256"] = (
                hashlib.sha256(_canonical_json_bytes_stdlib(_STAGE1_PREEXEC_RECEIPT)).hexdigest()
                if _STAGE1_PREEXEC_RECEIPT is not None
                else None
            )
            runtime_census["source_closure"] = source_closure
            _PREIMPORT_RUNTIME_RECEIPT = runtime_census
            source = authority["source"]
            client_runtime = authority["client_runtime"]
            source_root = Path(source["source_root"])
            worktree_root = Path(source["worktree_root"])
            site_packages = Path(client_runtime["site_packages_path"])
            if not (
                source_root == _SOURCE_ROOT
                and worktree_root == _SCRIPT_PATH.parents[2]
                and Path(client_runtime["python_path"]) == Path(sys.executable).absolute()
                and source_root.is_absolute()
                and site_packages.is_absolute()
                and source_root.resolve(strict=True) == source_root
                and site_packages.resolve(strict=True) == site_packages
                and site_packages.is_dir()
            ):
                raise ValueError("bootstrap path differs from exact CLI worktree/runtime")
            critical = source["critical_files"]
            if set(critical) != set(_CRITICAL_SOURCE_FILES):
                raise ValueError("critical bootstrap file set differs")
            for name, relative_path in _CRITICAL_SOURCE_FILES.items():
                binding = critical[name]
                path = worktree_root / relative_path
                path_metadata = path.lstat()
                if not stat.S_ISREG(path_metadata.st_mode) or path.resolve(strict=True) != path:
                    raise ValueError("critical bootstrap source is linked")
                source_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    opened = os.fstat(source_fd)
                    remaining = opened.st_size
                    digest = hashlib.sha256()
                    while remaining:
                        chunk = os.read(source_fd, min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("critical bootstrap source truncated")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    after = os.fstat(source_fd)
                finally:
                    os.close(source_fd)
                identity_before = (
                    path_metadata.st_dev,
                    path_metadata.st_ino,
                    path_metadata.st_size,
                    path_metadata.st_mtime_ns,
                    path_metadata.st_ctime_ns,
                )
                identity_opened = (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                identity_after = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                if not (
                    binding["relative_path"] == relative_path
                    and identity_before == identity_opened == identity_after
                    and digest.hexdigest() == binding["sha256"]
                ):
                    raise ValueError("critical bootstrap source digest differs")
        except _OwnedCommandStdlibError:
            raise
        except (OSError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID") from exc
    else:
        source_root = _SOURCE_ROOT
        candidates = sorted(Path(sys.executable).parent.parent.glob("lib/python*/site-packages"))
        if len(candidates) != 1:
            raise RuntimeError("GPU_SMOKE_CLIENT_RUNTIME_MISMATCH")
        site_packages = candidates[0]
    if args.command == "execute" and any("site-packages" in item for item in sys.path):
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    sys.path[:0] = [str(source_root), str(site_packages)]


def _close_inherited_file_descriptors(upper_bound: int) -> dict[str, object]:
    """Close every descriptor above stderr immediately before first unshare exec."""

    _, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard_limit == resource.RLIM_INFINITY or hard_limit != upper_bound:
        raise RuntimeError("GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED")
    if not 3 <= upper_bound <= 16_777_216:
        raise RuntimeError("GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED")
    for fd in (0, 1, 2):
        try:
            metadata = os.fstat(fd)
        except OSError as exc:
            raise RuntimeError("GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED") from exc
        if stat.S_ISSOCK(metadata.st_mode):
            raise RuntimeError("GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED")
    os.closerange(3, upper_bound)
    live_extra_fds: list[int] = []
    try:
        observed = [int(name) for name in os.listdir("/proc/self/fd") if name.isdecimal()]
    except OSError as exc:
        raise RuntimeError("GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED") from exc
    for fd in observed:
        if fd < 3:
            continue
        try:
            os.fstat(fd)
        except OSError:
            continue
        live_extra_fds.append(fd)
    if live_extra_fds:
        raise RuntimeError("GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED")
    forbidden = [
        name for name in sys.modules if name.split(".", 1)[0] in _FORBIDDEN_PRE_UNSHARE_MODULE_ROOTS
    ]
    if forbidden:
        raise RuntimeError("GPU_SMOKE_SOURCE_BINDING_INVALID")
    return _expected_outer_fd_closure_receipt()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    compile_packet = subparsers.add_parser(
        "compile-packet", help="compile the frozen secret-free fixtures without live resources"
    )
    compile_packet.add_argument("--qwen-fixture", required=True)
    compile_packet.add_argument("--mai-fixture", required=True)
    compile_packet.add_argument("--output-root", required=True)
    compile_packet.add_argument("--g1-5-seed", type=int, default=1729)
    inspect_inputs = subparsers.add_parser(
        "inspect-authority-inputs",
        help="hash local snapshots/runtimes without a GPU, client, socket, or model load",
    )
    inspect_inputs.add_argument("--model-config-manifest", required=True)
    inspect_inputs.add_argument("--qwen-snapshot", required=True)
    inspect_inputs.add_argument("--mai-snapshot", required=True)
    inspect_inputs.add_argument("--client-python", required=True)
    inspect_inputs.add_argument("--server-python", required=True)
    inspect_inputs.add_argument("--client-site-packages", required=True)
    inspect_inputs.add_argument("--server-site-packages", required=True)
    build_runtime = subparsers.add_parser(
        "build-private-runtime",
        help="build a sealed CPU-only Python runtime with exact CoW clones",
    )
    build_runtime.add_argument("--source-python", required=True)
    build_runtime.add_argument("--client-site-packages", required=True)
    build_runtime.add_argument("--server-site-packages", required=True)
    build_runtime.add_argument("--output-root", required=True)
    for name in ("prepare", "execute"):
        command = subparsers.add_parser(name)
        command.add_argument("--authority", required=True)
        command.add_argument("--authority-sha256", required=True)
        command.add_argument("--smoke-packet", required=True)
        command.add_argument("--model-config-manifest", required=True)
        if name == "execute":
            command.add_argument(
                "--confirm-execute",
                required=True,
                help=f"must be exact {_EXECUTE_CONFIRMATION!r}",
            )
            command.add_argument(
                "--inside-network-namespace-stage1",
                action="store_true",
                help=argparse.SUPPRESS,
            )
            command.add_argument(
                "--stage0-bootstrap-sha256",
                help=argparse.SUPPRESS,
            )
            command.add_argument(
                "--pinned-bootstrap-stage",
                choices=("STAGE0", "STAGE1", "STAGE2"),
                help=argparse.SUPPRESS,
            )
            command.add_argument(
                "--inside-network-namespace",
                action="store_true",
                help=argparse.SUPPRESS,
            )
            command.add_argument(
                "--namespace-sandboxed",
                action="store_true",
                help=argparse.SUPPRESS,
            )
            command.add_argument(
                "--stage1-receipt-b64",
                help=argparse.SUPPRESS,
            )
    return parser


def _error(exc: Any) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {
        "valid": False,
        "error_code": exc.code,
        "message": str(exc),
        "json_path": exc.json_path,
        "execution_started": exc.terminal_receipt is not None,
        "generated_action_executed": False,
        "replay_executed": False,
    }
    if exc.terminal_receipt is not None:
        result["terminal_receipt"] = exc.terminal_receipt
    return result


def _external_execute_arguments(args: argparse.Namespace) -> list[str]:
    return [
        "execute",
        "--authority",
        args.authority,
        "--authority-sha256",
        args.authority_sha256,
        "--smoke-packet",
        args.smoke_packet,
        "--model-config-manifest",
        args.model_config_manifest,
        "--confirm-execute",
        args.confirm_execute,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_argv)
    if args.command == "execute" and not (
        sys.flags.isolated == 1
        and sys.flags.ignore_environment == 1
        and sys.flags.no_site == 1
        and sys.flags.dont_write_bytecode == 1
        and sys.pycache_prefix == "/dev/null"
        and "site" not in sys.modules
    ):
        print(
            json.dumps(
                {
                    "valid": False,
                    "error_code": "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "message": (
                        "execute must start through exact Python flags "
                        "-I -S -B -X pycache_prefix=/dev/null"
                    ),
                    "execution_started": False,
                    "generated_action_executed": False,
                    "replay_executed": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2

    if args.command == "execute" and not (
        args.inside_network_namespace_stage1 or args.inside_network_namespace
    ):
        try:
            if args.confirm_execute != _EXECUTE_CONFIRMATION:
                raise RuntimeError("GPU_SMOKE_EXECUTE_CONFIRMATION_INVALID")
            authority, namespace, environment, pre_namespace_environment = _outer_execute_context(
                args
            )
            closure_receipt = _close_inherited_file_descriptors(
                cast(int, namespace["fd_close_upper_bound_exclusive"])
            )
            closure_sha256 = hashlib.sha256(
                _canonical_json_bytes_stdlib(closure_receipt)
            ).hexdigest()
            if closure_sha256 != namespace["outer_fd_closure_receipt_sha256"]:
                raise RuntimeError("GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED")
            environment["GPU_SMOKE_OUTER_FD_CLOSURE_SHA256"] = closure_sha256
            if environment != cast(dict[str, object], namespace["launcher_environment"]):
                raise RuntimeError("GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED")
            reexec_argv = [
                cast(str, namespace["env_path"]),
                "-i",
                *(f"{key}={value}" for key, value in sorted(pre_namespace_environment.items())),
                cast(str, namespace["unshare_path"]),
                "--user",
                "--map-root-user",
                "--net",
                cast(str, namespace["env_path"]),
                "-i",
                *(f"{key}={value}" for key, value in sorted(pre_namespace_environment.items())),
                "/usr/bin/python3.10",
                "-I",
                "-S",
                "-B",
                "-X",
                "pycache_prefix=/dev/null",
                "-c",
                _OUTER_STDLIB_BOOTSTRAP_CODE,
                "STAGE1",
                args.authority,
                args.authority_sha256,
                args.stage0_bootstrap_sha256,
                str(_SCRIPT_PATH),
                "-",
                *_external_execute_arguments(args),
            ]
            os.execve(cast(str, namespace["env_path"]), reexec_argv, {})
            raise AssertionError("os.execve unexpectedly returned")
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            error_code = (
                str(exc)
                if isinstance(exc, RuntimeError) and str(exc).startswith("GPU_SMOKE_")
                else "GPU_SMOKE_NETWORK_NAMESPACE_INVALID"
            )
            print(
                json.dumps(
                    {
                        "valid": False,
                        "error_code": error_code,
                        "message": "stdlib-only pre-unshare launcher failed closed",
                        "execution_started": False,
                        "generated_action_executed": False,
                        "replay_executed": False,
                        "owned_command": (
                            exc.receipt if isinstance(exc, _OwnedCommandStdlibError) else None
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 2

    if args.command == "execute" and args.inside_network_namespace_stage1:
        try:
            if args.inside_network_namespace or args.namespace_sandboxed:
                raise RuntimeError("GPU_SMOKE_NETWORK_NAMESPACE_INVALID")
            authority, namespace, environment, stage1_receipt = _stage1_preexec_context(args)
            encoded_receipt = _encode_stage1_receipt(stage1_receipt)
            private_runtime = cast(dict[str, object], authority["private_runtime"])
            sandbox_argv = [
                cast(str, namespace["env_path"]),
                "-i",
                *(f"{key}={value}" for key, value in sorted(environment.items())),
                cast(str, namespace["setpriv_path"]),
                "--no-new-privs",
                "--bounding-set=-all",
                "--ambient-caps=-all",
                "--inh-caps=-all",
                "--clear-groups",
                cast(str, private_runtime["python_path"]),
                "-I",
                "-S",
                "-B",
                "-X",
                "pycache_prefix=/dev/null",
                "-c",
                _OUTER_STDLIB_BOOTSTRAP_CODE,
                "STAGE2",
                args.authority,
                args.authority_sha256,
                args.stage0_bootstrap_sha256,
                str(_SCRIPT_PATH),
                encoded_receipt,
                *_external_execute_arguments(args),
            ]
            os.execve(cast(str, namespace["env_path"]), sandbox_argv, dict(os.environ))
            raise AssertionError("stage1 sandbox os.execve unexpectedly returned")
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            error_code = (
                str(exc)
                if isinstance(exc, RuntimeError) and str(exc).startswith("GPU_SMOKE_")
                else "GPU_SMOKE_NETWORK_NAMESPACE_INVALID"
            )
            print(
                json.dumps(
                    {
                        "valid": False,
                        "error_code": error_code,
                        "message": "stdlib-only inside-netns preexec stage failed closed",
                        "execution_started": False,
                        "generated_action_executed": False,
                        "replay_executed": False,
                        "owned_command": (
                            exc.receipt if isinstance(exc, _OwnedCommandStdlibError) else None
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 2

    try:
        _bootstrap_import_paths(args)
    except RuntimeError as exc:
        print(
            json.dumps(
                {
                    "valid": False,
                    "error_code": str(exc),
                    "message": "authority-bound import bootstrap failed",
                    "execution_started": False,
                    "generated_action_executed": False,
                    "replay_executed": False,
                    "owned_command": (
                        exc.receipt if isinstance(exc, _OwnedCommandStdlibError) else None
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2

    from mobile_world.offline.causal_replay.contracts import canonical_json_bytes
    from mobile_world.offline.gpu_live_smoke import (
        GpuLiveSmokeError,
        build_private_runtime,
        compile_gpu_smoke_packet,
        execute_gpu_live_smoke,
        inspect_authority_inputs,
        load_gpu_live_authority,
        load_gpu_smoke_packet,
        prepare_gpu_live_smoke,
        write_gpu_smoke_packet,
    )

    try:
        if args.command == "compile-packet":
            packet = compile_gpu_smoke_packet(
                args.qwen_fixture, args.mai_fixture, g1_5_seed=args.g1_5_seed
            )
            result = write_gpu_smoke_packet(packet, args.output_root)
        elif args.command == "inspect-authority-inputs":
            result = inspect_authority_inputs(
                model_config_manifest_path=args.model_config_manifest,
                qwen_snapshot_path=args.qwen_snapshot,
                mai_snapshot_path=args.mai_snapshot,
                client_python_path=args.client_python,
                server_python_path=args.server_python,
                client_site_packages_path=args.client_site_packages,
                server_site_packages_path=args.server_site_packages,
                outer_bootstrap_code_sha256=hashlib.sha256(
                    _OUTER_STDLIB_BOOTSTRAP_CODE.encode("utf-8")
                ).hexdigest(),
                outer_bootstrap_code_byte_count=len(_OUTER_STDLIB_BOOTSTRAP_CODE.encode("utf-8")),
            )
        elif args.command == "build-private-runtime":
            result = build_private_runtime(
                source_python_path=args.source_python,
                client_site_packages_path=args.client_site_packages,
                server_site_packages_path=args.server_site_packages,
                output_root=args.output_root,
            )
        elif args.command == "prepare":
            authority = load_gpu_live_authority(args.authority, args.authority_sha256)
            packet = load_gpu_smoke_packet(
                args.smoke_packet,
                cast(str, authority.bindings["smoke_packet_sha256"]),
                g1_5_seed=cast(int, authority.matrix["g1_5_seed"]),
            )
            result = prepare_gpu_live_smoke(authority, packet, args.model_config_manifest)
        else:
            if args.confirm_execute != _EXECUTE_CONFIRMATION:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_EXECUTE_CONFIRMATION_INVALID",
                    "execute confirmation phrase differs",
                    json_path="$.confirm_execute",
                )
            authority = load_gpu_live_authority(args.authority, args.authority_sha256)
            if not args.inside_network_namespace:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
                    "outer execute launcher did not enter the stdlib-only unshare branch",
                )
            if not args.namespace_sandboxed:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
                    "Stage2 was not entered through the verified setpriv/bootstrap chain",
                )
            os.umask(0o077)
            result = execute_gpu_live_smoke(
                authority_path=args.authority,
                authority_sha256=args.authority_sha256,
                smoke_packet_path=args.smoke_packet,
                model_config_manifest_path=args.model_config_manifest,
                stage1_preexec_receipt=cast(dict[str, JsonValue], _STAGE1_PREEXEC_RECEIPT),
                preimport_runtime_receipt=cast(dict[str, JsonValue], _PREIMPORT_RUNTIME_RECEIPT),
            )
    except GpuLiveSmokeError as exc:
        print(canonical_json_bytes(_error(exc)).decode())
        return 2
    print(canonical_json_bytes(cast(JsonValue, result)).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
