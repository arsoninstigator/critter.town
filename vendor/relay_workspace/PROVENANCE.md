# provenance

- upstream: https://github.com/block/buzz
- commit: `55823dd54f104abca39bdfece49d1cb5e30ca628` (`feat(cli): guard canvas set with --expected (#9945)`)
- licence: Apache-2.0 (see `LICENSE` in this directory). every crate declares `license.workspace = true` or
  `Apache-2.0`; there is no copyleft dependency and upstream ships no `NOTICE` file.
- local changes: none. every file here is byte for byte the upstream file at that commit.
  `UPSTREAM_SHA256SUMS` lists the sha256 of each one (symlinks as `link  path -> target`), so the claim is
  checked rather than trusted: `make vendor-verify` recomputes them.

## what this is

the whole server side of upstream, kept as one workspace: all 35 rust crates and the example bot, the
migrations and schema, deploy (compose and helm), the web and admin-web clients the relay serves, the
preview assets the relay embeds with `include_bytes!`, docs text, scripts, and the root manifests. the
root `Cargo.toml` is untouched and every workspace member it names is present, so `cargo build` and
`cargo test` resolve as they do upstream.

the platform does not compile this. it runs the published image named in
`deploy/compose/compose.yml`, which is built from this same source, and talks to the relay over its
protocol. the source is here so the thing we run is something we can read, diff and rebuild, not a
container we trust by its tag.

## what was left out, and why it builds without it

5476 of upstream's 7271 tracked files, all in one of these groups:

- `desktop/` (3473 files): the tauri desktop app. a separate product with its own toolchain, outside the
  cargo workspace (`exclude = ["desktop/src-tauri"]` in the root manifest).
- `mobile/` (1878 files): the flutter and swift apps. not in the cargo workspace.
- `benchmarks/` (116 files): load and latency harnesses. not in the cargo workspace.
- screenshots and media: `docs/assets/`, two preview frames and one crate readme image
  (`crates/buzz-agent/sprout-agent.png`). each omitted image was checked: nothing embeds or references it.

eight symlinks dangle because their target is in `desktop/src-tauri`: the `desktop-screenshot` agent skill
link under each of `.agents`, `.claude`, `.codex` and `.goose`, and the matching second link in each. they are
kept as upstream has them (a link is recorded in the manifest with its target) and nothing builds through them.

every image a build or a test embeds was put back (the invite preview the relay compiles in, the media
test fixtures, the web app icon), and each `include_bytes!`/`include_str!` path in the crates resolves.

## things that look like secrets and are not

upstream carries throwaway test keys, and they are kept unchanged because removing them would mean
editing upstream files:

- `crates/buzz-push-gateway/tests/fixtures/*.pem`: apns and app attestation test identities.
- `crates/buzz-push-gateway/src/android_attestation/google_roots.pem`: public root certificates.
- p-256 pkcs8 keys inside rust test constants named `TEST_EC_PKCS8_PEM`, `NIP_FI_EC_PEM` and similar, in
  `buzz-auth` and `buzz-relay`. upstream marks one with `sadscan:disable ... -- test`.
- `.env.example` and `deploy/compose/.env.example`: placeholders, no values.

none grants access to anything. a scanner that flags them is right to look and wrong to block.

## how it is used

`ingredients/relay/` runs it and speaks to it: it maps a crew, a department, a bloc and a wing onto
relay channels and our roles onto its roles. nothing outside `ingredients/relay/` imports from here.
