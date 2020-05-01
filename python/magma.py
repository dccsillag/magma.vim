import http.server
import json
import os
import queue
import socketserver
import sys
import threading
import time
from functools import partial
from uuid import uuid4

import vim

KS_IDLE = 0
KS_NONIDLE = 1
KS_BUSY = 2
KS_NOT_CONNECTED = 3

HS_RUNNING = 0
HS_DONE = 1
HS_ERROR = 2

EXECUTION_BIGTIME = 10


class Locked(object):
    def __init__(self, var):
        self.var = var
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            return self.var

    def set(self, newvar):
        with self.lock:
            self.var = newvar

    def __enter__(self, *args):
        self.lock.__enter__(*args)
        return self.var

    def __exit__(self, *args):
        return self.lock.__exit__(*args)


class Magma(object):
    uuid = None

    initialized = Locked(False)
    client = None
    history = Locked({})
    current_execution_count = Locked(0)
    background_loop = None
    background_server = None
    kernel_state = Locked(KS_NOT_CONNECTED)

    # general_lock = threading.Lock()

    events = queue.Queue()
    server_port = -1
    port = Locked(-1)

    iopub_queue = queue.Queue()
    has_error = Locked(False)
    execution_timestamp = None

    execution_queue = queue.Queue()
    code_lineno = Locked("")  # Vim stores its ints as str in Python

    main_buffer = None
    main_window_id = -1
    preview_empty_buffer = None
    preview_window_id = -1
    output_buffers = []

    sign_ids_hold = {}
    sign_ids_wait = {}
    sign_ids_running = {}
    sign_ids_ok = {}
    sign_ids_err = {}

    def __init__(self):
        self.uuid = uuid4()

    def __eq__(self, other):
        if not isinstance(other, Magma):
            return False
        else:
            return self.uuid == other.uuid

    def __del__(self):
        self.deinitialize()

    def initialize_common(self):
        self.kernel_state.set(KS_IDLE)
        self.main_buffer = vim.current.buffer
        self.main_window_id = vim.eval("win_getid()")

        if preview_window_enabled():
            vim.command("new")
            vim.command("setl nobuflisted")
            vim.command("f (magma.vim) Output Preview")
            vim.command("setl buftype=nofile")
            vim.command("setl noswapfile")
            vim.command("setl nomodifiable")
            vim.command("setl nonumber")
            vim.command("setl norelativenumber")
            vim.command("setl foldcolumn=0")
            vim.command("setl signcolumn=no")
            self.preview_empty_buffer = vim.current.buffer
            self.preview_window_id = vim.eval("win_getid()")
            vim.command("call win_gotoid(%s)" % self.main_window_id)

        vim.command("call timer_pause(g:magma_timer, 0)")

        self.initialized.set(True)

        self.start_background_loop()
        self.start_background_server()

    def initialize_new(self, kernel_name):
        """
        Initialize the client and a local kernel, if it isn't yet initialized.
        """

        import jupyter_client

        if not self.initialized.get():
            # self.client = jupyter_client.run_kernel(kernel_name=kernel_name)
            # self.client = jupyter_client.BlockingKernelClient()
            _, self.client = jupyter_client.manager.start_new_kernel(
                kernel_name=kernel_name
            )
            self.initialize_common()

    def initialize_remote(self, connection_file, ssh=None):
        """
        Initialize the client and connect to a kernel (possibly remote),
          if it isn't yet initialized.
        """

        import jupyter_client

        if not self.initialized.get():
            self.client = jupyter_client.BlockingKernelClient()
            if ssh is None:
                self.client.load_connection_file(connection_file)
            else:
                with open(connection_file) as f:
                    parsed = json.load(f)

                newports = jupyter_client.tunnel_to_kernel(connection_file, ssh)
                parsed["shell_port"], parsed["iopub_port"], parsed[
                    "stdin_port"
                ], parsed["hb_port"], parsed["control_port"] = newports

                with open(connection_file, "w") as f:
                    json.dump(parsed, f)

                self.client.load_connection_file(connection_file)

            self.client.start_channels()
            try:
                print("Connecting to the kernel...")
                self.client.wait_for_ready(timeout=60)
            except RuntimeError as err:
                self.client.stop_channels()
                print("Could not connect to existing kernel: %s" % err, file=sys.stderr)
                return
            self.initialize_common()

    def start_background_loop(self):
        self.background_loop = threading.Thread(target=self.update_loop)
        self.background_loop.start()

    def start_background_server(self):
        def run_server():
            try:
                with socketserver.TCPServer(
                    ("", 0), make_handler_for_instance(self)
                ) as httpd:
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

        if self.initialized.get():
            self.client.initialized = False
            self.kernel_state.set(KS_NOT_CONNECTED)
            self.client.shutdown(True)
            self.kernel_state.set(KS_IDLE)
            self.client.initialized = True

    def deinitialize(self):
        """
        Deinitialize the client, if it is initialized.
        """
        import requests

        if self.initialized.get():
            # FIXME: possible concurrency issue due to `self.server_port`:
            requests.post(
                "http://127.0.0.1:%d" % self.server_port, json={"action": "shutdown"}
            )

            self.initialized.set(False)

            self.background_loop.join()
            self.background_server.join()

            self.client.shutdown()
            self.kernel_state.set(KS_NOT_CONNECTED)
            self.main_buffer = None

            if preview_window_enabled():
                vim.command('call win_execute(%s, "bd")' % self.preview_window_id)
            self.main_window_id = -1
            self.preview_window_id = -1

            vim.command("call timer_pause(g:magma_timer, 1)")

    # ---

    def setup_ssh_tunneling(self, host, connection_file):
        import jupyter_client

        jupyter_client.tunnel_to_kernel(connection_file, host)

    def init_local(self):
        import jupyter_client

        if not self.initialized.get():
            # Select from available kernels
            # # List them
            kernelspecmanager = jupyter_client.kernelspec.KernelSpecManager()
            kernels = kernelspecmanager.get_all_specs()
            specs = [jupyter_client.kernelspec.get_kernel_spec(x) for x in kernels]
            # # Ask
            choice = int(
                vim.eval(
                    "inputlist(%r)"
                    % (
                        ["Choose the kernel to start:"]
                        + [
                            "%d. %s" % (a + 1, b)
                            for a, b in enumerate(y.display_name for y in specs)
                        ]
                    )
                )
            )
            if choice == 0:
                print("Cancelled.")
                return False

            self.initialize_new(specs[choice - 1].language)

            print(
                "Successfully initialized kernel %s!" % specs[choice - 1].display_name
            )

            return True

    def init_existing(self, connection_file):
        if not self.initialized.get():
            try:
                self.initialize_remote(connection_file)
            except:
                print("Could not connect to the kernel", file=sys.stderr)
                return False

            print("Successfully connected to the kernel!")

            return True

    def init_remote(self, host, connection_file):
        if not self.initialized.get():
            self.initialize_remote(connection_file, ssh=host)

            print("Successfully connected to the remote kernel!")

            return True

    def deinit(self):
        self.deinitialize()

    def evaluate(self, code, code_lineno):
        if not self.initialized.get():
            return

        # Check if we can actually evaluate this code (e.g. isn't already in queue)
        signs = self.getsigns_inbuffer()
        if any(int(sign["id"]) in self.sign_ids_hold for sign in signs):
            print("Trying to re-evaluate a line that is on hold", file=sys.stderr)
            return False
        if any(int(sign["id"]) in self.sign_ids_running for sign in signs):
            print(
                "Trying to re-evaluate a line that is already running", file=sys.stderr
            )
            return False

        with RunInLineNo(code_lineno):
            for lineno, linestr in self.paragraph_iter():
                signs_in_this_line = [sign for sign in signs if sign["lnum"] == lineno]
                for sign in signs_in_this_line:
                    vim.command(
                        "sign unplace %s group=magma buffer=%s"
                        % (sign["id"], self.main_buffer.number)
                    )
        self.setsign_wait(self.current_execution_count.get() + 1)

        if self.kernel_state.get() == KS_IDLE:
            self.kernel_state.set(KS_NONIDLE)

            self.port.set(-1)
            self.code_lineno.set(code_lineno)

            self.execution_timestamp = time.time()
            self.client.execute(code)
        elif self.kernel_state.get() in (KS_BUSY, KS_NONIDLE):
            with RunInLineNo(code_lineno):
                self.setsign_hold(
                    self.current_execution_count.get()
                    + self.execution_queue.qsize()
                    + 1
                )
            self.execution_queue.put((code, code_lineno))
        else:
            print("Invalid kernel state: %d" % self.kernel_state.get(), file=sys.stderr)
            return

    def read_session(self, session: dict):
        for sign in session["signs"]:
            defined_signs = vim.eval("sign_getdefined(%r)" % sign["name"])
            if len(defined_signs) == 0:
                if sign["name"] == "magma_hold":
                    raise Exception("Somehow, the magma_hold sign is not defined.")
                elif sign["name"].startswith("magma_running_"):
                    vim.command(
                        "sign define %s text=@"
                        " texthl=MagmaRunningSign"
                        " linehl=MagmaRunningLine" % sign["name"]
                    )
                elif sign["name"].startswith("magma_ok_"):
                    vim.command(
                        "sign define %s text=✓"
                        " texthl=MagmaOkSign"
                        " linehl=MagmaOkLine" % sign["name"]
                    )
                elif sign["name"].startswith("magma_err_"):
                    vim.command(
                        "sign define %s text=✗"
                        " texthl=MagmaErrSign"
                        " linehl=MagmaErrLine" % sign["name"]
                    )
                else:
                    raise Exception(
                        "Unknown sign defined in MagmaLoad'ed JSON: %r" % sign["name"]
                    )
            vim.eval(
                'sign_place(%s, "magma", "%s",'
                '%s, {"lnum": %s})'
                % (sign["id"], sign["name"], self.main_buffer.number, sign["lnum"])
            )

        self.history.set(session["history"])

    def write_session(self) -> dict:
        return {
            "signs": vim.eval(
                'sign_getplaced(%s, {"group": "magma"})' % self.main_buffer.number
            )[0]["signs"],
            "history": self.history.get(),
        }

    def read_session_file(self, path: str):
        with open(path) as f:
            session = json.load(f)
            session["history"] = {int(k): v for k, v in session["history"].items()}
            self.read_session(session)

    def write_session_file(self, path: str):
        with open(path, "w") as f:
            json.dump(self.write_session(), f)

    def start_outputs(self, hide, request_newline, allow_external):
        job = [
            "python3",
            os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "magma_output.py"
            ),
            self.server_port,
        ]
        #       '127.0.0.1', 10000 + self.current_execution_count]

        if request_newline and not allow_external:
            job.append("--semiquiet")
        elif not request_newline and not allow_external:
            job.append("--quiet")

        # Create the output buffer (and the job)
        bufno = vim.eval(
            "term_start(%r, {"
            ' "term_name": "(magma.vim) Out[%d]",'
            ' "hidden": %d,'
            ' "exit_cb": "MagmaOutbufSetOptions%d",'
            "})"
            % (
                job,
                self.current_execution_count.get(),
                int(hide),
                self.current_execution_count.get(),
            )
        )
        vim.command(
            "function! MagmaOutbufSetOptions%d(...)\n"
            '  call setbufvar(%s, "&buftype", "nofile")\n'
            '  call setbufvar(%s, "&swapfile", 0)\n'
            '  call setbufvar(%s, "&number", 0)\n'
            '  call setbufvar(%s, "&relativenumber", 0)\n'
            '  call setbufvar(%s, "&foldcolumn", 0)\n'
            '  call setbufvar(%s, "&signcolumn", "no")\n'
            '  call setbufvar(%s, "&listchars", "")\n'
            "endfunction" % ((self.current_execution_count.get(),) + 7 * (bufno,))
        )
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
        vim.command("call win_gotoid(%s)" % self.main_window_id)
        self.output_buffers.append(bufno)
        with self.history as history:
            history[self.current_execution_count.get()]["buffer_number"] = bufno

    def show_evaluated_output(self, withBang):
        import requests

        if not self.initialized.get():
            return

        lineno = vim.eval('line(".")')

        signs = self.getsigns_line(lineno)
        if len(signs) == 0:
            print("No output to show", file=sys.stderr)
            return
        signname = signs[0]["name"]

        if signname == "magma_hold":
            print("Requested paragraph is to be evaluated", file=sys.stderr)
        elif signname.startswith("magma_running_"):
            print("Requested paragraph is being evaluated", file=sys.stderr)
        elif signname.startswith("magma_err_"):
            print("Requested paragraph did not evaluate successfully", file=sys.stderr)
        elif signname.startswith("magma_ok_"):
            if self.kernel_state.get() != KS_IDLE:
                print(
                    "TODO: implement (in magma.vim) the ability to view output "
                    "while the kernel is busy",
                    file=sys.stderr,
                )
                return
            else:
                execution_count = int(signname[9:])

                self.port.set(-1)
                if preview_window_enabled():
                    self.start_outputs(False, True, withBang)
                else:
                    self.start_outputs(False, True, True)

                def forward_output():
                    # FIXME: Vim likes to crash here (output makes it look as if it runs twice):
                    while self.port.get() == -1:
                        time.sleep(0.1)

                    with self.history as history:
                        for data in history[execution_count]["output"]:
                            requests.post(
                                "http://127.0.0.1:%d" % self.port.get(), json=data
                            )

                    requests.post(
                        "http://127.0.0.1:%d" % self.port.get(), json={"type": "done"}
                    )

                threading.Thread(target=forward_output).start()

    def update(self):
        import requests

        if not self.initialized.get():
            return

        try:
            if self.iopub_queue.empty():
                message = self.client.get_iopub_msg(timeout=0.25)
            else:
                message = self.iopub_queue.get()

            if "content" not in message or "msg_type" not in message:
                return

            message_type = message["msg_type"]
            content = message["content"]
            if message_type == "execute_input":
                self.current_execution_count.set(content["execution_count"])
                with self.history as history:
                    history[self.current_execution_count.get()] = {
                        "type": "output",
                        "status": HS_RUNNING,
                        "output": [],
                    }

                def initiate_execution():
                    # with RunInLineNo(self.code_lineno.get()):
                    self.chsign_wait2running(self.current_execution_count.get())

                    self.has_error.set(False)
                    self.start_outputs(
                        preview_window_enabled(),
                        not preview_window_enabled(),
                        not preview_window_enabled(),
                    )

                self.events.put(initiate_execution)
                return
            elif message_type == "status":
                if content["execution_state"] == "idle":
                    while self.port.get() == -1:
                        time.sleep(0.1)

                    requests.post(
                        "http://127.0.0.1:%d" % self.port.get(), json={"type": "done"}
                    )

                    if self.has_error.get():
                        with self.history as hist:
                            hist[self.current_execution_count.get()][
                                "status"
                            ] = HS_ERROR
                        self.kernel_state.set(KS_IDLE)
                        if time.time() - self.execution_timestamp > EXECUTION_BIGTIME:
                            self.events.put(partial(notify_done, True))
                        self.events.put(
                            lambda: self.chsign_running2err(
                                self.current_execution_count.get()
                            )
                        )
                        # Clear the execution queue:
                        while not self.execution_queue.empty():
                            try:
                                self.execution_queue.get(False)
                            except queue.Empty:
                                continue
                            self.execution_queue.task_done()
                    else:
                        with self.history as hist:
                            hist[self.current_execution_count.get()]["status"] = HS_DONE
                        self.events.put(
                            lambda: self.chsign_running2ok(
                                self.current_execution_count.get()
                            )
                        )
                        if self.execution_queue.qsize() > 0:
                            self.kernel_state.set(KS_NONIDLE)

                            def next_from_queue():
                                self.chsign_hold2running(
                                    self.current_execution_count.get() + 1
                                )
                                self.kernel_state.set(KS_IDLE)
                                self.evaluate(*self.execution_queue.get())

                            self.events.put(next_from_queue)
                        else:
                            if (
                                time.time() - self.execution_timestamp
                                > EXECUTION_BIGTIME
                            ):
                                self.events.put(partial(notify_done, False))
                            self.kernel_state.set(KS_IDLE)
                elif content["execution_state"] == "busy":
                    self.kernel_state.set(KS_BUSY)
                self.events.put(lambda: vim.command("redrawstatus!"))
                return
            elif message_type == "execute_reply" and self.port.get() != -1:
                if content["status"] == "ok":
                    data = {"type": "output", "text": content["status"]}
                elif content["status"] == "error":
                    self.has_error.set(True)
                    data = {
                        "type": "error",
                        "error_type": content["ename"],
                        "error_message": content["evalue"],
                        "traceback": content["traceback"],
                    }
                elif content["status"] == "abort":
                    self.has_error.set(True)
                    data = {
                        "type": "error",
                        "error_type": "Aborted",
                        "error_message": "Kernel aborted with no error message",
                        "traceback": "",
                    }
                with self.history as history:
                    history[self.current_execution_count.get()]["output"].append(data)
            elif message_type == "execute_result" and self.port.get() != -1:
                data = {"type": "display", "content": content["data"]}
                with self.history as history:
                    history[self.current_execution_count.get()]["output"].append(data)
            elif message_type == "error" and self.port.get() != -1:
                self.has_error.set(True)
                data = {
                    "type": "error",
                    "error_type": content["ename"],
                    "error_message": content["evalue"],
                    "traceback": content["traceback"],
                }
                with self.history as history:
                    history[self.current_execution_count.get()]["output"].append(data)
            elif message_type == "display_data" and self.port.get() != -1:
                data = {"type": "display", "content": content["data"]}
                with self.history as history:
                    history[self.current_execution_count.get()]["output"].append(data)
            elif message_type == "stream" and self.port.get() != -1:
                name = content["name"]
                if name == "stdout":
                    data = {"type": "stdout", "content": content["text"]}
                elif name == "stderr":
                    data = {"type": "stderr", "content": content["text"]}
                else:
                    raise Exception("Unkown stream:name = '%s'" % name)
                with self.history as history:
                    history[self.current_execution_count.get()]["output"].append(data)
            elif self.port.get() == -1:
                self.iopub_queue.put(message)
                return
            else:
                return

            requests.post("http://127.0.0.1:%d" % self.port.get(), json=data)
        except queue.Empty:
            return

    def update_loop(self):
        while True:
            if not self.initialized.get():
                break

            self.update()
            time.sleep(0.05)

    def vim_update(self):
        if self.initialized.get():
            while self.events.qsize() > 0:
                self.events.get()()
            self.update_preview_window()

    def update_preview_window(self):
        if (
            not self.initialized.get()
            or self.preview_window_id == -1
            or int(vim.eval("win_id2win(%s)" % self.preview_window_id)) == 0
            or vim.current.buffer.number != self.main_buffer.number
        ):
            return

        signs = self.getsigns_line()

        if len(signs) == 0:
            if vim.current.line == "":
                vim.command(
                    'call win_execute(%s, "b %s")'
                    % (self.preview_window_id, self.preview_empty_buffer.number)
                )
            return

        sign_name = signs[0]["name"]

        if sign_name.startswith("magma_running_"):
            execution_count = int(sign_name[14:])
        elif sign_name.startswith("magma_ok_"):
            execution_count = int(sign_name[9:])
        elif sign_name.startswith("magma_err_"):
            execution_count = int(sign_name[10:])
        else:
            vim.command(
                'call win_execute(%s, "b %s")'
                % (self.preview_window_id, self.preview_empty_buffer.number)
            )
            return

        with self.history as history:
            if (
                execution_count not in history
                or "buffer_number" not in history[execution_count]
            ):
                vim.command(
                    'call win_execute(%s, "b %s")'
                    % (self.preview_window_id, self.preview_empty_buffer.number)
                )
                return

            bufnum = history[execution_count]["buffer_number"]

        vim.command(
            'call win_execute(%s, "b %s")'
            % (self.preview_window_id, self.preview_empty_buffer.number)
        )
        if int(vim.eval("bufexists(%s)" % bufnum)):
            vim.command(
                'call win_execute(%s, "b %s")' % (self.preview_window_id, bufnum)
            )

    def get_kernel_state(self, vim_var):
        vim.command("let %s = %s" % (vim_var, self.kernel_state.get()))

    # Iterate over the current paragraph

    @staticmethod
    def paragraph_iter():
        vim.command('let l:cursor_pos = getpos(".")')
        vim.command("normal {")
        if vim.current.line == "":
            vim.command("normal j")
        while vim.current.line != "":
            yield vim.eval('line(".")'), vim.current.line
            prev_linenr = vim.eval('line(".")')
            vim.command("normal j")
            if prev_linenr == vim.eval('line(".")'):
                break
        vim.command('call setpos(".", l:cursor_pos)')

    # Sign functions:

    # # Query:

    def getsigns_inbuffer(self):
        signs = vim.eval(
            'sign_getplaced(%s, {"group": "magma"})' % (self.main_buffer.number)
        )

        if len(signs) == 0:
            return []
        else:
            return signs[0]["signs"]

    def getsigns_line(self, lineno=None):
        if lineno is None:
            signs = vim.eval(
                "sign_getplaced(%s,"
                ' {"group": "magma","lnum": line(".")})' % (self.main_buffer.number)
            )
        else:
            signs = vim.eval(
                'sign_getplaced(%s, {"group": "magma","lnum": %s})'
                % (self.main_buffer.number, lineno)
            )

        if len(signs) == 0:
            return []
        else:
            return signs[0]["signs"]

    # # Modify:

    def setsign(state_sign_storage, sign_text, sign_texthl, sign_linehl):
        def inner(gen_sign_name):
            def func(self, execution_count):
                sign_name = gen_sign_name(execution_count)

                state_sign_storage(self)[execution_count] = []
                for lineno, linestr in self.paragraph_iter():
                    if not vim.eval('sign_getdefined("%s")' % sign_name):
                        vim.command(
                            "sign define %s text=%s"
                            " texthl=%s"
                            " linehl=%s"
                            % (sign_name, sign_text, sign_texthl, sign_linehl)
                        )
                    signid = vim.eval(
                        'sign_place(0, "magma", "%s",'
                        ' %s, {"lnum": %s})'
                        % (sign_name, self.main_buffer.number, lineno)
                    )
                    state_sign_storage(self)[execution_count].append(signid)

            return func

        return inner

    def unsetsign(state_sign_storage):
        def inner(gen_sign_name):
            def func(self, execution_count):
                for signid in state_sign_storage(self)[execution_count]:
                    vim.command(
                        "sign unplace %s group=magma buffer=%s"
                        % (signid, self.main_buffer.number)
                    )
                del state_sign_storage(self)[execution_count]

            return func

        return inner

    def chsign(
        state_sign_storage, new_state_sign_storage, sign_text, sign_texthl, sign_linehl
    ):
        def inner(gen_sign_name):
            def func(self, execution_count):
                sign_name = gen_sign_name(execution_count)

                new_state_sign_storage(self)[execution_count] = []
                for signid in state_sign_storage(self)[execution_count]:
                    lineno = vim.eval(
                        "sign_getplaced(%s,"
                        '{"group": "magma", "id": %s})'
                        % (self.main_buffer.number, signid)
                    )[0]["signs"][0]["lnum"]
                    vim.command(
                        "sign unplace %s group=magma buffer=%s"
                        % (signid, self.main_buffer.number)
                    )
                    if not vim.eval('sign_getdefined("%s")' % sign_name):
                        vim.command(
                            "sign define %s text=%s"
                            " texthl=%s"
                            " linehl=%s"
                            % (sign_name, sign_text, sign_texthl, sign_linehl)
                        )
                    signid = vim.eval(
                        'sign_place(0, "magma", "%s",'
                        ' %s, {"lnum": %s})'
                        % (sign_name, self.main_buffer.number, lineno)
                    )
                    new_state_sign_storage(self)[execution_count].append(signid)
                del state_sign_storage(self)[execution_count]

            return func

        return inner

    # setsign_*

    @setsign(lambda self: self.sign_ids_hold, "∗", "MagmaHoldSign", "MagmaHoldLine")
    def setsign_hold(_):
        return "magma_hold"

    @setsign(lambda self: self.sign_ids_wait, "-", "MagmaWaitSign", "MagmaWaitLine")
    def setsign_wait(_):
        return "magma_waiting"

    @setsign(
        lambda self: self.sign_ids_running, "@", "MagmaRunningSign", "MagmaRunningLine"
    )
    def setsign_running(execution_count):
        return "magma_running_%d" % execution_count

    @setsign(lambda self: self.sign_ids_ok, "✓", "MagmaOkSign", "MagmaOkLine")
    def setsign_ok(execution_count):
        return "magma_ok_%d" % execution_count

    @setsign(lambda self: self.sign_ids_err, "✗", "MagmaErrSign", "MagmaErrLine")
    def setsign_err(execution_count):
        return "magma_err_%d" % execution_count

    # unsetsign_*

    @unsetsign(lambda self: self.sign_ids_hold)
    def unsetsign_hold():
        pass

    @unsetsign(lambda self: self.sign_ids_wait)
    def unsetsign_wait():
        pass

    @unsetsign(lambda self: self.sign_ids_running)
    def unsetsign_running():
        pass

    @unsetsign(lambda self: self.sign_ids_ok)
    def unsetsign_ok():
        pass

    @unsetsign(lambda self: self.sign_ids_err)
    def unsetsign_err():
        pass

    # chsign_*2*

    @chsign(
        lambda self: self.sign_ids_hold,
        lambda self: self.sign_ids_running,
        "@",
        "MagmaRunningSign",
        "MagmaRunningLine",
    )
    def chsign_hold2running(execution_count):
        return "magma_running_%d" % execution_count

    @chsign(
        lambda self: self.sign_ids_wait,
        lambda self: self.sign_ids_running,
        "@",
        "MagmaRunningSign",
        "MagmaRunningLine",
    )
    def chsign_wait2running(execution_count):
        return "magma_running_%d" % execution_count

    @chsign(
        lambda self: self.sign_ids_running,
        lambda self: self.sign_ids_ok,
        "✓",
        "MagmaOkSign",
        "MagmaOkLine",
    )
    def chsign_running2ok(execution_count):
        return "magma_ok_%d" % execution_count

    @chsign(
        lambda self: self.sign_ids_running,
        lambda self: self.sign_ids_err,
        "✗",
        "MagmaErrSign",
        "MagmaErrLine",
    )
    def chsign_running2err(execution_count):
        return "magma_err_%d" % execution_count


