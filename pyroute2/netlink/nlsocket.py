'''
Base netlink socket and marshal
===============================

All the netlink providers are derived from the socket
class, so they provide normal socket API, including
`getsockopt()`, `setsockopt()`, they can be used in
poll/select I/O loops etc.

asynchronous I/O
----------------

To run async reader thread, one should call
`NetlinkSocket.bind(async_cache=True)`. In that case
a background thread will be launched. The thread will
automatically collect all the messages and store
into a userspace buffer.

.. note::
    There is no need to turn on async I/O, if you
    don't plan to receive broadcast messages.

ENOBUF and async I/O
--------------------

When Netlink messages arrive faster than a program
reads then from the socket, the messages overflow
the socket buffer and one gets ENOBUF on `recv()`::

    ... self.recv(bufsize)
    error: [Errno 105] No buffer space available

One way to avoid ENOBUF, is to use async I/O. Then the
library not only reads and buffers all the messages, but
also re-prioritizes threads. Suppressing the parser
activity, the library increases the response delay, but
spares CPU to read and enqueue arriving messages as
fast, as it is possible.

With logging level DEBUG you can notice messages, that
the library started to calm down the parser thread::

    DEBUG:root:Packet burst: the reader thread priority
        is increased, beware of delays on netlink calls
        Counters: delta=25 qsize=25 delay=0.1

This state requires no immediate action, but just some
more attention. When the delay between messages on the
parser thread exceeds 1 second, DEBUG messages become
WARNING ones::

    WARNING:root:Packet burst: the reader thread priority
        is increased, beware of delays on netlink calls
        Counters: delta=2525 qsize=213536 delay=3

This state means, that almost all the CPU resources are
dedicated to the reader thread. It doesn't mean, that
the reader thread consumes 100% CPU -- it means, that the
CPU is reserved for the case of more intensive bursts. The
library will return to the normal state only when the
broadcast storm will be over, and then the CPU will be
100% loaded with the parser for some time, when it will
process all the messages queued so far.

when async I/O doesn't help
---------------------------

Sometimes, even turning async I/O doesn't fix ENOBUF.
Mostly it means, that in this particular case the Python
performance is not enough even to read and store the raw
data from the socket. There is no workaround for such
cases, except of using something *not* Python-based.

One can still play around with SO_RCVBUF socket option,
but it doesn't help much. So keep it in mind, and if you
expect massive broadcast Netlink storms, perform stress
testing prior to deploy a solution in the production.

classes
-------
'''

import collections
import errno
import logging
import os
import random
import select
import struct
import threading
import time
import warnings
from queue import Queue
from socket import MSG_DONTWAIT, SO_RCVBUF, SO_SNDBUF, SOCK_DGRAM, SOL_SOCKET

from pyroute2 import config
from pyroute2.common import DEFAULT_RCVBUF, AddrPool
from pyroute2.config import AF_NETLINK
from pyroute2.netlink import (
    NETLINK_ADD_MEMBERSHIP,
    NETLINK_DROP_MEMBERSHIP,
    NETLINK_EXT_ACK,
    NETLINK_GENERIC,
    NETLINK_GET_STRICT_CHK,
    NETLINK_LISTEN_ALL_NSID,
    NLM_F_ACK,
    NLM_F_ACK_TLVS,
    NLM_F_DUMP,
    NLM_F_DUMP_INTR,
    NLM_F_MULTI,
    NLM_F_REQUEST,
    NLMSG_DONE,
    NLMSG_ERROR,
    SOL_NETLINK,
    mtypes,
    nlmsg,
    nlmsgerr,
)
from pyroute2.netlink.exceptions import (
    ChaoticException,
    NetlinkDecodeError,
    NetlinkDumpInterrupted,
    NetlinkError,
    NetlinkHeaderDecodeError,
)

log = logging.getLogger(__name__)
Stats = collections.namedtuple('Stats', ('qsize', 'delta', 'delay'))

NL_BUFSIZE = 32768


