import socketserver
import http.server
import json
import os
import queue
import sys
import threading
import time
from typing import Dict, List

import jupyter_client
import requests

import vim

KS_IDLE = 0
KS_NONIDLE = 1
KS_BUSY = 2
KS_NOT_CONNECTED = 3
KS_DEAD = 4

HS_RUNNING = 0
HS_DONE = 1


class State(object):
    initialized: bool = False
    client: jupyter_client.BlockingKernelClient = None
    history: Dict[str, dict] = {}
    current_execution_count: int = 0
    background_loop = None
    background_server = None
    kernel_state: int = KS_NOT_CONNECTED

    # general_lock: threading.Lock = threading.Lock()

    events: queue.Queue = queue.Queue()
    server_port: int = -1
    port: int = -1

    has_error: bool = False

    execution_queue: queue.Queue = queue.Queue()
    code_lineno: str = ""  # Vim stores its ints as str in Python

    main_buffer: vim.Buffer = None
    main_window_id: int = -1

    sign_ids_hold: Dict[int, List[int]] = {}
    sign_ids_running: Dict[int, List[int]] = {}
    sign_ids_ok: Dict[int, List[int]] = {}
    sign_ids_err: Dict[int, List[int]] = {}

    def __del__(self):
        self.deinitialize()

    def initialize_new(self, kernel_name):
        """
        Initialize the client and a local kernel, if it isn't yet initialized.
        """

        if not self.initialized:
            # self.client = jupyter_client.run_kernel(kernel_name=kernel_name)
            # self.client = jupyter_client.BlockingKernelClient()
            _, self.client = jupyter_client.manager.start_new_kernel(
                kernel_name=kernel_name)
            self.kernel_state = KS_IDLE
            self.main_buffer = vim.current.buffer
            self.main_window_id = vim.eval('win_getid()')
            self.initialized = True

            self.start_background_loop()
            self.start_background_server()

    def initialize_remote(self, connection_file, ssh=None):
        """
        Initialize the client and connect to a kernel (possibly remote),
          if it isn't yet initialized.
        """

        if not self.initialized:
            self.client = jupyter_client.BlockingKernelClient()
            if ssh is None:
                self.client.load_connection_file(connection_file)
            else:
                with open(connection_file) as f:
                    parsed = json.load(f)

                newports = jupyter_client.tunnel_to_kernel(connection_file,
                                                           ssh)
                parsed['shell_port'], parsed['iopub_port'], \
                    parsed['stdin_port'], parsed['hb_port'], \
                    parsed['control_port'] = newports

                with open(connection_file, 'w') as f:
                    json.dump(parsed, f)

                self.client.load_connection_file(connection_file)

            self.client.start_channels()
            try:
                print("Connecting to the kernel...")
                self.client.wait_for_ready(timeout=60)
            except RuntimeError as err:
                self.client.stop_channels()
                print("Could not connect to existing kernel: %s" % err)
                return
            self.kernel_state = KS_IDLE
            self.main_buffer = vim.current.buffer
            self.main_window_id = vim.eval('win_getid()')
            self.initialized = True

            self.start_background_loop()
            self.start_background_server()

    def start_background_loop(self):
        self.background_loop = threading.Thread(target=update_loop)
        self.background_loop.start()

    def start_background_server(self):
        def run_server():
            try:
                with socketserver.TCPServer(('', 0), MyHandler) as httpd:
                    _, self.server_port = httpd.server_address
                    httpd.serve_forever()
            except KeyboardInterrupt:
                return
        self.background_server = threading.Thread(target=run_server)
        self.background_server.start()

    def restart(self):
        """
        Restart the kernel.
        """

        if self.initialized:
            self.client.initialized = False
            self.kernel_state = KS_NOT_CONNECTED
            self.client.shutdown(True)
            self.kernel_state = KS_IDLE
            self.client.initialized = True

    def deinitialize(self):
        """
        Deinitialize the client, if it is initialized.
        """

        if self.initialized:
            self.client.shutdown()
            self.kernel_state = KS_NOT_CONNECTED
            self.main_buffer = None
            self.initialized = False
            requests.post('http://127.0.0.1:%d' % self.server_port,
                          json={'action': 'shutdown'})

            self.background_loop.join()
            self.background_server.join()


class MyHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global state

        content_length = int(self.headers['Content-length'])
        body = json.loads(self.rfile.read(content_length))
        self.send_response(202)
        self.end_headers()

        if 'action' in body and body['action'] == 'shutdown':
            raise KeyboardInterrupt

        state.port = body['port']

    def log_message(self, format, *args):
        pass  # do nothing


class RunInLineNo(object):
    def __init__(self, lineno=None):
        self.lineno = lineno

    def __enter__(self):
        self.current_line = vim.eval('getpos(".")')
        if self.lineno is not None:
            vim.command('normal %sG' % self.lineno)

    def __exit__(self, *_):
        vim.command('call setpos(".", %s)' % self.current_line)


state = State()


def setup_ssh_tunneling(host, connection_file):
    jupyter_client.tunnel_to_kernel(connection_file, host)


def init_local():
    global state

    if not state.initialized:
        # Select from available kernels
        # # List them
        kernelspecmanager = jupyter_client.kernelspec.KernelSpecManager()
        kernels = kernelspecmanager.get_all_specs()
        specs = [jupyter_client.kernelspec.get_kernel_spec(x) for x in kernels]
        # # Ask
        choice = int(vim.eval('inputlist(%r)'
                              % (["Choose the kernel to start:"] +
                                 ["%d. %s" % (a+1, b) for a, b in
                                     enumerate(y.display_name for y in specs)])
                              ))
        if choice == 0:
            print("Cancelled.")
            return False

        state.initialize_new(specs[choice-1].language)

        print('Successfully initialized kernel %s!'
              % specs[choice-1].display_name,
              sys.stderr)

        return True


def init_existing(connection_file):
    global state

    state.acquire()

    if not state.initialized:
        state.initialize_remote(connection_file)

        print('Successfully connected to the kernel!')

        return True


def init_remote(host, connection_file):
    global state

    if not state.initialized:
        state.initialize_remote(connection_file, ssh=host)

        print('Successfully connected to the remote kernel!')

        return True


def deinit():
    global state

    state.deinitialize()


def evaluate(code, code_lineno):
    global state

    if not state.initialized:
        return

    # Check if we can actually evaluate this code (e.g. isn't already in queue)
    signs = vim.eval('sign_getplaced(%s, {"group": "magma"})'
                     % (state.main_buffer.number)
                     )[0]['signs']
    if any(sign['id'] in state.sign_ids_hold for sign in signs):
        print("Trying to re-evaluate a line that is on hold",
              file=sys.stderr)
        return False
    if any(sign['id'] in state.sign_ids_running for sign in signs):
        print("Trying to re-evaluate a line that is already running",
              file=sys.stderr)
        return False

    with RunInLineNo(code_lineno):
        for lineno, linestr in paragraph_iter():
            signs_in_this_line = [sign for sign in signs
                                  if sign['lnum'] == lineno]
            for sign in signs_in_this_line:
                vim.command('sign unplace %s group=magma buffer=%s'
                            % (sign['id'], state.main_buffer.number))

    if state.kernel_state == KS_IDLE:
        state.kernel_state = KS_NONIDLE

        state.port = -1
        state.code_lineno = code_lineno

        state.client.execute(code)
    elif state.kernel_state == KS_BUSY or state.kernel_state == KS_NONIDLE:
        with RunInLineNo(code_lineno):
            setsign_hold(
                state.current_execution_count+state.execution_queue.qsize()+1)
        state.execution_queue.put((code, code_lineno))
    else:
        print("Invalid kernel state: %d" % state.kernel_state, sys.stderr)
        return


def start_outputs():
    global state

    job = ['python3',
           os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        'magma_output.py'),
           state.server_port]
    #       '127.0.0.1', 10000 + state.current_execution_count]

    vim.eval('term_start(%r, '
             '{"term_name": "(magma) Out[%d]", "term_finish": "close"})'
             % (job, state.current_execution_count))
    vim.command('call win_gotoid(%s)' % state.main_window_id)