magma_instances = []


def get_magma_instance():
    current_buffer = vim.current.buffer.number
    for instance in magma_instances:
        if (
            current_buffer == instance.main_buffer.number
            or current_buffer in instance.output_buffers
        ):
            return instance


def make_handler_for_instance(magma_instance):
    class MyHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers["Content-length"])
            body = json.loads(self.rfile.read(content_length))
            self.send_response(202)
            self.end_headers()

            if "action" in body and body["action"] == "shutdown":
                raise KeyboardInterrupt

            magma_instance.port.set(body["port"])

        def log_message(self, format, *args):
            pass  # do nothing

    return MyHandler


class RunInLineNo(object):
    def __init__(self, lineno=None):
        self.lineno = lineno

    def __enter__(self):
        self.current_line = vim.eval('getpos(".")')
        if self.lineno is not None:
            vim.command("normal %sG" % self.lineno)

    def __exit__(self, *_):
        vim.command('call setpos(".", %s)' % self.current_line)


def notify_done(has_error):
    if has_error:
        vim.command(
            'call popup_notification("'
            "Execution aborted due to error"
            '", {"title": "Magma"})'
        )
    else:
        vim.command(
            'call popup_notification("' "Done executing" '", {"title": "Magma"})'
        )
    vim.command('call sound_playevent("%s")' % vim.eval("g:magma_alert_sound"))


# Query for Magma options


def preview_window_enabled():
    return bool(int(vim.eval("g:magma_preview_window_enabled")))
