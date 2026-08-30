from __future__ import annotations

import argparse
import logging
import os
import sys

from .service import (
	BridgeApplication,
	BridgeConfig,
	CodexRuntime,
	create_server,
)

def run_login() -> int:
	runtime = CodexRuntime.create()
	try:
		handle = runtime.login_device_code()
		print(f"verification_url={handle.verification_url}")
		print(f"user_code={handle.user_code}")
		completion = handle.wait()
		if not completion.success:
			print("login_failed", file=sys.stderr)
			return 1
		print("login_complete")
		return 0
	finally:
		runtime.close()


def run_check_auth() -> int:
	model = os.environ.get("CODEX_BRIDGE_MODEL", "gpt-5.6-luna").strip()
	runtime = CodexRuntime.create()
	try:
		plan_type = runtime.require_chatgpt()
		if not runtime.has_model(model):
			print(f"model_unavailable={model}", file=sys.stderr)
			return 1
		print("auth_mode=chatgpt")
		print(f"plan_type={plan_type}")
		print(f"model={model}")
		return 0
	finally:
		runtime.close()


def run_serve() -> int:
	config = BridgeConfig.from_environment()
	runtime = CodexRuntime.create()
	application = BridgeApplication(
		runtime=runtime,
		token=config.token,
		model=config.model,
		timeout_seconds=config.timeout_seconds,
	)
	try:
		application.health()
		server = create_server(application, config.host, config.port)
		try:
			server.serve_forever()
		except KeyboardInterrupt:
			pass
		finally:
			server.server_close()
		return 0
	finally:
		runtime.close()


def main() -> int:
	parser = argparse.ArgumentParser(prog="supermemory-codex-bridge")
	parser.add_argument("command", choices=("serve", "login", "check-auth"))
	args = parser.parse_args()
	logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
	if args.command == "login":
		return run_login()
	if args.command == "check-auth":
		return run_check_auth()
	return run_serve()


if __name__ == "__main__":
	raise SystemExit(main())
