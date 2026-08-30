import http.client
import json
import os
import tempfile
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from openai_codex import ApprovalMode, Sandbox

from supermemory_codex_bridge.service import (
	BridgeAuthError,
	BridgeApplication,
	BridgeConfig,
	BridgeQuotaError,
	BridgeRuntimeError,
	BridgeTimeoutError,
	CODEX_CONFIG_OVERRIDES,
	CodexRuntime,
	RuntimeResponse,
	build_codex_environment,
	create_server,
	isolate_process_environment,
	resolve_workspace,
	validate_bridge_token,
)
from supermemory_codex_bridge.translator import CodexTurn


def sample_turn():
	return CodexTurn(
		mode="json_object",
		prompt='{"messages":[]}',
		developer_instructions="Return JSON only.",
		output_schema={"type": "object"},
		model="gpt-5.6-luna",
		tool_schemas={},
		tool_choice="auto",
	)


class FakeHandle:
	def __init__(self, result=None, wait_for_interrupt=False, ignore_interrupt=False):
		self.result = result or SimpleNamespace(
			final_response='{"description":"ok"}',
			items=[],
			usage=SimpleNamespace(
				last=SimpleNamespace(
					input_tokens=11,
					output_tokens=7,
					total_tokens=18,
				)
			),
		)
		self.wait_for_interrupt = wait_for_interrupt
		self.ignore_interrupt = ignore_interrupt
		self.interrupted = False
		self.done = threading.Event()

	def run(self):
		if self.wait_for_interrupt:
			self.done.wait()
		return self.result

	def interrupt(self):
		self.interrupted = True
		if not self.ignore_interrupt:
			self.done.set()

	def force_stop(self):
		self.done.set()


class FakeThread:
	def __init__(self, handle):
		self.handle = handle
		self.turn_kwargs = None
		self.turn_input = None

	def turn(self, turn_input, **kwargs):
		self.turn_input = turn_input
		self.turn_kwargs = kwargs
		return self.handle


class FakeCodex:
	def __init__(self, account_type="chatgpt", handle=None):
		root = SimpleNamespace(type=account_type)
		if account_type == "chatgpt":
			root.plan_type = SimpleNamespace(value="plus")
		self.account_response = SimpleNamespace(
			account=SimpleNamespace(root=root)
		)
		self.thread = FakeThread(handle or FakeHandle())
		self.thread_start_kwargs = None
		self.closed = False

	def account(self, refresh_token=False):
		self.refresh_token = refresh_token
		return self.account_response

	def thread_start(self, **kwargs):
		self.thread_start_kwargs = kwargs
		return self.thread

	def models(self, include_hidden=False):
		return SimpleNamespace(
			data=[SimpleNamespace(id="gpt-5.6-luna", hidden=False)]
		)

	def login_chatgpt_device_code(self):
		return SimpleNamespace(
			verification_url="https://auth.openai.com/codex/device",
			user_code="ABCD-1234",
		)

	def close(self):
		self.closed = True
		self.thread.handle.force_stop()


