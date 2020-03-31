import argparse
import codecs
import http.server
import json
import mimetypes
import os
import socketserver
import subprocess
import sys
import tempfile

import requests


has_output: bool = False
quiet: bool = False


def display_output(mimetype, content):
    if mimetype == 'text/plain':
        print(content)
    elif mimetype.startswith('image/'):
        extension = mimetypes.guess_extension(mimetype)
        tmppath = tempfile.mktemp(suffix=extension)
        with open(tmppath, 'wb') as tmpfile:
            decoded = codecs.decode(content.encode(), 'base64')
            tmpfile.write(decoded)
        if not quiet:
            os.system('feh --image-bg white %s &' % tmppath)
        os.system('tiv %s' % tmppath)
    elif mimetype == 'text/html':
        subprocess.run(['w3m', '-dump', '-T', 'text/html'],
                       input=content,
                       text=True)
    else:
        print("--------", file=sys.stderr)
        print("Example input:", file=sys.stderr)
        print(content)
        raise Exception("Unknown mimetype: %s" % mimetype)


def display_choose(content):
    if 'image/png' in content:
        display_output('image/png', content['image/png'])
    elif 'text/plain' in content:
        display_output('text/plain', content['text/plain'])
    elif 'text/html' in content:
        display_output('text/html', content['text/html'])
    else:
        print("--------", file=sys.stderr)
        print("Example input:", file=sys.stderr)
        print(content)
        raise Exception("Unmanageable mimetypes")


class MyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()

        self.wfile.write(b"Hello World!")

    def do_POST(self):
        global has_output

        content_length = int(self.headers['Content-length'])
        body = json.loads(self.rfile.read(content_length))
        self.send_response(202)
        self.end_headers()

        kind = body['type']
        if kind == 'output':
            has_output = True
            print(body['text'])
        elif kind == 'display':
            has_output = True
            display_choose(body['content'])
        elif kind == 'error':
            has_output = True
            # print("%s: %s"
            #       % (body['error_type'], body['error_message']),
            #       file=sys.stderr)
            print('\n'.join(body['traceback']), file=sys.stderr)
        elif kind == 'stdout':
            has_output = True
            sys.stdout.write(body['content'])
        elif kind == 'stderr':
            has_output = True
            sys.stderr.write(body['content'])
        elif kind == 'done':
            raise KeyboardInterrupt
        else:
            raise Exception("Unknown POST request type: %s" % kind)

    def log_message(self, format, *args):
        pass  # do nothing


def main():
    global quiet

    parser = argparse.ArgumentParser()
    parser.add_argument('parent',
                        type=int,
                        help="Port of the parent server")
    parser.add_argument('host',
                        type=str,
                        default='127.0.0.1',
                        nargs='?',
                        help="Where to host the server")
    parser.add_argument('port',
                        type=int,
                        default=0,
                        nargs='?',
                        help="Port to host the server")
    parser.add_argument('-q', '--quiet',
                        action='store_true',
                        help="Don't open any external windows (e.g. feh)")
    args = parser.parse_args()

    quiet = args.quiet

    try:
        with socketserver.TCPServer(('', args.port), MyHandler) as httpd:
            _, port = httpd.server_address
            requests.post("http://127.0.0.1:%d" % args.parent,
                          json={'port': port})
            # print("Serving at IP %s; port %d" % httpd.server_address)
            httpd.serve_forever()
    except KeyboardInterrupt:
        try:
            if has_output and not quiet:
                input()
            else:
                return
        except KeyboardInterrupt:
            return
        except EOFError:
            return


if __name__ == '__main__':
    sys.exit(main())
