"""Synthetic structural preservation, capture and compatibility regressions."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from research_script import SID, URL, ScriptedProviders
from test_orientation import new_state

from agent_system.company_intelligence import agent, html_cleaning, page_content
from agent_system.company_intelligence.agent import FetchResult, LiveProviders
from agent_system.company_intelligence.artifacts import read_json
from agent_system.company_intelligence.html_cleaning import ConversionError, clean_html
from agent_system.company_intelligence.page_content import (
    clean_markdown,
    clean_markdown_v1,
    cleaned_path,
    digest,
    load_cleaned,
)
from agent_system.company_intelligence.schemas import Source
from agent_system.company_intelligence.site_index import search_inventory
from agent_system.company_intelligence.tools import excerpt
from agent_system.company_intelligence.workflow import run_research

MARKETING = """<html><head><script type="application/ld+json">{"@type":"Organization","name":"Widget"}</script><script>hydration secret</script></head><body>
<header><nav aria-label="Global"><a href="/docs?edition=pro#limits">Documentation</a><a href="/suite"><p>Manage keys across clouds.</p></a></nav><a href="/login">Log in</a></header>
<nav aria-label="Breadcrumb"><a href="/">Home</a> / Widget</nav>
<h1>Cookie management platform</h1><p>Configure cookie preferences in your product.</p>
<div class="cookie-banner" role="dialog"><p>Cookies on this website</p><button>Accept all</button><button>Reject</button></div>
<form><p>Subscribe to our newsletter</p><input type="email"><button>Subscribe</button><p>Trial requires a business email; $20 after 14 days.</p></form>
<div class="ask-ai">Ask AI<a href="https://chatgpt.com/?q=PROMPT">ChatGPT</a><a href="https://claude.ai/new?q=PROMPT">Claude</a><a href="https://gemini.google.com/app?prompt=PROMPT">Gemini</a><a href="https://www.perplexity.ai/search?q=PROMPT">Perplexity</a></div>
<article><p>Our integration with <a href="https://chatgpt.com/?q=legitimate">ChatGPT</a> is optional.</p><p>The ChatGPT documentation is a useful reference.</p></article>
<button class="floating" aria-label="Open support chat"></button><div class="floating chat-widget"></div>
<img width="1" height="1" src="/track"><button aria-label="Close advertisement"></button>
<footer><p>© 2026 Example. All rights reserved.</p><p>Exports subject to local law. ISO 27001 certified. Contact team@example.com.</p></footer>
</body></html>"""

DOCS = """<article><header><h1>Deployment v3</h1></header><nav aria-label="Table of contents"><a href="#install">Install</a></nav>
<h2 id="install">Install</h2><pre><code>  &lt;script&gt;cookie banner&lt;/script&gt;\n\t&lt;!-- literal --&gt;\n\n\n\n x&nbsp;y</code></pre>
<p>Inline <code>x  =  2</code> uses two spaces. H<sub>2</sub>O; x<sup>2</sup>.</p>
<ol start="4"><li>Prepare<ul><li>Nested</li></ul></li><li>Deploy</li></ol><blockquote>Warning: only v3.</blockquote>
<p>first<br>second; A&nbsp; B&#8239;C \u200d\u200c\u2060\u202e\ufe0f\u00ad</p>
<details hidden><summary>Can I export?</summary><p>Only in Pro.</p></details><button id="linux">Linux</button><section role="tabpanel" hidden aria-labelledby="linux">Requires kernel 6.</section>
<section role="tabpanel" aria-label="Windows" style="display:none">Requires Windows 11.</section>
<math><mi>x</mi><mo>=</mo><mn>2</mn></math><p id="note">Footnote: quota per region.</p><a href="#note">[1]</a>
<svg aria-label="Deployment diagram"><title>Network</title><desc>Private nodes</desc><text>Gateway</text><path d="M0 0 HUGE_GEOMETRY"/></svg>
<figure><img src="data:image/png;base64,INLINE_PAYLOAD" alt="Network overview"><figcaption>Source: deployment guide</figcaption></figure><img src="/unknown.png" alt="">
<video src="/demo.mp4">Transcript: configure a private node.</video><noscript>Offline setup is available.</noscript>
<button aria-controls="lazy-panel"></button></article>"""

PRICING = """<h1>Plans</h1><div class="desktop-only" data-plan="pro"><h2>Pro</h2><p>$20 monthly</p><a href="/trial">14-day trial</a></div>
<div class="mobile-only" data-plan="pro"><h2>Pro</h2><p>$20 monthly</p><a href="/trial">14-day trial</a></div>
<div class="mobile-only" data-plan="enterprise"><h2>Enterprise</h2><p>$90 monthly; approval required.</p></div>
<table><caption>Monthly limits</caption><tr><th>Plan</th><th>Requests</th></tr><tr><td>Pro</td><td>10,000</td></tr></table>
<table><caption>Regional pricing</caption><tr><th rowspan="2" id="plan">Plan</th><th colspan="2">Region</th></tr><tr><th id="us">US</th><th id="eu">EU</th></tr><tr><th scope="row">Pro</th><td headers="plan us">$20</td><td headers="plan eu"><span aria-label="Not available">—</span></td></tr><tfoot><tr><td colspan="3">Tax excluded; per month.</td></tr></tfoot></table>"""

STORY = """<article><h1>A customer deployment</h1><p>Customer Alpha migrated two clusters in May 2026.</p>
<div class="desktop-only"><blockquote>Reduced setup time to 3 hours.</blockquote><p>Alpha, CTO; v3 deployment</p></div><div class="mobile-only"><blockquote>Reduced setup time to 3 hours.</blockquote><p>Alpha, CTO; v3 deployment</p></div>
<div class="carousel-clone"><blockquote>Reduced setup time to 3 hours.</blockquote><p>Beta, engineer; v2 deployment</p></div><p>Great product!</p><p>Requires private networking; measured with 12 nodes.</p></article>"""


def test_structural_marketing_rules_and_negative_matches() -> None:
    raw = MARKETING.replace("PROMPT", "encoded_prompt_" * 4000)
    result = clean_html(raw, URL, SID)
    text = clean_markdown(result.content)
    for keep in (
        "Cookie management platform",
        "Configure cookie preferences",
        "Manage keys",
        "Trial requires",
        "$20",
        "legitimate",
        "ISO 27001",
        "team@example.com",
    ):
        assert keep in text
    for removed in (
        "Cookies on this website",
        "Accept all",
        "Subscribe",
        "encoded_prompt_",
        "Open support chat",
        "All rights reserved",
        "hydration secret",
    ):
        assert removed not in text
    assert result.json_ld == [{"@type": "Organization", "name": "Widget"}]
    assert result.breadcrumbs == ["Breadcrumb Home / Widget"]
    assert any(
        link["url"] == "https://example.com/docs?edition=pro#limits"
        for link in result.discovery
    )
    assert "encoded_prompt_" not in json.dumps(result.__dict__)
    assert {r["rule"] for r in result.removals} >= {
        "consent_controls",
        "subscription_controls",
        "share_intent",
        "global_navigation_controls",
        "tracking_pixel",
        "floating_chat_launcher",
        "advertisement_control",
    }


def test_docs_pricing_and_story_preserve_evidence() -> None:
    docs = clean_html(DOCS, URL, SID)
    text = clean_markdown(docs.content)
    for keep in (
        "  <script>cookie banner</script>\n\t<!-- literal -->\n\n\n\n x\u00a0y",
        "`x  =  2`",
        "<sub>2</sub>",
        "<sup>2</sup>",
        "4. Prepare",
        "Nested",
        "5. Deploy",
        "first  \nsecond",
        "A B C",
        "\u200d\u200c\u2060\u202e\ufe0f\u00ad",
        "Only in Pro.",
        "Linux",
        "kernel 6",
        "Windows",
        "Windows 11",
        "<math>",
        "quota per region",
        "Deployment diagram",
        "Private nodes",
        "Gateway",
        "Network overview",
        "Source: deployment guide",
        "Transcript: configure",
        "Offline setup",
    ):
        assert keep in text
    assert "INLINE_PAYLOAD" not in text and "HUGE_GEOMETRY" not in text
    assert len(docs.assets) == 2 and docs.warnings
    assert docs == clean_html(DOCS, URL, SID)
    assert clean_markdown(text) == text
    pricing = clean_markdown(clean_html(PRICING, URL, SID).content)
    assert pricing.count("$20 monthly") == 1
    for keep in (
        "Enterprise",
        "$90 monthly",
        "approval required",
        "| Pro | 10,000 |",
        'rowspan="2"',
        'headers="plan us"',
        "Not available",
        "Tax excluded; per month.",
    ):
        assert keep in pricing
    story = clean_html(STORY, URL, SID).content
    assert story.count("Alpha, CTO") == 1 and "Beta, engineer" in story
    assert "Great product!" in story and "12 nodes" in story


def test_markdown_unicode_structure_and_idempotence() -> None:
    raw = "\ufeffProse\u00a0  text\x00value\r\nfirst  \r\nsecond\n\n\n\n\n    <script>literal()</script>\n    a\u00a0  b\n\n`x  \u00a0y` and  prose\n\n~~~html\n<!--keep-->\n\n\n\n\n~~~\n\n[^n]: footnote\n    continued\n"
    cleaned = clean_markdown(raw)
    for keep in (
        "Prose text\ufffdvalue",
        "first  \nsecond",
        "    <script>literal()</script>",
        "    a\u00a0  b",
        "`x  \u00a0y`",
        "<!--keep-->\n\n\n\n\n",
        "[^n]: footnote\n    continued",
    ):
        assert keep in cleaned
    assert cleaned == clean_markdown(cleaned)


def saved_source(
    directory: Path, markdown: str, html: str | None = None, raw_html: str | None = None
) -> Source:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sources").mkdir(exist_ok=True)
    source = Source(
        source_id=SID,
        requested_url=URL,
        filename=f"sources/{SID}.md",
        cleaner_version="4",
        outcome="success",
    )
    (directory / source.filename).write_bytes(markdown.encode())
    for field, text, suffix in (
        ("html_filename", html, ".html"),
        ("raw_html_filename", raw_html, ".raw.html"),
    ):
        if text is not None:
            filename = f"sources/{SID}{suffix}"
            (directory / filename).write_bytes(text.encode())
            setattr(source, field, filename)
    return source


def test_selection_reports_conversion_failure_and_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = saved_source(
        tmp_path / "new",
        "# Fallback\nUseful fact.",
        '<div id="app"><script>hydrate()</script></div>',
        "<h1>Raw content</h1><p>One\x00two.</p>",
    )
    text = load_cleaned(tmp_path / "new", source)
    assert "Raw content" in text and "One\ufffdtwo" in text
    report = read_json(tmp_path / "new" / source.cleaning_report_filename)
    assert (
        report["selected_representation"] == "raw_html"
        and len(report["raw_hashes"]) == 3
    )
    assert report["nul_replacements"] == 1 and report["cleaned_hash"] == digest(text)
    duplicate = saved_source(
        tmp_path / "same",
        "# Fallback\nUseful fact.",
        '<div id="app"><script>hydrate()</script></div>',
        "<h1>Raw content</h1><p>One\x00two.</p>",
    )
    assert load_cleaned(tmp_path / "same", duplicate) == text
    assert (tmp_path / "same" / duplicate.cleaning_report_filename).read_bytes() == (
        tmp_path / "new" / source.cleaning_report_filename
    ).read_bytes()

    def fail(*args: object) -> object:
        raise ConversionError("synthetic converter failure")

    monkeypatch.setattr(page_content, "clean_html", fail)
    fallback = saved_source(tmp_path / "fallback", "Fallback evidence", "<p>HTML</p>")
    assert load_cleaned(tmp_path / "fallback", fallback) == "Fallback evidence"
    assert "synthetic converter failure" in json.dumps(fallback.cleaning_summary)
    unusable = saved_source(tmp_path / "empty", "Loading...")
    with pytest.raises(ValueError, match="empty/loading/error"):
        load_cleaned(tmp_path / "empty", unusable)
    legacy_dir = tmp_path / "legacy"
    raw = "Old\x00text\n<!-- comment -->\n    <script>old behavior</script>"
    legacy = saved_source(legacy_dir, raw)
    legacy.cleaner_version = "1"
    legacy.cleaned_filename = f"sources/{SID}.cleaned.md"
    expected = clean_markdown_v1(raw)
    assert load_cleaned(legacy_dir, legacy) == expected
    historical = "Bytes actually seen by the old model\r\n"
    (legacy_dir / legacy.cleaned_filename).write_bytes(historical.encode())
    reopened = Source.model_validate_json(legacy.model_dump_json())
    assert load_cleaned(legacy_dir, reopened) == historical
    assert excerpt(legacy_dir, reopened)["cleaner_version"] == "1"
    assert (legacy_dir / legacy.filename).read_bytes() == raw.encode()
    reopened.cleaned_filename = "sources/../../secret"
    with pytest.raises(ValueError):
        cleaned_path(legacy_dir, reopened)
    reopened.cleaned_filename = f"sources/{SID}.cleaned.v4.md"
    (legacy_dir / reopened.cleaned_filename).symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        cleaned_path(legacy_dir, reopened)


def test_all_formats_one_sdk_call_shared_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    class FakeFirecrawl:
        def __init__(self, **kwargs: object) -> None:
            pass

        def scrape(self, url: str, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                markdown="Unmodified fallback\r\n",
                html=MARKETING,
                raw_html="<p>Raw capture</p>",
                metadata=None,
            )

    monkeypatch.setattr(agent, "FirecrawlClient", FakeFirecrawl)
    monkeypatch.setattr(agent, "public_url", lambda url, **kwargs: url)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fake-offline")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-offline")
    fetched = LiveProviders(tmp_path, "fake").retrieve("scrape_page", {"url": URL}, 90)
    assert len(calls) == 1 and calls[0]["formats"] == ["markdown", "html", "rawHtml"]
    assert calls[0]["only_main_content"] is False

    class Captures(ScriptedProviders):
        def retrieve(
            self, name: str, args: dict[str, object], timeout: float
        ) -> FetchResult:
            return fetched if name == "scrape_page" else FetchResult(candidates=[])

    initial = new_state()
    done = run_research(tmp_path, initial.job_id, initial, Captures(tmp_path))
    assert done.status == "completed"
    directory = tmp_path / "jobs" / done.job_id
    source = done.sources[SID]
    assert (directory / source.filename).read_bytes() == fetched.markdown.encode()
    assert (directory / source.html_filename).read_text() == MARKETING
    assert (directory / source.raw_html_filename).read_text() == fetched.raw_html
    delivery = excerpt(directory, source)
    full = read_json(directory / "orientation_context.json")
    page = json.loads(full["messages"][0]["content"])["pages"][0]
    assert (
        page["content_hash"]
        == delivery["content_hash"]
        == digest(load_cleaned(directory, source))
    )
    assert page["cleaner_version"] == delivery["cleaner_version"] == "4"
    assert page["cleaning_summary"] == delivery["cleaning_summary"]
    assert search_inventory(directory, "documentation")["entries"]
    before = {str(p): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    run_research(tmp_path, initial.job_id, providers=Captures(tmp_path))
    for path, content in before.items():
        assert Path(path).read_bytes() == content


def test_protected_block_loss_falls_back_without_hiding_programming_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = saved_source(
        tmp_path / "guard",
        "Saved fallback evidence",
        "<pre>  code\n</pre><table><tr><td rowspan=2>Limit</td></tr></table>",
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            html_cleaning,
            "render_markdown",
            lambda text: "converter lost protected content",
        )
        assert load_cleaned(tmp_path / "guard", source) == "Saved fallback evidence"
    assert "protected block" in json.dumps(source.cleaning_summary)
    reopened = Source.model_validate_json(source.model_dump_json())
    reopened.cleaning_summary = {}
    reopened.cleaning_report_filename = None
    assert load_cleaned(tmp_path / "guard", reopened) == "Saved fallback evidence"
    assert reopened.cleaning_summary == source.cleaning_summary

    def programming_error(*args: object) -> object:
        raise RuntimeError("unexpected programming error")

    monkeypatch.setattr(page_content, "clean_html", programming_error)
    broken = saved_source(tmp_path / "broken", "Fallback", "<p>HTML</p>")
    with pytest.raises(RuntimeError, match="unexpected programming error"):
        load_cleaned(tmp_path / "broken", broken)


def test_uncertain_components_code_and_variant_context_survive() -> None:
    raw = """<div class="cookie-banner"><p>A cookie product capability with no consent controls.</p></div>
    <pre><code><script>literal()</script><!-- literal --></code></pre>
    <section data-plan="pro"><h2>Pro</h2><div class="sticky-cta">14-day trial</div></section>
    <section data-plan="team"><h2>Team</h2><div class="sticky-cta">14-day trial</div></section>
    <table><tr><th>Example</th></tr><tr><td><pre>  a  b\n</pre></td></tr></table>
    <div class="ask-ai"><span>Ask AI</span><a href="https://claude.ai/new?q=widget">Launch</a><p>Integration supports <a href="https://claude.ai/new?q=editorial">a prompt</a> in our product.</p></div>"""
    result = clean_html(raw, URL, SID)
    assert "capability with no consent controls" in result.content and result.warnings
    assert "<script>literal()</script><!-- literal -->" in result.content
    assert result.content.count("14-day trial") == 2
    assert "<pre><code>  a  b\n</code></pre>" in result.content
    assert "q=editorial" in result.content and "q=widget" not in result.content


def test_nested_launch_footer_and_icon_regression(tmp_path: Path) -> None:
    raw = (Path(__file__).parent / "fixtures" / "cleaner_controls.html").read_text()
    raw = raw.replace("LONG_PROMPT", "encoded_payload_" * 4000)
    result = clean_html(raw, URL, SID)
    text = clean_markdown(result.content)
    for keep in (
        "Private compute",
        "Available on-premises and as SaaS",
        "Intel TDX",
        "AMD SEV-SNP",
        "NVIDIA Blackwell",
        "Hopper",
        "Deploy private clusters",
        "Product supports private deployments",
        "q=editorial",
        "q=normal",
        "q=code",
        "Compare plans",
        "ISO 27001",
        "Exports require approval",
        "team@example.com",
        "Certification",
        "Private cluster topology",
        "Gateway connects two nodes",
        "unresolved visual",
    ):
        assert keep in text
    for removed in (
        "encoded_payload_",
        "ASK AI ABOUT EXAMPLE",
        "](/products)",
        "Key management",
        "French",
        "All Right reserved",
        "[SVG: ]",
        "linkedin.com/company/example",
        "x.com/example",
    ):
        assert removed not in text
    assert text.count("Expand details") == 1
    assert "encoded_payload_" not in json.dumps(result.__dict__)
    assert {link["url"] for link in result.discovery} >= {
        "https://example.com/products",
        "https://example.com/keys",
        "https://example.com/privacy",
    }
    assert len([a for a in result.assets if a["kind"] == "svg"]) == 2
    assert result == clean_html(raw, URL, SID)


def test_v2_bytes_stay_historical_and_v3_paths_are_distinct(tmp_path: Path) -> None:
    source = saved_source(tmp_path, "Original fallback", "<p>New capture</p>")
    source.cleaner_version = "2"
    source.cleaned_filename = f"sources/{SID}.cleaned.v2.md"
    historical = "ASK AI ABOUT LEGACY: actual bytes the model saw\r\n"
    (tmp_path / source.cleaned_filename).write_bytes(historical.encode())
    reopened = Source.model_validate_json(source.model_dump_json())
    assert load_cleaned(tmp_path, reopened) == historical
    assert reopened.cleaner_version == "2"
    (tmp_path / source.cleaned_filename).unlink()
    with pytest.raises(ValueError, match="historical v2"):
        load_cleaned(tmp_path, reopened)
    assert not (tmp_path / source.cleaned_filename).exists()
