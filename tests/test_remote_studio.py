import io
import json
import os
from pathlib import Path
import sys
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from remote_studio import BASE_URL, RemoteStudio
from remote_source import NoRedirects, SourceHTTPError, SourceReportedError


class Response(io.BytesIO):
    def __init__(self, body, content_type='application/json', status=200):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers={'Content-Type':content_type}
        self.status=status


class Opener:
    def __init__(self, responses):
        self.responses=iter(responses)
        self.requests=[]
        self.options=[]

    def open(self, request, **kwargs):
        self.requests.append(request)
        self.options.append(kwargs)
        result=next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class NativeStudioTests(unittest.TestCase):
    def setUp(self):
        self.key='rls-sk-'+'synthetic-not-a-real-credential'
        environment=patch.dict(os.environ, {'STUDIO_API_KEY':self.key})
        environment.start()
        self.addCleanup(environment.stop)
        self.config={'studio':{'headers':{'X-Campaign-Id':'campaign-example',
                                         'X-Company-Id':'company-example',
                                         'X-Account-Id':'account-example'}}}

    def test_get_uses_fixed_host_scope_and_doseq_query_without_body(self):
        opener=Opener([Response({'audits':[]})])
        client=RemoteStudio(self.config,timeout=12,opener=opener)
        params={'subject_id':'task example&other', 'limit':5, 'tag':['one','two']}
        self.assertEqual(client.call_tool('studio',{'method':'GET','path':'/qc-audits/','params':params}),{'audits':[]})
        request=opener.requests[0]
        url=urllib.parse.urlsplit(request.full_url)
        self.assertEqual(url.scheme+'://'+url.netloc,BASE_URL)
        self.assertEqual(url.path,'/qc-audits/')
        self.assertEqual(urllib.parse.parse_qs(url.query),{'subject_id':['task example&other'],'limit':['5'],'tag':['one','two']})
        self.assertEqual(request.method,'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Authorization'),'Bearer '+self.key)
        self.assertEqual(request.get_header('User-agent'),'curl/8.0')
        for key,value in self.config['studio']['headers'].items():
            self.assertEqual(request.get_header(key.capitalize()),value)
        self.assertEqual(opener.options,[{'timeout':12}])

    def test_select_post_sends_raw_json_not_gateway_envelope(self):
        opener=Opener([Response({'rows':[{'id':'example'}]})])
        params={'query':'SELECT task_id FROM tasks LIMIT 1'}
        args={'method':'POST','path':'/querier/unstructured','params':params,
              'headers':self.config['studio']['headers'],'evidence':{'rationale':'Synthetic read fixture'}}
        RemoteStudio(self.config,opener=opener).call_tool('studio',args)
        request=opener.requests[0]
        self.assertEqual(request.full_url,BASE_URL+'/querier/unstructured')
        self.assertEqual(request.method,'POST')
        self.assertEqual(json.loads(request.data),params)
        self.assertEqual(request.get_header('Content-type'),'application/json')

    def test_all_current_collector_read_endpoints_accept_native_json(self):
        paths=['/worlds/world_example','/users/campaign/campaign_example',
               '/qc-audits/','/qc-audits/qcaud_example','/qc-specs/summary']
        values=[{'world_id':'example'},[],{'audits':[]},{'qc_spec_id':'example'},{'qc_specs':[]}]
        opener=Opener([Response(value) for value in values])
        client=RemoteStudio(self.config,opener=opener)
        for path,value in zip(paths,values):
            self.assertEqual(client.call_tool('studio',{'method':'GET','path':path}),value)

    def test_internal_read_validator_rejects_mutations_and_stacked_statements(self):
        opener=Opener([])
        client=RemoteStudio(self.config,opener=opener)
        args=[{'method':'DELETE','path':'/qc-audits/qcaud_example'},
              {'method':'POST','path':'/querier/unstructured','params':{'query':'DELETE FROM tasks'}},
              {'method':'POST','path':'/querier/unstructured','params':{'query':'SELECT 1; DELETE FROM tasks'}},
              {'method':'POST','path':'/querier/unstructured','params':{'query':'SELECT 1 -- comment'}},
              {'method':'POST','path':'/worlds/world_example','params':{'query':'SELECT 1'}}]
        for arguments in args:
            with self.assertRaises(ValueError):
                client.call_tool('studio',arguments)
        with self.assertRaises(ValueError):
            client.call_tool('slack_read_thread',{'method':'GET','path':'/qc-audits/'})
        self.assertEqual(opener.requests,[])

    def test_paths_cannot_override_host_query_fragment_or_escape_scope(self):
        opener=Opener([])
        client=RemoteStudio(self.config,opener=opener)
        for path in ['https://other.example/qc-audits/','//other.example/qc-audits/',
                     '/qc-audits/?limit=1','/qc-audits/#other','/worlds/../qc-audits/',
                     '/worlds/%2e%2e','/worlds/id/extra','/worlds//id','/tasks/id',
                     '/worlds/id\\other','/worlds/id\n']:
            with self.assertRaises(ValueError):
                client.call_tool('studio',{'method':'GET','path':path})
        with self.assertRaises(ValueError):
            client.call_tool('studio',{'method':'GET','path':'/qc-audits/','url':'https://other.example'})
        self.assertEqual(opener.requests,[])

    def test_request_cannot_override_scope_or_auth_headers(self):
        opener=Opener([])
        client=RemoteStudio(self.config,opener=opener)
        for headers in [{'Authorization':'replacement'}, {'X-Campaign-Id':'different'},
                        {**self.config['studio']['headers'],'Other':'value'}]:
            with self.assertRaises(ValueError):
                client.call_tool('studio',{'method':'GET','path':'/qc-audits/','headers':headers})
        self.assertEqual(opener.requests,[])

    def test_config_accepts_only_safe_scope_headers(self):
        for headers in [{}, {'Authorization':'replacement'}, {'x-campaign-id':'example'},
                        {'X-Campaign-Id':'example\r\nOther: value'}, {'X-Campaign-Id':None},
                        {'X-Campaign-Id':'example','Content-Type':'text/plain'}]:
            with self.assertRaises(ValueError):
                RemoteStudio({'studio':{'headers':headers}},opener=Opener([]))

    def test_key_is_environment_only_and_requires_native_prefix(self):
        for key in ['', 'synthetic-other-provider-key', 'rls-sk-', self.key+'\n', self.key+' space']:
            with patch.dict(os.environ,{'STUDIO_API_KEY':key}):
                config={**self.config,'STUDIO_API_KEY':self.key}
                with self.assertRaises(RuntimeError):
                    RemoteStudio(config,opener=Opener([]))

    def test_http_network_and_source_reported_errors_are_sanitized(self):
        for code in [401,403,429,500]:
            error=urllib.error.HTTPError(BASE_URL,code,'synthetic-private-message',{},io.BytesIO(b'synthetic-private-body'))
            with self.assertRaises(SourceHTTPError) as raised:
                RemoteStudio(self.config,opener=Opener([error])).call_tool('studio',{'method':'GET','path':'/qc-audits/'})
            self.assertEqual(raised.exception.status,code)
            self.assertNotIn('synthetic-private',str(raised.exception))
        with self.assertRaises(RuntimeError) as raised:
            RemoteStudio(self.config,opener=Opener([urllib.error.URLError('synthetic-private-network')])).call_tool('studio',{'method':'GET','path':'/qc-audits/'})
        self.assertNotIn('synthetic-private',str(raised.exception))
        with self.assertRaises(SourceReportedError):
            RemoteStudio(self.config,opener=Opener([Response({'error':'synthetic-private'})])).call_tool('studio',{'method':'GET','path':'/qc-audits/'})

    def test_non_native_text_scalar_or_broken_json_is_refused(self):
        for response in [Response(b'private text','text/plain'),Response(b'{"rows":[]}','text/plain'),
                         Response('JSON string'),Response(None),Response(b'private broken JSON')]:
            with self.assertRaises(SourceReportedError) as raised:
                RemoteStudio(self.config,opener=Opener([response])).call_tool('studio',{'method':'GET','path':'/qc-audits/'})
            self.assertNotIn('private',str(raised.exception))

    def test_shared_response_limits_and_redirect_protection_are_used(self):
        with patch('remote_source.MAX_RESPONSE_BYTES',16):
            with self.assertRaises(SourceReportedError):
                RemoteStudio(self.config,opener=Opener([Response({'long':'x'*40})])).call_tool('studio',{'method':'GET','path':'/qc-audits/'})
        with patch('remote_studio.time.monotonic',side_effect=[0,0,6]):
            with self.assertRaisesRegex(RuntimeError,'connection failed'):
                RemoteStudio(self.config,timeout=5,opener=Opener([Response({})])).call_tool('studio',{'method':'GET','path':'/qc-audits/'})
        with patch('remote_studio.urllib.request.build_opener',return_value=Opener([])) as factory:
            RemoteStudio(self.config)
            self.assertIsInstance(factory.call_args.args[0],NoRedirects)

    def test_close_clears_key_and_prevents_requests(self):
        opener=Opener([])
        client=RemoteStudio(self.config,opener=opener)
        client.close()
        self.assertEqual(client.token,'')
        with self.assertRaisesRegex(RuntimeError,'closed'):
            client.call_tool('studio',{'method':'GET','path':'/qc-audits/'})
        self.assertEqual(opener.requests,[])


if __name__=='__main__':
    unittest.main()
