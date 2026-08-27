"""Provider boundary, deterministic fake transport, and hard-disabled live send."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from mobile_world.offline.causal_replay.contracts import (
    AuthorizedProviderRequest,
    JsonValue,
    PreparedProviderRequest,
    ProviderResult,
    ProviderResultStatus,
    RawProviderResponse,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
)
from mobile_world.offline.causal_replay_runner.contracts import (
    FakeScenario,
    ReplayRunnerError,
)

PROVIDER_CONTRACT_VERSION = "v1"
FAKE_PROVIDER_CODEC_ID = "mobileworld.g1.provider.fake-conformance/v1"
FAKE_PROVIDER_ENDPOINT_REVISION = "fake://network-forbidden/v1"


class ActionParser(Protocol):
    @property
    def binding_id(self) -> str: ...

    @property
    def implementation_sha256(self) -> str: ...

    def parse(self, response_bytes: bytes) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]: ...


class JsonActionParser:
    """Fixture parser preserving the structured action object byte-semantically."""

    __slots__ = ()

    binding_id = "mobileworld.g1.fixture-json-action-parser/v1"
    implementation_sha256 = hashlib.sha256(
        b"mobileworld.g1.fixture-json-action-parser/implementation-v1"
    ).hexdigest()

    def parse(self, response_bytes: bytes) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        try:
            payload = json.loads(response_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayRunnerError("MALFORMED_RESPONSE", "response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ReplayRunnerError("PARSER_FAILURE", "response root is not an object")
        if payload.get("force_parser_failure") is True:
            raise ReplayRunnerError("PARSER_FAILURE", "fixture parser failure was requested")
        action = payload.get("action")
        if not isinstance(action, dict):
            raise ReplayRunnerError("PARSER_FAILURE", "response has no structured action")
        normalized = cast(dict[str, JsonValue], copy_json(cast(JsonValue, action)))
        return normalized, {
            "parser_binding_id": self.binding_id,
            "parse_outcome": "PARSED",
            "action_count": 1,
            "action_sha256": canonical_sha256(normalized),
        }


@dataclass(frozen=True)
class ProviderTransportFailure(RuntimeError):
    code: str
    message: str
    retryable: bool
    chunks: tuple[bytes, ...] = ()

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _artifact_ref(response_bytes: bytes) -> dict[str, JsonValue]:
    digest = hashlib.sha256(response_bytes).hexdigest()
    return {
        "sha256": digest,
        "byte_count": len(response_bytes),
        "media_type": "application/json",
        "schema_version": None,
        "relative_path": f"responses/sha256/{digest}.json",
    }


def _validate_model_parameters(
    application_request: JsonValue, model_parameters: dict[str, JsonValue]
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    if not isinstance(application_request, dict):
        raise ReplayRunnerError(
            "INVALID_APPLICATION_REQUEST", "OpenAI-compatible request must be an object"
        )
    sdk_arguments = model_parameters.get("sdk_arguments")
    transport = model_parameters.get("transport")
    if not isinstance(sdk_arguments, dict) or not isinstance(transport, dict):
        raise ReplayRunnerError(
            "INVALID_MODEL_PARAMETERS",
            "model parameters require sdk_arguments and transport objects",
        )
    captured_non_messages = {
        key: value for key, value in application_request.items() if key != "messages"
    }
    sdk_without_seed = {key: value for key, value in sdk_arguments.items() if key != "seed"}
    if sdk_without_seed != captured_non_messages:
        raise ReplayRunnerError(
            "MODEL_PARAMETERS_DRIFT",
            "SDK arguments other than the registered replay seed differ from capture",
        )
    seed = sdk_arguments.get("seed")
    if type(seed) is not int or seed not in {1729, 2718, 31415}:
        raise ReplayRunnerError("REPLAY_SEED_INVALID", "replay seed is not preregistered")
    if transport.get("sdk_max_retries") != 0:
        raise ReplayRunnerError("HIDDEN_PROVIDER_RETRY_FORBIDDEN", "SDK retries must be disabled")
    timeout_seconds = transport.get("timeout_seconds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ReplayRunnerError("INVALID_TIMEOUT", "timeout must be positive")
    return (
        cast(dict[str, JsonValue], copy_json(cast(JsonValue, sdk_arguments))),
        cast(dict[str, JsonValue], copy_json(cast(JsonValue, transport))),
    )


class OpenAICompatibleProviderCodec:
    """Pure encode/normalize boundary; live transport remains mechanically blocked."""

    __slots__ = (
        "_codec_id",
        "_last_parser_diagnostics",
        "encode_calls",
        "endpoint_revision",
        "normalize_calls",
        "parser",
        "send_calls",
    )

    def __init__(
        self,
        *,
        codec_id: str,
        endpoint_revision: str,
        parser: ActionParser,
    ) -> None:
        if not codec_id or not endpoint_revision:
            raise ReplayRunnerError("PROVIDER_IDENTITY_INVALID", "provider identity is missing")
        self._codec_id = codec_id
        self.endpoint_revision = endpoint_revision
        self.parser = parser
        self.encode_calls = 0
        self.send_calls = 0
        self.normalize_calls = 0
        self._last_parser_diagnostics: dict[str, JsonValue] | None = None

    @property
    def codec_id(self) -> str:
        return self._codec_id

    @property
    def contract_version(self) -> str:
        return PROVIDER_CONTRACT_VERSION

    def encode(
        self, application_request: JsonValue, model_parameters: dict[str, JsonValue]
    ) -> PreparedProviderRequest:
        final_application_request = final_sdk_arguments(application_request, model_parameters)
        encoded = canonical_json_bytes(cast(JsonValue, final_application_request))
        self.encode_calls += 1
        return PreparedProviderRequest(
            provider_codec_id=self.codec_id,
            provider_contract_version=self.contract_version,
            endpoint_revision=self.endpoint_revision,
            application_request_sha256=canonical_sha256(application_request),
            encoded_request_sha256=hashlib.sha256(encoded).hexdigest(),
            encoded_request=encoded,
            model_parameters=cast(
                dict[str, JsonValue], copy_json(cast(JsonValue, model_parameters))
            ),
            model_parameters_sha256=canonical_sha256(model_parameters),
        )

    def send(self, authorized: AuthorizedProviderRequest) -> RawProviderResponse:
        del authorized
        self.send_calls += 1
        raise ReplayRunnerError(
            "LIVE_TRANSPORT_DEFERRED",
            "real provider/model transport is not authorized in the CPU-only ALE-322 phase",
        )

    def normalize(
        self, authorized: AuthorizedProviderRequest, response: RawProviderResponse
    ) -> ProviderResult:
        self.normalize_calls += 1
        self._last_parser_diagnostics = None
        response_bytes = bytes(response.response_bytes)
        try:
            action, diagnostics = self.parser.parse(response_bytes)
        except ReplayRunnerError as exc:
            self._last_parser_diagnostics = {
                "parser_binding_id": self.parser.binding_id,
                "parse_outcome": "FAILED",
                "action_count": 0,
                "error_code": exc.code,
            }
            return _provider_result(
                authorized=authorized,
                status=ProviderResultStatus.PARSE_ERROR,
                response_bytes=response_bytes,
                action=None,
                error={"code": exc.code, "message": str(exc), "retryable": False},
            )
        self._last_parser_diagnostics = cast(
            dict[str, JsonValue], copy_json(cast(JsonValue, diagnostics))
        )
        return _provider_result(
            authorized=authorized,
            status=ProviderResultStatus.RETURNED,
            response_bytes=response_bytes,
            action=action,
            error=None,
        )

    def consume_parser_diagnostics(self) -> dict[str, JsonValue]:
        diagnostics = self._last_parser_diagnostics
        if diagnostics is None:
            raise ReplayRunnerError(
                "PARSER_DIAGNOSTICS_MISSING",
                "one parser diagnostics record is required after normalization",
            )
        self._last_parser_diagnostics = None
        return cast(dict[str, JsonValue], copy_json(cast(JsonValue, diagnostics)))


def final_sdk_arguments(
    application_request: JsonValue, model_parameters: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """Build exact SDK arguments with only the registered replay seed added."""

    sdk_arguments, _transport = _validate_model_parameters(application_request, model_parameters)
    assert isinstance(application_request, dict)
    final_application_request = cast(
        dict[str, JsonValue], copy_json(cast(JsonValue, sdk_arguments))
    )
    final_application_request["messages"] = copy_json(application_request["messages"])
    return final_application_request


def _provider_result(
    *,
    authorized: AuthorizedProviderRequest,
    status: ProviderResultStatus,
    response_bytes: bytes | None,
    action: dict[str, JsonValue] | None,
    error: dict[str, JsonValue] | None,
) -> ProviderResult:
    prepared = authorized.prepared
    response_sha = None if response_bytes is None else hashlib.sha256(response_bytes).hexdigest()
    return ProviderResult(
        provider_codec_id=prepared.provider_codec_id,
        provider_contract_version=prepared.provider_contract_version,
        endpoint_revision=prepared.endpoint_revision,
        status=status,
        application_request_sha256=prepared.application_request_sha256,
        encoded_request_sha256=prepared.encoded_request_sha256,
        response_sha256=response_sha,
        raw_response_ref=(None if response_bytes is None else _artifact_ref(response_bytes)),
        normalized_action=(
            None if action is None else cast(dict[str, JsonValue], copy_json(action))
        ),
        normalized_action_sha256=None if action is None else canonical_sha256(action),
        error=None if error is None else cast(dict[str, JsonValue], copy_json(error)),
        model_parameters=cast(
            dict[str, JsonValue], copy_json(cast(JsonValue, prepared.model_parameters))
        ),
        model_parameters_sha256=prepared.model_parameters_sha256,
    )


class DeterministicFakeProviderCodec(OpenAICompatibleProviderCodec):
    """Deterministic test-only ProviderCodec with visible scripted attempts."""

    __slots__ = (
        "_active_run_id",
        "_scenario_history_by_run",
        "_scenarios",
        "scenario_history",
    )

    def __init__(
        self,
        scenarios: Sequence[FakeScenario],
        *,
        parser: ActionParser | None = None,
        codec_id: str = FAKE_PROVIDER_CODEC_ID,
        endpoint_revision: str = FAKE_PROVIDER_ENDPOINT_REVISION,
    ) -> None:
        if not scenarios:
            raise ReplayRunnerError("FAKE_SCENARIO_EMPTY", "fake scenario script is empty")
        if (
            codec_id != FAKE_PROVIDER_CODEC_ID
            or endpoint_revision != FAKE_PROVIDER_ENDPOINT_REVISION
        ):
            raise ReplayRunnerError(
                "FAKE_PROVIDER_IDENTITY_INVALID",
                "fake conformance provider identity and endpoint are frozen",
            )
        super().__init__(
            codec_id=codec_id,
            endpoint_revision=endpoint_revision,
            parser=parser or JsonActionParser(),
        )
        self._scenarios = tuple(scenarios)
        self.scenario_history: list[FakeScenario] = []
        self._scenario_history_by_run: dict[str, list[FakeScenario]] = {}
        self._active_run_id: str | None = None

    @property
    def simulated(self) -> bool:
        return True

    def configuration(self) -> dict[str, JsonValue]:
        return {
            "codec_id": self.codec_id,
            "contract_version": self.contract_version,
            "endpoint_revision": self.endpoint_revision,
            "parser": {
                "binding_id": self.parser.binding_id,
                "implementation_sha256": self.parser.implementation_sha256,
            },
            "scenario_script": [item.value for item in self._scenarios],
            "simulated": True,
            "external_provider_invocation_allowed": False,
        }

    def begin_run(self, run_id: str) -> None:
        if not run_id:
            raise ReplayRunnerError("FAKE_RUN_ID_INVALID", "fake run ID is missing")
        if self._scenario_history_by_run.get(run_id):
            raise ReplayRunnerError(
                "FAKE_RUN_ALREADY_CONSUMED",
                "a fake-provider scenario stream cannot be restarted for the same run",
            )
        self._active_run_id = run_id
        self._scenario_history_by_run.setdefault(run_id, [])

    def _next_scenario(self) -> FakeScenario:
        if self._active_run_id is None:
            raise ReplayRunnerError(
                "FAKE_RUN_NOT_BOUND", "fake scenario must be bound to a logical run"
            )
        run_history = self._scenario_history_by_run[self._active_run_id]
        index = len(run_history)
        scenario = self._scenarios[min(index, len(self._scenarios) - 1)]
        run_history.append(scenario)
        self.scenario_history.append(scenario)
        return scenario

    def send(self, authorized: AuthorizedProviderRequest) -> RawProviderResponse:
        del authorized
        self.send_calls += 1
        scenario = self._next_scenario()
        failure_map = {
            FakeScenario.TIMEOUT: ("TIMEOUT", "fake timeout"),
            FakeScenario.HTTP_5XX: ("HTTP_5XX", "fake HTTP 5xx"),
            FakeScenario.CONNECTION_ERROR: ("CONNECTION_ERROR", "fake connection error"),
        }
        if scenario in failure_map:
            code, message = failure_map[scenario]
            raise ProviderTransportFailure(code, message, True)
        if scenario is FakeScenario.STREAMING_PARTIAL_ERROR:
            partial_chunks = (b'{"action":', b'{"type":"click"')
            raise ProviderTransportFailure(
                "CONNECTION_ERROR", "fake stream interrupted", True, chunks=partial_chunks
            )
        if scenario is FakeScenario.MALFORMED_RESPONSE:
            response = b"{not-json"
        elif scenario is FakeScenario.REFUSAL:
            response = canonical_json_bytes({"refusal": "fixture refusal", "action": None})
        elif scenario is FakeScenario.EMPTY_RESPONSE:
            response = b""
        elif scenario is FakeScenario.NO_OP:
            response = canonical_json_bytes({"action": {"type": "wait"}})
        elif scenario is FakeScenario.PARSER_FAILURE:
            response = canonical_json_bytes({"force_parser_failure": True})
        else:
            response = canonical_json_bytes({"action": {"type": "click", "coordinate": [101, 202]}})
        chunks: list[dict[str, JsonValue]] = []
        if scenario is FakeScenario.STREAMING_SUCCESS:
            split = max(1, len(response) // 3)
            raw_chunks = [response[:split], response[split : 2 * split], response[2 * split :]]
            for index, chunk in enumerate(raw_chunks):
                chunks.append(
                    {
                        "chunk_index": index,
                        "byte_count": len(chunk),
                        "sha256": hashlib.sha256(chunk).hexdigest(),
                        "is_final": index == len(raw_chunks) - 1,
                    }
                )
        return RawProviderResponse(
            response_bytes=response,
            transport_metadata={
                "scenario": scenario.value,
                "simulated": True,
                "external_provider_invoked": False,
                "latency_ms": 7,
                "token_usage": {"input_tokens": 11, "output_tokens": 5},
                "chunks": cast(list[JsonValue], chunks),
            },
        )

    def normalize(
        self, authorized: AuthorizedProviderRequest, response: RawProviderResponse
    ) -> ProviderResult:
        self.normalize_calls += 1
        scenario_value = response.transport_metadata.get("scenario")
        scenario = FakeScenario(cast(str, scenario_value))
        response_bytes = bytes(response.response_bytes)
        if scenario is FakeScenario.REFUSAL:
            self._last_parser_diagnostics = {
                "parser_binding_id": self.parser.binding_id,
                "parse_outcome": "REFUSAL",
                "action_count": 0,
                "error_code": "REFUSAL",
            }
            return _provider_result(
                authorized=authorized,
                status=ProviderResultStatus.PROVIDER_ERROR,
                response_bytes=response_bytes,
                action=None,
                error={
                    "code": "REFUSAL",
                    "message": "provider explicitly refused",
                    "retryable": False,
                },
            )
        if scenario is FakeScenario.EMPTY_RESPONSE:
            self._last_parser_diagnostics = {
                "parser_binding_id": self.parser.binding_id,
                "parse_outcome": "EMPTY_RESPONSE",
                "action_count": 0,
                "error_code": "EMPTY_RESPONSE",
            }
            return _provider_result(
                authorized=authorized,
                status=ProviderResultStatus.PARSE_ERROR,
                response_bytes=response_bytes,
                action=None,
                error={
                    "code": "EMPTY_RESPONSE",
                    "message": "provider returned no bytes",
                    "retryable": False,
                },
            )
        result = super().normalize(authorized, response)
        # super().normalize increments once; keep one normalize call per attempt.
        self.normalize_calls -= 1
        return result


_SEALED_FAKE_PROVIDER_DESCRIPTORS = (
    (OpenAICompatibleProviderCodec, "codec_id", OpenAICompatibleProviderCodec.codec_id),
    (
        OpenAICompatibleProviderCodec,
        "contract_version",
        OpenAICompatibleProviderCodec.contract_version,
    ),
    (OpenAICompatibleProviderCodec, "encode", OpenAICompatibleProviderCodec.encode),
    (OpenAICompatibleProviderCodec, "normalize", OpenAICompatibleProviderCodec.normalize),
    (
        OpenAICompatibleProviderCodec,
        "consume_parser_diagnostics",
        OpenAICompatibleProviderCodec.consume_parser_diagnostics,
    ),
    (DeterministicFakeProviderCodec, "simulated", DeterministicFakeProviderCodec.simulated),
    (
        DeterministicFakeProviderCodec,
        "configuration",
        DeterministicFakeProviderCodec.configuration,
    ),
    (DeterministicFakeProviderCodec, "begin_run", DeterministicFakeProviderCodec.begin_run),
    (
        DeterministicFakeProviderCodec,
        "_next_scenario",
        DeterministicFakeProviderCodec._next_scenario,
    ),
    (DeterministicFakeProviderCodec, "send", DeterministicFakeProviderCodec.send),
    (DeterministicFakeProviderCodec, "normalize", DeterministicFakeProviderCodec.normalize),
)
_SEALED_JSON_ACTION_PARSE = JsonActionParser.parse
_SEALED_JSON_ACTION_BINDING_ID = JsonActionParser.binding_id
_SEALED_JSON_ACTION_IMPLEMENTATION_SHA256 = JsonActionParser.implementation_sha256


def validate_fake_provider_implementation(provider: object) -> None:
    """Require the exact immutable CPU fake/provider parser implementation."""

    if (
        type(provider) is not DeterministicFakeProviderCodec
        or hasattr(provider, "__dict__")
        or type(provider.parser) is not JsonActionParser
        or hasattr(provider.parser, "__dict__")
        or JsonActionParser.parse is not _SEALED_JSON_ACTION_PARSE
        or JsonActionParser.binding_id != _SEALED_JSON_ACTION_BINDING_ID
        or JsonActionParser.implementation_sha256 != _SEALED_JSON_ACTION_IMPLEMENTATION_SHA256
        or any(
            getattr(owner, attribute) is not expected
            for owner, attribute, expected in _SEALED_FAKE_PROVIDER_DESCRIPTORS
        )
    ):
        raise ReplayRunnerError(
            "FAKE_PROVIDER_IMPLEMENTATION_MISMATCH",
            "CPU fake execution requires the sealed network-forbidden provider and parser methods",
        )


def normalize_fake_response_pure(
    authorized: AuthorizedProviderRequest, response_bytes: bytes
) -> tuple[ProviderResult, dict[str, JsonValue]]:
    """Recompute the sealed fake parser result without mutating provider state."""

    if response_bytes == b"":
        diagnostics: dict[str, JsonValue] = {
            "parser_binding_id": JsonActionParser.binding_id,
            "parse_outcome": "EMPTY_RESPONSE",
            "action_count": 0,
            "error_code": "EMPTY_RESPONSE",
        }
        return (
            _provider_result(
                authorized=authorized,
                status=ProviderResultStatus.PARSE_ERROR,
                response_bytes=response_bytes,
                action=None,
                error={
                    "code": "EMPTY_RESPONSE",
                    "message": "provider returned no bytes",
                    "retryable": False,
                },
            ),
            diagnostics,
        )
    try:
        payload = json.loads(response_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if (
        isinstance(payload, dict)
        and payload.get("refusal") is not None
        and payload.get("action") is None
    ):
        diagnostics = {
            "parser_binding_id": JsonActionParser.binding_id,
            "parse_outcome": "REFUSAL",
            "action_count": 0,
            "error_code": "REFUSAL",
        }
        return (
            _provider_result(
                authorized=authorized,
                status=ProviderResultStatus.PROVIDER_ERROR,
                response_bytes=response_bytes,
                action=None,
                error={
                    "code": "REFUSAL",
                    "message": "provider explicitly refused",
                    "retryable": False,
                },
            ),
            diagnostics,
        )
    parser = JsonActionParser()
    try:
        action, diagnostics = parser.parse(response_bytes)
    except ReplayRunnerError as exc:
        diagnostics = {
            "parser_binding_id": parser.binding_id,
            "parse_outcome": "FAILED",
            "action_count": 0,
            "error_code": exc.code,
        }
        return (
            _provider_result(
                authorized=authorized,
                status=ProviderResultStatus.PARSE_ERROR,
                response_bytes=response_bytes,
                action=None,
                error={"code": exc.code, "message": str(exc), "retryable": False},
            ),
            diagnostics,
        )
    return (
        _provider_result(
            authorized=authorized,
            status=ProviderResultStatus.RETURNED,
            response_bytes=response_bytes,
            action=action,
            error=None,
        ),
        diagnostics,
    )


def parser_adapter(
    binding_id: str,
    parse: Callable[[bytes], tuple[dict[str, JsonValue], dict[str, JsonValue]]],
    *,
    implementation_sha256: str,
) -> ActionParser:
    """Create a declared parser adapter for later host-equivalence tests."""

    if (
        not binding_id
        or len(implementation_sha256) != 64
        or any(character not in "0123456789abcdef" for character in implementation_sha256)
    ):
        raise ReplayRunnerError(
            "PARSER_DECLARATION_INVALID",
            "parser adapters require a stable binding and lowercase implementation SHA-256",
        )

    class _Adapter:
        @property
        def binding_id(self) -> str:
            return binding_id

        @property
        def implementation_sha256(self) -> str:
            return implementation_sha256

        def parse(self, response_bytes: bytes) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
            action, diagnostics = parse(response_bytes)
            return (
                cast(dict[str, JsonValue], copy_json(cast(JsonValue, action))),
                cast(dict[str, JsonValue], copy_json(cast(JsonValue, diagnostics))),
            )

    return _Adapter()
