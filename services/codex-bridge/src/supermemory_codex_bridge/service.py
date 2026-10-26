from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Mapping, MutableMapping
import uuid

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

from .translator import (
	BridgeProtocolError,
	CodexTurn,
	build_codex_turn,
	parse_codex_result,
	to_chat_completion,
)


BASE_INSTRUCTIONS = (
	"Act only as a structured decision engine for the external memory curator. "
	"Do not inspect files, run commands, access networks, call native tools, "
	"or perform the external functions yourself. Return only the structured "
	"answer requested by the current output schema."
)

CODEX_ENVIRONMENT_KEYS = {
	"CODEX_HOME",
	"HOME",
	"LANG",
	"LC_ALL",
	"PATH",
	"SSL_CERT_DIR",
	"SSL_CERT_FILE",
	"TMPDIR",
}
DISABLED_CODEX_FEATURES = (
	"apply_patch_streaming_events",
	"apps",
	"artifact",
	"auth_elicitation",
	"browser_use",
	"browser_use_external",
	"browser_use_full_cdp_access",
	"code_mode",
	"code_mode_host",
	"code_mode_only",
	"computer_use",
	"deferred_executor",
	"deferred_tool_world_state",
	"enable_mcp_apps",
	"executor_capability_discovery",
	"external_agent_memory_import",
	"fast_mode",
	"goals",
	"hooks",
	"image_generation",
	"in_app_browser",
	"mcp_2026_07_28",
	"memories",
	"multi_agent",
	"multi_agent_v2",
	"network_proxy",
	"plugin_sharing",
	"plugins",
	"recommended_plugins",
	"remote_plugin",
	"request_permissions_tool",
	"shell_snapshot",
	"shell_tool",
	"shell_zsh_fork",
	"skill_mcp_dependency_install",
	"skill_search",
	"sleep_tool",
	"standalone_web_search",
	"tool_call_mcp_elicitation",
	"tool_suggest",
	"unified_exec",
	"view_image",
	"workspace_dependencies",
)
CODEX_CONFIG_OVERRIDES = tuple(
	f"features.{feature}=false" for feature in DISABLED_CODEX_FEATURES
) + (
	'shell_environment_policy.inherit="none"',
	'web_search="disabled"',
	'model_reasoning_effort="high"',
	'agents.enabled=false',
	'tools.update_plan.enabled=false',
	'tools.experimental_request_user_input.enabled=false',
	'features.code_mode_host.disable_in_process_fallback=true',
)
ALLOWED_TURN_ITEM_TYPES = {
	"AgentMessageThreadItem",
	"ReasoningThreadItem",
	"UserMessageThreadItem",
}
MAX_BODY_BYTES = 8 * 1024 * 1024
LOGGER = logging.getLogger("supermemory_codex_bridge")


class BridgeAuthError(RuntimeError):
	pass


class BridgeQuotaError(RuntimeError):
	pass


class BridgeRuntimeError(RuntimeError):
	pass


class BridgePromptTooLargeError(RuntimeError):
	pass


class BridgeTimeoutError(TimeoutError):
	pass


@dataclass(frozen=True)
class RuntimeResponse:
	text: str
	usage: dict[str, int]


def validate_bridge_token(token: str) -> str:
	if not re.fullmatch(r"[0-9a-f]{64}", token):
		raise ValueError("CODEX_BRIDGE_TOKEN must be 64 lowercase hexadecimal characters")
	return token


@dataclass(frozen=True)
class BridgeConfig:
	token: str
	model: str
	host: str
	port: int
	timeout_seconds: float

	@classmethod
	def from_environment(
		cls,
		source: Mapping[str, str] | None = None,
	) -> "BridgeConfig":
		source = os.environ if source is None else source
		token = validate_bridge_token(source.get("CODEX_BRIDGE_TOKEN", ""))
		model = source.get("CODEX_BRIDGE_MODEL", "gpt-6-luna").strip()
		host = source.get("CODEX_BRIDGE_HOST", "0.0.0.0").strip()
		try:
			port = int(source.get("CODEX_BRIDGE_PORT", "8787"))
			timeout_seconds = float(
				source.get("CODEX_BRIDGE_TIMEOUT_SECONDS", "240")
			)
		except ValueError as error:
			raise ValueError("bridge port and timeout must be numeric") from error
		if not model:
			raise ValueError("CODEX_BRIDGE_MODEL is required")
		if not host:
			raise ValueError("CODEX_BRIDGE_HOST is required")
		if not 1 <= port <= 65535:
			raise ValueError("CODEX_BRIDGE_PORT must be between 1 and 65535")
		if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
			raise ValueError("CODEX_BRIDGE_TIMEOUT_SECONDS must be positive")
		return cls(
			token=token,
			model=model,
			host=host,
			port=port,
			timeout_seconds=timeout_seconds,
		)