def show_evaluated_output(manual=True):
    global state

    if not state.initialized:
        return

    lineno = vim.eval('line(".")')

    signname = vim.eval('sign_getplaced(%s, {"group": "magma", "lnum": %s})'
                        % (state.main_buffer.number, lineno)
                        )[0]['signs'][0]['name']

    if signname == 'magma_hold':
        if manual:
            print("Requested paragraph is to be evaluated", sys.stderr)
    elif signname.startswith('magma_running_'):
        if manual:
            print("Requested paragraph is being evaluated", sys.stderr)
    elif signname.startswith('magma_err_'):
        if manual:
            print("Requested paragraph did not evaluate successfully",
                  sys.stderr)
    elif signname.startswith('magma_ok_'):
        if state.kernel_state != KS_IDLE:
            print("TODO: implement (in magma.vim) the ability to view output "
                  "while the kernel is busy", sys.stderr)
        else:
            execution_count = int(signname[9:])

            state.port = -1
            start_outputs()

            def forward_output():
                while state.port == -1:
                    time.sleep(0.1)

                for data in state.history[execution_count]['output']:
                    requests.post('http://127.0.0.1:%d'
                                  % state.port,
                                  json=data)

                requests.post('http://127.0.0.1:%d'
                              % state.port,
                              json={'type': 'done'})

            threading.Thread(target=forward_output).start()


def update():
    global state

    if not state.initialized:
        return

    try:
        message = state.client.get_iopub_msg(timeout=0.25)
        if 'content' not in message or \
           'msg_type' not in message:
            return

        message_type = message['msg_type']
        content = message['content']
        if message_type == 'execute_input':
            state.current_execution_count = content['execution_count']
            state.history[state.current_execution_count] = {
                'type': 'output',
                'status': HS_RUNNING,
                'output': []
            }

            def initiate_execution():
                with RunInLineNo(state.code_lineno):
                    print("%s vs %s" % (state.code_lineno,
                                        vim.eval('getpos(".")')))
                    setsign_running(state.current_execution_count)
                state.has_error = False
                start_outputs()
            state.events.put(initiate_execution)
            return
        elif message_type == 'status':
            if content['execution_state'] == 'idle':
                requests.post('http://127.0.0.1:%d'
                              % state.port,
                              json={'type': 'done'})

                if state.has_error:
                    state.kernel_state = KS_IDLE
                    state.events.put(lambda: setsign_running2err(
                                         state.current_execution_count))
                    # Clear the execution queue:
                    while not state.execution_queue.empty():
                        try:
                            state.execution_queue.get(False)
                        except queue.Empty:
                            continue
                        state.execution_queue.task_done()
                else:
                    state.events.put(lambda: setsign_running2ok(
                                         state.current_execution_count))
                    if state.execution_queue.qsize() > 0:
                        state.kernel_state = KS_NONIDLE

                        def next_from_queue():
                            setsign_hold2running(
                                    state.current_execution_count+1)
                            state.kernel_state = KS_IDLE
                            evaluate(*state.execution_queue.get())
                        state.events.put(next_from_queue)
                    else:
                        state.kernel_state = KS_IDLE
            elif content['execution_state'] == 'busy':
                state.kernel_state = KS_BUSY
            state.events.put(lambda: vim.command('redrawstatus!'))
            return
        elif message_type == 'execute_reply' and state.port != -1:
            if content['status'] == 'ok':
                data = {
                    'type': 'output',
                    'text': content['status'],
                }
            elif content['status'] == 'error':
                state.has_error = True
                data = {
                    'type': 'error',
                    'error_type': content['ename'],
                    'error_message': content['evalue'],
                    'traceback': content['traceback'],
                }
            elif content['status'] == 'abort':
                state.has_error = True
                data = {
                    'type': 'error',
                    'error_type': "Aborted",
                    'error_message': "Kernel aborted with no error message",
                    'traceback': "",
                }
            state.history[state.current_execution_count]['output'].append(data)
        elif message_type == 'execute_result' and state.port != -1:
            data = {
                'type': 'display',
                'content': content['data'],
            }
            state.history[state.current_execution_count]['output'].append(data)
        elif message_type == 'error' and state.port != -1:
            state.has_error = True
            data = {
                'type': 'error',
                'error_type': content['ename'],
                'error_message': content['evalue'],
                'traceback': content['traceback'],
            }
            state.history[state.current_execution_count]['output'].append(data)
        elif message_type == 'display_data' and state.port != -1:
            data = {
                'type': 'display',
                'content': content['data'],
            }
            state.history[state.current_execution_count]['output'].append(data)
        elif message_type == 'stream' and state.port != -1:
            name = content['name']
            if name == 'stdout':
                data = {
                    'type': 'stdout',
                    'content': content['text'],
                }
            elif name == 'stderr':
                data = {
                    'type': 'stderr',
                    'content': content['text'],
                }
            else:
                raise Exception("Unkown stream:name = '%s'" % name)
            state.history[state.current_execution_count]['output'].append(data)
        else:
            return

        requests.post('http://127.0.0.1:%d'
                      % state.port,
                      json=data)
    except queue.Empty:
        return


