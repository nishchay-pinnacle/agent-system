import json
from pathlib import Path

from research_agent.cleaning import clean_markdown
from research_agent.html_cleaning import clean_html

URL = "https://example.com/"
SID = "s_test"

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


def test_documentation_fixtures_keep_restrictions_and_error_guidance() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    category = clean_html(
        (fixtures / "documentation_category.html").read_text(), URL, SID
    ).content
    for evidence in (
        "404 errors and how to recover",
        "Linux prerequisites",
        "Azure VM",
        "Enterprise only, subject to regional restrictions",
    ):
        assert evidence in category
    controls = clean_html(
        (fixtures / "documentation_controls.html").read_text(), URL, SID
    ).content
    for evidence in (
        "Intel TDX",
        "AMD SEV-SNP",
        "Platform measurements must match",
        "Attestation required",
        "Search is a product capability",
    ):
        assert evidence in controls
    assert "CTRL+K" not in controls
