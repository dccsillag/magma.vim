# vim: fdm=marker
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

HS_RUNNING = 0
HS_DONE = 1


class Locked(object):  # {{{
    def __init__(self, var):  # {{{
        self.var = var
        self.lock = threading.Lock()  # }}}

    def get(self):  # {{{
        with self.lock:
            return self.var  # }}}

    def set(self, newvar):  # {{{
        with self.lock:
            self.var = newvar  # }}}

    def __enter__(self, *args):  # {{{
        self.lock.__enter__(*args)
        return self.var  # }}}

    def __exit__(self, *args):  # {{{
        return self.lock.__exit__(*args)  # }}}}}}


class State(object):  # {{{
    initialized = Locked(False)
    client: jupyter_client.BlockingKernelClient = None
    history = Locked({})
    current_execution_count = Locked(0)
    background_loop = None
    background_server = None
    kernel_state = Locked(KS_NOT_CONNECTED)

    # general_lock: threading.Lock = threading.Lock()

    events: queue.Queue = queue.Queue()
    server_port: int = -1
    port = Locked(-1)

    iopub_queue: queue.Queue = queue.Queue()
    has_error = Locked(False)

    execution_queue: queue.Queue = queue.Queue()
    code_lineno = Locked("")  # Vim stores its ints as str in Python

    main_buffer: vim.Buffer = None
    main_window_id: int = -1
    preview_empty_buffer: vim.Buffer = None
    preview_window_id: int = -1
    output_buffer_numbers: Dict[int, int] = {}

    sign_ids_hold: Dict[int, List[int]] = {}
    sign_ids_running: Dict[int, List[int]] = {}
    sign_ids_ok: Dict[int, List[int]] = {}
    sign_ids_err: Dict[int, List[int]] = {}

    def __del__(self):  # {{{
        self.deinitialize()  # }}}

    def initialize_new(self, kernel_name):  # {{{
        """
        Initialize the client and a local kernel, if it isn't yet initialized.
        """

        if not self.initialized.get():
            # self.client = jupyter_client.run_kernel(kernel_name=kernel_name)
            # self.client = jupyter_client.BlockingKernelClient()
            _, self.client = jupyter_client.manager.start_new_kernel(
                kernel_name=kernel_name)
            self.kernel_state.set(KS_IDLE)
            self.main_buffer = vim.current.buffer
            self.main_window_id = vim.eval('win_getid()')

            vim.command('new')
            vim.command('setl nobuflisted')
            self.preview_empty_buffer = vim.current.buffer
            self.preview_window_id = vim.eval('win_getid()')
            vim.command('call win_gotoid(%s)' % self.main_window_id)

            vim.command('call timer_pause(g:magma_timer, 0)')

            self.initialized.set(True)

            self.start_background_loop()
            self.start_background_server()  # }}}

    def initialize_remote(self, connection_file, ssh=None):  # {{{
        """
        Initialize the client and connect to a kernel (possibly remote),
          if it isn't yet initialized.
        """

        if not self.initialized.get():
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
                print("Could not connect to existing kernel: %s" % err,
                      file=sys.stderr)
                return
            self.kernel_state.set(KS_IDLE)
            self.main_buffer = vim.current.buffer
            self.main_window_id = vim.eval('win_getid()')

            vim.command('new')
            vim.command('setl nobuflisted')
            self.preview_empty_buffer = vim.current.buffer
            self.preview_window_id = vim.eval('win_getid()')
            vim.command('call win_gotoid(%s)' % self.main_window_id)

            vim.command('call timer_pause(g:magma_timer, 0)')

            self.initialized.set(True)

            self.start_background_loop()
            self.start_background_server()  # }}}

    def start_background_loop(self):  # {{{
        self.background_loop = threading.Thread(target=update_loop)
        self.background_loop.start()  # }}}

    def start_background_server(self):  # {{{
        def run_server():
            try:
                with socketserver.TCPServer(('', 0), MyHandler) as httpd:
                    _, self.server_port = httpd.server_address
                    httpd.serve_forever()
            except KeyboardInterrupt:
                return
        self.background_server = threading.Thread(target=run_server)
        self.background_server.start()  # }}}

    def restart(self):  # {{{
        """
        Restart the kernel.
        """

        if self.initialized.get():
            self.client.initialized = False
            self.kernel_state.set(KS_NOT_CONNECTED)
            self.client.shutdown(True)
            self.kernel_state.set(KS_IDLE)
            self.client.initialized = True  # }}}

    def deinitialize(self):  # {{{
        """
        Deinitialize the client, if it is initialized.
        """

        if self.initialized.get():
            self.client.shutdown()
            self.kernel_state.set(KS_NOT_CONNECTED)
            self.main_buffer = None
            self.initialized.set(False)
            # FIXME: possible concurrency issue due to `self.server_port`:
            requests.post('http://127.0.0.1:%d'
                          % self.server_port,
                          json={'action': 'shutdown'})

            vim.command('call timer_pause(g:magma_timer, 1)')

            self.background_loop.join()
            self.background_server.join()  # }}}}}}