class Enough:
    pass


class CompileContext:
    def __init__(self, netlink_socket):
        self.netlink_socket = netlink_socket
        self.netlink_socket.compiled = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self.netlink_socket.compiled = None


class Marshal:
    '''
    Generic marshalling class
    '''

    msg_map = {}
    type_offset = 4
    type_format = 'H'
    error_type = NLMSG_ERROR
    debug = False

    def __init__(self):
        self.lock = threading.Lock()
        # one marshal instance can be used to parse one
        # message at once
        self.msg_map = self.msg_map or {}
        self.defragmentation = {}

    def parse(self, data, seq=None, callback=None):
        '''
        Parse string data.

        At this moment all transport, except of the native
        Netlink is deprecated in this library, so we should
        not support any defragmentation on that level
        '''
        offset = 0
        result = []
        # there must be at least one header in the buffer,
        # 'IHHII' == 16 bytes
        while offset <= len(data) - 16:
            # pick type and length
            (length,) = struct.unpack_from('I', data, offset)
            if length == 0:
                break
            error = None
            (msg_type,) = struct.unpack_from(
                self.type_format, data, offset + self.type_offset
            )
            if msg_type == self.error_type:
                code = abs(struct.unpack_from('i', data, offset + 16)[0])
                if code > 0:
                    error = NetlinkError(code)

            msg_class = self.msg_map.get(msg_type, nlmsg)
            msg = msg_class(data, offset=offset)

            if msg_type in (NLMSG_DONE, NLMSG_ERROR):
                # get flags
                flags = struct.unpack_from('H', data, offset + 6)[0]
                if flags & NLM_F_ACK_TLVS:
                    msg = nlmsgerr(data, offset=offset)
            try:
                msg.decode()
                if isinstance(msg, nlmsgerr):
                    error = NetlinkError(
                        abs(msg['error']), msg.get_attr('NLMSGERR_ATTR_MSG')
                    )

                msg['header']['error'] = error
                # try to decode encapsulated error message
                if error is not None:
                    enc_type = struct.unpack_from('H', data, offset + 24)[0]
                    enc_class = self.msg_map.get(enc_type, nlmsg)
                    enc = enc_class(data, offset=offset + 20)
                    enc.decode()
                    msg['header']['errmsg'] = enc
                if callback and seq == msg['header']['sequence_number']:
                    if callback(msg):
                        offset += msg.length
                        continue
            except NetlinkHeaderDecodeError as e:
                # in the case of header decoding error,
                # create an empty message
                msg = nlmsg()
                msg['header']['error'] = e
            except NetlinkDecodeError as e:
                msg['header']['error'] = e

            mtype = msg['header'].get('type', None)
            if mtype in (1, 2, 3, 4):
                msg['event'] = mtypes.get(mtype, 'none')
            self.fix_message(msg)
            offset += msg.length
            result.append(msg)

        return result

    def fix_message(self, msg):
        pass


# 8<-----------------------------------------------------------
# Singleton, containing possible modifiers to the NetlinkSocket
# bind() call.
#
# Normally, you can open only one netlink connection for one
# process, but there is a hack. Current PID_MAX_LIMIT is 2^22,
# so we can use the rest to modify the pid field.
#
# See also libnl library, lib/socket.c:generate_local_port()
sockets = AddrPool(minaddr=0x0, maxaddr=0x3FF, reverse=True)
# 8<-----------------------------------------------------------


class LockProxy:
    def __init__(self, factory, key):
        self.factory = factory
        self.refcount = 0
        self.key = key
        self.internal = threading.Lock()
        self.lock = factory.klass()

    def acquire(self, *argv, **kwarg):
        with self.internal:
            self.refcount += 1
            return self.lock.acquire()

    def release(self):
        with self.internal:
            self.refcount -= 1
            if (self.refcount == 0) and (self.key != 0):
                try:
                    del self.factory.locks[self.key]
                except KeyError:
                    pass
            return self.lock.release()

    def __enter__(self):
        self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


