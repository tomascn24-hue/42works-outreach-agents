"""Public web access for lead collection and research."""
import http.client
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

USER_AGENT = "42worksOutreachResearch/0.1"
MAX_BYTES = 2_000_000
_SKIP = {"script", "style", "noscript", "svg", "template", "nav", "footer"}
_BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "section", "article", "br", "tr", "td", "title", "header"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            # Line breaks inside HTML source are not sentence boundaries; only block tags are.
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html):
    parser = _Text()
    parser.feed(html)
    return re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))


def is_public_host(host):
    """Lead data decides which hosts we fetch, so internal addresses are refused."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme not in ("http", "https") or not is_public_host(parts.hostname or ""):
            raise urllib.error.URLError("redirect to a non-public address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpFetcher:
    """Fetches public pages, honouring robots.txt. Returns None on any failure."""

    def __init__(self, timeout=15):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_SafeRedirect)
        self.robots = {}

    def _allowed(self, parts, url):
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            request = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
            try:
                with self.opener.open(request, timeout=self.timeout) as resp:
                    rp.parse(resp.read(MAX_BYTES).decode("utf-8", errors="replace").splitlines())
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    rp.disallow_all = True
                else:
                    rp.allow_all = True
            except (OSError, http.client.HTTPException, ValueError):
                rp.allow_all = True
            self.robots[base] = rp
        return self.robots[base].can_fetch(USER_AGENT, url)

    def get(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not is_public_host(parts.hostname or ""):
            return None
        if not self._allowed(parts, url):
            return None
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(request, timeout=self.timeout) as resp:
                if "html" not in resp.headers.get("Content-Type", ""):
                    return None
                raw = resp.read(MAX_BYTES)
                return raw.decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
        except (OSError, http.client.HTTPException, ValueError):
            return None


class FakeFetcher:
    """Serves pages from a dict; used by the tests and the demo."""

    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        return self.pages.get(url)