class MyHandler(http.server.BaseHTTPRequestHandler):  # {{{
    def do_POST(self):  # {{{
        global state

        content_length = int(self.headers['Content-length'])
        body = json.loads(self.rfile.read(content_length))
        self.send_response(202)
        self.end_headers()

        if 'action' in body and body['action'] == 'shutdown':
            raise KeyboardInterrupt

        state.port.set(body['port'])  # }}}

    def log_message(self, format, *args):  # {{{
        pass  # do nothing}}}}}}


class RunInLineNo(object):  # {{{
    def __init__(self, lineno=None):  # {{{
        self.lineno = lineno  # }}}

    def __enter__(self):  # {{{
        self.current_line = vim.eval('getpos(".")')
        if self.lineno is not None:
            vim.command('normal %sG' % self.lineno)  # }}}

    def __exit__(self, *_):  # {{{
        vim.command('call setpos(".", %s)' % self.current_line)  # }}}}}}


state = State()


def setup_ssh_tunneling(host, connection_file):  # {{{
    jupyter_client.tunnel_to_kernel(connection_file, host)  # }}}


def init_local():  # {{{
    global state

    if not state.initialized.get():
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
              % specs[choice-1].display_name)

        return True  # }}}


def init_existing(connection_file):  # {{{
    global state

    if not state.initialized.get():
        state.initialize_remote(connection_file)

        print('Successfully connected to the kernel!')

        return True  # }}}


def init_remote(host, connection_file):  # {{{
    global state

    if not state.initialized.get():
        state.initialize_remote(connection_file, ssh=host)

        print('Successfully connected to the remote kernel!')

        return True  # }}}


def deinit():  # {{{
    global state

    state.deinitialize()  # }}}


def evaluate(code, code_lineno):  # {{{
    global state

    if not state.initialized.get():
        return

    # Check if we can actually evaluate this code (e.g. isn't already in queue)
    signs = getsigns_inbuffer()
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

    if state.kernel_state.get() == KS_IDLE:
        state.kernel_state.set(KS_NONIDLE)

        state.port.set(-1)
        state.code_lineno.set(code_lineno)

        state.client.execute(code)
    elif state.kernel_state.get() in (KS_BUSY, KS_NONIDLE):
        with RunInLineNo(code_lineno):
            setsign_hold(
                state.current_execution_count.get() +
                state.execution_queue.qsize()+1)
        state.execution_queue.put((code, code_lineno))
    else:
        print("Invalid kernel state: %d" % state.kernel_state.get(),
              file=sys.stderr)
        return  # }}}


