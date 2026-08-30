import copy
import json
from pathlib import Path
import unittest

from supermemory_codex_bridge.translator import (
	BridgeProtocolError,
	build_codex_turn,
	parse_codex_result,
	to_chat_completion,
)


FIXTURE_DIR = (
	Path(__file__).parent / "fixtures" / "supermemory-v0.0.8"
)


def captured_requests():
	return [
		json.loads(line)
		for line in (FIXTURE_DIR / "requests.jsonl").read_text().splitlines()
		if line.strip()
	]


class BuildCodexTurnTest(unittest.TestCase):
	def test_fixture_preserves_complete_four_round_tool_transcript(self):
		requests = captured_requests()

		self.assertEqual(len(requests), 4)
		third_messages = requests[2]["body"]["messages"]
		fourth_messages = requests[3]["body"]["messages"]
		self.assertEqual(
			[call["id"] for call in third_messages[2]["tool_calls"]],
			["call_search_1", "call_search_2", "call_search_3"],
		)
		self.assertEqual(
			[message["tool_call_id"] for message in third_messages[3:]],
			["call_search_1", "call_search_2", "call_search_3"],
		)
		self.assertEqual(fourth_messages[-2]["tool_calls"][0]["id"], "call_create_1")
		self.assertEqual(fourth_messages[-1]["tool_call_id"], "call_create_1")

	def test_builds_json_object_turn_for_container_summary(self):
		request = captured_requests()[0]["body"]

		turn = build_codex_turn(request, "gpt-5.6-luna")

		self.assertEqual(turn.mode, "json_object")
		self.assertIsNone(turn.output_schema)
		self.assertEqual(turn.model, "fixture-model")
		self.assertIn("Project Finch", turn.prompt)

	def test_preserves_existing_tool_call_ids_and_tool_results(self):
		request = copy.deepcopy(captured_requests()[1]["body"])
		request["messages"].extend(
			[
				{
					"role": "assistant",
					"content": None,
					"tool_calls": [
						{
							"id": "call_search_1",
							"type": "function",
							"function": {
								"name": "searchMemories",
								"arguments": '{"query":"Project Finch"}',
							},
						}
					],
				},
				{
					"role": "tool",
					"tool_call_id": "call_search_1",
					"content": '{"results":[]}',
				},
			]
		)

		turn = build_codex_turn(request, "gpt-5.6-luna")

		self.assertEqual(turn.mode, "tool_bridge")
		self.assertIn('"id":"call_search_1"', turn.prompt)
		self.assertIn('"tool_call_id":"call_search_1"', turn.prompt)
		self.assertEqual(
			turn.output_schema["properties"]["tool_calls"]["maxItems"],
			128,
		)
		item_schema = turn.output_schema["properties"]["tool_calls"]["items"]
		self.assertEqual(
			item_schema["required"],
			["name", "arguments_json"],
		)
		self.assertNotIn("oneOf", item_schema)

	def test_rejects_streaming_and_multiple_choices(self):
		request = copy.deepcopy(captured_requests()[1]["body"])
		request["stream"] = True
		with self.assertRaisesRegex(BridgeProtocolError, "streaming"):
			build_codex_turn(request, "gpt-5.6-luna")

		request["stream"] = False
		request["n"] = 2
		with self.assertRaisesRegex(BridgeProtocolError, "n=1"):
			build_codex_turn(request, "gpt-5.6-luna")

	def test_rejects_non_text_and_malformed_tool_messages(self):
		request = copy.deepcopy(captured_requests()[1]["body"])
		request["messages"][1]["content"] = [{"type": "text", "text": "no"}]
		with self.assertRaisesRegex(BridgeProtocolError, "content"):
			build_codex_turn(request, "gpt-5.6-luna")

		request = copy.deepcopy(captured_requests()[2]["body"])
		request["messages"][2]["tool_calls"][0]["function"]["arguments"] = "not-json"
		with self.assertRaisesRegex(BridgeProtocolError, "arguments"):
			build_codex_turn(request, "gpt-5.6-luna")


