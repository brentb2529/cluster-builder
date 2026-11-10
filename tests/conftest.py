"""pytest-socket (pulled in by Home Assistant's test plugin when it is installed) blocks sockets; the HTTP tests ask for them."""

import pytest


@pytest.fixture
def sockets():
    try:
        import pytest_socket

        pytest_socket.enable_socket()
    except ImportError:
        pass
    yield
