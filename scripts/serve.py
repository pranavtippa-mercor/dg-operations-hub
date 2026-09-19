#!/usr/bin/env python3
"""Local production preview. Private data is served only on the loopback interface."""
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path(__file__).resolve().parents[1]
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT/'out'),**kw)
 def do_GET(self):
  if urlparse(self.path).path.endswith('/__local/snapshot'):
   if self.headers.get('Sec-Fetch-Site') in ('cross-site',):self.send_error(403);return
   data=(ROOT/'.private/snapshot.json').read_bytes();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data);return
  if self.path.startswith('/dg-operations-hub/'):
   self.path=self.path[len('/dg-operations-hub'):]
  super().do_GET()
if __name__=='__main__':
 print('Local preview: http://127.0.0.1:5189/dg-operations-hub/',flush=True)
 ThreadingHTTPServer(('127.0.0.1',5189),Handler).serve_forever()
