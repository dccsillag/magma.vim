import json
import os
import queue
import sys
import threading
import time
from typing import Dict

import jupyter_client
import requests

import vim

KS_IDLE = 0
KS_BUSY = 1
KS_NOT_CONNECTED = 2
KS_DEAD = 3

HS_RUNNING = 0
HS_DONE = 1


class State(object):
    initialized: bool = False
    client: jupyter_client.BlockingKernelClient = None
    history: Dict[str, dict] = {}
    current_execution_count: int = 0
    background_loop = None
    kernel_state: int = KS_NOT_CONNECTED
    events: queue.Queue = queue.Queue()

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
            self.initialized = True

            self.start_background_loop()

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
            self.initialized = True

            self.start_background_loop()

    def start_background_loop(self):
        self.background_loop = threading.Thread(target=update_loop)
        self.background_loop.start()

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
            self.initialized = False

            self.background_loop.join()


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

        print()
        vim.command('echomsg "Successfully initialized kernel %s!"'
                    % specs[choice-1].display_name)

        return True


def init_existing(connection_file):
    global state

    if not state.initialized:
        state.initialize_remote(connection_file)

        vim.command('echomsg "Successfully connected to the kernel!"')

        return True


def init_remote(host, connection_file):
    global state

    if not state.initialized:
        state.initialize_remote(connection_file, ssh=host)

        vim.command('echomsg "Successfully connected to the remote kernel!"')

        return True


def deinit():
    global state

    state.deinitialize()


def evaluate(code):
    global state

    if not state.initialized:
        return

    msg_id = state.client.execute(code)

    reply = state.client.get_shell_msg(msg_id)
    content = reply['content']
    status = content['status']

    state.current_execution_count = content['execution_count']
    state.history[state.current_execution_count] = {
        'type': 'output',
        'status': HS_RUNNING,
        'output': []
    }
    if status == 'error':
        state.history[state.current_execution_count]['output'].append({
            'type': 'error',
            'error_type': content['ename'],
            'error_message': content['evalue'],
            'traceback': content['traceback'],
        })
    elif status == 'abort':
        state.history[state.current_execution_count]['output'].append({
            'type': 'error',
            'error_type': "Aborted",
            'error_message': "Kernel aborted with no error message",
            'traceback': "",
        })
    start_outputs()


def start_outputs():
    global state

    job = ['python3',
           os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        'magma_output.py'),
           '127.0.0.1', 10000 + state.current_execution_count]
    # TODO: add to a map of output jobs in order to communicate jupyter IO

    vim.eval('term_start(%r, {"term_name": "(magma) Out[%d]"})'
             % (job, state.current_execution_count))


def show_output():
    global state

    pass  # # if not evaluated:
    pass  # #    evaluate
    pass  # # show popup with evaluated code


def update():
    global state

    if not state.initialized:
        return

    try:
        message = state.client.get_iopub_msg(timeout=0.25)
        if 'content' not in message or \
           'msg_type' not in message:
            return

        # pprint(message)
        message_type = message['msg_type']
        content = message['content']
        if message_type == 'execute_reply':
            if content['status'] == 'ok':
                data = {
                    'type': 'output',
                    'text': content['status'],
                }
            elif content['status'] == 'error':
                data = {
                    'type': 'error',
                    'error_type': content['ename'],
                    'error_message': content['evalue'],
                    'traceback': content['traceback'],
                }
            elif content['status'] == 'abort':
                data = {
                    'type': 'error',
                    'error_type': "Aborted",
                    'error_message': "Kernel aborted with no error message",
                    'traceback': "",
                }
            state.history[state.current_execution_count]['output'].append(data)
        elif message_type == 'execute_result':
            data = {
                'type': 'display',
                'content': content['data'],
            }
            state.history[state.current_execution_count]['output'].append(data)
        elif message_type == 'error':
            data = {
                'type': 'error',
                'error_type': content['ename'],
                'error_message': content['evalue'],
                'traceback': content['traceback'],
            }
            state.history[state.current_execution_count]['output'].append(data)
        elif message_type == 'display_data':
            data = {
                'type': 'display',
                'content': content['data'],
            }
            state.history[state.current_execution_count]['output'].append(data)
        elif message_type == 'stream':
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
        elif message_type == 'execute_input':
            return  # TODO
        elif message_type == 'status':
            if content['execution_state'] == 'idle':
                state.kernel_state = KS_IDLE
                requests.post('http://127.0.0.1:%d'
                              % (10000 + state.current_execution_count),
                              json={'type': 'done'})
            elif content['execution_state'] == 'busy':
                state.kernel_state = KS_BUSY
            state.events.put(lambda: vim.command('redrawstatus!'))
            return
        else:
            return

        requests.post('http://127.0.0.1:%d'
                      % (10000 + state.current_execution_count),
                      json=data)
    except queue.Empty:
        return


def update_loop():
    global state

    while True:
        if not state.initialized:
            break
        update()
        time.sleep(0.5)


def vim_update():
    global state

    if state.initialized:
        while state.events.qsize() > 0:
            state.events.get()()


def get_kernel_state(vim_var):
    global state

    vim.command('let %s = %s' % (vim_var, state.kernel_state))
