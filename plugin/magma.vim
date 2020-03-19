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

function s:MagmaInit()
    python3 magma.init()
endfunction

function s:MagmaDeinit()
    python3 magma.deinit()
endfunction

function s:MagmaEvaluate(code)
    python3 magma.evaluate(vim.eval("a:code"))
endfunction

function s:MagmaShow()
    let l:code = s:GetParagraph()

    python3 magma.evaluate(vim.eval('l:code'))
endfunction

function MagmaUpdate(...)
    python3 magma.update()
endfunction


command! -nargs=0 MagmaInit call s:MagmaInit()
command! -nargs=0 MagmaDeinit call s:MagmaDeinit()
command! -nargs=0 MagmaShow call s:MagmaShow()
command! -nargs=0 MagmaEvaluate call s:MagmaEvaluate(s:GetParagraph())

nnoremap <Leader><Leader>p :MagmaPopup<CR>
nnoremap <Leader><Leader><Leader> :MagmaEvaluate<CR>

let g:magma_update_timer = timer_start(1000, 'MagmaUpdate', {'repeat': -1})

augroup magma
    autocmd!
    " autocmd CursorHold * call s:MagmaUpdate()
    " TODO: add more commands for automatically running :MagmaDeinit ↓↓↓↓↓
    autocmd! VimLeave * MagmaDeinit
augroup END
