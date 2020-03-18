" Author:       Daniel Csillag
" Description:  Work with Jupyter Kernels from Vim!

" Check if the user's Vim has all of the required features.
if !has('popup')
    echoerr "magma.vim requires `popup` support"
    exit
endif
if !has('python3')
    echoerr "magma.vim requires `python3` support"
    exit
endif

" Load the Python implementation
let s:plugin_root_dir = fnamemodify(resolve(expand('<sfile>:p')), ':h')

python3 << EOF
import sys
from os.path import normpath, join
import vim

plugin_root_dir = vim.eval('s:plugin_root_dir')
python_root_dir = normpath(join(plugin_root_dir, "..", "python"))
sys.path.insert(0, python_root_dir)

PYTHON3_BIN = 'python3'
MAGMA_OUTPUT_SCRIPT = vim.eval(join(plugin_root_dir, "..", "python", "magma_output.py"))

import magma
EOF

" VimL stuff

function l:GetParagraph()
    let l:cursor_pos = getpos(".")

    let l:output = "..."  " TODO

    call setpos(".", l:cursor_pos)

    return l:output
endfunction

" Python wrappers

function l:MagmaInit()
    python3 magma.init()
endfunction

function l:MagmaDeinit()
    python3 magma.deinit()
endfunction

function l:MagmaEvaluate(code)
    python3 magma.evaluate(a:code)
endfunction

function l:MagmaShow()
    let l:code = l:GetParagraph()

    python3 magma.evaluate(vim.eval('l:code'))
endfunction

function l:MagmaUpdate()
    python3 magma.update()
endfunction


command! -nargs=0 MagmaInit call l:MagmaInit()
command! -nargs=0 MagmaDeinit call l:MagmaDeinit()
command! -nargs=0 MagmaShow call l:MagmaShow()
command! -nargs=0 MagmaEvaluate call l:MagmaEvaluate()

nnoremap <Leader><Leader>p :MagmaPopup<CR>
nnoremap <Leader><Leader><Leader> :MagmaEvaluate<CR>

augroup magma
    autocmd!
    autocmd CursorHold * call MagmaUpdate()
    " TODO: add more commands for automatically running :MagmaDeinit ↓↓↓↓↓
    autocmd! VimLeave * MagmaDeinit
augroup END