class ParseCodexResultTest(unittest.TestCase):
	def setUp(self):
		self.turn = build_codex_turn(
			captured_requests()[1]["body"],
			"gpt-5.6-luna",
		)

	def test_preserves_order_for_94_create_memory_calls(self):
		response = json.dumps(
			{
				"content": None,
				"tool_calls": [
					{
						"name": "CreateMemory",
						"arguments_json": json.dumps(
							{"memory": f"fact-{index}"}
						),
					}
					for index in range(94)
				],
			}
		)

		result = parse_codex_result(self.turn, response)
		completion = to_chat_completion(
			result,
			model="fixture-model",
			request_id="batch-94",
		)

		self.assertEqual(len(result.tool_calls), 94)
		self.assertEqual(
			[call.arguments["memory"] for call in result.tool_calls],
			[f"fact-{index}" for index in range(94)],
		)
		self.assertEqual(result.finish_reason, "tool_calls")
		self.assertEqual(
			[
				json.loads(call["function"]["arguments"])["memory"]
				for call in completion["choices"][0]["message"]["tool_calls"]
			],
			[f"fact-{index}" for index in range(94)],
		)

	def test_rejects_unknown_tool_and_invalid_arguments(self):
		unknown = json.dumps(
			{
				"content": None,
				"tool_calls": [
					{"name": "unknown", "arguments_json": "{}"}
				],
			}
		)
		with self.assertRaisesRegex(BridgeProtocolError, "output schema"):
			parse_codex_result(self.turn, unknown)

		invalid = json.dumps(
			{
				"content": None,
				"tool_calls": [
					{"name": "CreateMemory", "arguments_json": "{}"}
				],
			}
		)
		with self.assertRaisesRegex(BridgeProtocolError, "invalid arguments"):
			parse_codex_result(self.turn, invalid)

	def test_rejects_output_outside_strict_bridge_schema(self):
		invalid_results = (
			{
				"content": None,
				"tool_calls": [],
				"extra": "not allowed",
			},
			{
				"content": None,
				"tool_calls": [
					{
						"name": "CreateMemory",
						"arguments_json": '{"memory":"fact"}',
						"extra": "not allowed",
					}
				],
			},
			{
				"content": None,
				"tool_calls": [
					{"name": [], "arguments_json": "{}"}
				],
			},
		)

		for invalid in invalid_results:
			with self.subTest(invalid=invalid):
				with self.assertRaisesRegex(BridgeProtocolError, "output schema"):
					parse_codex_result(self.turn, json.dumps(invalid))

	def test_rejects_prose_when_a_required_tool_is_missing(self):
		request = copy.deepcopy(captured_requests()[1]["body"])
		request["tool_choice"] = "required"
		turn = build_codex_turn(request, "gpt-5.6-luna")

		with self.assertRaisesRegex(BridgeProtocolError, "requires a tool"):
			parse_codex_result(
				turn,
				json.dumps({"content": "I would create it", "tool_calls": []}),
			)

	def test_rejects_tool_calls_when_tool_choice_is_none(self):
		request = copy.deepcopy(captured_requests()[1]["body"])
		request["tool_choice"] = "none"
		turn = build_codex_turn(request, "gpt-5.6-luna")

		with self.assertRaisesRegex(BridgeProtocolError, "tool_choice forbids"):
			parse_codex_result(
				turn,
				json.dumps(
					{
						"content": None,
						"tool_calls": [
							{
								"name": "CreateMemory",
								"arguments_json": '{"memory":"forbidden"}',
							}
						],
					}
				),
			)

	def test_builds_openai_tool_call_envelope(self):
		result = parse_codex_result(
			self.turn,
			json.dumps(
				{
					"content": None,
					"tool_calls": [
						{
							"name": "searchMemories",
							"arguments_json": json.dumps(
								{"query": "Project Finch"}
							),
						}
					],
				}
			),
		)

		completion = to_chat_completion(
			result,
			model="fixture-model",
			request_id="request-1",
			usage={"prompt_tokens": 10, "completion_tokens": 2},
		)

		choice = completion["choices"][0]
		self.assertEqual(choice["finish_reason"], "tool_calls")
		self.assertEqual(
			choice["message"]["tool_calls"][0]["function"]["name"],
			"searchMemories",
		)
		self.assertEqual(
			json.loads(
				choice["message"]["tool_calls"][0]["function"][
					"arguments"
				]
			),
			{"query": "Project Finch"},
		)

	def test_keeps_json_object_as_assistant_content(self):
		turn = build_codex_turn(captured_requests()[0]["body"], "gpt-5.6-luna")

		result = parse_codex_result(
			turn,
			'{"description":"Synthetic project workspace."}',
		)

		self.assertEqual(result.finish_reason, "stop")
		self.assertEqual(
			json.loads(result.content),
			{"description": "Synthetic project workspace."},
		)


if __name__ == "__main__":
	unittest.main()