class LockFactory:
    def __init__(self, klass=threading.RLock):
        self.klass = klass
        self.locks = {0: LockProxy(self, 0)}

    def __enter__(self):
        self.locks[0].acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.locks[0].release()

    def __getitem__(self, key):
        if key is None:
            key = 0
        if key not in self.locks:
            self.locks[key] = LockProxy(self, key)
        return self.locks[key]

    def __delitem__(self, key):
        del self.locks[key]


class NetlinkSocketBase:
    '''
    Generic netlink socket
    '''

    def __init__(
        self,
        family=NETLINK_GENERIC,
        port=None,
        pid=None,
        fileno=None,
        sndbuf=1048576,
        rcvbuf=1048576,
        all_ns=False,
        async_qsize=None,
        nlm_generator=None,
        target='localhost',
        ext_ack=False,
        strict_check=False,
    ):
        # 8<-----------------------------------------
        self.config = {
            'family': family,
            'port': port,
            'pid': pid,
            'fileno': fileno,
            'sndbuf': sndbuf,
            'rcvbuf': rcvbuf,
            'all_ns': all_ns,
            'async_qsize': async_qsize,
            'target': target,
            'nlm_generator': nlm_generator,
            'ext_ack': ext_ack,
            'strict_check': strict_check,
        }
        # 8<-----------------------------------------
        self.addr_pool = AddrPool(minaddr=0x000000FF, maxaddr=0x0000FFFF)
        self.epid = None
        self.port = 0
        self.fixed = True
        self.family = family
        self._fileno = fileno
        self._sndbuf = sndbuf
        self._rcvbuf = rcvbuf
        self.backlog = {0: []}
        self.error_deque = collections.deque(maxlen=1000)
        self.callbacks = []  # [(predicate, callback, args), ...]
        self.pthread = None
        self.closed = False
        self.compiled = None
        self.uname = config.uname
        self.target = target
        self.capabilities = {
            'create_bridge': config.kernel > [3, 2, 0],
            'create_bond': config.kernel > [3, 2, 0],
            'create_dummy': True,
            'provide_master': config.kernel[0] > 2,
        }
        self.backlog_lock = threading.Lock()
        self.read_lock = threading.Lock()
        self.sys_lock = threading.RLock()
        self.change_master = threading.Event()
        self.lock = LockFactory()
        self._sock = None
        self._ctrl_read, self._ctrl_write = os.pipe()
        if async_qsize is None:
            async_qsize = config.async_qsize
        self.async_qsize = async_qsize
        if nlm_generator is None:
            nlm_generator = config.nlm_generator
        self.nlm_generator = nlm_generator
        self.buffer_queue = Queue(maxsize=async_qsize)
        self.qsize = 0
        self.log = []
        self.get_timeout = 30
        self.get_timeout_exception = None
        self.all_ns = all_ns
        self.ext_ack = ext_ack
        self.strict_check = strict_check
        if pid is None:
            self.pid = os.getpid() & 0x3FFFFF
            self.port = port
            self.fixed = self.port is not None
        elif pid == 0:
            self.pid = os.getpid()
        else:
            self.pid = pid
        # 8<-----------------------------------------
        self.groups = 0
        self.marshal = Marshal()
        # 8<-----------------------------------------
        if not nlm_generator:
            warnings.warn(
                '\n\nplease note: some netlink socket methods will soon '
                'return iterators instead of lists.\nsee more: '
                'https://github.com/svinota/pyroute2/discussions/972\n',
                UserWarning,
            )

            def nlm_request(*argv, **kwarg):
                return tuple(self._genlm_request(*argv, **kwarg))

            def get(*argv, **kwarg):
                return tuple(self._genlm_get(*argv, **kwarg))

            self._genlm_request = self.nlm_request
            self._genlm_get = self.get

            self.nlm_request = nlm_request
            self.get = get

            def nlm_request_batch(*argv, **kwarg):
                return tuple(self._genlm_request_batch(*argv, **kwarg))

            self._genlm_request_batch = self.nlm_request_batch
            self.nlm_request_batch = nlm_request_batch

        # Set defaults
        self.post_init()

    def post_init(self):
        pass

    def clone(self):
        return type(self)(**self.config)

    def close(self, code=errno.ECONNRESET):
        if code > 0 and self.pthread:
            self.buffer_queue.put(
                struct.pack('IHHQIQQ', 28, 2, 0, 0, code, 0, 0)
            )
        try:
            os.close(self._ctrl_write)
            os.close(self._ctrl_read)
        except OSError:
            # ignore the case when it is closed already
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def release(self):
        warnings.warn('deprecated, use close() instead', DeprecationWarning)
        self.close()

    def register_callback(self, callback, predicate=lambda x: True, args=None):
        '''
        Register a callback to run on a message arrival.

        Callback is the function that will be called with the
        message as the first argument. Predicate is the optional
        callable object, that returns True or False. Upon True,
        the callback will be called. Upon False it will not.
        Args is a list or tuple of arguments.

        Simplest example, assume ipr is the IPRoute() instance::

            # create a simplest callback that will print messages
            def cb(msg):
                print(msg)

            # register callback for any message:
            ipr.register_callback(cb)

        More complex example, with filtering::

            # Set object's attribute after the message key
            def cb(msg, obj):
                obj.some_attr = msg["some key"]

            # Register the callback only for the loopback device, index 1:
            ipr.register_callback(cb,
                                  lambda x: x.get('index', None) == 1,
                                  (self, ))

        Please note: you do **not** need to register the default 0 queue
        to invoke callbacks on broadcast messages. Callbacks are
        iterated **before** messages get enqueued.
        '''
        if args is None:
            args = []
        self.callbacks.append((predicate, callback, args))

    def unregister_callback(self, callback):
        '''
        Remove the first reference to the function from the callback
        register
        '''
        cb = tuple(self.callbacks)
        for cr in cb:
            if cr[1] == callback:
                self.callbacks.pop(cb.index(cr))
                return

    def register_policy(self, policy, msg_class=None):
        '''
        Register netlink encoding/decoding policy. Can
        be specified in two ways:
        `nlsocket.register_policy(MSG_ID, msg_class)`
        to register one particular rule, or
        `nlsocket.register_policy({MSG_ID1: msg_class})`
        to register several rules at once.
        E.g.::

            policy = {RTM_NEWLINK: ifinfmsg,
                      RTM_DELLINK: ifinfmsg,
                      RTM_NEWADDR: ifaddrmsg,
                      RTM_DELADDR: ifaddrmsg}
            nlsocket.register_policy(policy)

        One can call `register_policy()` as many times,
        as one want to -- it will just extend the current
        policy scheme, not replace it.
        '''
        if isinstance(policy, int) and msg_class is not None:
            policy = {policy: msg_class}

        assert isinstance(policy, dict)
        for key in policy:
            self.marshal.msg_map[key] = policy[key]

        return self.marshal.msg_map

    def unregister_policy(self, policy):
        '''
        Unregister policy. Policy can be:

            - int -- then it will just remove one policy
            - list or tuple of ints -- remove all given
            - dict -- remove policies by keys from dict

        In the last case the routine will ignore dict values,
        it is implemented so just to make it compatible with
        `get_policy_map()` return value.
        '''
        if isinstance(policy, int):
            policy = [policy]
        elif isinstance(policy, dict):
            policy = list(policy)

        assert isinstance(policy, (tuple, list, set))

        for key in policy:
            del self.marshal.msg_map[key]

        return self.marshal.msg_map

    def get_policy_map(self, policy=None):
        '''
        Return policy for a given message type or for all
        message types. Policy parameter can be either int,
        or a list of ints. Always return dictionary.
        '''
        if policy is None:
            return self.marshal.msg_map

        if isinstance(policy, int):
            policy = [policy]

        assert isinstance(policy, (list, tuple, set))

        ret = {}
        for key in policy:
            ret[key] = self.marshal.msg_map[key]

        return ret

    def sendto(self, *argv, **kwarg):
        return self._sendto(*argv, **kwarg)

    def recv(self, *argv, **kwarg):
        if self.pthread is not None:
            data_in = self.buffer_queue.get()
            if isinstance(data_in, Exception):
                raise data_in
            return data_in
        return self._sock.recv(*argv, **kwarg)

    def recv_into(self, data, *argv, **kwarg):
        if self.pthread is not None:
            data_in = self.buffer_queue.get()
            if isinstance(data, Exception):
                raise data_in
            data[:] = data_in
            return len(data_in)
        return self._sock.recv_into(data, *argv, **kwarg)

    def recv_all(self, bufsize=NL_BUFSIZE):
        buffers = []
        while True:
            try:
                buffers.append(self._sock.recv(bufsize, MSG_DONTWAIT))
            except BlockingIOError:
                return buffers

    def recv_all_into(self, buffer):
        count = 0
        while True:
            page = buffer.get_free_page()
            try:
                count += self._sock.recv_into(page.view, 0, MSG_DONTWAIT)
            except BlockingIOError:
                page.free()
                return count

    def compile(self):
        return CompileContext(self)

    def _send_batch(self, msgs, addr=(0, 0)):
        with self.backlog_lock:
            for msg in msgs:
                self.backlog[msg['header']['sequence_number']] = []
        # We have locked the message locks in the caller already.
        data = bytearray()
        for msg in msgs:
            if not isinstance(msg, nlmsg):
                msg_class = self.marshal.msg_map[msg['header']['type']]
                msg = msg_class(msg)
            msg.reset()
            msg.encode()
            data += msg.data
        if self.compiled is not None:
            return self.compiled.append(data)
        self._sock.sendto(data, addr)

    def put(
        self,
        msg,
        msg_type,
        msg_flags=NLM_F_REQUEST,
        addr=(0, 0),
        msg_seq=0,
        msg_pid=None,
    ):
        '''
        Construct a message from a dictionary and send it to
        the socket. Parameters:

            - msg -- the message in the dictionary format
            - msg_type -- the message type
            - msg_flags -- the message flags to use in the request
            - addr -- `sendto()` addr, default `(0, 0)`
            - msg_seq -- sequence number to use
            - msg_pid -- pid to use, if `None` -- use os.getpid()

        Example::

            s = IPRSocket()
            s.bind()
            s.put({'index': 1}, RTM_GETLINK)
            s.get()
            s.close()

        Please notice, that the return value of `s.get()` can be
        not the result of `s.put()`, but any broadcast message.
        To fix that, use `msg_seq` -- the response must contain the
        same `msg['header']['sequence_number']` value.
        '''
        if msg_seq != 0:
            self.lock[msg_seq].acquire()
        try:
            if msg_seq not in self.backlog:
                self.backlog[msg_seq] = []
            if not isinstance(msg, nlmsg):
                msg_class = self.marshal.msg_map[msg_type]
                msg = msg_class(msg)
            if msg_pid is None:
                msg_pid = self.epid or os.getpid()
            msg['header']['type'] = msg_type
            msg['header']['flags'] = msg_flags
            msg['header']['sequence_number'] = msg_seq
            msg['header']['pid'] = msg_pid
            self.sendto_gate(msg, addr)
        except:
            raise
        finally:
            if msg_seq != 0:
                self.lock[msg_seq].release()

    def sendto_gate(self, msg, addr):
        msg.reset()
        msg.encode()
        if self.compiled is not None:
            return self.compiled.append(msg.data)
        return self._sock.sendto(msg.data, addr)

    def load_backlog(self, chunk, msg_seq=0, callback=None):
        '''
        Parse a chunk and load messages into the backlog
        '''
        seqs = set()
        for msg in self.marshal.parse(chunk, msg_seq, callback):
            seq = msg['header']['sequence_number']
            msg['header']['target'] = self.target
            msg['header']['stats'] = Stats(0, 0, 0)
            if seq not in self.backlog:
                seq = 0
            seqs.add(seq)
            self.backlog[seq].append(msg)
            for cr in self.callbacks:
                try:
                    if cr[0](msg):
                        cr[1](msg, *cr[2])
                except Exception as e:
                    log.warning(f'Callback fail: {cr} -> {e}')
        for seq in seqs:
            os.write(self._ctrl_write, b'\x01')

    def fetch_backlog(self, msg_seq=0, terminate=None, noraise=False):
        '''
        Fetch and yield already parsed messages from the backlog.
        '''
        enough = False
        tmsg = None
        with self.lock[msg_seq]:
            ret = self.backlog[msg_seq]
            self.backlog[msg_seq] = []

        if ret:
            os.read(self._ctrl_read, 1)

        # Collect messages up to the terminator.
        # Terminator conditions:
        #  * NLMSG_ERROR != 0
        #  * NLMSG_DONE
        #  * terminate() function (if defined)
        #  * not NLM_F_MULTI
        for index in range(len(ret)):
            msg = ret[index]

            if msg_seq == 0:
                yield msg
                continue

            # If there is an error, raise exception
            if msg['header']['error'] is not None and not noraise:
                with self.lock[msg_seq]:
                    # reschedule all the remaining messages, including
                    # errors and acks, into a separate deque
                    self.error_deque.extend(ret[index + 1 :])
                    self.error_deque.extend(self.backlog[msg_seq])
                    del self.backlog[msg_seq]
                raise msg['header']['error']

            # If it is a terminator message, say "enough"
            # and requeue all the rest into the backlog zero
            if callable(terminate):
                tmsg = terminate(msg)
                if isinstance(tmsg, nlmsg):
                    yield msg

            if (msg['header']['type'] == NLMSG_DONE) or tmsg:
                enough = True

            # If it is just a normal message, append it to
            # the response
            if not enough:
                # finish the loop on single messages
                if not msg['header']['flags'] & NLM_F_MULTI:
                    enough = True
                yield msg

            # Enough is enough, requeue the rest and delete
            # our backlog
            if enough:
                with self.lock[0]:
                    self.backlog[0].extend(ret[index + 1 :])
                    del self.backlog[msg_seq]
                break

        if enough or msg_seq == 0:
            yield Enough

    def get(
        self,
        bufsize=DEFAULT_RCVBUF,
        msg_seq=0,
        terminate=None,
        callback=None,
        noraise=False,
    ):
        '''
        Get parsed messages list. If `msg_seq` is given, return
        only messages with that `msg['header']['sequence_number']`,
        saving all other messages into `self.backlog`.

        The routine is thread-safe.

        The `bufsize` parameter can be:

            - -1: bufsize will be calculated from the first 4 bytes of
                the network data
            - 0: bufsize will be calculated from SO_RCVBUF sockopt
            - int >= 0: just a bufsize

        If `noraise` is true, error messages will be treated as any
        other message.
        '''
        while True:
            with self.read_lock:
                fdlist = (self.fileno(), self._ctrl_read)
                rlist, _, _ = select.select(fdlist, [], fdlist)

                if self.fileno() in rlist:
                    chunk = self.recv(32768)
                    self.load_backlog(chunk, msg_seq, callback)
                    continue

            for msg in self.fetch_backlog(msg_seq, terminate, noraise):
                if msg is Enough:
                    return
                yield msg

    def nlm_request_batch(self, msgs, noraise=False):
        """
        This function is for messages which are expected to have side effects.
        Do not blindly retry in case of errors as this might duplicate them.
        """
        expected_responses = []
        acquired = 0
        seqs = self.addr_pool.alloc_multi(len(msgs))
        try:
            for seq in seqs:
                self.lock[seq].acquire()
                acquired += 1
            for seq, msg in zip(seqs, msgs):
                msg['header']['sequence_number'] = seq
                if 'pid' not in msg['header']:
                    msg['header']['pid'] = self.epid or os.getpid()
                if (msg['header']['flags'] & NLM_F_ACK) or (
                    msg['header']['flags'] & NLM_F_DUMP
                ):
                    expected_responses.append(seq)
            self._send_batch(msgs)
            if self.compiled is not None:
                for data in self.compiled:
                    yield data
            else:
                for seq in expected_responses:
                    for msg in self.get(msg_seq=seq, noraise=noraise):
                        if msg['header']['flags'] & NLM_F_DUMP_INTR:
                            # Leave error handling to the caller
                            raise NetlinkDumpInterrupted()
                        yield msg
        finally:
            # Release locks in reverse order.
            for seq in seqs[acquired - 1 :: -1]:
                self.lock[seq].release()

            with self.backlog_lock:
                for seq in seqs:
                    # Clear the backlog. We may have raised an error
                    # causing the backlog to not be consumed entirely.
                    if seq in self.backlog:
                        del self.backlog[seq]
                    self.addr_pool.free(seq, ban=0xFF)

    def nlm_request(
        self,
        msg,
        msg_type,
        msg_flags=NLM_F_REQUEST | NLM_F_DUMP,
        terminate=None,
        callback=None,
    ):

        msg_seq = self.addr_pool.alloc()
        defer = None
        with self.lock[msg_seq]:
            retry_count = 0
            try:
                while True:
                    try:
                        self.put(msg, msg_type, msg_flags, msg_seq=msg_seq)
                        if self.compiled is not None:
                            for data in self.compiled:
                                yield data
                        else:
                            for msg in self.get(
                                msg_seq=msg_seq,
                                terminate=terminate,
                                callback=callback,
                            ):
                                # analyze the response for effects to be
                                # deferred
                                if (
                                    defer is None
                                    and msg['header']['flags']
                                    & NLM_F_DUMP_INTR
                                ):
                                    defer = NetlinkDumpInterrupted()
                                yield msg
                        break
                    except NetlinkError as e:
                        if e.code != errno.EBUSY:
                            raise
                        if retry_count >= 30:
                            raise
                        log.warning('Error 16, retry {}.'.format(retry_count))
                        time.sleep(0.3)
                        retry_count += 1
                        continue
                    except Exception:
                        raise
            finally:
                # Ban this msg_seq for 0xff rounds
                #
                # It's a long story. Modern kernels for RTM_SET.*
                # operations always return NLMSG_ERROR(0) == success,
                # even not setting NLM_F_MULTI flag on other response
                # messages and thus w/o any NLMSG_DONE. So, how to detect
                # the response end? One can not rely on NLMSG_ERROR on
                # old kernels, but we have to support them too. Ty, we
                # just ban msg_seq for several rounds, and NLMSG_ERROR,
                # being received, will become orphaned and just dropped.
                #
                # Hack, but true.
                self.addr_pool.free(msg_seq, ban=0xFF)
            if defer is not None:
                raise defer