def read_session(session: dict):  # {{{
    global state

    for sign in session['signs']:
        defined_signs = vim.eval('sign_getdefined(%r)' % sign['name'])
        if len(defined_signs) == 0:
            if sign['name'] == 'magma_hold':
                raise Exception("Somehow, the magma_hold sign is not defined.")
            elif sign['name'].startswith('magma_running_'):
                vim.command('sign define %s text=@'
                            ' texthl=MagmaRunningSign'
                            ' linehl=MagmaRunningLine'
                            % sign['name'])
            elif sign['name'].startswith('magma_ok_'):
                vim.command('sign define %s text=✓'
                            ' texthl=MagmaOkSign'
                            ' linehl=MagmaOkLine'
                            % sign['name'])
            elif sign['name'].startswith('magma_err_'):
                vim.command('sign define %s text=✗'
                            ' texthl=MagmaErrSign'
                            ' linehl=MagmaErrLine'
                            % sign['name'])
            else:
                raise Exception("Unknown sign defined in MagmaLoad'ed JSON: %r"
                                % sign['name'])
        vim.eval('sign_place(%s, "magma", "%s",'
                 '%s, {"lnum": %s})'
                 % (sign['id'],
                    sign['name'],
                    state.main_buffer.number,
                    sign['lnum']))

    state.history.set(session['history'])  # }}}


def write_session() -> dict:  # {{{
    return {
            'signs': vim.eval('sign_getplaced(%s, {"group": "magma"})'
                              % state.main_buffer.number)[0]['signs'],
            'history': state.history.get(),
            }  # }}}


def read_session_file(path: str):  # {{{
    with open(path) as f:
        session = json.load(f)
        session['history'] = {int(k): v for k, v in session['history'].items()}
        read_session(session)  # }}}


def write_session_file(path: str):  # {{{
    with open(path, 'w') as f:
        json.dump(write_session(), f)  # }}}


def start_outputs(hide, request_newline, allow_external):  # {{{
    global state

    job = ['python3',
           os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        'magma_output.py'),
           state.server_port]
    #       '127.0.0.1', 10000 + state.current_execution_count]

    if request_newline and not allow_external:
        job.append("--semiquiet")
    elif not request_newline and not allow_external:
        job.append("--quiet")

    # Create the output buffer (and the job)
    bufno = vim.eval('term_start(%r, {'
                     ' "term_name": "(magma) Out[%d]",'
                     ' "hidden": %d,'
                     ' "exit_cb": "MagmaOutbufSetNofile%d",'
                     '})'
                     % (job,
                        state.current_execution_count.get(),
                        int(hide),
                        state.current_execution_count.get()))
    vim.command('function! MagmaOutbufSetNofile%d(...)\n'
                '  call setbufvar(%s, "&buftype", "nofile")\n'
                'endfunction'
                % (state.current_execution_count.get(),
                   bufno))
    vim.command('call setbufvar(%s, "&buflisted", 0)' % bufno)
    # vim.command('new')
    # bufno = vim.eval('bufnr()')
    # job = vim.eval('job_start(%r, {'
    #                ' "out_io": "buffer",'
    #                ' "out_buf": %s,'
    #                ' "out_modifiable": 0'
    #                '})'
    #                % (job, bufno))
    # vim.command('set buftype=nofile')
    # vim.command('set nobuflisted')
    vim.command('call win_gotoid(%s)' % state.main_window_id)
    with state.history as history:
        history[state.current_execution_count.get()]['buffer_number'] = bufno
    # }}}


def show_evaluated_output(withBang):  # {{{
    global state

    if not state.initialized.get():
        return

    lineno = vim.eval('line(".")')

    signs = getsigns_line(lineno)
    if len(signs) == 0:
        print("No output to show", file=sys.stderr)
        return
    signname = signs[0]['name']

    if signname == 'magma_hold':
        print("Requested paragraph is to be evaluated", file=sys.stderr)
    elif signname.startswith('magma_running_'):
        print("Requested paragraph is being evaluated", file=sys.stderr)
    elif signname.startswith('magma_err_'):
        print("Requested paragraph did not evaluate successfully",
              file=sys.stderr)
    elif signname.startswith('magma_ok_'):
        if state.kernel_state.get() != KS_IDLE:
            print("TODO: implement (in magma.vim) the ability to view output "
                  "while the kernel is busy", file=sys.stderr)
        else:
            execution_count = int(signname[9:])

            state.port.set(-1)
            if int(vim.eval('g:magma_preview_window_enabled')):
                start_outputs(False, True, withBang)
            else:
                start_outputs(False, True, True)

            def forward_output():
                while state.port.get() == -1:
                    time.sleep(0.1)

                with state.history as history:
                    for data in history[execution_count]['output']:
                        requests.post('http://127.0.0.1:%d'
                                      % state.port.get(),
                                      json=data)

                requests.post('http://127.0.0.1:%d'
                              % state.port.get(),
                              json={'type': 'done'})

            threading.Thread(target=forward_output).start()  # }}}


