# vim: fdm=indent
from typing import Dict
import queue
import os
import requests
import vim
import jupyter_client


class State(object):
    initialized: bool = False
    client: jupyter_client.KernelClient = None
    waiting: Dict[str, dict] = {}
    output_count: int = 0

    def __del__(self):
        self.deinitialize()

    def initialize_new(self, kernel_name):
        """
        Initialize the client and a local kernel, if it isn't yet initialized.
        """

        if not self.initialized:
            # self.client = jupyter_client.run_kernel(kernel_name=kernel_name)
            # self.client = jupyter_client.KernelClient()
            _, self.client = jupyter_client.manager.start_new_kernel(
                kernel_name=kernel_name)
            self.initialized = True

    def initialize_remote(self, connection_file):
        """
        Initialize the client and connect to a kernel (possibly remote), if it isn't yet initialized.
        """

        if not self.initialized:
            cf = jupyter_client.find_connection_file(connection_file)
            self.client = jupyter_client.KernelClient(connection_file=cf)
            self.initialized = True

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


state = State()


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

        state.initialize_local(specs[choice-1].language)

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
        if 'content' not in message:
            return
        content = message['content']
        if 'data' in content:
            data = content['data']
            requests.post('http://127.0.0.1:%d' % (10000 + state.output_count),
                          json=data)
            # print("data:", data)
        # if 'execution_state' in content:
        #     state = content['execution_state']
    except queue.Empty:
        return
