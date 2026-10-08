# mybrew

An on-demand Intel bottle cache for Homebrew. Homebrew no longer ships bottles for most
formulae on Intel Macs; mybrew builds them on GitHub's free `macos-15-intel` runners and
serves them from your own tap. Compilation is the cache-miss handler.

This repository is the **engine** (behavior). Each user runs their own **instance**, a tap
named `<user>/homebrew-mybrew` that holds state: formulae, bottle registry, workflow config.
See [eduardobobsin/homebrew-mybrew](https://github.com/eduardobobsin/homebrew-mybrew).

## Run your own

Create an instance from [homebrew-mybrew-template](https://github.com/eduardobobsin/homebrew-mybrew-template)
and follow [docs/SETUP.md](docs/SETUP.md). Only a GitHub account is required; S3 storage
and a self-hosted Intel runner are optional.

## Client

```bash
brew tap <you>/mybrew && brew trust --tap <you>/mybrew
mybrew install <formula>   # official bottle → brew; mybrew bottle → tap; neither → build on GitHub, then install
mybrew plan <formula>      # where each piece would come from, without changing anything
```

On a cache miss, each formula that needs a bottle is built as its own GitHub run as soon as its
dependencies are published, up to `MYBREW_PARALLEL` (default 4) at a time, with live progress:

```
==> Building 4 bottle(s) on github.com/you/homebrew-mybrew
  ✔ xz      built in 3m12s
  ⠹ cmake   building [5/10] 14m02s  (build-time)
  ⏸ lz4     waiting for cmake
  ⏸ libzip  waiting for lz4
  ✔ zstd    official bottle
```

In a terminal the block redraws in place with a spinner and the running job's step count;
in logs and pipes (or with `NO_COLOR`/`TERM=dumb`) it prints one line per change instead.
A failed formula skips only what depends on it; finished bottles stay cached. Ctrl-C leaves running
builds alone, and the next `mybrew install` attaches to them.

Anything else (`mybrew list`, `mybrew upgrade`, ...) is passed to `brew`. Building needs `gh`, logged in.

## Actions

| Action | Does |
|---|---|
| `actions/plan` | Walks the runtime dependency graph; lists the target plus every dependency with no Intel bottle and no current mybrew bottle, dependencies first |
| `actions/import-formula` | Copies a homebrew/core formula at the API's commit, verifies its SHA-256, drops the upstream bottle block |
| `actions/build-bottle` | Registers the checkout as a tap; for each planned formula in order: `brew install --build-bottle`, `brew test`, `brew bottle --json`, `brew bottle --merge --write` |
| `actions/publish-github-release` | Uploads bottles to a release, updates `registry/bottles.json`, commits the formula |
| `actions/verify-install` | On a fresh runner, installs the given formulae from the published tap and fails unless all were poured from bottles |
| `actions/publish-s3` | Treats the build artifact as untrusted: recomputes the plan, validates each bottle JSON and tarball checksum, re-imports formulae from homebrew-core and writes their bottle blocks itself; then uploads to S3 via OIDC and commits |

## Roadmap

| Milestone | Capability | Status |
|---|---|---|
| M0 Bottle | GitHub builds one Intel bottle | done (dos2unix 7.5.7) |
| M1 Install | Custom tap installs that bottle, no compilation | done |
| M2 Publish | S3 storage via OIDC, fully automated single formula | done |
| M3 Dependencies | Recursive private fallback for dependencies without Intel bottles | done (calc→readline, jdupes) |
| M4 Demand | `mybrew install foo` triggers a build on cache miss | done (v0.4.0) |
| M5 Platform | Template instance repo so others can deploy their own | done (v0.5.0) |

## Development

```bash
python3 -m unittest discover -s tests
```
