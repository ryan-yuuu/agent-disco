# Install links `disco` into a directory already on PATH

A `curl | bash` installer is a child of the caller's shell and can never mutate
its `PATH`, which is why `disco` used to need a terminal restart. The installer
now symlinks the shim into the first directory that is already on `PATH` and
writable (`~/.local/bin`, then `/usr/local/bin`): `PATH` resolves at *lookup*
time, so a command appearing in a directory the shell already searches is found
immediately. The env-file and profile hooks remain as the fallback.

## Considered options

- **Do nothing; document `source ~/.agent-disco/env`.** What rustup, uv, deno,
  nvm and gcloud all do. Cheap, but accepts a step that is avoidable on most
  developer machines.
- **`sudo` into `/usr/local/bin` (Ollama's approach).** The only route that is
  instant on a *stock* Mac, where `/usr/local/bin` is on the default `PATH` and
  `~/.local/bin` is not. Rejected: `curl | bash` has no stdin to prompt on, it
  breaks rootless/CI installs, and it puts root-owned files outside
  `$CALFCORD_HOME`.
- **Ship via a package manager (Homebrew tap, npm wrapper).** Why `brew install`
  and `npm install -g` feel instant — they inherit a directory something else
  already put on `PATH`. Viable as additive channels later; neither removes the
  need to fix the `curl | bash` path.

## Consequences

- The symlink lives outside `$CALFCORD_HOME`, so `rm -rf ~/.agent-disco` no
  longer fully uninstalls.
- `READY` is asserted from resolution (`-x` plus `command -v`), not from `ln`
  succeeding — otherwise another `disco` earlier on `PATH` wins silently and
  `disco init` drives someone else's program.
- Nothing qualifies on a stock macOS; it degrades to the old behaviour. This is
  best-effort, never a requirement.
- zsh is wired via `.zshenv`, not `.zprofile`, which only login shells read —
  leaving `disco` unreachable in a non-login `zsh -i` such as VS Code's terminal,
  where restarting never helped. The trade: `.zshenv` runs before
  `/etc/zprofile`, whose `path_helper(8)` demotes our dir, so we win reachability
  and lose precedence. That only matters against a same-named command, which we
  detect and warn about.
- **The `-f` in the `zsh -f` ZDOTDIR probe is load-bearing.** zsh locates
  `.zshenv` from `/etc/zshenv` or the inherited env only, then reads it once. A
  plain `zsh -c` sources `~/.zshenv` and reports the ZDOTDIR *that file* sets —
  the common XDG stub idiom — sending the hook to a `$ZDOTDIR/.zshenv` zsh has
  already finished looking for. Dead file, permanently broken activation.