class BatchAddrPool:
    def alloc(self, *argv, **kwarg):
        return 0

    def free(self, *argv, **kwarg):
        pass


class BatchBacklogQueue(list):
    def append(self, *argv, **kwarg):
        pass

    def pop(self, *argv, **kwarg):
        pass


class BatchBacklog(dict):
    def __getitem__(self, key):
        return BatchBacklogQueue()

    def __setitem__(self, key, value):
        pass

    def __delitem__(self, key):
        pass


class BatchSocket(NetlinkSocketBase):
    def post_init(self):

        self.backlog = BatchBacklog()
        self.addr_pool = BatchAddrPool()
        self._sock = None
        self.reset()

    def reset(self):
        self.batch = bytearray()

    def nlm_request(
        self,
        msg,
        msg_type,
        msg_flags=NLM_F_REQUEST | NLM_F_DUMP,
        terminate=None,
        callback=None,
    ):
        msg_seq = self.addr_pool.alloc()
        msg_pid = self.epid or os.getpid()

        msg['header']['type'] = msg_type
        msg['header']['flags'] = msg_flags
        msg['header']['sequence_number'] = msg_seq
        msg['header']['pid'] = msg_pid
        msg.data = self.batch
        msg.offset = len(self.batch)
        msg.encode()
        return []

    def get(self, *argv, **kwarg):
        pass


