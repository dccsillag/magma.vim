# vim: fdm=indent
from typing import Dict
import vim
import jupyter_client


class State(object):
    initialized: bool = False
    client: jupyter_client.KernelClient = None
    waiting: Dict[str, dict] = {}
    output_count: int = 0

    def __del__(self):
        self.deinitialize()

    def initialize(self, kernel_name):
        """
        Initialize the client, if it isn't yet initialized.
        """

        if not self.initialized:
            self.client = jupyter_client.run_kernel(kernel_name=kernel_name)
            # self.client = jupyter_client.KernelClient()
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


def init():
    global state

    if not state.initialized:
        # Select from available kernels
        ## List them
        kernelspecmanager = jupyter_client.kernelspec.KernelSpecManager()
        kernels = kernelspecmanager.get_all_specs()
        specs = [jupyter_client.kernelspec.get_kernel_spec(x) for x in kernels]
        ## Ask
        choice = vim.eval('inputlist(%r)' % ("Choose the kernel to start:" + ["%d. %s" % x for x in enumerate(y.display_name for y in specs)]))
        if choice == 0:
            return False

        state.initialize(specs[choice-1].language)

        vim.command('echomsg "Successfully initialized kernel %s!"')

        return True


def deinit():
    global state

    state.deinitialize()


def evaluate(code):
    global state

    if not init():
        return

    msg_id = state.client.execute(code)
    state.waiting[msg_id] = {
        'type': 'output'
    }


def start_outputs():
    global state

    state.output_count += 1
    job = [PYTHON3_BIN, MAGMA_OUTPUT_SCRIPT, 'localhost', 10000 + state.output_count]  # noqa: F821
    # TODO: add to a map of output jobs in order to communicate jupyter IO

    vim.eval('term_start(%r, {"term_name": "(magma) Out[%d]"})' % (job, state.output_count))


def show_output():
    global state

    pass  # # if not evaluated:
    pass  # #    evaluate
    pass  # # show popup with evaluated code


def update():
    global state

    # Process `shell` channel
    msg = state.client.get_shell_msg()
    while msg is not None:
        # TODO
        msg = state.client.get_shell_msg()
    # Process `iopub` channel
    msg = state.client.get_iopub_msg()
    while msg is not None:
        # TODO
        msg = state.client.get_iopub_msg()
    # Process `stdin` channel
    msg = state.client.get_stdin_msg()
    while msg is not None:
        # TODO
        msg = state.client.get_stdin_msg()
