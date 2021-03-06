#!/usr/bin python3
#
#       Copyright 2019 - The Android Open Source Project
#
#       Licensed under the Apache License, Version 2.0 (the "License");
#       you may not use this file except in compliance with the License.
#       You may obtain a copy of the License at
#
#               http://www.apache.org/licenses/LICENSE-2.0
#
#       Unless required by applicable law or agreed to in writing, software
#       distributed under the License is distributed on an "AS IS" BASIS,
#       WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#       See the License for the specific language governing permissions and
#       limitations under the License.
"""Python module for GNSS Abstract Instrument Library."""

import socket
from acts import logger


class SocketInstrumentError(Exception):
    """Abstract Instrument Error Class, via Socket and SCPI."""

    def __init__(self, error, command=None):
        """Init method for Socket Instrument Error.

        Args:
            error: Exception error.
            command: Additional information on command,
                Type, Str.
        """
        super(SocketInstrumentError, self).__init__(error)
        self._error_code = error
        self._error_message = self._error_code
        if command is not None:
            self._error_message = 'Command {} returned the error: {}.'.format(
                repr(command), repr(self._error_message))

    def __str__(self):
        return self._error_message


class SocketInstrument(object):
    """Abstract Instrument Class, via Socket and SCPI."""

    def __init__(self, ip_addr, ip_port):
        """Init method for Socket Instrument.

        Args:
            ip_addr: IP Address.
                Type, str.
            ip_port: TCPIP Port.
                Type, str.
        """
        self._socket_timeout = 120
        self._socket_buffer_size = 1024

        self._ip_addr = ip_addr
        self._ip_port = ip_port

        self._escseq = '\n'
        self._codefmt = 'utf-8'

        self._logger = logger.create_tagged_trace_logger(
            '%s:%s' % (self._ip_addr, self._ip_port))

        self._socket = None

    def _connect_socket(self):
        """Init and Connect to socket."""
        try:
            self._socket = socket.create_connection(
                (self._ip_addr, self._ip_port), timeout=self._socket_timeout)
            resp = self._query('*IDN?')

            infmsg = 'Inst-ID: {}'.format(resp)
            self._logger.debug(infmsg)

            infmsg = 'Opened Socket connection to {}:{} with handle {}.'.format(
                repr(self._ip_addr), repr(self._ip_port), repr(self._socket))
            self._logger.debug(infmsg)

        except socket.timeout:
            errmsg = 'Socket timeout while connecting to instrument.'
            self._logger.exception(errmsg)
            raise SocketInstrumentError(errmsg)

        except socket.error:
            errmsg = 'Socket error while connecting to instrument.'
            self._logger.exception(errmsg)
            raise SocketInstrumentError(errmsg)

    def _send(self, cmd):
        """Send command via Socket.

        Args:
            cmd: Command to send,
                Type, Str.
        """
        cmd_es = cmd + self._escseq

        try:
            self._socket.sendall(cmd_es.encode(self._codefmt))
            self._logger.debug('Sent %r to %r:%r.', cmd, self._ip_addr,
                               self._ip_port)

        except socket.timeout:
            errmsg = ('Socket timeout while sending command {} '
                      'to instrument.').format(repr(cmd))
            self._logger.exception(errmsg)
            raise SocketInstrumentError(errmsg)

        except socket.error:
            errmsg = ('Socket error while sending command {} '
                      'to instrument.').format(repr(cmd))
            self._logger.exception(errmsg)
            raise SocketInstrumentError(errmsg)

        except Exception as err:
            errmsg = ('Error {} while sending command {} '
                      'to instrument.').format(repr(cmd), repr(err))
            self._logger.exception(errmsg)
            raise

    def _recv(self):
        """Receive response via Socket.

        Returns:
            resp: Response from Instrument via Socket,
                Type, Str.
        """
        resp = ''

        try:
            while True:
                resp_tmp = self._socket.recv(self._socket_buffer_size)
                resp_tmp = resp_tmp.decode(self._codefmt)
                resp += resp_tmp
                if len(resp_tmp) < self._socket_buffer_size:
                    break

        except socket.timeout:
            errmsg = 'Socket timeout while receiving response from instrument.'
            self._logger.exception(errmsg)
            raise SocketInstrumentError(errmsg)

        except socket.error:
            errmsg = 'Socket error while receiving response from instrument.'
            self._logger.exception(errmsg)
            raise SocketInstrumentError(errmsg)

        except Exception as err:
            errmsg = ('Error {} while receiving response '
                      'from instrument').format(repr(err))
            self._logger.exception(errmsg)
            raise

        resp = resp.rstrip(self._escseq)

        self._logger.debug('Received %r from %r:%r.', resp, self._ip_addr,
                           self._ip_port)

        return resp

    def _close_socket(self):
        """Close Socket Instrument."""
        if not self._socket:
            return

        try:
            self._socket.shutdown(socket.SHUT_RDWR)
            self._socket.close()
            self._socket = None
            self._logger.debug('Closed Socket Instrument %r:%r.',
                               self._ip_addr, self._ip_port)

        except Exception as err:
            errmsg = 'Error {} while closing instrument.'.format(repr(err))
            self._logger.exception(errmsg)
            raise

    def _query(self, cmd):
        """query instrument via Socket.

        Args:
            cmd: Command to send,
                Type, Str.

        Returns:
            resp: Response from Instrument via Socket,
                Type, Str.
        """
        self._send(cmd + ';*OPC?')
        resp = self._recv()
        return resp
