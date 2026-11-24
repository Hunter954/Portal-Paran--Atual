import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from time import monotonic
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser


AGENT = "ParanaAtualRadar"
MAX_BYTES = 2_000_000
TLS_CONTEXT = ssl.create_default_context()


class FetchError(ValueError):
    pass


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    body: bytes


def public_url(value):
    if len(value) > 1000 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise FetchError("Informe uma URL HTTP ou HTTPS válida.")
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").rstrip(".").encode("idna").decode()
        port = parts.port
        if parts.username is not None or not host or "." not in host:
            raise ValueError
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError
        if scheme not in {"http", "https"} or port not in (None, 80 if scheme == "http" else 443):
            raise ValueError
        if host.endswith((".local", ".localhost", ".internal", ".test", ".invalid", ".onion", ".home.arpa")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global or address.is_multicast:
                raise ValueError
        return urlunsplit((scheme, host, quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                           quote(parts.query, safe="/%?:@!$&'()*+,;=-._~"), ""))
    except (UnicodeError, ValueError) as exc:
        raise FetchError("Informe uma URL pública HTTP ou HTTPS, sem credenciais.") from exc


def canonical_url(value):
    url = urlsplit(public_url(value))
    pairs = parse_qsl(url.query, keep_blank_values=True)
    kept = [(key, field) for key, field in pairs if
            not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}]
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode(kept) if pairs != kept else url.query, ""))


def resolve_public(host, port):
    destinations = socket.getaddrinfo(host + "." if not host[0].isdigit() else host, port,
                                      type=socket.SOCK_STREAM)
    for _, _, _, _, address in destinations:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise FetchError("Endereço de rede não permitido.")
    return destinations


def direct_connect(destinations, timeout):
    last_error = None
    deadline = monotonic() + timeout
    for family, socktype, proto, _, address in destinations:
        with_socket = socket.socket(family, socktype, proto)
        try:
            with_socket.settimeout(max(0.1, deadline - monotonic()))
            with_socket.connect(address)
            return with_socket
        except OSError as exc:
            last_error = exc
            with_socket.close()
            if monotonic() >= deadline:
                break
    raise FetchError("A conexão com a fonte expirou ou falhou.") from last_error


class PublicFetcher:
    def __init__(self):
        self.robots = {}

    def get(self, value, *, conditional=None):
        return self._get(value, conditional=conditional, check_robots=True)

    def _get(self, value, *, conditional=None, check_robots=False):
        url = public_url(value)
        for _ in range(5):
            if check_robots:
                self._check_robots(url)
            response = self._request(url, conditional)
            location = response.headers.get("location")
            if response.status not in {301, 302, 303, 307, 308} or not location:
                return response
            url = public_url(urljoin(url, location))
        raise FetchError("A fonte retornou redirecionamentos demais.")

    def _check_robots(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            response = self._get(origin + "/robots.txt")
            if response.status not in {200, 404, 410}:
                raise FetchError(f"Acesso ao robots.txt recusado (HTTP {response.status}).")
            rules = RobotFileParser()
            rules.parse(response.body.decode("utf-8", errors="replace").splitlines()
                        if response.status == 200 else [])
            self.robots[origin] = rules
        if not self.robots[origin].can_fetch(AGENT, url):
            raise FetchError("A URL está restrita pelo robots.txt da fonte.")

    @staticmethod
    def _request(url, conditional):
        parts = urlsplit(url)
        secure = parts.scheme == "https"
        port = 443 if secure else 80
        headers = {"User-Agent": AGENT + "/1.0 (+https://paranaatual.com.br/)",
                   "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html, */*",
                   "Accept-Encoding": "identity", "Connection": "close"}
        if conditional and conditional[0] == url:
            if conditional[1]:
                headers["If-None-Match"] = conditional[1]
            if conditional[2]:
                headers["If-Modified-Since"] = conditional[2]
        connection = (http.client.HTTPSConnection(parts.hostname, port, timeout=6, context=TLS_CONTEXT)
                      if secure else http.client.HTTPConnection(parts.hostname, port, timeout=6))
        try:
            destinations = resolve_public(parts.hostname, port)
            connection._create_connection = lambda _, timeout, __: direct_connect(destinations, timeout)
            connection.request("GET", urlunsplit(("", "", parts.path, parts.query, "")), headers=headers)
            response = connection.getresponse()
            metadata = {k.lower(): v for k, v in response.getheaders()}
            body = bytearray()
            if response.status == 200:
                deadline = monotonic() + 8
                while True:
                    chunk = response.read1(32768)
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_BYTES or monotonic() > deadline:
                        raise FetchError("A resposta excedeu o limite de tamanho ou tempo.")
            return Response(url, response.status, metadata, bytes(body))
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError("Falha de rede, DNS ou TLS ao consultar a fonte.") from exc
        finally:
            connection.close()

