" Author:       Daniel Csillag
" Description:  Work with Jupyter Kernels from Vim!

" Load the Python implementation
let s:plugin_root_dir = fnamemodify(resolve(expand('<sfile>:p')), ':h')

python3 << EOF
import sys
from os.path import normpath, join
import vim

plugin_root_dir = vim.eval('s:plugin_root_dir')
python_root_dir = normpath(join(plugin_root_dir, "..", "python"))
sys.path.insert(0, python_root_dir)

import magma
EOF

" Helpers

function s:GlobalOption(name, default_value)
    execute('let g:' . a:name . ' = ' . a:default_value)
endfunction

" VimL stuff

function s:GetParagraph()
    let l:cursor_pos = getpos(".")

    normal "myip

    call setpos(".", l:cursor_pos)

    return getreg('m')
endfunction

" Python wrappers

function s:MagmaInit(...)
    if a:0 == 0
python3 << EOF
magma.magma_instances.append(magma.Magma())
if not magma.magma_instances[-1].init_local():
    magma.magma_instances.pop()
EOF
    else
python3 << EOF
magma.magma_instances.append(magma.Magma())
if not magma.magma_instances[-1].init_existing(vim.eval('a:1')):
    magma.magma_instances.pop()
EOF
    endif
endfunction

function s:MagmaRemoteInit(host, connection_file)
    let l:connection_file = system('mktemp')
    " Remove the trailing newline:
    let l:connection_file = l:connection_file[:strlen(l:connection_file)-2]
    " Copy the connection file:
    execute "!scp \"scp://" . a:host . "/" . a:connection_file . "\" " . l:connection_file
python3 << EOF
magma.magma_instances.append(magma.Magma())
if not magma.magma_instances[-1].init_remote(vim.eval('a:host'), vim.eval('l:connection_file')):
    magma.magma_instances.pop()
EOF

    " Setup SSH tunneling
    " python3 magma.setup_ssh_tunneling(vim.eval('a:host'), vim.eval('l:connection_file'))
    " Finally, initialize Magma with the given (copied) connection file
    " call s:MagmaInit(l:connection_file)
endfunction

function s:MagmaDeinit()
python3 << EOF
magma_instance = magma.get_magma_instance()
if magma_instance is not None:
    magma_instance.deinit()
    del magma.magma_instances[magma.magma_instances.index(magma_instance)]
EOF
endfunction

function s:MagmaEvaluate(code)
python3 << EOF
magma_instance = magma.get_magma_instance()
if magma_instance is not None:
    magma_instance.evaluate(vim.eval("a:code"), vim.eval('line(".")'))
EOF
endfunction

function s:MagmaShow(bang)
    if a:bang == "!"
python3 << EOF
magma_instance = magma.get_magma_instance()
if magma_instance is not None:
    magma_instance.show_evaluated_output(True)
EOF
    else
python3 << EOF
magma_instance = magma.get_magma_instance()
if magma_instance is not None:
    magma_instance.show_evaluated_output(False)
EOF
    endif
endfunction

function s:MagmaLoad(path)
python3 << EOF
magma_instance = magma.get_magma_instance()
if magma_instance is not None:
    magma_instance.read_session_file(vim.eval('a:path'))
EOF
endfunction

function s:MagmaSave(path)
python3 << EOF
magma_instance = magma.get_magma_instance()
if magma_instance is not None:
    magma_instance.write_session_file(vim.eval('a:path'))
EOF
endfunction

function MagmaState()
python3 << EOF
magma_instance = magma.get_magma_instance()
if magma_instance is not None:
    magma_instance.get_kernel_state('g:magma_kernel_state')
EOF
    return g:magma_kernel_state
endfunction

function MagmaUpdate(...)
python3 << EOF
magma_instance = magma.get_magma_instance()
if magma_instance is not None:
    magma_instance.vim_update()
EOF
endfunction


sign define magma_hold text=* texthl=MagmaHoldSign

command! -nargs=? MagmaInit call s:MagmaInit(<f-args>)
command! -nargs=+ MagmaRemoteInit call s:MagmaRemoteInit(<f-args>)
command! -nargs=0 MagmaDeinit call s:MagmaDeinit()
command! -nargs=0 -bang MagmaShow call s:MagmaShow("<bang>")
command! -nargs=0 MagmaEvaluate call s:MagmaEvaluate(s:GetParagraph())
command! -nargs=1 MagmaLoad call s:MagmaLoad(<f-args>)
command! -nargs=1 MagmaSave call s:MagmaSave(<f-args>)

" Create the options:

call s:GlobalOption("magma_preview_window_enabled", 1)
call s:GlobalOption("magma_alert_sound", "\"bell\"")

let g:magma_timer = timer_start(100, 'MagmaUpdate', { 'repeat': -1 })
call timer_pause(g:magma_timer, 1)

augroup magma
    autocmd!
    " TODO: add more commands for automatically running :MagmaDeinit ↓↓↓↓↓
    autocmd VimLeave * MagmaDeinit
augroup END