def build_codex_environment(
	source: Mapping[str, str] | None = None,
) -> dict[str, str]:
	source = os.environ if source is None else source
	return {
		key: value
		for key in CODEX_ENVIRONMENT_KEYS
		if (value := source.get(key)) is not None
	}


def isolate_process_environment(
	environment: MutableMapping[str, str],
) -> dict[str, str]:
	isolated = build_codex_environment(environment)
	environment.clear()
	environment.update(isolated)
	return isolated


def resolve_workspace(source: Mapping[str, str] | None = None) -> str:
	source = os.environ if source is None else source
	workspace = source.get("CODEX_BRIDGE_WORKSPACE", "/workspace")
	if not os.path.isabs(workspace) or not os.path.isdir(workspace):
		raise BridgeRuntimeError(
			"bridge workspace must be an existing absolute directory"
		)
	return workspace


def _map_runtime_error(error: Exception) -> Exception:
	message = str(error).lower()
	if "exceeds the maximum length" in message:
		# Codex rejects the input cleanly (JSON-RPC -32602); the runtime stays healthy.
		return BridgePromptTooLargeError("prompt exceeds the Codex input limit")
	if any(
		marker in message
		for marker in (
			"usage limit",
			"quota",
			"rate limit",
			"rate_limit",
			"http 429",
		)
	):
		return BridgeQuotaError("Codex subscription quota is unavailable")
	if any(
		marker in message
		for marker in (
			"unauthorized",
			"not authenticated",
			"please log in",
			"please login",
			"invalid_grant",
			"refresh token",
		)
	):
		return BridgeAuthError("Codex ChatGPT authentication is unavailable")
	return BridgeRuntimeError(f"Codex runtime failed: {type(error).__name__}")


class CodexRuntime:
	def __init__(
		self,
		codex: Any,
		*,
		workspace: str = "/workspace",
	):
		self._codex = codex
		self.workspace = workspace
		self._available = True

	@classmethod
	def create(
		cls,
		cwd: str | None = None,
		*,
		environment: MutableMapping[str, str] | None = None,
	) -> "CodexRuntime":
		environment = os.environ if environment is None else environment
		cwd = cwd or resolve_workspace(environment)
		isolated_environment = isolate_process_environment(environment)
		config = CodexConfig(
			cwd=cwd,
			env=isolated_environment,
			config_overrides=CODEX_CONFIG_OVERRIDES,
			client_name="supermemory_codex_bridge",
			client_title="SuperMemory Codex Bridge",
		)
		return cls(Codex(config=config), workspace=cwd)

	def _require_available(self) -> None:
		if not self._available:
			raise BridgeRuntimeError("Codex runtime is unavailable")

	@property
	def available(self) -> bool:
		return self._available

	def _runtime_error(self, error: Exception) -> Exception:
		mapped = _map_runtime_error(error)
		# The mapped error hides the cause; keep a short, content-free hint for diagnosis.
		LOGGER.warning("runtime_error type=%s detail=%.300r", type(error).__name__, str(error))
		if isinstance(mapped, BridgeRuntimeError):
			self.close()
		return mapped

	def close(self) -> None:
		if self._available:
			self._available = False
			self._codex.close()

	def login_device_code(self):
		self._require_available()
		return self._codex.login_chatgpt_device_code()

	def require_chatgpt(self) -> str:
		self._require_available()
		try:
			account_wrapper = self._codex.account(refresh_token=True).account
		except Exception as error:
			raise self._runtime_error(error) from error
		if account_wrapper is None:
			raise BridgeAuthError("Codex must be authenticated with ChatGPT")
		account = account_wrapper.root
		if account.type != "chatgpt":
			raise BridgeAuthError("Codex must be authenticated with ChatGPT")
		plan_type = account.plan_type
		return getattr(plan_type, "value", str(plan_type))

	def has_model(self, model: str) -> bool:
		self._require_available()
		try:
			models = self._codex.models(include_hidden=False).data
		except Exception as error:
			raise self._runtime_error(error) from error
		return any(entry.id == model and not entry.hidden for entry in models)

	def run(
		self,
		turn: CodexTurn,
		*,
		timeout_seconds: float,
	) -> RuntimeResponse:
		self._require_available()

		def run_turn():
			thread = self._codex.thread_start(
				approval_mode=ApprovalMode.deny_all,
				base_instructions=BASE_INSTRUCTIONS,
				config={"web_search": "disabled"},
				developer_instructions=turn.developer_instructions,
				ephemeral=True,
				model=turn.model,
				cwd=self.workspace,
				sandbox=Sandbox.read_only,
				service_name="supermemory_codex_bridge",
			)
			self._require_available()
			handle = thread.turn(
				turn.prompt,
				approval_mode=ApprovalMode.deny_all,
				output_schema=turn.output_schema,
				sandbox=Sandbox.read_only,
			)
			self._require_available()
			return handle.run()

		executor = ThreadPoolExecutor(max_workers=1)
		future = executor.submit(run_turn)
		try:
			result = future.result(timeout=timeout_seconds)
		except FutureTimeoutError as error:
			# Retire the ephemeral runtime: setup and interruption can block too.
			self.close()
			raise BridgeTimeoutError(
				f"Codex turn timed out after {timeout_seconds:g} seconds"
			) from error
		except Exception as error:
			raise self._runtime_error(error) from error
		finally:
			executor.shutdown(wait=False, cancel_futures=True)

		unexpected = {
			type(getattr(item, "root", item)).__name__ for item in getattr(result, "items", [])
		} - ALLOWED_TURN_ITEM_TYPES
		if unexpected == {"ContextCompactionThreadItem"}:
			# Codex compacted the prompt because it outgrew the model's context window. The runtime is
			# healthy, so reject this request without retiring it (a retired runtime restarts the bridge).
			raise BridgePromptTooLargeError("prompt exceeds the model context window")
		if unexpected:
			LOGGER.warning("disabled_item types=%s", sorted(unexpected))
			self.close()
			raise BridgeRuntimeError("Codex attempted a disabled native tool")
		if not result.final_response:
			raise BridgeRuntimeError("Codex turn completed without a final response")

		usage = {
			"prompt_tokens": 0,
			"completion_tokens": 0,
			"total_tokens": 0,
		}
		if result.usage is not None:
			last = result.usage.last
			usage = {
				"prompt_tokens": last.input_tokens,
				"completion_tokens": last.output_tokens,
				"total_tokens": last.total_tokens,
			}
		return RuntimeResponse(text=result.final_response, usage=usage)


