"""The application server must accept a page's burst of connections."""

from __future__ import annotations

import socket

from token_tracker import server, widget


def test_server_queues_a_burst_of_connections_it_has_not_accepted_yet():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    clients = []
    try:
        for _ in range(40):  # nothing accepts these: the default queue of 5 refuses the rest on Windows
            client = socket.create_connection(httpd.server_address, timeout=5)
            clients.append(client)
    finally:
        for client in clients:
            client.close()
        httpd.server_close()
    assert len(clients) == 40


def test_widget_server_uses_the_same_queue():
    assert widget._ExclusiveHTTPServer.request_queue_size == server.ThreadingHTTPServer.request_queue_size == 128
