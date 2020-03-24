import sys
import os
import subprocess
import http.server
import json
import socketserver
import tempfile
import mimetypes
import codecs
import argparse


def show_output(mimetype, content):
    if mimetype == 'text/plain':
        print(content)
    elif mimetype.startswith('image/'):
        extension = mimetypes.guess_extension(mimetype)
        tmppath = tempfile.mktemp(suffix=extension)
        with open(tmppath, 'wb') as tmpfile:
            decoded = codecs.decode(content.encode(), 'base64')
            tmpfile.write(decoded)
        os.system('tiv %s' % tmppath)
        os.system('feh --image-bg white %s' % tmppath)
    elif mimetype == 'text/html':
        print("Rendering HTML...")
        subprocess.run(['w3m', '-dump', '-T', 'text/html'],
                       input=content,
                       text=True)
    else:
        print("--------", file=sys.stderr)
        print("Example input:", file=sys.stderr)
        print(content)
        raise Exception("Unknown mimetype: %s" % mimetype)


class MyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()

        self.wfile.write(b"Hello World!")

    def do_POST(self):
        content_length = int(self.headers['Content-length'])
        body = json.loads(self.rfile.read(content_length))
        self.send_response(202)
        self.end_headers()

        kind = body['type']
        if kind == 'output':
            for mimetype, content in body['content'].items():
                show_output(mimetype, content)
        elif kind == 'stdout':
            print(body['content'], file=sys.stdout)
        elif kind == 'stderr':
            print(body['content'], file=sys.stderr)
        else:
            raise Exception("Unknown POST request type: %s" % kind)

        # if body['done']:
        #     self.server.shutdown()

    def log_message(self, format, *args):
        pass  # do nothing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('host',
                        type=str,
                        default='localhost',
                        nargs='?',
                        help="Where to host the server")
    parser.add_argument('port',
                        type=int,
                        default=0,
                        nargs='?',
                        help="Port to host the server")
    args = parser.parse_args()

    try:
        with socketserver.TCPServer(('', args.port), MyHandler) as httpd:
            # print("Serving at IP %s; port %d" % httpd.server_address)
            httpd.serve_forever()
    except KeyboardInterrupt:
        return


if __name__ == '__main__':
    sys.exit(main())
