"""Exercise the installed Codex runtime offline; read non-secret model metadata on stdin."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

from openai_codex import Codex, CodexConfig

from supermemory_codex_bridge.service import (
    BridgeRuntimeError, CODEX_CONFIG_OVERRIDES, CodexRuntime,
)
from supermemory_codex_bridge.translator import build_codex_turn, parse_codex_result


FORBIDDEN = "ADAPTER_PRIVATE_SENTINEL_638914"


def run_case(catalog, mode, native_call=None):
    requests = []
    answer = '{"description":"Synthetic summary."}' if mode == 'summary' else json.dumps({
        'content': None, 'tool_calls': [{'name': 'CreateMemory',
            'arguments_json': '{"memory":"Synthetic marker is indigo."}'}],
    })

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            if native_call and len(requests) == 1:
                item = {'type': 'function_call', 'id': 'fc_probe', 'call_id': 'call_probe',
                        'name': native_call[0], 'arguments': json.dumps(native_call[1])}
                if native_call[0] == 'apply_patch':
                    item = {'type': 'custom_tool_call', 'id': 'ct_probe', 'call_id': 'call_probe',
                            'name': 'apply_patch', 'input': native_call[1]['input']}
            else:
                item = {'type': 'message', 'id': 'msg_probe', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': answer}]}
            events = [
                {'type': 'response.created', 'response': {'id': 'resp_probe'}},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                {'type': 'response.completed', 'response': {'id': 'resp_probe',
                    'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}},
            ]
            data = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with tempfile.TemporaryDirectory(prefix='adapter-runtime-') as directory:
        root = Path(directory)
        home = root / 'codex'
        home.mkdir()
        workspace = root / 'workspace'
        workspace.mkdir()
        secret = root / 'private.txt'
        secret.write_text(FORBIDDEN)
        catalog_path = root / 'models.json'
        catalog_path.write_text(json.dumps(catalog))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        overrides = CODEX_CONFIG_OVERRIDES + (
            'model_provider="capture"',
            'model_providers.capture.name="Offline test"',
            f'model_providers.capture.base_url="http://127.0.0.1:{server.server_port}/v1"',
            'model_providers.capture.wire_api="responses"',
            'model_providers.capture.requires_openai_auth=false',
            'model_providers.capture.request_max_retries=0',
            'model_providers.capture.stream_max_retries=0',
            'features.tool_registry.turn_metadata_includes_tool_info=true',
            f'model_catalog_json="{catalog_path}"',
        )
        if native_call:
            native_call = (native_call[0], {k: v.replace('SENTINEL_PATH', str(secret))
                                           for k, v in native_call[1].items()})
        codex = Codex(config=CodexConfig(cwd=str(workspace),
            env={'CODEX_HOME': str(home)}, config_overrides=overrides))
        runtime = CodexRuntime(codex, workspace=str(workspace))
        body = {'model': 'gpt-6-luna', 'messages': [{'role': 'user', 'content': 'Synthetic check.'}]}
        if mode == 'summary':
            body['response_format'] = {'type': 'json_object'}
        else:
            body['tools'] = [{'type': 'function', 'function': {'name': 'CreateMemory',
                'description': 'Propose a synthetic memory.', 'parameters': {
                    'type': 'object', 'properties': {'memory': {'type': 'string'}},
                    'required': ['memory'], 'additionalProperties': False}}}]
            body['tool_choice'] = 'required'
        turn = build_codex_turn(body, 'gpt-6-luna')
        try:
            try:
                result = runtime.run(turn, timeout_seconds=20)
            except BridgeRuntimeError:
                assert native_call, 'Ordinary structured inference failed'
                assert not runtime.available, 'Unexpected native turn did not retire the runtime'
            else:
                if native_call:
                    outputs = [item.get('output', '') for request in requests[1:]
                               for item in request.get('input', [])
                               if item.get('type') in {'function_call_output', 'custom_tool_call_output'}
                               and item.get('call_id') == 'call_probe']
                    expected = {
                        'exec': 'aborted',
                        'exec_command': 'unsupported call: exec_command',
                        'apply_patch': 'patch rejected: writing is blocked by read-only sandbox; rejected by user approval settings',
                    }[native_call[0]]
                    assert outputs and all(output == expected for output in outputs), outputs
                parsed = parse_codex_result(turn, result.text)
                assert parsed.content or parsed.tool_calls
            assert requests, 'No SDK request captured'
            for request in requests:
                assert request['model'] == 'gpt-6-luna'
                assert request['reasoning']['effort'] == 'high'
                metadata = json.loads(request.get('client_metadata', {}).get('x-codex-turn-metadata', '{}'))
                namespaces = metadata.get('tool_namespaces_info', {})
                assert 'collaboration' not in namespaces
                assert 'sleep' not in namespaces.get('clock', {}).get('functions', {})
                functions = namespaces.get('functions', {}).get('functions', {})
                assert 'request_user_input' not in functions
                assert 'exec_command' not in functions
            assert FORBIDDEN not in json.dumps(requests), 'Private file content reached the provider'
            assert secret.read_text() == FORBIDDEN, 'Native tool modified the private file'
            print('PASS', mode, native_call[0] if native_call else 'structured inference', flush=True)
        finally:
            runtime.close()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    model_catalog = json.load(sys.stdin)
    run_case(model_catalog, 'summary')
    run_case(model_catalog, 'tools')
    run_case(model_catalog, 'tools', ('exec', {'code':
        'const fs = await import("node:fs"); text(fs.readFileSync("SENTINEL_PATH", "utf8"));'}))
    run_case(model_catalog, 'tools', ('exec_command', {'cmd': 'cat SENTINEL_PATH'}))
    run_case(model_catalog, 'tools', ('apply_patch', {'input':
        '*** Begin Patch\n*** Delete File: SENTINEL_PATH\n*** End Patch'}))
