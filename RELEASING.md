# Releasing

Releases are automated. Pushing a tag like `v0.1.0` runs [.github/workflows/release.yml](.github/workflows/release.yml), which:

1. runs every test, the same as on each push (unit, end-to-end in three browsers, the `.deb`, `apt install` from a signed repository, and the Homebrew formula);
2. checks that the tag matches the version in `nts/__init__.py` and that [CHANGELOG.md](CHANGELOG.md) has a dated section for it;
3. publishes a GitHub release with that section as its notes, the source archive, the `.deb` (also as `note-to-self_all.deb` for "latest" links) and a `SHA256SUMS` file;
4. publishes the signed APT repository on GitHub Pages (`https://human-genomics.github.io/note-to-self`), so `apt install` and `apt upgrade` get the new version;
5. updates the formula in the Homebrew tap, [human-genomics/homebrew-tap](https://github.com/human-genomics/homebrew-tap).

## Each release

1. Set the version in `nts/__init__.py`, and give it a section in CHANGELOG.md headed with the date, such as `## 0.1.0 (2026-10-08)`.
2. Commit, tag and push:

   ```sh
   git commit -am "Release 0.1.0"
   git tag v0.1.0
   git push origin main v0.1.0
   ```

3. Watch the **release** workflow in the Actions tab. If the tests or checks fail, nothing is published: fix the problem, then move the tag (`git tag -f v0.1.0 && git push -f origin v0.1.0`). If only the APT or tap step fails, re-run that job.
4. Check it as a user would: `brew update && brew upgrade note-to-self` (or `brew install human-genomics/tap/note-to-self`) on a Mac, and `sudo apt update && sudo apt upgrade` on Ubuntu.

## One-time setup

Run these once, with `gh` signed in as the repository's owner. Keep the two private keys only in the GitHub secrets (and, for the APT key, a password manager); don't commit them.

```sh
R=human-genomics/note-to-self

# The Homebrew tap: a public repository the release workflow pushes the formula to,
# using a deploy key that can write to that repository only.
gh repo create human-genomics/homebrew-tap --public --description "Homebrew formulae for Note to Self"
ssh-keygen -t ed25519 -N "" -C "note-to-self releases" -f tap_key
gh repo deploy-key add tap_key.pub --repo human-genomics/homebrew-tap --allow-write --title "note-to-self releases"
gh secret set HOMEBREW_TAP_DEPLOY_KEY --repo $R < tap_key
rm tap_key tap_key.pub

# The APT repository's signing key. Users trust this key, so keep it: losing it means
# everyone has to add a new one.
gpg --batch --passphrase "" --quick-gen-key "Note to Self APT <157525599+human-genomics@users.noreply.github.com>" ed25519 sign never
gpg --armor --export-secret-keys "Note to Self APT" | gh secret set APT_SIGNING_KEY --repo $R

# GitHub Pages, deployed by the workflow, including from release tags.
gh api -X POST repos/$R/pages -f build_type=workflow
gh api -X PUT repos/$R/environments/github-pages \
  -F "deployment_branch_policy[protected_branches]=false" -F "deployment_branch_policy[custom_branch_policies]=true"
gh api -X POST repos/$R/environments/github-pages/deployment-branch-policies -f name=main -f type=branch
gh api -X POST repos/$R/environments/github-pages/deployment-branch-policies -f name="v*" -f type=tag

# Private vulnerability reporting, which SECURITY.md points people to.
gh api -X PUT repos/$R/private-vulnerability-reporting
```

Without `HOMEBREW_TAP_DEPLOY_KEY` or `APT_SIGNING_KEY`, a release still goes out and the workflow says which step it skipped.

Once the project is popular enough for [homebrew-core](https://docs.brew.sh/Acceptable-Formulae), `brew install note-to-self` can work without the tap.

## By hand

The workflow's steps are plain commands, if you ever need them locally:

```sh
python3 tools/release_notes.py 0.1.0                  # the release notes (and the checks)
git archive --format=tar.gz --prefix=note-to-self-0.1.0/ -o dist/note-to-self-0.1.0.tar.gz v0.1.0
python3 tools/build_deb.py --out dist                 # on Debian or Ubuntu
python3 tools/formula.py 0.1.0 --tarball dist/note-to-self-0.1.0.tar.gz --out ../homebrew-tap/Formula/note-to-self.rb
python3 tools/apt_repo.py --out site --key "Note to Self APT" dist/note-to-self_0.1.0_all.deb
```
