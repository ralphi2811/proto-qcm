"""Nettoyage minimal du HTML produit par l'éditeur avant rendu PDF."""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser

ALLOWED = {
    "b", "strong", "i", "em", "u", "s", "sub", "sup", "code", "pre", "br",
    "p", "div", "span", "ul", "ol", "li", "img",
}
VOID = {"br", "img"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            return
        if tag == "img":
            src = dict(attrs).get("src") or ""
            # seules les images embarquées sont autorisées (pas d'accès réseau au rendu)
            if src.startswith("data:image/"):
                self.out.append(f'<img src="{escape(src, quote=True)}">')
            return
        if tag in VOID:
            self.out.append(f"<{tag}>")
            return
        self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        self.out.append(escape(data))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def clean_html(html: str) -> str:
    p = _Sanitizer()
    p.feed(html or "")
    p.close()
    return p.result()