class NetlinkSocket(NetlinkSocketBase):
    def post_init(self):
        # recreate the underlying socket
        with self.sys_lock:
            if self._sock is not None:
                self._sock.close()
            self._sock = config.SocketBase(
                AF_NETLINK, SOCK_DGRAM, self.family, self._fileno
            )
            self.setsockopt(SOL_SOCKET, SO_SNDBUF, self._sndbuf)
            self.setsockopt(SOL_SOCKET, SO_RCVBUF, self._rcvbuf)
            if self.ext_ack:
                self.setsockopt(SOL_NETLINK, NETLINK_EXT_ACK, 1)
            if self.all_ns:
                self.setsockopt(SOL_NETLINK, NETLINK_LISTEN_ALL_NSID, 1)
            if self.strict_check:
                self.setsockopt(SOL_NETLINK, NETLINK_GET_STRICT_CHK, 1)

    def __getattr__(self, attr):
        if attr in (
            'getsockname',
            'getsockopt',
            'makefile',
            'setsockopt',
            'setblocking',
            'settimeout',
            'gettimeout',
            'shutdown',
            'recvfrom',
            'recvfrom_into',
            'fileno',
        ):
            return getattr(self._sock, attr)
        elif attr in ('_sendto', '_recv', '_recv_into'):
            return getattr(self._sock, attr.lstrip("_"))

        raise AttributeError(attr)

    def bind(self, groups=0, pid=None, **kwarg):
        '''
        Bind the socket to given multicast groups, using
        given pid.

            - If pid is None, use automatic port allocation
            - If pid == 0, use process' pid
            - If pid == <int>, use the value instead of pid
        '''
        if pid is not None:
            self.port = 0
            self.fixed = True
            self.pid = pid or os.getpid()

        if 'async' in kwarg:
            # FIXME
            # raise deprecation error after 0.5.3
            #
            log.warning(
                'use "async_cache" instead of "async", '
                '"async" is a keyword from Python 3.7'
            )

        self.groups = groups
        # if we have pre-defined port, use it strictly
        if self.fixed:
            self.epid = self.pid + (self.port << 22)
            self._sock.bind((self.epid, self.groups))
        else:
            for port in range(1024):
                try:
                    self.port = port
                    self.epid = self.pid + (self.port << 22)
                    self._sock.bind((self.epid, self.groups))
                    break
                except Exception:
                    # create a new underlying socket -- on kernel 4
                    # one failed bind() makes the socket useless
                    self.post_init()
            else:
                raise KeyError('no free address available')

    def add_membership(self, group):
        self.setsockopt(SOL_NETLINK, NETLINK_ADD_MEMBERSHIP, group)

    def drop_membership(self, group):
        self.setsockopt(SOL_NETLINK, NETLINK_DROP_MEMBERSHIP, group)

    def close(self, code=errno.ECONNRESET):
        '''
        Correctly close the socket and free all resources.
        '''
        with self.sys_lock:
            if self.closed:
                return
            self.closed = True

        if self.pthread:
            os.write(self._ctrl_write, b'exit')
            self.pthread.join()
        super(NetlinkSocket, self).close(code=code)

        # Common shutdown procedure
        self._sock.close()


class ChaoticNetlinkSocket(NetlinkSocket):

    success_rate = 1

    def __init__(self, *argv, **kwarg):
        self.success_rate = kwarg.pop('success_rate', 0.7)
        super(ChaoticNetlinkSocket, self).__init__(*argv, **kwarg)

    def get(self, *argv, **kwarg):
        if random.random() > self.success_rate:
            raise ChaoticException()
        return super(ChaoticNetlinkSocket, self).get(*argv, **kwarg)
