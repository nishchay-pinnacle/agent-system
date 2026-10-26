---
name: orientation
description: Establish an initial company/product understanding using only the two supplied pages.
---

1. Use read_page for both starting URLs. Deduplicate identical URLs: one read can
   fulfill both inputs. Do not read additional pages, including discovered links.
2. Use the complete saved evidence to establish provisional product boundaries and
   an initial understanding. Keep company-wide claims distinct from product claims.
3. Use update_profile to save a useful Markdown profile with important unknowns,
   conditions, uncertainty, and source references. Cite each starting source using
   its exact saved path (for example [Company overview](sources/s_....md)). Use a
   structure appropriate to this product, not a fixed questionnaire.
4. Use build_site_index with the company URL's root (scheme and hostname plus /).
   The index discovers broadly using sitemap and subdomain mapping. Its entries
   are discovery leads, not evidence that you have read. Do not scrape them.
5. Stop after the index is saved. The runtime prints completion and output paths.
   Failure or exhausted limits means incomplete orientation, never success.
