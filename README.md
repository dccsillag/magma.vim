This plugin is obsolete, and will not be maintained.

A new version of it, for NeoVim, is available at [dccsillag/magma-nvim](https://github.com/dccsillag/magma-nvim).

---

Magma
===

Gearing Vim for proper data science.

Magma is a Vim plugin that turns it into a Jupyter client. In the future, it will also support arbitrary REPLs (somewhat like `vim-slime`).

Quickstart
---

To start Magma, run `:MagmaInit`. It will ask you which Jupyter kernel you want to use, and then start it.

From then on, you can use `:MagmaEvaluate` to evaluate the current paragraph. You can also use the `<Leader><Leader><Leader>` default keymapping.

To shutdown the kernel, use `:MagmaDeinit`. This is done automatically when you leave Vim.