class CodexEnvironmentTest(unittest.TestCase):
	def test_replaces_inherited_environment_before_sdk_start(self):
		environment = {
			"PATH": "/usr/bin",
			"HOME": "/home/bridge",
			"CODEX_HOME": "/var/lib/codex",
			"CODEX_BRIDGE_TOKEN": "a" * 64,
			"OPENAI_API_KEY": "sk-paid",
		}

		isolated = isolate_process_environment(environment)

		self.assertEqual(environment, isolated)
		self.assertEqual(set(environment), {"PATH", "HOME", "CODEX_HOME"})

	def test_scrubs_bridge_and_provider_secrets(self):
		environment = build_codex_environment(
			{
				"PATH": "/usr/bin",
				"HOME": "/home/bridge",
				"CODEX_HOME": "/var/lib/codex",
				"LANG": "C.UTF-8",
				"CODEX_BRIDGE_TOKEN": "local-secret",
				"OPENAI_API_KEY": "sk-paid",
				"CODEX_API_KEY": "sk-codex",
				"ANTHROPIC_API_KEY": "sk-ant",
			}
		)

		self.assertEqual(environment["CODEX_HOME"], "/var/lib/codex")
		self.assertNotIn("CODEX_BRIDGE_TOKEN", environment)
		self.assertNotIn("OPENAI_API_KEY", environment)
		self.assertNotIn("CODEX_API_KEY", environment)
		self.assertNotIn("ANTHROPIC_API_KEY", environment)

	def test_requires_64_lowercase_hex_local_token(self):
		self.assertEqual(validate_bridge_token("a" * 64), "a" * 64)
		for invalid in ("sk-local", "a" * 63, "A" * 64, "z" * 64):
			with self.subTest(invalid=invalid):
				with self.assertRaises(ValueError):
					validate_bridge_token(invalid)

	def test_reads_bridge_configuration_from_environment(self):
		config = BridgeConfig.from_environment(
			{
				"CODEX_BRIDGE_TOKEN": "a" * 64,
				"CODEX_BRIDGE_MODEL": "gpt-5.6-luna",
				"CODEX_BRIDGE_HOST": "127.0.0.1",
				"CODEX_BRIDGE_PORT": "9876",
				"CODEX_BRIDGE_TIMEOUT_SECONDS": "42",
			}
		)

		self.assertEqual(config.port, 9876)
		self.assertEqual(config.timeout_seconds, 42)
		self.assertEqual(config.model, "gpt-5.6-luna")

	def test_resolves_existing_configured_workspace(self):
		with tempfile.TemporaryDirectory() as workspace:
			self.assertEqual(
				resolve_workspace({"CODEX_BRIDGE_WORKSPACE": workspace}),
				workspace,
			)

		with self.assertRaisesRegex(BridgeRuntimeError, "workspace"):
			resolve_workspace({"CODEX_BRIDGE_WORKSPACE": "/missing/workspace"})


class CodexRuntimeTest(unittest.TestCase):
	def test_create_scrubs_parent_and_disables_native_tool_features(self):
		environment = {
			"PATH": "/usr/bin",
			"HOME": "/home/bridge",
			"CODEX_HOME": "/var/lib/codex",
			"CODEX_BRIDGE_TOKEN": "a" * 64,
			"OPENAI_API_KEY": "sk-paid",
		}
		with tempfile.TemporaryDirectory() as workspace:
			with patch("supermemory_codex_bridge.service.Codex") as constructor:
				constructor.return_value = FakeCodex()
				runtime = CodexRuntime.create(
					cwd=workspace,
					environment=environment,
				)

		config = constructor.call_args.kwargs["config"]
		self.assertNotIn("CODEX_BRIDGE_TOKEN", environment)
		self.assertNotIn("OPENAI_API_KEY", environment)
		self.assertEqual(config.env, environment)
		self.assertEqual(config.config_overrides, CODEX_CONFIG_OVERRIDES)
		for feature in (
			"apply_patch_freeform",
			"deferred_executor",
			"js_repl",
			"search_tool",
			"standalone_web_search",
			"web_search_cached",
			"web_search_request",
		):
			self.assertIn(
				f"features.{feature}=false",
				config.config_overrides,
			)
		self.assertIn('web_search="disabled"', config.config_overrides)
		runtime.close()

	def test_requires_chatgpt_account(self):
		with self.assertRaisesRegex(BridgeAuthError, "ChatGPT"):
			CodexRuntime(FakeCodex(account_type="apiKey")).require_chatgpt()

	def test_reports_plan_and_available_model(self):
		runtime = CodexRuntime(FakeCodex())

		self.assertEqual(runtime.require_chatgpt(), "plus")
		self.assertTrue(runtime.has_model("gpt-5.6-luna"))
		self.assertFalse(runtime.has_model("missing-model"))

	def test_runs_ephemeral_deny_all_read_only_turn(self):
		fake = FakeCodex()
		runtime = CodexRuntime(fake)

		response = runtime.run(sample_turn(), timeout_seconds=1)

		self.assertEqual(fake.thread_start_kwargs["ephemeral"], True)
		self.assertEqual(
			fake.thread_start_kwargs["approval_mode"], ApprovalMode.deny_all
		)
		self.assertEqual(
			fake.thread_start_kwargs["sandbox"], Sandbox.read_only
		)
		self.assertEqual(
			fake.thread_start_kwargs["config"],
			{"web_search": "disabled"},
		)
		self.assertEqual(fake.thread_start_kwargs["cwd"], "/workspace")
		self.assertEqual(fake.thread.turn_input, sample_turn().prompt)
		self.assertEqual(
			fake.thread.turn_kwargs["output_schema"],
			sample_turn().output_schema,
		)
		self.assertEqual(response.text, '{"description":"ok"}')
		self.assertEqual(
			response.usage,
			{
				"prompt_tokens": 11,
				"completion_tokens": 7,
				"total_tokens": 18,
			},
		)

	def test_interrupts_turn_on_timeout(self):
		handle = FakeHandle(wait_for_interrupt=True)
		runtime = CodexRuntime(FakeCodex(handle=handle))

		with self.assertRaisesRegex(BridgeTimeoutError, "timed out"):
			runtime.run(sample_turn(), timeout_seconds=0.01)

		self.assertTrue(handle.interrupted)

	def test_closes_runtime_when_interruption_does_not_settle(self):
		handle = FakeHandle(wait_for_interrupt=True, ignore_interrupt=True)
		fake = FakeCodex(handle=handle)
		runtime = CodexRuntime(fake, interrupt_grace_seconds=0.01)

		with self.assertRaisesRegex(BridgeTimeoutError, "timed out"):
			runtime.run(sample_turn(), timeout_seconds=0.01)

		self.assertTrue(fake.closed)
		with self.assertRaisesRegex(BridgeRuntimeError, "unavailable"):
			runtime.run(sample_turn(), timeout_seconds=1)

	def test_rejects_any_native_tool_item(self):
		result = SimpleNamespace(
			final_response='{"description":"unsafe"}',
			items=[type("CommandExecutionThreadItem", (), {})()],
			usage=None,
		)
		fake = FakeCodex(handle=FakeHandle(result=result))
		runtime = CodexRuntime(fake)

		with self.assertRaisesRegex(BridgeRuntimeError, "native tool"):
			runtime.run(sample_turn(), timeout_seconds=1)
		self.assertTrue(fake.closed)

	def test_retires_runtime_for_native_tool_without_final_response(self):
		result = SimpleNamespace(
			final_response=None,
			items=[type("WebSearchThreadItem", (), {})()],
			usage=None,
		)
		fake = FakeCodex(handle=FakeHandle(result=result))
		runtime = CodexRuntime(fake)

		with self.assertRaisesRegex(BridgeRuntimeError, "native tool"):
			runtime.run(sample_turn(), timeout_seconds=1)
		self.assertTrue(fake.closed)

	def test_closes_official_sdk_client(self):
		fake = FakeCodex()
		runtime = CodexRuntime(fake)

		runtime.close()

		self.assertTrue(fake.closed)

	def test_starts_official_device_code_login(self):
		runtime = CodexRuntime(FakeCodex())

		handle = runtime.login_device_code()

		self.assertEqual(handle.user_code, "ABCD-1234")


