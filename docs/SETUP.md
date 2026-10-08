# Run your own mybrew

You need a GitHub account and an Intel Mac on macOS Sequoia. AWS is optional.

## 1. Create your instance

On [eduardobobsin/homebrew-mybrew-template](https://github.com/eduardobobsin/homebrew-mybrew-template),
click **Use this template → Create a new repository**:

- **Name it `homebrew-mybrew`.** Homebrew maps the tap `<you>/mybrew` to that name.
- **Make it public.** GitHub's standard runners, including `macos-15-intel`, are free only
  for public repositories; on a private one every build spends your plan's macOS minutes.

## 2. Choose where bottles live

**GitHub Release (default, nothing to set up).** Bottles go to a release called `bottles` in
your instance repo.

**S3 (optional).** With an AWS profile that can manage IAM and S3:

```bash
git clone https://github.com/eduardobobsin/mybrew && cd mybrew
scripts/setup-aws.sh --repo <you>/homebrew-mybrew --bucket <globally-unique-name> --profile <admin> --check
scripts/setup-aws.sh --repo <you>/homebrew-mybrew --bucket <globally-unique-name> --profile <admin>
```

This creates the GitHub OIDC provider (if missing), a bucket readable only under `bottles/*`,
and a role only your repo's `main` branch can assume, then sets the `MYBREW_*` repository
variables. No AWS keys are stored anywhere. The workflow switches to S3 as soon as
`MYBREW_S3_BUCKET` is set.

## 3. Install the client

```bash
brew tap <you>/mybrew
brew trust --tap <you>/mybrew
brew install <you>/mybrew/mybrew
gh auth login        # mybrew starts builds through the GitHub CLI
```

## 4. Use it

```bash
mybrew install <formula>
```

If Homebrew has an Intel bottle, this is just `brew install`. If your instance has one, it
installs that. Otherwise it starts a build in your repo (Actions → Build bottle), waits for it,
and installs the result. Dependencies without Intel bottles are built first. You can also start
builds by hand from the Actions tab to warm the cache.

## Building on your own Intel Mac

GitHub plans to retire hosted Intel macOS runners in 2027. To build on a Mac you own instead,
register it as a [self-hosted runner](https://docs.github.com/actions/hosting-your-own-runners)
for your instance repo and set repository variables:

- `MYBREW_RUNNER` = `["self-hosted","macOS","X64"]`
- `MYBREW_VERIFY` = `false` (the verify job uninstalls formulae; it must never run on a machine
  you use)

The Mac must run Sequoia, since bottles are tagged `sequoia`. The workflow only runs on manual
dispatch, so pull requests from forks cannot reach your runner.

## Updating

The instance workflow pins the engine to a release tag (`@v0.8.0`). To upgrade, change the tag
in `.github/workflows/build.yml` and the `url`/`sha256` in `Formula/mybrew.rb`.

## Troubleshooting

- **`brew untap` says "Refusing to load formula … from untrusted tap".** Homebrew loads a
  tap's formulae before removing it. Trust it for the removal:
  `brew trust --tap <you>/mybrew && brew untap <you>/mybrew && brew untrust --tap <you>/mybrew`.
- **A formula is "installed from homebrew/core" and mybrew refuses to replace it.** mybrew never
  uninstalls things you installed elsewhere unless you pass `--replace`
  (`mybrew install --replace <formula>`), which lists what uses the old version and then swaps it
  for the mybrew one.
