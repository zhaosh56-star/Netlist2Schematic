from dataclasses import replace
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from time import monotonic
import unittest
from unittest.mock import patch

from integrations.auto_canvas_huyangc.runtime import run_bounded
from integrations.auto_canvas_huyangc.web_jobs import JobManager, WebJobConfig, validate_request, refusal_message
from integrations.auto_canvas_huyangc.web_server import API, EditorAssets, LayoutServer


def request():
    return dict(type='plan',runId=7,project=dict(schemaVersion=58,documents=[dict(id='doc')]),documentIds=['doc'])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.config=WebJobConfig(self.root,self.root,self.root,'node',self.root/'jobs')

    def tearDown(self):
        self.temporary.cleanup()

    def test_request_identity_and_document_limits(self):
        for payload in (None,{},dict(request(),runId=True),dict(request(),documentIds=['missing']),
                        dict(request(),documentIds=['doc','doc']),dict(request(),documentIds=[])):
            with self.subTest(payload=payload),self.assertRaises(ValueError):
                validate_request(payload)

    def test_client_paths_budgets_and_commands_are_not_execution_inputs(self):
        value=validate_request(dict(request(),engine_root='C:/untrusted',command=['bad'],engine_seconds=99999))
        self.assertEqual(set(value),{'type','runId','project','documentIds'})

    def test_refusal_uses_structured_reason_instead_of_escaped_trace_tail(self):
        (self.root/'native-validation.json').write_text(json.dumps(dict(
            status='failed',message='The native interface order changed.')),encoding='utf8')
        self.assertEqual(refusal_message(self.root,ValueError('huge trace tail')),
                         'The native interface order changed.')

    def test_queue_is_bounded_and_cancel_releases_capacity(self):
        m=JobManager(replace(self.config,queue_limit=1),start=False)
        first=m.submit(request())
        with self.assertRaises(RuntimeError):m.submit(request())
        self.assertEqual(m.cancel_job(first['jobId'])['status'],'cancelled')
        second=m.submit(request())
        self.assertNotEqual(first['jobId'],second['jobId'])
        m.close()

    def test_heartbeat_and_lease_cancel_only_the_abandoned_job(self):
        m=JobManager(self.config,start=False)
        a,b=m.submit(request()),m.submit(request())
        m.jobs[a['jobId']].last_seen=1
        m.jobs[b['jobId']].last_seen=9
        m.expire(now=10)
        self.assertTrue(m.jobs[a['jobId']].cancel.is_set())
        self.assertEqual(m.get(b['jobId'])['status'],'queued')
        m.close()

    def test_completed_result_is_not_reclassified_on_lease_expiry(self):
        m=JobManager(self.config,start=False)
        job=m.jobs[m.submit(request())['jobId']]
        job.state='succeeded';job.results=[dict(documentId='doc',result=dict(edits=[]))]
        m.expire(now=job.last_seen+100)
        self.assertEqual(m.get(job.id)['status'],'succeeded')
        self.assertFalse(job.cancel.is_set())
        m.close()

    def test_web_budget_includes_input_adapter_and_atomic_draft_verification(self):
        manager=JobManager(self.config,start=False)
        job=manager.jobs[manager.submit(request())['jobId']]
        clock=[0.];calls=[]
        def command(job,label,args,seconds):
            calls.append((label,args,seconds))
            if label=='native-input':
                clock[0]+=20
                (job.folder/'source.icproj.json').write_text('{}',encoding='utf8')
            elif label.endswith('-python'):
                self.assertIn('--result-policy',args)
                self.assertEqual(args[args.index('--result-policy')+1],'editable')
                self.assertEqual(args[args.index('--layout-budget')+1],'adaptive')
                clock[0]+=259
                output=job.folder/'cell-1';output.mkdir()
                (output/'adapter-report.json').write_text(json.dumps(dict(status='editable_draft',
                    native_project=str(output/'layout.icproj.json'))),encoding='utf8')
            else:
                clock[0]+=.5
                result=dict(resultKind='editable_draft',result=dict(edits=[dict(kind='move_instance')],
                    groups=[],diagnostics=[dict(severity='warning',message='Incomplete draft')]))
                (job.folder/'cell-1/browser-result.json').write_text(json.dumps(result),encoding='utf8')
        manager._command=command
        try:
            with patch('integrations.auto_canvas_huyangc.web_jobs.perf_counter',side_effect=lambda:clock[0]):
                manager._run(job)
            self.assertEqual(self.config.engine_seconds,300)
            self.assertEqual(calls[0][2],30)
            self.assertEqual(calls[1][2],260)
            self.assertEqual(calls[2][2],21)
            self.assertIn('--allow-draft',calls[2][1])
            self.assertEqual(job.results[0]['resultKind'],'editable_draft')
            self.assertLessEqual(job.execution_seconds,300)
        finally:manager.close()

    def test_cancel_event_stops_owned_child_without_waiting_for_budget(self):
        event=threading.Event();timer=threading.Timer(0.25,event.set)
        timer.start();before=monotonic()
        try:
            code=run_bounded([sys.executable,'-c','import time; time.sleep(30)'],seconds=30,cancel_event=event)
        finally:timer.cancel()
        self.assertEqual(code,130)
        self.assertLess(monotonic()-before,5)

    def test_lost_worker_lease_stops_a_live_python_job(self):
        manager=JobManager(replace(self.config,lease_seconds=0.6),start=False)
        exited=threading.Event()
        def run(job):
            try:
                manager._command(job,'sleeping-child',[sys.executable,'-c','import time; time.sleep(30)'],30)
            finally:
                exited.set()
        manager._run=run
        for target in (manager._worker,manager._reaper):
            thread=threading.Thread(target=target,daemon=True)
            manager.threads.append(thread);thread.start()
        job=manager.submit(request())
        try:
            self.assertTrue(exited.wait(5),'Abandoned child remained running past its lease')
            self.assertEqual(manager.get(job['jobId'],touch=False)['status'],'cancelled')
        finally:manager.close()

    def test_new_asset_namespace_avoids_old_worker_cache_without_modifying_build(self):
        web=self._assets()
        before={p:p.read_bytes() for p in web.rglob('*') if p.is_file()}
        a,b=EditorAssets(web,'first'),EditorAssets(web,'second')
        self.assertNotEqual(a.prefix,b.prefix)
        index,_,cache=a.read('/')
        self.assertIn(a.prefix.encode(),index)
        self.assertEqual(cache,'no-store')
        js,_,_=a.read(a.prefix+'assets/index.js')
        self.assertIn(b'python-auto-layout.worker.js',js)
        worker,_,cache=a.read(a.prefix+'assets/python-auto-layout.worker.js')
        self.assertIn(b'first',worker);self.assertEqual(cache,'no-store')
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        with self.assertRaises(FileNotFoundError):a.read(b.prefix+'assets/index.js')

    def test_path_traversal_and_missing_assets_are_not_spa_fallbacks(self):
        a=EditorAssets(self._assets(),'token')
        for path in ('/../../private.txt','/assets/missing.js','/api/no-such-api'):
            with self.subTest(path=path),self.assertRaises(FileNotFoundError):a.read(path)

    def _assets(self):
        web=self.root/'web';(web/'assets').mkdir(parents=True,exist_ok=True)
        (web/'index.html').write_text('<title>Auto Canvas</title><script src="/assets/index.js"></script>',encoding='utf8')
        (web/'assets/index.js').write_text('new Worker(new URL("/assets/auto-layout.worker-original.js",import.meta.url));',encoding='utf8')
        (web/'assets/auto-layout.worker-original.js').write_text('original',encoding='utf8')
        return web


