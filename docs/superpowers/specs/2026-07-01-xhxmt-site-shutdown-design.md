# xhxmt.github.io Shutdown Design

**Goal:** Stop `xhxmt.github.io` from showing any public showcase content while keeping the site technically reachable.

**Current Context:** The published site is a single static page driven by [xhxmt.github.io/index.html](/home/tfisher/paper_factory/xhxmt.github.io/index.html:1). The user wants "不显示内容", and explicitly selected the minimal option: keep the site up, but make the homepage blank.

**Chosen Approach:** Replace the current homepage with a minimal blank HTML document. The page will keep only the basic HTML structure, set a white background, render no visible content, and add `noindex, nofollow, noarchive` to reduce search engine visibility.

## Options Considered

1. Blank homepage only.
Recommended. Minimal, reversible, low risk.

2. Blank homepage plus removing public static assets.
Stronger privacy, but unnecessary for the stated goal and more disruptive.

3. Return a 404 or maintenance page.
Not aligned with "不显示内容" as closely as a true blank page.

## Scope

- Modify only `xhxmt.github.io/index.html`.
- Do not remove PDFs, images, or repository history.
- Push the `xhxmt.github.io` repository update.
- Update the root repository submodule/gitlink pointer and push that too.

## Verification

- Local `index.html` contains no prior showcase text or scripts.
- `curl` of the local file content shows only the minimal blank document.
- `git push` succeeds for `xhxmt.github.io`.
- `curl https://xhxmt.github.io/` returns `200` and does not contain the previous showcase title/content.
