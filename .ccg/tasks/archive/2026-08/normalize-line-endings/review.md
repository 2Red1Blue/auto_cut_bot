# Review

- Added repository-wide LF normalization through `.gitattributes`, with explicit
  CRLF exceptions only for `.bat`, `.cmd`, and `.ps1` files.
- Verified the policy with `git check-attr` and a clean Windows worktree at
  commit `e956db55`.
- Configured both desktop worktrees with `core.autocrlf=false` and `core.eol=lf`.
- Preserved seven real local modifications and three untracked GPU deployment
  files in the old desktop worktree. A backup patch of real modifications was
  retained outside Git; no untracked file was removed.

The old worktree still reports Windows stat/line-ending cache noise, so the
clean `auto_cut_bot_v213` worktree is the supported execution checkout.