class BridgeApplication:
	def __init__(
		self,
		*,
		runtime: CodexRuntime,
		token: str,
		model: str,
		timeout_seconds: float,
	):
		self.runtime = runtime
		self.token = token
		self.model = model
		self.timeout_seconds = timeout_seconds
		self.capacity = threading.BoundedSemaphore(1)

	def health(self) -> dict[str, Any]:
		plan_type = self.runtime.require_chatgpt()
		if not self.runtime.has_model(self.model):
			raise BridgeRuntimeError(
				f"configured Codex model is unavailable: {self.model}"
			)
		return {
			"status": "ok",
			"auth_mode": "chatgpt",
			"plan_type": plan_type,
			"model": self.model,
		}


class BridgeHTTPServer(ThreadingHTTPServer):
	daemon_threads = True

	def __init__(self, address, application):
		super().__init__(address, BridgeHandler)
		self.application = application


class BridgeHandler(BaseHTTPRequestHandler):
	server_version = "supermemory-codex-bridge/0.1"

	@property
	def application(self) -> BridgeApplication:
		return self.server.application

	def log_message(self, _format, *_args):
		return

	def finish(self) -> None:
		try:
			super().finish()
		finally:
			if not self.application.runtime.available:
				# Called on a request thread, after flushing its error response.
				self.server.shutdown()

	def _send_json(
		self,
		status: int,
		payload: dict[str, Any],
		*,
		extra_headers: Mapping[str, str] | None = None,
	) -> None:
		encoded = json.dumps(
			payload,
			ensure_ascii=False,
			separators=(",", ":"),
		).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(encoded)))
		for name, value in (extra_headers or {}).items():
			self.send_header(name, value)
		self.end_headers()
		self.wfile.write(encoded)

	def _send_error(
		self,
		status: int,
		message: str,
		code: str,
		*,
		extra_headers: Mapping[str, str] | None = None,
	) -> None:
		self._send_json(
			status,
			{
				"error": {
					"message": message,
					"type": code,
					"code": code,
				}
			},
			extra_headers=extra_headers,
		)

	def _authenticated(self) -> bool:
		values = self.headers.get_all("Authorization", [])
		if len(values) != 1:
			return False
		received = values[0]
		if len(received.encode()) > 256:
			return False
		expected = f"Bearer {self.application.token}"
		return hmac.compare_digest(received.encode(), expected.encode())

	def _require_authentication(self) -> bool:
		if self._authenticated():
			return True
		self._send_error(401, "unauthorized", "invalid_api_key")
		return False

	def do_GET(self) -> None:
		if not self._require_authentication():
			return
		if self.path == "/health":
			try:
				self._send_json(200, self.application.health())
			except BridgeAuthError:
				self._send_error(401, "ChatGPT authentication unavailable", "authentication_error")
			except BridgeQuotaError:
				self._send_error(429, "Codex subscription quota unavailable", "rate_limit_error")
			except BridgeRuntimeError:
				self._send_error(503, "Codex runtime unavailable", "service_unavailable")
			return
		if self.path == "/v1/models":
			try:
				self.application.health()
			except BridgeAuthError:
				self._send_error(401, "ChatGPT authentication unavailable", "authentication_error")
				return
			except BridgeQuotaError:
				self._send_error(429, "Codex subscription quota unavailable", "rate_limit_error")
				return
			except BridgeRuntimeError:
				self._send_error(503, "Codex runtime unavailable", "service_unavailable")
				return
			self._send_json(
				200,
				{
					"object": "list",
					"data": [
						{
							"id": self.application.model,
							"object": "model",
							"created": 1788048000,
							"owned_by": "openai-codex-subscription",
						}
					],
				},
			)
			return
		self._send_error(404, "not found", "not_found")

	def _read_json_body(self) -> tuple[dict[str, Any] | None, int]:
		try:
			content_length = int(self.headers.get("Content-Length", ""))
		except ValueError:
			self._send_error(400, "invalid Content-Length", "invalid_request")
			return None, 0
		if content_length < 0:
			self._send_error(400, "invalid Content-Length", "invalid_request")
			return None, 0
		if content_length > MAX_BODY_BYTES:
			self._send_error(413, "request body too large", "request_too_large")
			return None, content_length
		try:
			body = json.loads(self.rfile.read(content_length) or b"{}")
		except (json.JSONDecodeError, UnicodeDecodeError):
			self._send_error(400, "invalid JSON body", "invalid_request")
			return None, content_length
		if not isinstance(body, dict):
			self._send_error(400, "request body must be an object", "invalid_request")
			return None, content_length
		return body, content_length

	def do_POST(self) -> None:
		started_at = time.monotonic()
		request_id = uuid.uuid4().hex
		status = 500
		body_size = 0
		if not self._require_authentication():
			return
		if self.path != "/v1/chat/completions":
			self._send_error(404, "not found", "not_found")
			return
		body, body_size = self._read_json_body()
		if body is None:
			return
		if body.get("model", self.application.model) != self.application.model:
			self._send_error(400, "unsupported model", "invalid_request")
			return
		if not self.application.capacity.acquire(blocking=False):
			self._send_error(
				429,
				"bridge is busy",
				"rate_limit_error",
				extra_headers={"Retry-After": "5"},
			)
			return

		try:
			try:
				turn = build_codex_turn(body, self.application.model)
			except BridgeProtocolError:
				status = 400
				self._send_error(status, "unsupported request", "invalid_request")
				return

			try:
				runtime_response = self.application.runtime.run(
					turn,
					timeout_seconds=self.application.timeout_seconds,
				)
			except BridgeAuthError:
				status = 401
				self._send_error(status, "ChatGPT authentication unavailable", "authentication_error")
				return
			except BridgeQuotaError:
				status = 429
				self._send_error(status, "Codex subscription quota unavailable", "rate_limit_error")
				return
			except BridgeTimeoutError:
				status = 504
				self._send_error(status, "Codex turn timed out", "timeout")
				return
			except BridgePromptTooLargeError:
				status = 413
				self._send_error(status, "prompt too large for the model context", "invalid_request")
				return
			except BridgeRuntimeError:
				status = 503
				self._send_error(status, "Codex runtime unavailable", "service_unavailable")
				return

			try:
				result = parse_codex_result(turn, runtime_response.text)
			except BridgeProtocolError:
				status = 502
				self._send_error(status, "invalid Codex response", "invalid_upstream_response")
				return

			completion = to_chat_completion(
				result,
				model=self.application.model,
				request_id=request_id,
				usage=runtime_response.usage,
			)
			status = 200
			self._send_json(status, completion)
		finally:
			self.application.capacity.release()
			duration_ms = int((time.monotonic() - started_at) * 1000)
			LOGGER.info(
				"request_complete id=%s route=%s bytes=%d duration_ms=%d model=%s status=%d",
				request_id,
				self.path,
				body_size,
				duration_ms,
				self.application.model,
				status,
			)


def create_server(
	application: BridgeApplication,
	host: str,
	port: int,
) -> BridgeHTTPServer:
	return BridgeHTTPServer((host, port), application)
