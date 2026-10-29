# Pages fonts

The Pages site uses the unmodified Latin1 and Pi WOFF2 subsets from
[IBM Plex commit `763c36ef9117782905ae010056dfbe8fd2653a25`](https://github.com/IBM/plex/tree/763c36ef9117782905ae010056dfbe8fd2653a25).
They come from `packages/plex-sans/fonts/split/woff2/` and
`packages/plex-mono/fonts/split/woff2/` at the same revision as the complete fonts.
Copyright 2017 IBM Corp.; Reserved Font Name "Plex". See [SIL OFL 1.1](OFL.txt).

`site.css` uses Latin1 subsets for English text and loads the medium Pi subsets
only for the right and northeast arrows. Font preloads in `index.html` match the
Latin1 files used above the fold. When adding characters outside these ranges,
include the appropriate upstream subset and its Unicode range.

Keep the complete fonts: the reference client's font assets share their contents.
