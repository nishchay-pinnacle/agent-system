"""Conservative, page-local DOM transformations; uncertain components survive.

Rules below require structural UI signals as well as labels. They intentionally do
not claim to recognize every vendor's widget. Hidden state is never a deletion rule.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from html import escape
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from bs4.exceptions import ParserRejectedMarkup
from markdownify import markdownify
from pydantic import JsonValue


class ConversionError(ValueError):
    """A detected parser/converter failure eligible for saved-Markdown fallback."""


@dataclass
class HTMLResult:
    content: str = ""
    removals: list[dict[str, JsonValue]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    discovery: list[dict[str, str]] = field(default_factory=list)
    assets: list[dict[str, JsonValue]] = field(default_factory=list)
    json_ld: list[JsonValue] = field(default_factory=list)
    breadcrumbs: list[str] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)


def attr(tag: Tag, key: str) -> str:
    value = tag.get(key, "")
    return " ".join(value) if isinstance(value, list) else str(value)


def prose(text: str) -> str:
    return re.sub(r"[ \t\u00a0\u202f]+", " ", text)


def visible_text(tag: Tag) -> str:
    return " ".join(tag.stripped_strings)


def share_destination(url: str) -> bool:
    """Intent/prompt route plus parameter, never hostname alone."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").removeprefix("www.")
    query = parse_qs(parsed.query)
    ai_paths = {
        "chatgpt.com": {"/", "/chat"},
        "chat.openai.com": {"/", "/chat"},
        "claude.ai": {"/new", "/chat"},
        "gemini.google.com": {"/", "/app"},
        "perplexity.ai": {"/", "/search"},
    }
    if host in ai_paths:
        return parsed.path in ai_paths[host] and bool(
            {"q", "query", "prompt", "text"} & query.keys()
        )
    routes = {
        "twitter.com": "/intent/tweet",
        "x.com": "/intent/tweet",
        "linkedin.com": "/sharing/share-offsite",
        "facebook.com": "/sharer",
    }
    return host in routes and parsed.path.startswith(routes[host]) and bool(query)


def render_markdown(fragment: str) -> str:
    try:
        content = markdownify(
            fragment,
            bs4_options="html.parser",
            heading_style="ATX",
            bullets="-",
            escape_underscores=False,
            escape_asterisks=False,
        )
    except ValueError as exc:
        raise ConversionError("HTML-to-Markdown conversion failed") from exc
    if not isinstance(content, str):
        raise ConversionError("HTML converter returned non-text")
    return content