def update_loop():
    global state

    while True:
        if not state.initialized:
            break
        try:
            update()
        except Exception as e:
            print(e, file=sys.stderr)
        time.sleep(0.5)


def vim_update():
    global state

    if state.initialized:
        while state.events.qsize() > 0:
            try:
                state.events.get()()
            except Exception as e:
                print(e, file=sys.stderr)


def get_kernel_state(vim_var):
    global state

    vim.command('let %s = %s' % (vim_var, state.kernel_state))


def paragraph_iter():
    vim.command('let l:cursor_pos = getpos(".")')
    vim.command('normal {')
    if vim.current.line == "":
        vim.command('normal j')
    while vim.current.line != "":
        yield vim.eval('line(".")'), vim.current.line
        prev_linenr = vim.eval('line(".")')
        vim.command('normal j')
        if prev_linenr == vim.eval('line(".")'):
            break
    vim.command('call setpos(".", l:cursor_pos)')


def setsign_hold(execution_count):
    global state

    state.sign_ids_hold[execution_count] = []
    for lineno, linestr in paragraph_iter():
        signid = vim.eval('sign_place(0, "magma", "magma_hold",'
                          '%s, {"lnum": %s})'
                          % (state.main_buffer.number, lineno))
        state.sign_ids_hold[execution_count].append(signid)


def unsetsign_hold(execution_count):
    global state

    for signid in state.sign_ids_hold[execution_count]:
        vim.command('sign unplace %s group=magma buffer=%s'
                    % (signid, state.main_buffer.number))
        # vim.command('sign unplace %s group=magma buffer=%s'
        #             % (signid, vim.current.buffer.number))
    del state.sign_ids_hold[execution_count]


def setsign_running(execution_count):
    global state

    state.sign_ids_running[execution_count] = []
    for lineno, linestr in paragraph_iter():
        vim.command('sign define magma_running_%d text=@@'
                    'texthl=MagmaRunningSign'
                    % (execution_count))
        signid = vim.eval('sign_place(0, "magma", "magma_running_%d",'
                          '%s, {"lnum": %s})'
                          % (execution_count,
                             state.main_buffer.number,
                             lineno))
        state.sign_ids_running[execution_count].append(signid)


def unsetsign_running(execution_count):
    global state

    for signid in state.sign_ids_running[execution_count]:
        vim.command('sign unplace %s group=magma buffer=%s'
                    % (signid, state.main_buffer.number))
        # vim.command('sign unplace %s group=magma buffer=%s'
        #             % (signid, vim.current.buffer.number))
    del state.sign_ids_running[execution_count]


