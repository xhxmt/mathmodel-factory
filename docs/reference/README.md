# Excellent-Paper Reference Data

Markdown analysis in this directory is committed to git. Large downloaded
artifacts (PDFs, page images, OCR output) live under `external/` and are
**not** committed — recreate them locally with the fetch scripts.

## Fetch scripts

```bash
# 2024 CUMCM excellent-paper galleries (images + optional PDF assembly)
python3 scripts/dxs_2024_paper_fetch.py --out external/2024_excellent_papers

# Gallery API metadata (2025 list, category indexes, etc.)
python3 scripts/dxs_gallery_api_fetch.py
```

After fetch, analysis agents and humans can read:

- `external/2024_excellent_papers/<paper_id>/` — per-paper HTML, images, PDF
- `external/2025A_excellent_A2022729/`, `external/2023A_excellent_A1865112/` — single-paper showcases

## Committed analysis (this directory)

| File | Purpose |
|------|---------|
| `2024A_writing_comparison.md` | 2024A writing benchmark vs recent runs |
| `2023_2025A_writing_commonality.md` | Cross-year A-topic excellent-paper patterns |
| `excellent_paper_visualization_study.md` | Visualization role study from excellent papers |

These informed the 2026-06-24 workflow improvements documented in `CHANGELOG.md`.