def clean_html(raw: str, page_url: str, source_id: str) -> HTMLResult:
    """Clean one representation; caller archives originals and normalizes Markdown."""
    result = HTMLResult()
    try:
        soup = BeautifulSoup(raw, "html.parser")
    except ParserRejectedMarkup as exc:
        raise ConversionError("HTML parser rejected capture") from exc
    tags = list(soup.find_all(True))
    locators = {id(tag): f"{tag.name}:node({i})" for i, tag in enumerate(tags, 1)}

    def locator(tag: Tag) -> str:
        return locators.get(id(tag), tag.name)

    def warn(message: str) -> None:
        if message not in result.warnings:
            result.warnings.append(message)

    def remove(tag: Tag, rule: str) -> None:
        result.removals.append(
            {"rule": rule, "locator": locator(tag), "line": tag.sourceline}
        )
        tag.decompose()

    def discovery(tag: Tag) -> None:
        for link in [tag] if tag.name == "a" else tag.find_all("a", href=True):
            if attr(link, "href"):
                result.discovery.append(
                    {
                        "url": attr(link, "href"),
                        "title": visible_text(link),
                        "description": "",
                    }
                )

    def asset(tag: Tag, kind: str, original: str = "") -> str:
        reference = f"sources/{source_id}.assets/{kind}-{hashlib.sha256((locator(tag) + original).encode()).hexdigest()[:16]}"
        # Payloads stay in the capture. Locators are deterministic and representation-local.
        result.assets.append(
            {
                "reference": reference,
                "kind": kind,
                "locator": locator(tag),
                "line": tag.sourceline,
            }
        )
        return reference

    # Executable payloads, comments: skip protected examples; retain JSON-LD separately.
    for tag in list(soup.find_all(["script", "style"])):
        if tag.find_parent(["pre", "code"]):
            tag.replace_with(NavigableString(str(tag)))
            continue
        if tag.name == "script" and attr(tag, "type").lower() == "application/ld+json":
            try:
                value: JsonValue = json.loads(tag.get_text())
                result.json_ld.append(value)
            except (ValueError, RecursionError):
                warn("Invalid JSON-LD was not treated as evidence")
        remove(tag, "executable_payload")
    for comment in list(soup.find_all(string=lambda text: isinstance(text, Comment))):
        if not comment.find_parent(["pre", "code"]):
            result.removals.append(
                {"rule": "html_comment", "locator": "comment", "line": None}
            )
            comment.extract()
        else:
            comment.replace_with(NavigableString("<!--" + str(comment) + "-->"))

    base_url = page_url
    base = soup.find("base", href=True)
    if base:
        # Local import avoids the tools -> page_content -> cleaner dependency cycle.
        from agent_system.company_intelligence.tools import public_url

        try:
            candidate_base = urljoin(page_url, attr(base, "href"))
            public_url(
                candidate_base
            )  # Validation only; retain query/fragment spelling.
            base_url = candidate_base
        except ValueError:
            warn("Invalid base URL ignored; links resolve against the page URL")
    for tag in list(soup.find_all(["head", "base", "meta", "link"])):
        if tag.parent is not None:
            remove(tag, "document_metadata")

    # Resolve labels before geometry/widgets are transformed. Duplicate only in-component.
    for tag in list(soup.find_all(True)):
        if tag.find_parent(["pre", "code"]):
            continue
        labels = [attr(tag, "aria-label")]
        for target in attr(tag, "aria-labelledby").split():
            labelled = soup.find(id=target)
            if labelled:
                labels.append(visible_text(labelled))
        for label in dict.fromkeys(label for label in labels if label):
            if label not in visible_text(tag) and label != attr(tag, "alt"):
                tag.insert(0, NavigableString(label + " "))
        if (attr(tag, "aria-controls") or attr(tag, "data-src")) and not visible_text(
            tag
        ):
            warn(
                "Interactive or lazy content may not be present in this capture; no interaction was performed"
            )
        for key in ("href", "src", "poster"):
            original = attr(tag, key)
            if not original:
                continue
            if original.startswith("data:"):
                tag[key] = asset(tag, "inline", original)
                continue
            try:
                resolved = urljoin(base_url, original)
            except ValueError:
                warn("Malformed link retained as captured")
                continue
            result.links.append(
                {
                    "locator": locator(tag),
                    "attribute": key,
                    "original_hash": hashlib.sha256(original.encode()).hexdigest(),
                    "original": (
                        urlsplit(original)._replace(query="").geturl()
                        if share_destination(resolved)
                        else original
                    ),
                    "resolved": (
                        urlsplit(resolved)._replace(query="").geturl()
                        if share_destination(resolved)
                        else resolved
                    ),
                }
            )
            tag[key] = resolved

    # Exact duplicate UI components only; roles/classes identify responsive or cloned UI.
    seen: set[str] = set()
    copy_classes = {
        "desktop-only",
        "mobile-only",
        "carousel-clone",
        "slick-cloned",
        "sticky-cta",
    }
    for tag in list(soup.find_all(True)):
        if tag.parent is None or tag.find_parent(["pre", "code", "table"]):
            continue
        classes = attr(tag, "class").split()
        family = next((x for x in classes if x in copy_classes), None)
        if not family:
            continue
        canonical = BeautifulSoup(str(tag), "html.parser")
        root = canonical.find(True)
        assert root is not None
        for node in [root, *root.find_all(True)]:
            if node is root:
                for key in ("id", "hidden", "aria-hidden"):
                    node.attrs.pop(key, None)
                retained_classes = [
                    x for x in attr(node, "class").split() if x not in copy_classes
                ]
                if retained_classes:
                    node["class"] = " ".join(retained_classes)
                else:
                    node.attrs.pop("class", None)
            for text in list(node.find_all(string=True, recursive=False)):
                if not text.find_parent(["pre", "code"]):
                    text.replace_with(prose(str(text)))
        context_parts: list[str] = []
        for parent in tag.parents:
            if isinstance(parent, Tag):
                context_parts.extend(
                    attr(parent, key)
                    for key in ("data-plan", "data-product", "lang", "aria-label")
                )
                heading = parent.find(re.compile(r"^h[1-6]$"), recursive=False)
                if heading:
                    context_parts.append(visible_text(heading))
        fingerprint = str(canonical) + json.dumps(context_parts)
        if fingerprint in seen:
            remove(tag, "exact_ui_duplicate")
        else:
            seen.add(fingerprint)

    # Bounded consent UI AND at least two actual consent controls. Never product prose.
    for tag in list(soup.find_all(["div", "section", "aside", "dialog", "form"])):
        if tag.parent is None or tag.find_parent(["pre", "code"]):
            continue
        identity = " ".join(
            [attr(tag, "id"), attr(tag, "class"), attr(tag, "aria-label")]
        ).lower()
        controls = [visible_text(t).lower() for t in tag.find_all(["button", "input"])]
        consent = sum(
            any(
                word in label for word in ("accept", "reject", "preferences", "consent")
            )
            for label in controls
        )
        bounded = (
            tag.name == "dialog"
            or attr(tag, "role") in {"dialog", "alertdialog"}
            or any(
                x in identity
                for x in ("cookie-banner", "consent-banner", "cookie-consent")
            )
        )
        if (
            bounded
            and consent >= 2
            and not tag.find(
                ["pre", "code", "table", "article", "blockquote", "details"]
            )
        ):
            discovery(tag)
            remove(tag, "consent_controls")
        elif bounded:
            warn("Ambiguous consent component retained")

    # Sharing requires an explicit widget label and a recognized intent destination.
    for tag in list(soup.find_all(["div", "aside", "section", "nav", "ul"])):
        if tag.parent is None or tag.find_parent(["pre", "code", "table"]):
            continue
        label = " ".join(
            [attr(tag, "aria-label"), attr(tag, "id"), attr(tag, "class")]
        ).lower()
        direct = (
            " ".join(
                [
                    *(str(x) for x in tag.find_all(string=True, recursive=False)),
                    *(
                        visible_text(c)
                        for c in tag.find_all(
                            ["span", "h2", "h3", "h4"], recursive=False
                        )
                        if not c.find("a")
                    ),
                ]
            )
            .strip()
            .lower()
        )
        labelled_widget = any(
            x in label
            for x in ("share-widget", "social-share", "share this", "ask-ai", "ask ai")
        ) or direct in {"share", "share this", "ask ai", "ask ai about this page"}
        if not labelled_widget:
            continue
        removed = False
        for link in list(tag.find_all("a", href=True)):
            if any(
                isinstance(parent, Tag) and parent.name in {"p", "blockquote"}
                for parent in link.parents
                if parent is not tag and tag in parent.parents
            ):
                continue  # Editorial prose inside a mixed component is not a launch control.
            if share_destination(attr(link, "href")):
                remove(link, "share_intent")
                removed = True
        if removed:
            # Only empty/label-only shells, never adjacent paragraphs or product detail.
            if visible_text(tag).lower() in {
                "",
                "share",
                "share this",
                "ask ai",
                "ask ai about this page",
            }:
                remove(tag, "share_widget_shell")

    # Breadcrumb trail requires an explicit landmark/label; preserve hierarchy in metadata.
    for tag in list(soup.find_all(True)):
        if tag.parent is None or tag.find_parent(["pre", "code"]):
            continue
        if "breadcrumb" in (
            attr(tag, "aria-label") + " " + attr(tag, "class")
        ).lower() and tag.find("a"):
            result.breadcrumbs.append(visible_text(tag))
            discovery(tag)
            remove(tag, "breadcrumb_trail")

    # Global nav/menu controls only; preserve article TOCs and descriptive mega-menu prose.
    for nav in list(soup.find_all(["nav", "menu"])) + list(
        soup.find_all(True, attrs={"role": ["navigation", "menu", "menubar"]})
    ):
        if nav.parent is None or nav.find_parent(
            ["article", "pre", "code", "table", "blockquote"]
        ):
            continue
        if "contents" in attr(nav, "aria-label").lower() or (
            nav.find_all("a", href=True)
            and all(
                urlsplit(attr(a, "href")).fragment for a in nav.find_all("a", href=True)
            )
        ):
            continue
        for link in list(nav.find_all(["a", "button"])):
            if link.parent is None:
                continue
            discovery(link)
            if link.find(
                [
                    "p",
                    "section",
                    "h1",
                    "h2",
                    "h3",
                    "pre",
                    "code",
                    "table",
                    "blockquote",
                    "img",
                    "svg",
                ]
            ) or link.find(class_=re.compile(r"description|summary")):
                warn("Descriptive navigation content retained")
                continue
            remove(link, "global_navigation_controls")

    # Header/footer isolated utilities and copyright only, not entire semantic containers.
    for region in soup.find_all(["header", "footer"]):
        if region.find_parent("article"):
            continue
        for tag in list(region.find_all(["a", "button", "p", "small"])):
            if tag.parent is None:
                continue
            label_text = visible_text(tag)
            if tag.name in {"a", "button"} and label_text.lower() in {
                "log in",
                "login",
                "sign in",
                "search",
                "menu",
                "back to top",
            }:
                discovery(tag)
                remove(tag, "header_footer_utility")
            elif tag.name in {"p", "small"} and re.fullmatch(
                r"(?:©|Copyright\s*©?)\s*\d{4}(?:[-–]\d{4})?[^.\n]*(?:\.\s*All rights reserved\.?)?",
                label_text,
                re.I,
            ):
                remove(tag, "isolated_copyright")

    # Subscription form needs email input + explicit subscription control. Keep terms.
    for form in soup.find_all("form"):
        if not form.find("input", attrs={"type": "email"}):
            continue
        form_controls = form.find_all(["button", "input"])
        if not any(
            re.search(r"subscribe|newsletter", visible_text(c) + attr(c, "value"), re.I)
            for c in form_controls
        ):
            continue
        for control in form_controls:
            remove(control, "subscription_controls")
        for tag in list(form.find_all(["label", "p", "legend"])):
            if re.fullmatch(
                r"(?:subscribe to (?:our|the) newsletter|sign up for (?:our|the) newsletter|email(?: address)?)[.!:]?",
                visible_text(tag),
                re.I,
            ):
                remove(tag, "subscription_boilerplate")

    # Floating chat controls need launcher semantics + fixed placement. Preserve transcripts.
    for tag in list(soup.find_all(True)):
        if tag.parent is None or tag.find_parent(["pre", "code"]):
            continue
        identity = (attr(tag, "id") + " " + attr(tag, "class")).lower()
        floating = (
            "position:fixed" in attr(tag, "style").replace(" ", "")
            or "floating" in identity
        )
        launcher = tag.name == "button" and bool(
            re.fullmatch(
                r"(?:open |launch )?(?:support chat|chat with us|help chat)",
                visible_text(tag),
                re.I,
            )
        )
        empty_shell = (
            not visible_text(tag)
            and "chat-widget" in identity
            and not tag.find(["img", "video", "audio"])
        )
        if floating and (launcher or empty_shell):
            remove(tag, "floating_chat_launcher")
        elif (attr(tag, "role") == "button" or tag.name == "button") and attr(
            tag, "aria-label"
        ).lower() in {"close advertisement", "close ad"}:
            remove(tag, "advertisement_control")
        elif (
            tag.name == "img"
            and attr(tag, "width") == "1"
            and attr(tag, "height") == "1"
        ):
            remove(tag, "tracking_pixel")
        elif (
            tag.name == "img"
            and attr(tag, "role") in {"none", "presentation"}
            and not attr(tag, "alt")
            and not visible_text(tag)
        ):
            remove(tag, "decorative_asset")
        elif (
            tag.name in {"div", "span", "iframe"}
            and not visible_text(tag)
            and attr(tag, "data-tracking")
            and not tag.find(True)
        ):
            remove(tag, "empty_tracking_element")

    # Prose-only whitespace normalization keeps pre/code and semantic math intact.
    for text in list(soup.find_all(string=True)):
        if not text.find_parent(["pre", "code", "math"]):
            text.replace_with(prose(str(text)))

    block_tables = {
        id(t)
        for t in soup.find_all("table")
        if t.find(["pre", "table", "ul", "ol", "p"])
    }
    protected: dict[str, str] = {}

    def protect(tag: Tag, content: str, block: bool = False) -> None:
        token = f"PINNACLEPROTECTED{len(protected):06d}TOKEN"
        while token in raw:
            token += "X"
        protected[token] = content
        tag.replace_with(
            NavigableString(
                ("\n\n" if block else "") + token + ("\n\n" if block else "")
            )
        )

    for tag in list(soup.find_all("svg")):
        if tag.find_parent(["pre", "code"]):
            continue
        labels = list(
            dict.fromkeys(
                [
                    attr(tag, "aria-label"),
                    *(
                        str(t).strip()
                        for t in tag.find_all(string=True, recursive=False)
                    ),
                    *[visible_text(t) for t in tag.find_all(["title", "desc", "text"])],
                ]
            )
        )
        reference = asset(tag, "svg")
        warn(
            "SVG geometry retained only in archived capture; visual interpretation remains unresolved"
        )
        tag.replace_with(
            NavigableString(
                "[SVG: " + "; ".join(x for x in labels if x) + f"]({reference})"
            )
        )
    for media in soup.find_all(["video", "audio", "iframe"]):
        if attr(media, "src"):
            media.append(NavigableString(f" [Media source]({attr(media, 'src')})"))
        warn("Media/embedded content was not fetched or transcribed")
    for tag in list(soup.find_all("img")):
        if not attr(tag, "alt"):
            tag["alt"] = visible_text(tag) or "Image (content not interpreted)"
            warn("Image meaning may require visual interpretation")
        # markdownify does not retain image aria descriptions added as child strings.
        elif visible_text(tag):
            tag["alt"] = attr(tag, "alt") + " — " + visible_text(tag)
    for tag in list(soup.find_all("pre")):
        # Parsed code text, including whitespace; no stripping or language guessing.
        code = tag.get_text()
        if tag.find_parent("table"):
            protect(tag, "<pre><code>" + escape(code) + "</code></pre>")
            continue
        width = max([2, *[len(m[0]) for m in re.finditer(r"`+", code)]]) + 1
        fence = "`" * width
        protect(
            tag,
            fence + "\n" + code + ("" if code.endswith("\n") else "\n") + fence,
            True,
        )
    for tag in list(soup.find_all("code")):
        code = tag.get_text()
        if tag.find_parent("table") or "\n" in code or code != code.strip():
            protect(tag, "<code>" + escape(code) + "</code>")
        else:
            fence = "`" * (max([0, *[len(m[0]) for m in re.finditer(r"`+", code)]]) + 1)
            padding = " " if "`" in code else ""
            protect(tag, fence + padding + code + padding + fence)
    for tag in list(soup.find_all(["sub", "sup", "math"])):
        if tag.parent is None or tag.find_parent(["math", "sup", "sub"]):
            continue
        # Semantic markup only; discard event/style attributes, never textual meaning.
        for node in [tag, *tag.find_all(True)]:
            node.attrs = {
                k: v
                for k, v in node.attrs.items()
                if k in {"id", "href", "display", "mathvariant", "encoding"}
            }
        protect(tag, str(tag))
    for table in list(soup.find_all("table")):
        if table.parent is None:
            continue
        rows = table.find_all("tr")
        cells = [row.find_all(["th", "td"], recursive=False) for row in rows]
        simple = (
            bool(cells)
            and id(table) not in block_tables
            and all(len(row) == len(cells[0]) for row in cells)
            and all(c.name == "th" for c in cells[0])
            and not table.find(True, attrs={"rowspan": True})
            and not table.find(True, attrs={"colspan": True})
            and not table.find(["table", "pre", "ul", "ol"])
            and not any(c.name == "th" for row in cells[1:] for c in row)
        )
        if simple:

            def cell_text(cell: Tag) -> str:
                value = render_markdown(cell.decode_contents())
                return value.strip().replace("|", "\\|").replace("\n", "<br>")

            rendered = [
                "| " + " | ".join(cell_text(c) for c in row) + " |" for row in cells
            ]
            rendered.insert(1, "| " + " | ".join("---" for _ in cells[0]) + " |")
            caption = table.find("caption")
            protect(
                table,
                (visible_text(caption) + "\n\n" if caption else "")
                + "\n".join(rendered),
                True,
            )
        else:
            for node in [table, *table.find_all(True)]:
                node.attrs = {
                    k: v
                    for k, v in node.attrs.items()
                    if k
                    in {
                        "rowspan",
                        "colspan",
                        "scope",
                        "headers",
                        "id",
                        "abbr",
                        "href",
                        "src",
                        "alt",
                        "title",
                        "aria-label",
                        "start",
                        "value",
                    }
                }
                if node.name not in {
                    "table",
                    "caption",
                    "colgroup",
                    "col",
                    "thead",
                    "tbody",
                    "tfoot",
                    "tr",
                    "th",
                    "td",
                    "p",
                    "br",
                    "strong",
                    "em",
                    "a",
                    "img",
                    "code",
                    "pre",
                    "ul",
                    "ol",
                    "li",
                    "sub",
                    "sup",
                }:
                    node.unwrap()
            protect(table, str(table), True)
    content = render_markdown(str(soup))
    # Every protected marker must survive exactly once. Restore outer tables before inner code.
    for token, value in reversed(list(protected.items())):
        if content.count(token) != 1:
            raise ConversionError("Converter dropped or duplicated a protected block")
        content = content.replace(token, value)
    result.content = content
    return result
