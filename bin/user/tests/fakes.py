# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Broker/network-free stand-ins used by the test modules.

FakeOpener replaces urllib.request.urlopen (module-level, so it intercepts
calls made from inside weewx.restx.RESTThread.post_request regardless of
which module imported urllib.request) so HTTP posting can be exercised
without a live Grafana Cloud endpoint.
"""


class FakeResponse:
    """Stand-in for the object returned by urllib.request.urlopen()."""

    def __init__(self, code=200, body=b''):
        self.code = code
        self._body = body

    def read(self):
        return self._body


class FakeOpener:
    """Stand-in for urllib.request.urlopen.

    Records every (request, data, timeout) it is called with. Responses (or
    exceptions to raise) are queued up in advance with queue_response(); if
    the queue is empty, a plain 200 FakeResponse is returned.
    """

    def __init__(self):
        self.calls = []
        self._responses = []

    def queue_response(self, response_or_exception):
        self._responses.append(response_or_exception)

    def __call__(self, request, data=None, timeout=None):
        self.calls.append((request, data, timeout))
        if not self._responses:
            return FakeResponse()
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result