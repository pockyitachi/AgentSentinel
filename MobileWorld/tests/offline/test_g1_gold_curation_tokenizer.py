"""CPU-only tests for the opt-in G1.6 local pinned-tokenizer loader.

All tokenizer bytes, manifests, and runtime objects in this module are synthetic.
The tests do not inspect a real snapshot, import Transformers, open a network
connection, use a GPU, load model weights, or execute replay/action code.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
import types
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MOBILEWORLD_SOURCE_ROOT = REPOSITORY_ROOT / "MobileWorld/src"
LAUNCHER = REPOSITORY_ROOT / "MobileWorld/scripts/run_g1_gold_curation.py"

# Importing mobile_world normally imports the Android runtime.  This test needs
# only the offline package, so expose its package search path directly.
if "mobile_world" not in sys.modules:
    mobile_world_package = types.ModuleType("mobile_world")
    mobile_world_package.__path__ = [str(MOBILEWORLD_SOURCE_ROOT / "mobile_world")]
    sys.modules["mobile_world"] = mobile_world_package

from mobile_world.offline.gold_curation import local_tokenizer as subject  # noqa: E402
from mobile_world.offline.gold_curation.contracts import (  # noqa: E402
    CurationError,
    canonical_sha256,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _Encoding:
    def __init__(self, ids: Any) -> None:
        self.ids = ids


class _FakeTokenizer:
    loaded_json: list[str] = []
    encode_calls: list[tuple[str, bool]] = []
    ids: Any = [3, 5, 8]

    @classmethod
    def reset(cls) -> None:
        cls.loaded_json = []
        cls.encode_calls = []
        cls.ids = [3, 5, 8]

    @classmethod
    def from_str(cls, value: str) -> _FakeTokenizer:
        cls.loaded_json.append(value)
        return cls()

    def encode(self, text: str, *, add_special_tokens: bool) -> _Encoding:
        type(self).encode_calls.append((text, add_special_tokens))
        ids = type(self).ids(text) if callable(type(self).ids) else type(self).ids
        return _Encoding(deepcopy(ids))


def _artifact_bytes(model_id: str) -> dict[str, bytes]:
    return {
        "tokenizer.json": _canonical_json_bytes({"synthetic_model": model_id}),
        "tokenizer_config.json": _canonical_json_bytes(
            {"add_special_tokens": False, "synthetic_model": model_id}
        ),
        "special_tokens_map.json": _canonical_json_bytes({"eos_token": f"<{model_id}-eos>"}),
    }


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "0.22.2",
    imported: list[str] | None = None,
) -> None:
    _FakeTokenizer.reset()
    monkeypatch.setattr(subject.importlib.metadata, "version", lambda package: version)

    def import_module(name: str) -> types.SimpleNamespace:
        assert name == "tokenizers"
        if imported is not None:
            imported.append(name)
        return types.SimpleNamespace(Tokenizer=_FakeTokenizer)

    monkeypatch.setattr(subject.importlib, "import_module", import_module)


def _synthetic_bound_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, Any], dict[str, Path], dict[str, dict[str, bytes]]]:
    models: list[dict[str, Any]] = []
    pinned_models: dict[str, dict[str, str]] = {}
    roots: dict[str, Path] = {}
    artifact_sets: dict[str, dict[str, bytes]] = {}
    identities = {
        "qwen3vl_8b": (
            "Qwen/Synthetic-Tokenizer",
            "q" * 40,
        ),
        "mai_ui_8b": (
            "Tongyi-MAI/Synthetic-Tokenizer",
            "m" * 40,
        ),
    }
    for model_id, (tokenizer_id, revision) in identities.items():
        root = tmp_path / model_id
        root.mkdir()
        roots[model_id] = root
        artifacts = _artifact_bytes(model_id)
        artifact_sets[model_id] = artifacts
        inventory = []
        for name, data in artifacts.items():
            (root / name).write_bytes(data)
            inventory.append({"path": name, "byte_count": len(data), "sha256": _sha256(data)})
        tokenizer = {
            "revision": revision,
            "tokenizers_version": "0.22.2",
            "use_fast": True,
            "trust_remote_code": False,
            "counting_call": "tokenizer.encode(text, add_special_tokens=False)",
            "artifacts": inventory,
        }
        models.append(
            {
                "model_id": model_id,
                "local_snapshot_reference": str(root),
                "tokenizer": tokenizer,
            }
        )
        pinned_models[model_id] = {
            "tokenizer_id": tokenizer_id,
            "tokenizer_binding_sha256": canonical_sha256(tokenizer),
            "revision": revision,
        }
    manifest = {"schema_version": "synthetic-model-config/v1", "models": models}
    manifest_path = tmp_path / "model_config_manifest.json"
    manifest_bytes = _canonical_json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(subject, "MODEL_CONFIG_MANIFEST_SHA256", _sha256(manifest_bytes))
    monkeypatch.setattr(subject, "PINNED_MODELS", pinned_models)
    return manifest_path, manifest, roots, artifact_sets


def _assert_unavailable(call: Any) -> None:
    with pytest.raises(CurationError) as exc:
        call()
    assert exc.value.code == "PINNED_TOKENIZER_UNAVAILABLE"


def test_synthetic_manifest_artifacts_runtime_and_counting_are_exact_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _manifest, roots, artifact_sets = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    imported: list[str] = []
    _install_fake_runtime(monkeypatch, imported=imported)
    _FakeTokenizer.ids = lambda text: [index for index, _ in enumerate(text.encode("utf-8"))]

    counters = subject.load_local_pinned_token_counters(
        manifest_path,
        snapshot_roots=roots,
    )

    assert set(counters) == {"qwen3vl_8b", "mai_ui_8b"}
    assert imported == ["tokenizers"]
    assert len(_FakeTokenizer.loaded_json) == 2
    assert set(_FakeTokenizer.loaded_json) == {
        artifact_sets[model_id]["tokenizer.json"].decode("utf-8") for model_id in roots
    }
    for model_id, counter in counters.items():
        assert counter.tokenizer_id == subject.PINNED_MODELS[model_id]["tokenizer_id"]
        assert (
            counter.tokenizer_sha256 == subject.PINNED_MODELS[model_id]["tokenizer_binding_sha256"]
        )
        assert counter.count("éx") == 3
    assert _FakeTokenizer.encode_calls == [("éx", False)] * 4


def test_manifest_bytes_are_hash_bound_and_manifest_symlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _manifest, _roots, _artifacts = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    expected = subject._load_manifest(manifest_path)
    assert expected["schema_version"] == "synthetic-model-config/v1"

    changed = tmp_path / "changed-manifest.json"
    changed.write_bytes(manifest_path.read_bytes() + b"\n")
    _assert_unavailable(lambda: subject._load_manifest(changed))

    link = tmp_path / "manifest-link.json"
    link.symlink_to(manifest_path)
    _assert_unavailable(lambda: subject._load_manifest(link))


def test_missing_manifest_and_missing_tokenizers_runtime_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _manifest, _roots, _artifacts = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    _assert_unavailable(lambda: subject._load_manifest(tmp_path / "absent.json"))

    def missing_version(_package: str) -> str:
        raise subject.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(subject.importlib.metadata, "version", missing_version)
    _assert_unavailable(lambda: subject.load_local_pinned_token_counters(manifest_path))


def test_tokenizers_version_drift_fails_before_runtime_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _manifest, _roots, _artifacts = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    imported: list[str] = []
    _install_fake_runtime(monkeypatch, version="0.22.1", imported=imported)
    _assert_unavailable(lambda: subject.load_local_pinned_token_counters(manifest_path))
    assert imported == []


@pytest.mark.parametrize(
    ("field", "drift"),
    [
        ("revision", "different-revision"),
        ("tokenizers_version", "0.22.1"),
        ("use_fast", False),
        ("trust_remote_code", True),
        ("counting_call", "tokenizer.encode(text)"),
    ],
)
def test_each_semantic_tokenizer_manifest_binding_field_is_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    drift: Any,
) -> None:
    manifest_path, manifest, _roots, _artifacts = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    _install_fake_runtime(monkeypatch)
    changed = deepcopy(manifest)
    changed["models"][0]["tokenizer"][field] = drift
    monkeypatch.setattr(subject, "_load_manifest", lambda _path: changed)
    _assert_unavailable(lambda: subject.load_local_pinned_token_counters(manifest_path))


@pytest.mark.parametrize("model_id", ["qwen3vl_8b", "mai_ui_8b"])
@pytest.mark.parametrize(
    ("artifact_name", "mutation"),
    [
        ("tokenizer.json", "same-size-hash-drift"),
        ("tokenizer.json", "size-drift"),
        ("tokenizer_config.json", "same-size-hash-drift"),
        ("tokenizer_config.json", "size-drift"),
        ("special_tokens_map.json", "same-size-hash-drift"),
        ("special_tokens_map.json", "size-drift"),
    ],
)
def test_every_declared_artifact_size_and_hash_is_checked_before_runtime_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    artifact_name: str,
    mutation: str,
) -> None:
    manifest_path, _manifest, roots, _artifacts = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    imported: list[str] = []
    _install_fake_runtime(monkeypatch, imported=imported)
    path = roots[model_id] / artifact_name
    original = path.read_bytes()
    if mutation == "same-size-hash-drift":
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    else:
        path.write_bytes(original[:-1])

    _assert_unavailable(
        lambda: subject.load_local_pinned_token_counters(
            manifest_path,
            snapshot_roots=roots,
        )
    )
    assert imported == []


def test_missing_artifact_and_symlinked_artifact_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _manifest, roots, _artifacts = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    _install_fake_runtime(monkeypatch)
    missing = roots["qwen3vl_8b"] / "tokenizer_config.json"
    missing.unlink()
    _assert_unavailable(
        lambda: subject.load_local_pinned_token_counters(manifest_path, snapshot_roots=roots)
    )

    # Restore the exact bytes, then replace one declared artifact with a link to
    # an identically sized/hash-matching file.  The path itself is not frozen bytes.
    expected = _artifact_bytes("qwen3vl_8b")["tokenizer_config.json"]
    backing = tmp_path / "unbound-tokenizer-config.json"
    backing.write_bytes(expected)
    missing.symlink_to(backing)
    _assert_unavailable(
        lambda: subject.load_local_pinned_token_counters(manifest_path, snapshot_roots=roots)
    )


def test_symlinked_snapshot_root_and_unknown_model_root_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _manifest, roots, _artifacts = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    _install_fake_runtime(monkeypatch)
    linked_root = tmp_path / "qwen-linked-root"
    linked_root.symlink_to(roots["qwen3vl_8b"], target_is_directory=True)
    supplied = dict(roots)
    supplied["qwen3vl_8b"] = linked_root
    _assert_unavailable(
        lambda: subject.load_local_pinned_token_counters(
            manifest_path,
            snapshot_roots=supplied,
        )
    )
    _assert_unavailable(
        lambda: subject.load_local_pinned_token_counters(
            manifest_path,
            snapshot_roots={**roots, "unregistered_model": tmp_path},
        )
    )


@pytest.mark.parametrize("invalid_ids", [(1, 2), [1, True], [1, "2"], [1, -1]])
def test_invalid_token_id_sequences_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_ids: Any,
) -> None:
    manifest_path, _manifest, roots, _artifacts = _synthetic_bound_environment(
        tmp_path, monkeypatch
    )
    _install_fake_runtime(monkeypatch)
    _FakeTokenizer.ids = invalid_ids
    counters = subject.load_local_pinned_token_counters(manifest_path, snapshot_roots=roots)
    with pytest.raises(CurationError) as exc:
        counters["qwen3vl_8b"].count_without_special_tokens("human-authored exact text")
    assert exc.value.code == "TOKEN_COUNTER_INVALID"


def _load_launcher_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("g1_gold_curation_launcher_test", LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("enable_tokenizers", [False, True])
def test_cli_loads_and_injects_local_counters_only_with_explicit_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enable_tokenizers: bool,
) -> None:
    launcher = _load_launcher_module()
    loaded: list[Path] = []
    counters = {"synthetic": object()}
    publication_arguments: list[Any] = []
    uvicorn_calls: list[dict[str, Any]] = []
    candidate_workspaces: list[Any] = []
    app_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def load_counters(path: Path) -> dict[str, object]:
        loaded.append(path)
        return counters

    class FakePublication:
        def __init__(self, *, preview_token_counters: Any) -> None:
            publication_arguments.append(preview_token_counters)

    class FakeReviewerRegistry:
        @staticmethod
        def load(path: Path) -> tuple[str, Path]:
            return ("registry", path)

    class FakeStore:
        formal_annotation_open = False

        def __init__(self, root: Path, *_args: Any, **_kwargs: Any) -> None:
            self.root = root

        def assert_formal_ai_assistance_eligibility(self, exposed: set[str]) -> None:
            assert exposed == set()

    class FakeCandidateWorkspace:
        def __init__(self, root: Path, publication: Any, **kwargs: Any) -> None:
            self.root = root
            self.publication = publication
            self.kwargs = kwargs
            self.campaign_id = "g1aicampaign-synthetic000000000000"
            candidate_workspaces.append(self)

        def assert_formal_registry_eligible(self, registry: Any) -> None:
            assert registry[0] == "registry"

        def formal_registry_guard(self, registry: Any) -> Any:
            assert registry[0] == "registry"
            return nullcontext()

        def exposed_stable_principal_commitments(self) -> set[str]:
            return set()

    monkeypatch.setattr(launcher, "load_local_pinned_token_counters", load_counters)
    monkeypatch.setattr(launcher, "CurationPublication", FakePublication)
    monkeypatch.setattr(launcher, "ReviewerRegistry", FakeReviewerRegistry)
    monkeypatch.setattr(launcher, "AnnotationStore", FakeStore)
    monkeypatch.setattr(launcher, "AICandidateWorkspace", FakeCandidateWorkspace)

    def create_app(*app_args: Any, **app_kwargs: Any) -> str:
        app_calls.append((app_args, app_kwargs))
        return "synthetic-asgi-app"

    monkeypatch.setattr(launcher, "create_app", create_app)
    monkeypatch.setattr(
        launcher.uvicorn,
        "run",
        lambda _app, **kwargs: uvicorn_calls.append(kwargs),
    )
    argv = [
        str(LAUNCHER),
        "--annotation-root",
        str(tmp_path / "workspace"),
        "--reviewer-registry",
        str(tmp_path / "reviewers.json"),
        "--ai-candidate-root",
        str(tmp_path / "candidates"),
    ]
    if enable_tokenizers:
        argv.append("--load-local-pinned-tokenizers")
    monkeypatch.setattr(sys, "argv", argv)

    assert launcher.main() == 0
    assert publication_arguments == [counters if enable_tokenizers else None]
    assert len(candidate_workspaces) == 1
    assert candidate_workspaces[0].root == tmp_path / "candidates"
    assert candidate_workspaces[0].kwargs == {"forbidden_roots": (tmp_path / "workspace",)}
    assert len(app_calls) == 1
    assert app_calls[0][1] == {
        "ai_candidate_workspace": None,
        "ai_exposure_workspace": candidate_workspaces[0],
    }
    assert len(loaded) == int(enable_tokenizers)
    if enable_tokenizers:
        assert loaded[0] == (
            REPOSITORY_ROOT / "mobileworld_audit_handoff/g1/model_config_manifest.v1.json"
        )
    assert uvicorn_calls == [
        {
            "host": "127.0.0.1",
            "port": 8766,
            "workers": 1,
            "reload": False,
            "proxy_headers": False,
            "access_log": False,
        }
    ]


@pytest.mark.parametrize("candidate_name", ["workspace", "workspace/candidates", "."])
def test_cli_rejects_ai_candidate_root_overlap_before_workspace_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_name: str,
) -> None:
    launcher = _load_launcher_module()
    annotation_root = tmp_path / "workspace"
    candidate_root = tmp_path / candidate_name

    class BombPublication:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("publication was constructed before root overlap rejection")

    monkeypatch.setattr(launcher, "CurationPublication", BombPublication)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(LAUNCHER),
            "--annotation-root",
            str(annotation_root),
            "--reviewer-registry",
            str(tmp_path / "reviewers.json"),
            "--solo-first-pass",
            "--ai-candidate-root",
            str(candidate_root),
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        launcher.main()
    assert exc_info.value.code == 2
    assert not annotation_root.exists()


def test_cli_codec_gate_preparation_rejects_ai_candidate_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    called = False

    def write_gate(*_args: Any, **_kwargs: Any) -> Path:
        nonlocal called
        called = True
        return tmp_path / "unexpected.json"

    monkeypatch.setattr(launcher, "write_codec_gate_receipt", write_gate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(LAUNCHER),
            "--g1-5-publication-manifest",
            str(tmp_path / "g1-5.json"),
            "--prepare-codec-gate-output-root",
            str(tmp_path / "gate"),
            "--ai-candidate-root",
            str(tmp_path / "candidates"),
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        launcher.main()
    assert exc_info.value.code == 2
    assert called is False


def test_cli_rejects_symlink_alias_overlap_before_workspace_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_module()
    annotation_root = tmp_path / "workspace"
    candidate_alias = tmp_path / "candidate-alias"
    candidate_alias.symlink_to(annotation_root, target_is_directory=True)

    class BombPublication:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("publication was constructed before alias overlap rejection")

    monkeypatch.setattr(launcher, "CurationPublication", BombPublication)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(LAUNCHER),
            "--annotation-root",
            str(annotation_root),
            "--reviewer-registry",
            str(tmp_path / "reviewers.json"),
            "--solo-first-pass",
            "--ai-candidate-root",
            str(candidate_alias),
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        launcher.main()
    assert exc_info.value.code == 2
    assert not annotation_root.exists()


def test_tokenizer_loader_and_cli_have_no_model_provider_network_gpu_or_replay_path() -> None:
    paths = [Path(subject.__file__).resolve(), LAUNCHER]
    forbidden_import_roots = {
        "transformers",
        "torch",
        "openai",
        "anthropic",
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "socket",
        "subprocess",
        "vllm",
        "docker",
        "kubernetes",
    }
    forbidden_calls = {
        "urlopen",
        "getaddrinfo",
        "create_connection",
        "Popen",
        "check_call",
        "check_output",
        "cuda",
        "execute_action",
        "execute_live_arm",
        "restore",
        "replay",
    }
    for path in paths:
        tree = ast.parse(path.read_bytes(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                assert imported.isdisjoint(forbidden_import_roots), (path, imported)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not any(
                    module == item or module.startswith(item + ".")
                    for item in forbidden_import_roots
                ), (path, module)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                else:
                    continue
                assert name not in forbidden_calls, (path, name, node.lineno)
