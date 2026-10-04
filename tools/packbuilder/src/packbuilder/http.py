"""The one way the builder talks to the internet.

The rules every source gets, whoever wrote the adapter:

- an honest ``User-Agent`` naming the project and where to find it;
- at most one request a second to any one host;
- a cache on disk, keyed by address, that a rebuild reads instead of asking again, and that
  ``--no-network`` builds from alone;
- failures are raised as :class:`FetchError` with the address and the reason, never swallowed —
  every way a connection can fail, not only the common ones, so one broken answer stops one game;
- no answer larger than :data:`MAX_BYTES`;
- a ``Retry-After`` is waited out, up to :data:`MAX_RETRY_AFTER`, and a longer one is a failure;
- a site's ``robots.txt`` is read before anything else is asked of it (GitHub's API and files are
  used under GitHub's API terms instead), and a path it disallows is not fetched.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import pathlib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from packbuilder import VERSION
from packbuilder.files import write_bytes

USER_AGENT = f"wuwa-packbuilder/{VERSION} (+https://github.com/dotStray/wuwa-preset)"

# The largest answer read: the biggest real one (a hash repository's tree) is a few megabytes.
MAX_BYTES = 64 * 1024 * 1024

# The longest Retry-After waited out; a source asking for longer fails this run instead.
MAX_RETRY_AFTER = 300.0

# Hosts that get the token, and whose robots.txt does not apply (their API terms do).
GITHUB_HOSTS = ("api.github.com", "raw.githubusercontent.com", "github.com", "codeload.github.com")


class FetchError(Exception):
    """A source could not be read."""


class Fetcher:
    def __init__(
        self,
        cache: pathlib.Path,
        *,
        offline: bool = False,
        delay: float = 1.0,
        token: str | None = None,
        token_hosts: tuple[str, ...] = ("api.github.com",),
        exempt_from_robots: tuple[str, ...] = GITHUB_HOSTS,
    ):
        self.cache = cache
        self.offline = offline
        self.delay = delay
        self.token = token
        self.token_hosts = token_hosts
        self.exempt_from_robots = exempt_from_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _cache_path(self, url: str) -> pathlib.Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache / "http" / digest[:2] / digest

    def get(self, url: str, *, fresh: bool = True) -> bytes:
        """The body at ``url``.

        ``fresh=True`` asks the network (and refreshes the cache); ``fresh=False`` is content
        addressed by the URL itself — a picture, a file at a fixed commit — so a cached copy is
        used without asking. Offline, only the cache is read.
        """
        path = self._cache_path(url)
        if path.is_file() and (self.offline or not fresh):
            return path.read_bytes()
        if self.offline:
            raise FetchError(f"{url}: not in the cache, and the build was asked not to use the network.")

        parts = urllib.parse.urlparse(url)
        host = parts.netloc
        self._check_robots(url, parts)

        self._pace(host)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        if self.token and host in self.token_hosts:
            # Never sent on to wherever a redirect points.
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")

        body = self._download(request, url)
        self._last[host] = time.monotonic()
        # Whole or not at all: a cut-off copy with fresh=False would otherwise stick, across runs too.
        write_bytes(path, body)
        return body

    def _pace(self, host: str) -> None:
        wait = self._last.get(host, 0.0) + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _check_robots(self, url: str, parts: urllib.parse.ParseResult) -> None:
        host = parts.netloc
        if host in self.exempt_from_robots:
            return
        if host not in self._robots:
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                self._pace(host)
                request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=30) as response:
                    kind = response.headers.get_content_type()
                    text = response.read(1024 * 1024).decode("utf-8", "replace")
                # A site with no robots.txt often answers with its home page: that is no rules at all.
                if kind == "text/plain":
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(text.splitlines())
            except urllib.error.HTTPError as error:
                if error.code in (401, 403):
                    raise FetchError(f"{robots_url}: HTTP {error.code}, so {host} is not read.") from error
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, ssl.SSLError) as error:
                raise FetchError(f"{robots_url}: {getattr(error, 'reason', error)}") from error
            finally:
                self._last[host] = time.monotonic()
            self._robots[host] = parser
        parser = self._robots[host]
        if parser is not None and not parser.can_fetch(USER_AGENT, url):
            raise FetchError(f"{url}: {host}'s robots.txt does not allow it, so it is not fetched.")

    @staticmethod
    def _download(request: urllib.request.Request, url: str) -> bytes:
        """Three tries for a network hiccup; none for an answer that will not change."""
        problem = "no answer"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    declared = response.headers.get("Content-Length")
                    body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise FetchError(f"{url}: the answer is larger than {MAX_BYTES // (1024 * 1024)} MB, so it was not kept.")
                # A read with a limit returns what came, even when the connection ended early; the
                # length the server gave is what tells a cut-off answer from a whole one.
                if declared is not None and declared.isdigit() and int(declared) != len(body):
                    raise http.client.IncompleteRead(body, int(declared) - len(body))
                return body
            except urllib.error.HTTPError as error:
                problem = f"HTTP {error.code} {error.reason}"
                if error.code in (401, 403, 404, 410):
                    break
                if error.code in (429, 503) and (asked := _retry_after(error)) is not None:
                    if asked > MAX_RETRY_AFTER:
                        raise FetchError(f"{url}: asked to wait {asked:.0f} seconds, longer than a build waits.") from error
                    if attempt < 2:
                        time.sleep(asked)
                    continue
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, ssl.SSLError) as error:
                # IncompleteRead and the rest of http.client's, and a broken TLS session: a hiccup like any other.
                problem = str(getattr(error, "reason", error)) or type(error).__name__
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        raise FetchError(f"{url}: {problem}")

    def get_json(self, url: str, *, fresh: bool = True) -> object:
        body = self.get(url, fresh=fresh)
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise FetchError(f"{url}: the answer was not JSON ({error.msg}).") from error


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    """The seconds a ``Retry-After`` asks for, when it is a number of seconds."""
    value = error.headers.get("Retry-After") if error.headers else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None