class FakeBridgeRuntime:
	def __init__(self):
		self.error = None
		self.text_override = None
		self.run_calls = 0

	def require_chatgpt(self):
		return "plus"

	def has_model(self, model):
		return model == "fixture-model"

	def run(self, turn, *, timeout_seconds):
		self.run_calls += 1
		if self.error:
			raise self.error
		if self.text_override is not None:
			text = self.text_override
		elif turn.mode == "json_object":
			text = '{"description":"Synthetic project workspace."}'
		else:
			text = json.dumps(
				{
					"content": None,
					"tool_calls": [
						{
							"name": "CreateMemory",
							"arguments_json": '{"memory":"synthetic fact"}',
						}
					],
				}
			)
		return RuntimeResponse(
			text=text,
			usage={
				"prompt_tokens": 10,
				"completion_tokens": 2,
				"total_tokens": 12,
			},
		)


class BridgeHTTPTest(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.runtime = FakeBridgeRuntime()
		cls.token = "a" * 64
		cls.application = BridgeApplication(
			runtime=cls.runtime,
			token=cls.token,
			model="fixture-model",
			timeout_seconds=1,
		)
		cls.server = create_server(cls.application, "127.0.0.1", 0)
		cls.thread = threading.Thread(
			target=cls.server.serve_forever,
			daemon=True,
		)
		cls.thread.start()
		cls.port = cls.server.server_address[1]

	@classmethod
	def tearDownClass(cls):
		cls.server.shutdown()
		cls.server.server_close()
		cls.thread.join(timeout=2)

	def setUp(self):
		self.runtime.error = None
		self.runtime.text_override = None
		self.runtime.run_calls = 0

	def request(self, method, path, body=None, token=None, synchronize=True):
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
		headers = {}
		if token is not None:
			headers["Authorization"] = f"Bearer {token}"
		if body is not None:
			headers["Content-Type"] = "application/json"
			body = json.dumps(body)
		connection.request(method, path, body=body, headers=headers)
		response = connection.getresponse()
		payload = response.read()
		connection.close()
		if synchronize:
			self.assertTrue(self.application.capacity.acquire(timeout=1))
			self.application.capacity.release()
		return response, json.loads(payload) if payload else None

	def summary_request(self):
		return {
			"model": "fixture-model",
			"messages": [
				{
					"role": "user",
					"content": "Project Finch secret prompt content",
				}
			],
			"response_format": {"type": "json_object"},
		}

	def test_requires_correct_bearer(self):
		missing, _ = self.request("GET", "/health")
		wrong, _ = self.request("GET", "/health", token="b" * 64)

		self.assertEqual(missing.status, 401)
		self.assertEqual(wrong.status, 401)

	def test_serves_health_models_and_completion(self):
		health, health_body = self.request("GET", "/health", token=self.token)
		models, models_body = self.request("GET", "/v1/models", token=self.token)
		completion, completion_body = self.request(
			"POST",
			"/v1/chat/completions",
			body=self.summary_request(),
			token=self.token,
		)

		self.assertEqual(health.status, 200)
		self.assertEqual(health_body["auth_mode"], "chatgpt")
		self.assertEqual(models.status, 200)
		self.assertEqual(models_body["data"][0]["id"], "fixture-model")
		self.assertEqual(completion.status, 200)
		self.assertEqual(
			json.loads(completion_body["choices"][0]["message"]["content"]),
			{"description": "Synthetic project workspace."},
		)

	def test_rejects_unknown_route_and_streaming(self):
		not_found, _ = self.request("GET", "/v1/responses", token=self.token)
		body = self.summary_request()
		body["stream"] = True
		streaming, streaming_body = self.request(
			"POST",
			"/v1/chat/completions",
			body=body,
			token=self.token,
		)

		self.assertEqual(not_found.status, 404)
		self.assertEqual(streaming.status, 400)
		self.assertEqual(streaming_body["error"]["code"], "invalid_request")

	def test_rejects_oversized_request_before_reading_body(self):
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
		connection.putrequest("POST", "/v1/chat/completions")
		connection.putheader("Authorization", f"Bearer {self.token}")
		connection.putheader("Content-Type", "application/json")
		connection.putheader("Content-Length", str(8 * 1024 * 1024 + 1))
		connection.endheaders()
		response = connection.getresponse()
		response.read()
		connection.close()

		self.assertEqual(response.status, 413)

	def test_rejects_when_capacity_is_busy(self):
		self.assertTrue(self.application.capacity.acquire(timeout=1))
		try:
			response, _ = self.request(
				"POST",
				"/v1/chat/completions",
				body=self.summary_request(),
				token=self.token,
				synchronize=False,
			)
		finally:
			self.application.capacity.release()

		self.assertEqual(response.status, 429)
		self.assertEqual(response.getheader("Retry-After"), "5")

	def test_maps_runtime_failures(self):
		for error, expected_status in (
			(BridgeAuthError("auth"), 401),
			(BridgeQuotaError("quota"), 429),
			(BridgeRuntimeError("runtime"), 503),
			(BridgeTimeoutError("timeout"), 504),
		):
			with self.subTest(error=type(error).__name__):
				self.runtime.error = error
				response, _ = self.request(
					"POST",
					"/v1/chat/completions",
					body=self.summary_request(),
					token=self.token,
				)
				self.assertEqual(response.status, expected_status)

	def test_maps_invalid_codex_output_to_502(self):
		self.runtime.text_override = "not-json"

		response, body = self.request(
			"POST",
			"/v1/chat/completions",
			body=self.summary_request(),
			token=self.token,
		)

		self.assertEqual(response.status, 502)
		self.assertEqual(body["error"]["code"], "invalid_upstream_response")

	def test_logs_metadata_without_prompt_or_authorization(self):
		with self.assertLogs("supermemory_codex_bridge", level="INFO") as logs:
			response, _ = self.request(
				"POST",
				"/v1/chat/completions",
				body=self.summary_request(),
				token=self.token,
			)

		joined = "\n".join(logs.output)
		self.assertEqual(response.status, 200)
		self.assertNotIn("Project Finch", joined)
		self.assertNotIn(self.token, joined)
		self.assertNotIn("Synthetic project workspace", joined)


if __name__ == "__main__":
	unittest.main()
