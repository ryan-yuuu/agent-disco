# Install links `disco` into a directory already on PATH

A `curl | bash` installer is a child of the caller's shell and can never mutate
its `PATH`, which is why `disco` used to require a terminal restart. The
installer now symlinks the `disco` shim into the first directory that is already
on the caller's `PATH` and writable (`~/.local/bin`, then `/usr/local/bin`),
because `PATH` is resolved at *lookup* time — a command appearing in a directory
the shell already searches is found immediately, with no restart and no `rehash`.
The env-file + profile hooks stay as the fallback, and the closing message is
conditional on which tier actually fired.

## Considered options

- **Do nothing; document `source ~/.agent-disco/env`.** This is what rustup, uv,
  deno, nvm, pnpm and gcloud all do, and our installer already printed it. Cheap
  and honest, but it accepts a step that is avoidable on most developer machines.
- **Install into `/usr/local/bin` with `sudo` (Ollama's approach).** The only
  route that is instant on a *stock* Mac, because `/usr/local/bin` is on the
  default `PATH` while `~/.local/bin` is not. Rejected: `curl | bash` has no
  stdin to prompt for a password on, it breaks rootless/CI/container installs,
  and it puts root-owned files outside `$CALFCORD_HOME`.
- **Ship through a package manager (Homebrew tap, npm wrapper).** This is why
  `brew install` and `npm install -g` feel instant — they inherit a directory
  something else already put on `PATH`; the restart is paid once per *package
  manager*, not per tool. Both remain viable as additive channels, but neither
  removes the need to fix the `curl | bash` path, and both are separate work.

## Consequences

- The symlink lives **outside `$CALFCORD_HOME`**, so `rm -rf ~/.agent-disco` no
  longer fully uninstalls. `docs/installation.md` covers removing it, guarded so
  an unrelated tool's `disco` is never destroyed.
- **`READY` is asserted from resolution, not from `ln` succeeding.** The link
  must be `-x` and `command -v disco` must resolve to it. Without this, an
  earlier `PATH` entry holding another `disco` would silently win and `disco
  init` would drive someone else's program.
- On a stock macOS nothing qualifies and the install degrades to the old
  behaviour. This is a best-effort optimisation, never a requirement.
- zsh is wired via `.zshenv` rather than `.zprofile`: `.zprofile` is read only by
  *login* shells, leaving `disco` unreachable in a non-login `zsh -i` such as VS
  Code's integrated terminal — where restarting never helped. The trade is real
  but small: `.zshenv` runs before `/etc/zprofile`, whose `path_helper(8)`
  reorders `PATH` and demotes our dir, so we win reachability and lose
  precedence. That only matters against a same-named command, which we detect
  and warn about because no message the installer prints can fix it.
- `zsh_dotdir` probes with **`zsh -f`**, and the flag is load-bearing rather than
  incidental. zsh locates `.zshenv` from `/etc/zshenv` or the inherited env only,
  then reads it once. A plain `zsh -c` sources `~/.zshenv` and reports the
  `ZDOTDIR` *that file* sets — the common XDG stub idiom — sending the hook to a
  `$ZDOTDIR/.zshenv` zsh has already finished looking for. Dead file, permanently
  broken activation. `-f` reproduces zsh's own lookup and avoids executing the
  user's rc code during install.