class HttpTests(unittest.TestCase):
    _assets = WebTests._assets

    def setUp(self):
        WebTests.setUp(self)
        self.manager=JobManager(self.config,start=False)
        self.server=LayoutServer(('127.0.0.1',0),config=self.config,web_root=self._assets(),
                                 token='test-token',manager=self.manager,max_body_bytes=1000)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        WebTests.tearDown(self)

    def call(self,method,path,value=None,headers=None):
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        body=json.dumps(value) if value is not None else None
        h={'content-type':'application/json','x-netlist2schematic-token':'test-token',**(headers or {})}
        connection.request(method,path,body=body,headers=h)
        response=connection.getresponse();raw=response.read()
        code,reply_headers=response.status,dict(response.getheaders())
        connection.close()
        try:data=json.loads(raw)
        except (ValueError,UnicodeError):data=raw
        return code,data,reply_headers

    def test_native_button_backend_health_and_submit_poll_cancel(self):
        code,data,_=self.call('GET',API+'/health')
        self.assertEqual(code,200);self.assertEqual(data['engine'],'Netlist2Schematic')
        self.assertEqual(data['candidateSchedule'],'normal-2-3-then-shared-recovery-v1')
        self.assertEqual(data['layoutBudgetPolicy'],'adaptive')
        code,data,_=self.call('POST',API+'/jobs',request())
        self.assertEqual(code,202);ident=data['jobId']
        self.assertEqual(self.call('GET',API+'/jobs/'+ident)[1]['status'],'queued')
        self.assertEqual(self.call('DELETE',API+'/jobs/'+ident)[1]['status'],'cancelled')

    def test_host_origin_credentials_and_cross_site_rejected(self):
        for headers in ({'Host':'attacker.example'}, {'Origin':'https://attacker.example'},
                        {'Origin':'null'},{'x-netlist2schematic-token':'wrong'},
                        {'x-netlist2schematic-token':'é'},{'Sec-Fetch-Site':'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.call('POST',API+'/jobs',request(),headers)[0],403)
        self.assertEqual(len(self.manager.jobs),0)

    def test_invalid_input_media_size_and_queue_full_are_explicit(self):
        self.assertEqual(self.call('POST',API+'/jobs',{})[0],400)
        self.assertEqual(self.call('POST',API+'/jobs',request(),{'content-type':'text/plain'})[0],415)
        self.assertEqual(self.call('POST',API+'/jobs',{'padding':'x'*2000})[0],413)
        for _ in range(4):self.assertEqual(self.call('POST',API+'/jobs',request())[0],202)
        self.assertEqual(self.call('POST',API+'/jobs',request())[0],429)

    def test_assets_and_api_never_return_old_worker_or_cors_permission(self):
        code,index,_=self.call('GET','/')
        self.assertEqual(code,200);self.assertIn(b'Netlist2Schematic',index)
        code,worker,h=self.call('GET',self.server.assets.prefix+'assets/python-auto-layout.worker.js')
        self.assertEqual(code,200);self.assertNotIn(b'__N2S_TOKEN__',worker)
        self.assertEqual(h['Cache-Control'],'no-store')
        self.assertNotIn('Access-Control-Allow-Origin',h)
        self.assertEqual(self.call('OPTIONS',API+'/jobs')[0],405)
        self.assertEqual(self.call('GET','/api/missing')[0],404)


if __name__=='__main__':unittest.main()