def update():  # {{{
    global state

    if not state.initialized.get():
        return

    try:
        if state.iopub_queue.empty():
            message = state.client.get_iopub_msg(timeout=0.25)
        else:
            message = state.iopub_queue.get()

        if 'content' not in message or \
           'msg_type' not in message:
            return

        message_type = message['msg_type']
        content = message['content']
        if message_type == 'execute_input':  # {{{
            state.current_execution_count.set(content['execution_count'])
            with state.history as history:
                history[state.current_execution_count.get()] = {
                    'type': 'output',
                    'status': HS_RUNNING,
                    'output': [],
                }

            def initiate_execution():
                with RunInLineNo(state.code_lineno.get()):
                    setsign_running(state.current_execution_count.get())
                state.has_error.set(False)
                has_preview = bool(int(vim.eval(
                    'g:magma_preview_window_enabled')))
                start_outputs(has_preview, not has_preview, not has_preview)
            state.events.put(initiate_execution)
            return  # }}}
        elif message_type == 'status':  # {{{
            if content['execution_state'] == 'idle':  # {{{
                while state.port.get() == -1:
                    time.sleep(0.1)

                requests.post('http://127.0.0.1:%d'
                              % state.port.get(),
                              json={'type': 'done'})

                if state.has_error.get():
                    state.kernel_state.set(KS_IDLE)
                    state.events.put(lambda: chsign_running2err(
                                         state.current_execution_count.get()))
                    # Clear the execution queue:
                    while not state.execution_queue.empty():
                        try:
                            state.execution_queue.get(False)
                        except queue.Empty:
                            continue
                        state.execution_queue.task_done()
                else:
                    state.events.put(lambda: chsign_running2ok(
                                         state.current_execution_count.get()))
                    if state.execution_queue.qsize() > 0:
                        state.kernel_state.set(KS_NONIDLE)

                        def next_from_queue():
                            chsign_hold2running(
                                    state.current_execution_count.get()+1)
                            state.kernel_state.set(KS_IDLE)
                            evaluate(*state.execution_queue.get())
                        state.events.put(next_from_queue)
                    else:
                        state.kernel_state.set(KS_IDLE)  # }}}
            elif content['execution_state'] == 'busy':  # {{{
                state.kernel_state.set(KS_BUSY)  # }}}
            state.events.put(lambda: vim.command('redrawstatus!'))
            return  # }}}
        elif message_type == 'execute_reply' and state.port.get() != -1:  # {{{
            if content['status'] == 'ok':  # {{{
                data = {
                    'type': 'output',
                    'text': content['status'],
                }  # }}}
            elif content['status'] == 'error':  # {{{
                state.has_error.set(True)
                data = {
                    'type': 'error',
                    'error_type': content['ename'],
                    'error_message': content['evalue'],
                    'traceback': content['traceback'],
                }  # }}}
            elif content['status'] == 'abort':  # {{{
                state.has_error.set(True)
                data = {
                    'type': 'error',
                    'error_type': "Aborted",
                    'error_message': "Kernel aborted with no error message",
                    'traceback': "",
                }  # }}}
            with state.history as history:
                history[state.current_execution_count.get()]['output'] \
                    .append(data)  # }}}
        elif (message_type == 'execute_result' and  # {{{
              state.port.get() != -1):
            data = {
                'type': 'display',
                'content': content['data'],
            }
            with state.history as history:
                history[state.current_execution_count.get()]['output'] \
                    .append(data)  # }}}
        elif message_type == 'error' and state.port.get() != -1:  # {{{
            state.has_error.set(True)
            data = {
                'type': 'error',
                'error_type': content['ename'],
                'error_message': content['evalue'],
                'traceback': content['traceback'],
            }
            with state.history as history:
                history[state.current_execution_count.get()]['output'] \
                    .append(data)
            # }}}
        elif message_type == 'display_data' and state.port.get() != -1:  # {{{
            data = {
                'type': 'display',
                'content': content['data'],
            }
            with state.history as history:
                history[state.current_execution_count.get()]['output'] \
                    .append(data)
            # }}}
        elif message_type == 'stream' and state.port.get() != -1:  # {{{
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
            with state.history as history:
                history[state.current_execution_count.get()]['output'] \
                    .append(data)
            # }}}
        elif state.port.get() == -1:
            state.iopub_queue.put(message)
            return
        else:  # {{{
            return  # }}}

        requests.post('http://127.0.0.1:%d'
                      % state.port.get(),
                      json=data)
    except queue.Empty:
        return  # }}}


