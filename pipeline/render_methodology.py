#!/usr/bin/env python3
"""Render docs/methodology.md -> docs/methodology.html (static, for GitHub Pages)."""
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent.parent / "docs"
md = (DOCS / "methodology.md").read_text(encoding="utf-8")
body = markdown.markdown(md, extensions=["tables", "fenced_code", "toc", "sane_lists"])
html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ComputeNAV Methodology v0</title>
<link rel="stylesheet" href="assets/style.css">
<style>main{{max-width:960px}} main table{{display:block;overflow-x:auto;background:#fff;border:1px solid var(--line)}} blockquote{{background:var(--warnbg);border-left:4px solid #f0b54a;margin:12px 0;padding:8px 14px}}</style>
</head><body>
<header class="banner" role="note"><div class="banner-row">
<span class="pill">High-school student research project</span>
<span class="pill">Indicative data only</span>
<span class="pill">Not investment advice</span></div></header>
<main><p><a href="index.html">← Back to dashboard</a></p>
{body}
</main></body></html>
"""
(DOCS / "methodology.html").write_text(html, encoding="utf-8")
print("wrote", DOCS / "methodology.html")
