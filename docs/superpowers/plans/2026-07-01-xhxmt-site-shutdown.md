# xhxmt.github.io Shutdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `xhxmt.github.io` public showcase content with a blank homepage and sync that state to GitHub.

**Architecture:** The site is a single static `index.html`, so shutdown is a single-file replacement followed by repository synchronization. The root repository also tracks the site repository pointer, so both repositories must be updated and verified.

**Tech Stack:** Static HTML, Git, `curl`

---

### Task 1: Replace homepage with a blank document

**Files:**
- Modify: `xhxmt.github.io/index.html`

- [ ] **Step 1: Replace page content with minimal blank HTML**

- [ ] **Step 2: Verify the file contains no previous showcase copy**

Run: `rg -n "Modeling Factory|CUMCM|板凳龙|paperAbstract|PROBLEMS_METADATA|github" xhxmt.github.io/index.html`
Expected: no matches

### Task 2: Publish the site repository update

**Files:**
- Modify: `xhxmt.github.io/index.html`

- [ ] **Step 1: Commit the site repository**

Run:
```bash
git -C /home/tfisher/paper_factory/xhxmt.github.io add index.html
git -C /home/tfisher/paper_factory/xhxmt.github.io commit -m "docs: blank xhxmt site homepage"
```

- [ ] **Step 2: Push the site repository**

Run: `git -C /home/tfisher/paper_factory/xhxmt.github.io push`
Expected: push succeeds

- [ ] **Step 3: Verify remote blank page**

Run: `curl -L --max-time 20 -s https://xhxmt.github.io/`
Expected: minimal blank HTML without prior showcase text

### Task 3: Sync root repository pointer

**Files:**
- Modify: `xhxmt.github.io` (gitlink)

- [ ] **Step 1: Stage and commit the updated gitlink**

Run:
```bash
git -C /home/tfisher/paper_factory add xhxmt.github.io
git -C /home/tfisher/paper_factory commit -m "docs: sync blanked xhxmt site"
```

- [ ] **Step 2: Push the root repository**

Run: `git -C /home/tfisher/paper_factory push`
Expected: push succeeds

- [ ] **Step 3: Verify clean working trees**

Run:
```bash
git -C /home/tfisher/paper_factory status --short --branch
git -C /home/tfisher/paper_factory/xhxmt.github.io status --short --branch
```
Expected: both repositories are clean