def update_loop():  # {{{
    global state

    while True:
        if not state.initialized.get():
            break

        update()
        time.sleep(0.5)  # XXX }}}


def vim_update():  # {{{
    global state

    if state.initialized.get():
        while state.events.qsize() > 0:
            state.events.get()()
        update_preview_window()  # }}}


def update_preview_window():  # {{{
    global state

    if not state.initialized.get() or \
       state.preview_window_id == -1 or \
       int(vim.eval('win_id2win(%s)' % state.preview_window_id)) == 0 or \
       vim.current.buffer.number != state.main_buffer.number:
        return

    signs = getsigns_line()

    if len(signs) == 0:
        if vim.current.line == "":
            vim.command('call win_execute(%s, "b %s")'
                        % (state.preview_window_id,
                           state.preview_empty_buffer.number))
        return

    assert len(signs) == 1

    sign_name = signs[0]['name']

    if sign_name.startswith('magma_running_'):
        execution_count = int(sign_name[14:])
    elif sign_name.startswith('magma_ok_'):
        execution_count = int(sign_name[9:])
    elif sign_name.startswith('magma_err_'):
        execution_count = int(sign_name[10:])
    else:
        vim.command('call win_execute(%s, "b %s")'
                    % (state.preview_window_id,
                       state.preview_empty_buffer.number))
        return

    with state.history as history:
        if execution_count not in history or \
           'buffer_number' not in history[execution_count]:
            vim.command('call win_execute(%s, "b %s")'
                        % (state.preview_window_id,
                           state.preview_empty_buffer.number))
            return

        bufnum = history[execution_count]['buffer_number']

    vim.command('call win_execute(%s, "b %s")'
                % (state.preview_window_id,
                   state.preview_empty_buffer.number))
    if int(vim.eval('bufexists(%s)' % bufnum)):
        vim.command('call win_execute(%s, "b %s")'
                    % (state.preview_window_id, bufnum))
    # }}}


def get_kernel_state(vim_var):  # {{{
    global state

    vim.command('let %s = %s' % (vim_var, state.kernel_state.get()))  # }}}


# Iterate over the current paragraph


def paragraph_iter():  # {{{
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
    vim.command('call setpos(".", l:cursor_pos)')  # }}}


# Sign functions:


# # Query:


def getsigns_inbuffer():  # {{{
    signs = vim.eval('sign_getplaced(%s, {"group": "magma"})'
                     % (state.main_buffer.number))

    if len(signs) == 0:
        return []
    else:
        return signs[0]['signs']  # }}}


def getsigns_line(lineno=None):  # {{{
    if lineno is None:
        signs = vim.eval('sign_getplaced(%s,'
                         ' {"group": "magma","lnum": line(".")})'
                         % (state.main_buffer.number))
    else:
        signs = vim.eval('sign_getplaced(%s, {"group": "magma","lnum": %s})'
                         % (state.main_buffer.number, lineno))

    if len(signs) == 0:
        return []
    else:
        return signs[0]['signs']  # }}}


# # Modify:


