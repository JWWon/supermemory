#!/usr/bin/env python3

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import unittest


def sanitize(method, path, headers, body):
	return {
		"method": method,
		"path": path,
		"headers": {
			key.lower(): value
			for key, value in headers.items()
			if key.lower()
			not in {"authorization", "cookie", "content-length", "host"}
		},
		"body": body,
	}


def default_completion(body):
	return {
		"id": "chatcmpl-capture",
		"object": "chat.completion",
		"created": int(time.time()),
		"model": body.get("model", "fixture-model"),
		"choices": [
			{
				"index": 0,
				"message": {"role": "assistant", "content": "capture complete"},
				"finish_reason": "stop",
			}
		],
		"usage": {
			"prompt_tokens": 1,
			"completion_tokens": 1,
			"total_tokens": 2,
		},
	}


class CaptureState:
	def __init__(self, capture_file, replies_file=None):
		self.capture_file = Path(capture_file)
		self.capture_file.parent.mkdir(parents=True, exist_ok=True)
		self.lock = threading.Lock()
		self.index = 0
		self.replies = []
		if replies_file and Path(replies_file).exists():
			self.replies = [
				json.loads(line)
				for line in Path(replies_file).read_text().splitlines()
				if line.strip()
			]

	def record(self, captured):
		with self.lock:
			with self.capture_file.open("a", encoding="utf-8") as output:
				output.write(json.dumps(captured, ensure_ascii=False) + "\n")
			index = self.index
			self.index += 1
			return index

	def reply(self, index, body):
		if index < len(self.replies):
			return self.replies[index]
		return default_completion(body)


class CaptureHandler(BaseHTTPRequestHandler):
	server_version = "supermemory-capture/1"

	def log_message(self, _format, *_args):
		return

	def send_json(self, status, payload):
		encoded = json.dumps(payload).encode()
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(encoded)))
		self.end_headers()
		self.wfile.write(encoded)

	def do_GET(self):
		if self.path.rstrip("/") == "/v1/models":
			self.send_json(
				200,
				{
					"object": "list",
					"data": [
						{
							"id": "fixture-model",
							"object": "model",
							"created": 1788048000,
							"owned_by": "capture",
						}
					],
				},
			)
			return
		self.send_json(404, {"error": {"message": "not found"}})

	def do_POST(self):
		length = int(self.headers.get("Content-Length", "0"))
		body = json.loads(self.rfile.read(length) or b"{}")
		captured = sanitize("POST", self.path, self.headers, body)
		index = self.server.capture_state.record(captured)
		self.send_json(200, self.server.capture_state.reply(index, body))


class CaptureServer(ThreadingHTTPServer):
	def __init__(self, address, capture_state):
		super().__init__(address, CaptureHandler)
		self.capture_state = capture_state


class SanitizeTest(unittest.TestCase):
	def test_removes_authorization(self):
		captured = sanitize(
			"POST",
			"/v1/chat/completions",
			{
				"Authorization": "Bearer capture-only",
				"Content-Type": "application/json",
			},
			{
				"model": "fixture-model",
				"messages": [{"role": "user", "content": "synthetic"}],
			},
		)

		self.assertNotIn("authorization", captured["headers"])
		self.assertEqual(
			captured["body"]["messages"][0]["content"],
			"synthetic",
		)


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--self-test", action="store_true")
	parser.add_argument("--host", default="127.0.0.1")
	parser.add_argument("--port", default=18787, type=int)
	args = parser.parse_args()
	if args.self_test:
		unittest.main(argv=[__file__])
		return

	capture_file = os.environ.get("CAPTURE_FILE")
	if not capture_file:
		parser.error("CAPTURE_FILE is required")
	state = CaptureState(capture_file, os.environ.get("REPLIES_FILE"))
	CaptureServer((args.host, args.port), state).serve_forever()


if __name__ == "__main__":
	main()
