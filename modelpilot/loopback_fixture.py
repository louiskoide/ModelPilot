"""Owned loopback HTTP fixture only: no externally supplied endpoint or credentials."""
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading
from .proxy import UsageObserver

class LoopbackFixture:
    def __init__(self,mode='json'):
        if mode not in ('json','stream','truncated','error','cancel'):
            raise ValueError('Unknown fixture scenario')
        self.mode=mode
        self.calls=0
        self.last_request=None
        self.cancelled=threading.Event()
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.0'
            def log_message(self,*args):pass
            def do_POST(self):
                from .fixture_dispatch import StrictFixture
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.calls+=1;fixture.last_request=payload
                try:response=StrictFixture().reply(payload)
                except ValueError:
                    self.send_error(400);return
                if fixture.mode=='error':self.send_error(429);return
                stream=fixture.mode!='json'
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream' if stream else 'application/json')
                self.end_headers()
                if stream:
                    frames=[{'type':'message_start','message':response},
                            {'type':'message_delta','usage':{'output_tokens':4}}]
                    if fixture.mode!='truncated':frames.append({'type':'message_stop'})
                    raw=b''.join(b'data: '+json.dumps(f).encode()+b'\n\n' for f in frames)
                else:raw=json.dumps(response).encode()
                try:
                    # Tiny chunks exercise fragmented SSE parsing across arbitrary boundaries.
                    for offset in range(0,len(raw),7):
                        self.wfile.write(raw[offset:offset+7]);self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=False
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def __enter__(self):return self
    def __exit__(self,*args):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def reply(self,request):
        if self.cancelled.is_set():raise InterruptedError('Fixture cancelled')
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        observer=UsageObserver(self.mode!='json')
        try:
            conn.request('POST','/v1/messages',json.dumps(request).encode(),{'Content-Type':'application/json'})
            response=conn.getresponse()
            if response.status!=200:raise RuntimeError('Fixture HTTP rejection')
            while True:
                data=response.read1(7)
                if not data:break
                if self.mode=='cancel':self.cancelled.set()
                if self.cancelled.is_set():raise InterruptedError('Fixture cancelled')
                observer.feed(data)
            observer.finish()
            if observer.invalid or not observer.complete:raise EOFError('Incomplete fixture stream')
            return {'model':observer.model,'usage':observer.usage}
        finally:conn.close()
