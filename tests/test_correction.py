from __future__ import annotations
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from nitro_ai_judge_cli import api, cli, completion, contests, state, submissions
from nitro_ai_judge_cli import json_output
from nitro_ai_judge_cli.transfers import CHUNK, Multipart, receive, atomic_copy
from nitro_ai_judge_cli.manager.openapi import contract
from nitro_ai_judge_cli.manager.app import create_app, operations
from nitro_ai_judge_cli.manager.store import ManagerStore
from nitro_ai_judge_cli.play_protocol import BASE_PATH


class StreamingTests(unittest.TestCase):
    def test_large_receive_is_bounded_and_checks_truncation(self):
        class Response:
            def __init__(self, size): self.remaining = size
            def read(self, size):
                self.asserted_size = size
                if not 0 < size <= CHUNK: raise AssertionError(size)
                value = b'x' * min(size, self.remaining)
                self.remaining -= len(value)
                return value
        class Sink:
            count = 0
            def write(self, value): self.count += len(value)
        sink = Sink()
        self.assertEqual(receive(Response(64*1024*1024), 200, {}, sink), b'')
        self.assertEqual(sink.count, 64*1024*1024)
        with self.assertRaisesRegex(OSError, 'Incomplete'):
            receive(Response(5), 200, {'Content-Length': '10'}, Sink())

    def test_successful_html_pages_are_not_truncated_to_error_preview_limit(self):
        body = b'<html>' + b'x' * (3*CHUNK)
        self.assertEqual(receive(io.BytesIO(body),200,{'Content-Type':'text/html'}), body)

    def test_multipart_is_replayable_and_chunked(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'large.csv')
            with path.open('wb') as stream: stream.truncate(8*CHUNK)
            body = Multipart({'note': 'test'}, {'output': (str(path), 'text/csv')})
            first = list(body)
            self.assertTrue(all(len(part) <= CHUNK for part in first))
            self.assertEqual(sum(map(len, first)), body.length)
            self.assertEqual(list(body), first)
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(OSError, 'changed'):
                list(body)

    def test_atomic_failure_preserves_existing_destination(self):
        class Broken(io.BytesIO):
            def read(self, size=-1): raise OSError('cancelled transfer')
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, 'output')
            target.write_bytes(b'original')
            with self.assertRaises(OSError): atomic_copy(Broken(b'new'), str(target), force=True)
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual([p.name for p in Path(root).iterdir()], ['output'])

    def test_redirect_replays_stream_and_errors_do_not_write_sink(self):
        from tests.test_api import _Response
        sink = io.BytesIO()
        with patch.object(api, '_open_once', side_effect=[_Response(307, b'', {'Location':'/next'}), _Response(200, b'abc')]) as opener:
            self.assertEqual(api.request('/first', base_url='https://example.test', output=sink)[0], 200)
        self.assertEqual(sink.getvalue(), b'abc')
        self.assertEqual(opener.call_count, 2)
        sink = io.BytesIO()
        with patch.object(api, '_open_once', return_value=_Response(404, b'error')):
            self.assertEqual(api.request('/missing', output=sink)[0], 404)
        self.assertEqual(sink.getvalue(), b'')


class JSONTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.env = patch.dict(os.environ, {'NAIJ_STATE_DIR': self.root.name})
        self.env.start(); self.addCleanup(self.env.stop)
        state.configure_state_dir(None)
        self.addCleanup(state.reset_state_paths)

    def invoke(self, words):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cli.main(words)
        if result == 0:
            try: import jsonschema
            except ImportError: pass
            else:
                schema = json.loads((Path(__file__).parents[1]/'docs/json-output.schema.json').read_text())
                jsonschema.validate(json.loads(out.getvalue()), schema)
        return result, out.getvalue(), err.getvalue()

    def test_readonly_command_matrix(self):
        state.set_contest({'organizationSlug':'org','competitionSlug':'contest'})
        state.set_task({'id':'backend-task'})
        manager=MagicMock()
        manager.info.return_value={'api_version':1}
        manager.health.return_value={'status':'healthy'}
        manager.competitions.return_value=[]
        manager.competition.return_value={'reference':'org/contest','workspace_state':'running'}
        manager.operation_history.return_value={'operations':[], 'next_offset':None}
        with patch.object(cli,'require_auth',return_value=({'username':'fixture'}, ('',''),'fixture')), patch.object(contests,'load_tasks',return_value=[{'id':'backend-task'}]), patch.object(submissions,'load_submissions',return_value=([{'id':'canonical-id'}],1)), patch.object(submissions,'load_submission',return_value={'id':'canonical-id','completeTaskScore':.8}), patch.object(json_output.ManagerClient,'from_state',return_value=manager):
            for words in [['tasks'], ['submissions'], ['submission','canonical-id'], ['use'], ['ls'], ['play','ls'], ['play','status'], ['play','ps'], ['play','operations'], ['play','manager','status']]:
                with self.subTest(words=words):
                    code, output, error=self.invoke([*words,'--json'])
                    self.assertEqual(code,0,error)
                    self.assertEqual(json.loads(output)['schema_version'],1)

    def test_task_json_preserves_backend_id_and_number(self):
        auth = ({'username':'tester'}, ('cf','cookie'), 'bearer')
        with patch.object(cli, 'require_auth', return_value=auth), patch.object(contests, 'load_tasks', return_value=[{'id':'backend','title':'Task'}]), patch.object(contests, 'load_task_view', return_value={'task':{'id':'backend','statement':'body'}}):
            code, output, error = self.invoke(['task','org/contest','1','--json'])
        self.assertEqual(code, 0, error)
        value = json.loads(output)
        self.assertEqual(value['schema_version'], 1)
        self.assertEqual(value['data']['id'], 'backend')
        self.assertEqual(value['data']['number'], 1)

    def test_auth_errors_and_progress_cannot_pollute_json(self):
        with patch.object(cli, 'require_auth', side_effect=lambda: print('login needed')):
            code, output, error = self.invoke(['contests','--json'])
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertIn('login needed', error)
        def load(*args, **kwargs): print('progress'); return []
        with patch.object(cli, 'require_auth', return_value=({}, ('',''), 'x')), patch.object(contests, 'load_competitions', side_effect=load):
            code, output, error = self.invoke(['contests','--json'])
        self.assertEqual(json.loads(output)['data'], [])
        self.assertIn('progress', error)

    def test_offline_and_readonly_use(self):
        state.update_cache('contests', 'all', [{'title':'cached'}])
        with patch.object(cli, 'require_auth', side_effect=AssertionError('network/auth')):
            code, output, error = self.invoke(['ls','--offline','--json'])
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)['cached'])
        self.assertEqual(json.loads(output)['data'][0]['title'], 'cached')
        code, output, error = self.invoke(['use','org/contest','--json'])
        self.assertEqual(code, 1)
        self.assertEqual(output, '')

    def test_discovered_categories_complete_and_external_links_rejected(self):
        context = {'contest': {'organizationSlug':'org','competitionSlug':'contest'}, 'task':{'id':'id'},
                   'cache':{'task_files':{'org/contest/id':[{'key':'weights_v2'}]}}}
        self.assertIn('weights_v2', completion.candidates(['download-data','-c','w'], context))
        self.assertIsNone(contests.task_file_category_from_href('org','contest','id','https://unrelated.test/competitions/org/contest/id/weights_v2/download'))


class OpenAPITests(unittest.TestCase):
    def test_every_versioned_route_is_documented_with_security_and_responses(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        store = ManagerStore(str(Path(root.name, 'manager.db')))
        self.addCleanup(store.close)
        app = create_app(store=store, api_token='test-fixture', backend=object())
        spec = contract()
        checked_in = json.loads((Path(__file__).parents[1]/'docs/play-manager.openapi.json').read_text())
        self.assertEqual(spec, checked_in)
        actual = set()
        for route in app.router.routes():
            path = route.resource.canonical
            if path.startswith(BASE_PATH+'/api/v1/') and route.method != 'HEAD':
                actual.add((path[len(BASE_PATH):], route.method.lower()))
        expected = {(path, method) for path, value in spec['paths'].items() for method in value}
        self.assertEqual(actual, expected)
        for path, methods in spec['paths'].items():
            for method, operation in methods.items():
                self.assertTrue(operation['responses'])
                self.assertIn('security', operation)
                params = {p['name'] for p in operation['parameters'] if p['in']=='path'}
                import re
                self.assertEqual(params, set(re.findall(r'\{(\w+)\}', path)))

    def test_contract_validates(self):
        try: from openapi_spec_validator import validate
        except ImportError: self.skipTest('install openapi-spec-validator for schema validation')
        validate(contract())


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_pagination_timings_and_redaction(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        store = ManagerStore(str(Path(root.name, 'manager.db')))
        self.addCleanup(store.close)
        for index in range(4):
            store.create_operation(str(index), 'org/contest', 'pull', {})
            store.event(str(index), 'pulling', 'Pulling')
            store.fail(str(index), {'type':'operation_failed','message':'token=example-private-token'})
        request = MagicMock()
        request.app = {'store':store}
        request.query = {'limit':'2','offset':'1','competition':'org/contest','status':'failed','action':'pull'}
        value = json.loads((await operations(request)).text)
        self.assertEqual(len(value['operations']), 2)
        self.assertEqual(value['next_offset'], 3)
        item = value['operations'][0]
        self.assertIn('duration', item)
        self.assertIn('started_at', item)
        self.assertIn('finished_at', item)
        self.assertNotIn('example-private-token', json.dumps(value))
        self.assertNotIn('events', item)
        self.assertNotIn('options', item)
