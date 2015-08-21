###############################################################################
#
# The MIT License (MIT)
#
# Copyright (c) Tavendo GmbH
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
###############################################################################

from __future__ import absolute_import, print_function
import signal

import six

from autobahn.wamp import protocol
from autobahn.wamp.types import ComponentConfig
from autobahn.websocket.protocol import parseWsUrl
from autobahn.asyncio.websocket import WampWebSocketClientFactory

try:
    import asyncio
except ImportError:
    # Trollius >= 0.3 was renamed to asyncio
    # noinspection PyUnresolvedReferences
    import trollius as asyncio

import txaio
txaio.use_asyncio()

from autobahn.asyncio.websocket import WampWebSocketClientFactory
from autobahn.wamp.runner import _ApplicationRunner, Connection


__all__ = (
    'ApplicationSession',
    'ApplicationSessionFactory',
    'ApplicationRunner',
    'Connection',
    'connect_to',
)


class ApplicationSession(protocol.ApplicationSession):
    """
    WAMP application session for asyncio-based applications.
    """


class ApplicationSessionFactory(protocol.ApplicationSessionFactory):
    """
    WAMP application session factory for asyncio-based applications.
    """

    session = ApplicationSession
    """
    The application session class this application session factory will use.
    Defaults to :class:`autobahn.asyncio.wamp.ApplicationSession`.
    """


def _create_unix_stream_transport(loop, cfg, wamp_transport_factory):
    """
    Internal helper.

    Creates a Unix socket as the stream transport.
    """
    return asyncio.async(loop.create_unix_connection(wamp_transport_factory, cfg['path']))


def _connect_stream(loop, cfg, wamp_transport_factory):
    """
    Internal helper.

    Connects the given wamp_transport_factory to a stream endpoint, as
    determined from the cfg that's passed in (which should be just the
    "endpoint" part). Returns Deferred that fires with IProtocol
    """

    is_secure, host, port, resource, path, params = parseWsUrl(cfg['url'])
    ep = cfg['endpoint']
    if ep['type'] == 'unix':
        f = _create_unix_stream_transport(loop, cfg, wamp_transport_factory)

    elif ep['type'] == 'tcp':
        if ep.get('version', 4) == 4:
            ssl = is_secure
            ssl = ep.get('tls', ssl)
            f = loop.create_connection(
                    wamp_transport_factory, ep['host'], ep['port'],
                    ssl=ssl,
            )

        else:
            raise RuntimeError("FIXME: IPv6 asyncio")

    else:
        raise RuntimeError("Unknown type='{}'".format(cfg['type']))

    return f


def _create_wamp_factory(reactor, cfg, session_factory):
    """
    Internal helper.

    This creates the appropriate protocol-factory (that implements
    tx:`IProtocolFactory <twisted.internet.interfaces.IProtocolFactory>`)

    XXX deal with debug/debug_wamp etcetc.
    """

    if cfg['type'] == 'rawsocket':
        raise RuntimeError("No rawsocket/asyncio impl")

    # only other type is websocket
    return WampWebSocketClientFactory(session_factory, url=cfg['url'])


# XXX counter-intuitively (?) this is called via the common Connection
# class in wamp/runner.py when used internally -- but does need a
# custom asyncio/twisted implementation because of the different way
# shutdown works.


def connect_to(loop, transport_config, session):
    """
    :param transport_config: dict containing valid client transport
    config (see :mod:`autobahn.wamp.transport`)

    :param session_factory: callable that takes a ComponentConfig and
    returns a new ISession instance (usually simply your
    ApplicationSession subclass)

    :returns: Future that callbacks with a protocol instance after a
    connection has been made (not necessarily a WAMP session joined
    yet, however)
    """

    def create():
        return session

    transport_factory = _create_wamp_factory(loop, transport_config, create)
    f0 = _connect_stream(loop, transport_config, transport_factory)

    # mutate the return value of _connect_stream to be just the
    # protocol so that the API of connect_to is the "same" for Twisted
    # and asyncio (although the protocol returned is a native Twisted
    # or asyncio object).
    # both provide protocol.transport to get the transport

    # XXX is there a better idiom for this in asyncio?
    f1 = asyncio.Future()
    def return_proto(result):
        try:
            transport, protocol = result.result()
            transport.connectionLost = protocol.connection_lost
            f1.set_result(protocol)
        except Exception as e:
            f1.set_exception(e)
    f0.add_done_callback(return_proto)
    return f1


class ApplicationRunner(_ApplicationRunner):
    """
    Provides a high-level API that is (mostly) consistent across
    asyncio and Twisted code.

    If you want more control over the reactor and logging, see the
    :class:`autobahn.wamp.runner.Connection` class.

    If you need lower-level control than that, see :meth:`connect_to`
    which attempts a single connection to a single transport.
    """

    def run(self, session_factory):
        """
        Run the application component.

        :param session_factory: A factory that produces instances of :class:`autobahn.asyncio.wamp.ApplicationSession`
           when called with an instance of :class:`autobahn.wamp.types.ComponentConfig`.
        :type session_factory: callable
        """

        # set up the event-loop and ensure txaio is using the same one
        loop = self._loop
        if loop is None:
            loop = asyncio.get_event_loop()
        txaio.use_asyncio()
        txaio.config.loop = loop
        try:
            # want to shut down nicely on TERM
            loop.add_signal_handler(signal.SIGTERM, loop.stop)
        except NotImplementedError:
            # signals are not available on Windows
            pass

        session = session_factory(ComponentConfig(realm=self.realm, extra=self.extra))
        self.connection = Connection(
            session,
            self._transports,
            loop,
        )

        # now enter the asyncio event loop
        try:
            loop.run_until_complete(self.connection.open())
        except KeyboardInterrupt:
            # wait until we send Goodbye if user hit ctrl-c
            # (done outside this except so SIGTERM gets the same handling)
            pass

        # give Goodbye message a chance to go through, if we still
        # have an active session
        if hasattr(protocol, '_session') and protocol._session is not None:
            if protocol._session._session_id:
                loop.run_until_complete(protocol._session.leave())
        loop.close()
