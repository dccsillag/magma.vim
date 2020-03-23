from typing import Dict
import queue
import json
import sys
import os
import time
import threading
import requests
import vim
import jupyter_client


class State(object):
    initialized: bool = False
    client: jupyter_client.BlockingKernelClient = None
    waiting: Dict[str, dict] = {}
    output_count: int = 0
    background_loop = None

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
            self.client.shutdown(True)
            self.client.initialized = True

    def deinitialize(self):
        """
        Deinitialize the client, if it is initialized.
        """

        if self.initialized:
            self.client.shutdown()
            self.initialized = False

            self.background_loop.join()


state = State()


def setup_ssh_tunneling(host, connection_file):
    jupyter_client.tunnel_to_kernel(connection_file, host)
    # with open(connection_file) as f:
    #     parsed = json.load(f)
    #
    # ports = [value for key, value in parsed.items() if key.endswith('_port')]
    # for port in ports:
    #     os.system('ssh {host} -f -N -L {port}:localhost:{port}'
    #               .format(host=host, port=port))


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

    # execution_count = reply['execution_count']
    state.waiting[msg_id] = {
        'type': 'output',
        # 'execution_count': execution_count,
    }
    start_outputs()
    # reply = state.client.get_shell_msg(msg_id)
    # status = reply['status']
    # if status == 'ok':
    # elif status == 'error':
    #     print("(magma.vim) EXCEPTION: %s: %s" % (status['ename'], status['evalue']))
    # elif status == 'abort':
    #     print("(magma.vim) Execution aborted")


def start_outputs():
    global state

    state.output_count += 1
    job = ['python3',
           os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        'magma_output.py'),
           '127.0.0.1', 10000 + state.output_count]
    # TODO: add to a map of output jobs in order to communicate jupyter IO

    vim.eval('term_start(%r, {"term_name": "(magma) Out[%d]"})'
             % (job, state.output_count))


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
        done = False
        if 'content' in message:  # type = `display_data`
            content = message['content']
            if 'data' in content:
                data = {
                    'type': 'output',
                    'content': content['data'],
                }
                if 'execution_count' in content:
                    done = True
            else:
                return
        elif 'name' in message:  # type = `stream`
            name = message['name']
            if name == 'stdout':
                data = {
                    'type': 'stdout',
                    'content': message['text'],
                }
            elif name == 'stderr':
                data = {
                    'type': 'stderr',
                    'content': message['text'],
                }
            else:
                raise Exception("Unkown stream:name = '%s'" % name)
        else:
            return

        requests.post('http://127.0.0.1:%d' % (10000 + state.output_count),
                      json=data)
        if done:
            requests.post('http://127.0.0.1:%d' % (10000 + state.output_count),
                          json={'type': 'done'})
        # if 'execution_state' in content:
        #     state = content['execution_state']
    except queue.Empty:
        return


def update_loop():
    global state

    while True:
        if not state.initialized:
            break
        update()
        time.sleep(0.5)
