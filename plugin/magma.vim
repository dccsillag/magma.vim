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
        python3 magma.init_local()
    else
        python3 magma.init_existing(vim.eval('a:1'))
    endif
endfunction

function s:MagmaRemoteInit(host, connection_file)
    let l:connection_file = system('mktemp')
    " Remove the trailing newline:
    let l:connection_file = l:connection_file[:strlen(l:connection_file)-2]
    " Copy the connection file:
    execute "!scp \"scp://" . a:host . "/" . a:connection_file . "\" " . l:connection_file
    python3 magma.init_remote(vim.eval('a:host'), vim.eval('l:connection_file'))

    " Setup SSH tunneling
    " python3 magma.setup_ssh_tunneling(vim.eval('a:host'), vim.eval('l:connection_file'))
    " Finally, initialize Magma with the given (copied) connection file
    " call s:MagmaInit(l:connection_file)
endfunction

function s:MagmaDeinit()
    python3 magma.deinit()
endfunction

function s:MagmaEvaluate(code)
    python3 magma.evaluate(vim.eval("a:code"), vim.eval('line(".")'))
endfunction

function s:MagmaShow()
    python3 magma.show_evaluated_output()
endfunction

function s:MagmaLoad(path)
    python3 magma.read_session_file(vim.eval('a:path'))
endfunction

function s:MagmaSave(path)
    python3 magma.write_session_file(vim.eval('a:path'))
endfunction

function MagmaState()
    python3 magma.get_kernel_state('g:magma_kernel_state')
    return g:magma_kernel_state
endfunction

function MagmaUpdate(...)
    python3 magma.vim_update()
endfunction


sign define magma_hold text=* texthl=MagmaHoldSign

command! -nargs=? MagmaInit call s:MagmaInit(<f-args>)
command! -nargs=+ MagmaRemoteInit call s:MagmaRemoteInit(<f-args>)
command! -nargs=0 MagmaDeinit call s:MagmaDeinit()
command! -nargs=0 MagmaShow call s:MagmaShow()
command! -nargs=0 MagmaEvaluate call s:MagmaEvaluate(s:GetParagraph())
command! -nargs=1 MagmaLoad call s:MagmaLoad(<f-args>)
command! -nargs=1 MagmaSave call s:MagmaSave(<f-args>)

let g:magma_timer = timer_start(100, 'MagmaUpdate', { 'repeat': -1 })

nnoremap <Leader><Leader><Leader> :MagmaEvaluate<CR>
nnoremap <Leader><Leader>o :MagmaShow<CR>

augroup magma
    autocmd!
    " TODO: add more commands for automatically running :MagmaDeinit ↓↓↓↓↓
    autocmd! VimLeave * MagmaDeinit
augroup END