def setsign(state_sign_storage, sign_text, sign_texthl, sign_linehl):  # {{{
    def inner(gen_sign_name):  # {{{
        def func(execution_count):  # {{{
            global state

            sign_name = gen_sign_name(execution_count)

            state_sign_storage[execution_count] = []
            for lineno, linestr in paragraph_iter():
                if not vim.eval('sign_getdefined("%s")'
                                % sign_name):
                    vim.command('sign define %s text=%s'
                                ' texthl=%s'
                                ' linehl=%s'
                                % (sign_name,
                                   sign_text,
                                   sign_texthl,
                                   sign_linehl))
                signid = vim.eval('sign_place(0, "magma", "%s",'
                                  ' %s, {"lnum": %s})'
                                  % (sign_name,
                                     state.main_buffer.number,
                                     lineno))
                state_sign_storage[execution_count].append(signid)  # }}}

        return func  # }}}

    return inner  # }}}


def unsetsign(state_sign_storage):  # {{{
    def inner(gen_sign_name):  # {{{
        def func(execution_count):  # {{{
            global state

            for signid in state_sign_storage[execution_count]:
                vim.command('sign unplace %s group=magma buffer=%s'
                            % (signid, state.main_buffer.number))
            del state_sign_storage[execution_count]  # }}}

        return func  # }}}

    return inner  # }}}


def chsign(state_sign_storage, new_state_sign_storage,  # {{{
           sign_text, sign_texthl, sign_linehl):
    def inner(gen_sign_name):  # {{{
        def func(execution_count):  # {{{
            global state

            sign_name = gen_sign_name(execution_count)

            new_state_sign_storage[execution_count] = []
            for signid in state_sign_storage[execution_count]:
                lineno = vim.eval('sign_getplaced(%s,'
                                  '{"group": "magma", "id": %s})'
                                  % (state.main_buffer.number, signid)
                                  )[0]['signs'][0]['lnum']
                vim.command('sign unplace %s group=magma buffer=%s'
                            % (signid, state.main_buffer.number))
                if not vim.eval('sign_getdefined("%s")'
                                % sign_name):
                    vim.command('sign define %s text=%s'
                                ' texthl=%s'
                                ' linehl=%s'
                                % (sign_name,
                                   sign_text,
                                   sign_texthl,
                                   sign_linehl))
                signid = vim.eval('sign_place(0, "magma", "%s",'
                                  ' %s, {"lnum": %s})'
                                  % (sign_name,
                                     state.main_buffer.number,
                                     lineno))
                new_state_sign_storage[execution_count].append(signid)
            del state_sign_storage[execution_count]  # }}}

        return func  # }}}

    return inner  # }}}


# setsign_*  {{{


@setsign(state.sign_ids_hold, "∗", 'MagmaHoldSign', 'MagmaHoldLine')
def setsign_hold(_):
    return 'magma_hold'


@setsign(state.sign_ids_running, "@", 'MagmaRunningSign', 'MagmaRunningLine')
def setsign_running(execution_count):
    return 'magma_running_%d' % execution_count


@setsign(state.sign_ids_ok, "✓", 'MagmaOkSign', 'MagmaOkLine')
def setsign_ok(execution_count):
    return 'magma_ok_%d' % execution_count


@setsign(state.sign_ids_err, "✗", 'MagmaErrSign', 'MagmaErrLine')
def setsign_err(execution_count):
    return 'magma_err_%d' % execution_count
# }}}


# unsetsign_*  {{{

@unsetsign(state.sign_ids_hold)
def unsetsign_hold():
    pass


@unsetsign(state.sign_ids_running)
def unsetsign_running():
    pass


@unsetsign(state.sign_ids_ok)
def unsetsign_ok():
    pass


@unsetsign(state.sign_ids_err)
def unsetsign_err():
    pass
# }}}


# chsign_*2*  {{{


@chsign(state.sign_ids_hold, state.sign_ids_running, "@", 'MagmaRunningSign',
        'MagmaRunningLine')
def chsign_hold2running(execution_count):
    return 'magma_running_%d' % execution_count


@chsign(state.sign_ids_running, state.sign_ids_ok, "✓", 'MagmaOkSign',
        'MagmaOkLine')
def chsign_running2ok(execution_count):
    return 'magma_ok_%d' % execution_count


@chsign(state.sign_ids_running, state.sign_ids_err, "✗", 'MagmaErrSign',
        'MagmaErrLine')
def chsign_running2err(execution_count):
    return 'magma_err_%d' % execution_count
# }}}
