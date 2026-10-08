#!/usr/bin/env python3
"""Separate PDF process: export.py supplies a timeout and retains files on failure."""
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

from weasyprint import HTML, URLFetcher


class LocalFetcher(URLFetcher):
    def __init__(self, root):
        super().__init__(allowed_protocols={"file", "data"}, allow_redirects=False)
        self.root = Path(root).resolve()

    def fetch(self, url, headers=None):
        parsed = urlsplit(url)
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path)).resolve()
            if parsed.netloc not in {"", "localhost"} or not path.is_relative_to(self.root) or not path.is_file():
                raise ValueError("PDF resource is outside this record's directory")
        elif parsed.scheme == "data":
            if not url.lower().startswith("data:image/") or len(url) > 32 * 1024 * 1024:
                raise ValueError("Unsupported embedded image")
        else:
            raise ValueError("Remote PDF resources are unavailable in backup exports")
        return super().fetch(url, headers)


def render(source, target):
    source = Path(source).resolve()
    HTML(filename=str(source), url_fetcher=LocalFetcher(source.parent)).write_pdf(str(target))


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2])