def setsign_hold2running(execution_count):
    global state

    state.sign_ids_running[execution_count] = []
    for signid in state.sign_ids_hold[execution_count]:
        lineno = vim.eval('sign_getplaced(%s,'
                          '{"group": "magma", "id": %s})'
                          % (state.main_buffer.number, signid)
                          )[0]['signs'][0]['lnum']
        vim.command('sign unplace %s group=magma buffer=%s'
                    % (signid, state.main_buffer.number))
        vim.command('sign define magma_running_%d text=@@'
                    'texthl=MagmaRunningSign'
                    % (execution_count))
        signid = vim.eval('sign_place(0, "magma", "magma_running_%d",'
                          '%s, {"lnum": %s})'
                          % (execution_count,
                             state.main_buffer.number,
                             lineno))
        state.sign_ids_running[execution_count].append(signid)
    del state.sign_ids_hold[execution_count]


def setsign_ok(execution_count):
    global state

    state.sign_ids_ok[execution_count] = []
    for lineno, linestr in paragraph_iter():
        vim.command('sign define magma_ok_%d text=:: texthl=MagmaOkSign'
                    % (execution_count))
        signid = vim.eval('sign_place(0, "magma", "magma_ok_%d",'
                          '%s, {"lnum": %s})'
                          % (execution_count,
                             state.main_buffer.number,
                             lineno))
        state.sign_ids_ok[execution_count].append(signid)


def unsetsign_ok(execution_count):
    global state

    for signid in state.sign_ids_ok[execution_count]:
        vim.command('sign unplace %s group=magma buffer=%s'
                    % (signid, state.main_buffer.number))
        # vim.command('sign unplace %s group=magma buffer=%s'
        #             % (signid, vim.current.buffer.number))
    del state.sign_ids_ok[execution_count]


def setsign_running2ok(execution_count):
    global state

    state.sign_ids_ok[execution_count] = []
    for signid in state.sign_ids_running[execution_count]:
        lineno = vim.eval('sign_getplaced(%s, {"group": "magma", "id": %s})'
                          % (state.main_buffer.number, signid)
                          )[0]['signs'][0]['lnum']
        vim.command('sign unplace %s group=magma buffer=%s'
                    % (signid, state.main_buffer.number))
        vim.command('sign define magma_ok_%d text=:: texthl=MagmaOkSign'
                    % (execution_count))
        signid = vim.eval('sign_place(0, "magma", "magma_ok_%d",'
                          '%s, {"lnum": %s})'
                          % (execution_count,
                             state.main_buffer.number,
                             lineno))
        state.sign_ids_ok[execution_count].append(signid)
    del state.sign_ids_running[execution_count]


def setsign_err(execution_count):
    global state

    state.sign_ids_err[execution_count] = []
    for lineno, linestr in paragraph_iter():
        vim.command('sign define magma_err_%d text=!! texthl=MagmaErrSign'
                    % (execution_count))
        signid = vim.eval('call sign_place(0, "magma", "magma_err_%d",'
                          '%s, {"lnum": %s})'
                          % (execution_count,
                             state.main_buffer.number,
                             lineno))
        state.sign_ids_err[execution_count].append(signid)


def unsetsign_err(execution_count):
    global state

    for signid in state.sign_ids_err[execution_count]:
        vim.command('sign unplace %s group=magma buffer=%s'
                    % (signid, state.main_buffer.number))
        # vim.command('sign unplace %s group=magma buffer=%s'
        #             % (signid, vim.current.buffer.number))
    del state.sign_ids_err[execution_count]


def setsign_running2err(execution_count):
    global state

    state.sign_ids_err[execution_count] = []
    for signid in state.sign_ids_running[execution_count]:
        lineno = vim.eval('sign_getplaced(%s, {"group": "magma", "id": %s})'
                          % (state.main_buffer.number, signid)
                          )[0]['signs'][0]['lnum']
        vim.command('sign unplace %s group=magma buffer=%s'
                    % (signid, state.main_buffer.number))
        vim.command('sign define magma_err_%d text=!! texthl=MagmaErrSign'
                    % (execution_count))
        signid = vim.eval('sign_place(0, "magma", "magma_err_%d",'
                          '%s, {"lnum": %s})'
                          % (execution_count,
                             state.main_buffer.number,
                             lineno))
        state.sign_ids_err[execution_count].append(signid)
    del state.sign_ids_running[execution_count]
