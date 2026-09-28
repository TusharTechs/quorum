"""Client addresses behind reverse proxies: rate limits must not be dodgeable with a forged header."""

from django.test import RequestFactory


def _net(settings, remote, xff=None, trusted=()):
    from quorum.core import ratelimit

    settings.TRUSTED_PROXIES = list(trusted)
    meta = {"REMOTE_ADDR": remote}
    if xff:
        meta["HTTP_X_FORWARDED_FOR"] = xff
    return ratelimit.client_net(RequestFactory().get("/", **meta))


def test_forwarded_header_is_ignored_without_a_trusted_proxy(settings):
    assert _net(settings, "203.0.113.9", xff="1.2.3.4") == "203.0.113.0/24"


def test_rightmost_untrusted_hop_is_the_client(settings):
    trusted = ["172.16.0.0/12", "10.0.0.2"]
    # nginx appends: a client that forges "1.2.3.4" still shows up as itself (198.51.100.7)
    assert _net(settings, "172.18.0.5", xff="1.2.3.4, 198.51.100.7", trusted=trusted) == "198.51.100.0/24"
    # two trusted proxy hops in front of the app
    assert _net(settings, "10.0.0.2", xff="198.51.100.7, 172.18.0.9", trusted=trusted) == "198.51.100.0/24"
    assert _net(settings, "172.18.0.5", xff="2001:db8:abcd:12::1", trusted=trusted) == "2001:db8:abcd::/48"


def test_invalid_trusted_entries_are_skipped(settings):
    assert _net(settings, "172.18.0.5", xff="198.51.100.7", trusted=["not-an-ip", "172.16.0.0/12"]) == "198.51.100.0/24"


def test_platform_client_ip_header_is_believed_only_from_a_trusted_peer(settings):
    from quorum.core import ratelimit

    settings.CLIENT_IP_HEADER = "X-Real-IP"
    settings.TRUSTED_PROXIES = ["100.64.0.0/10"]
    edge = RequestFactory().get("/", REMOTE_ADDR="100.64.3.2", HTTP_X_REAL_IP="198.51.100.7",
                                HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert ratelimit.client_ip(edge) == "198.51.100.7"
    direct = RequestFactory().get("/", REMOTE_ADDR="203.0.113.9", HTTP_X_REAL_IP="1.2.3.4")
    assert ratelimit.client_ip(direct) == "203.0.113.9"
    junk = RequestFactory().get("/", REMOTE_ADDR="100.64.3.2", HTTP_X_REAL_IP="not-an-ip",
                                HTTP_X_FORWARDED_FOR="198.51.100.8")
    assert ratelimit.client_ip(junk) == "198.51.100.8"
