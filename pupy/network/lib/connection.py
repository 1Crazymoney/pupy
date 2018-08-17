# -*- coding: utf-8 -*-

__all__ = ('SyncRequestDispatchQueue', 'PupyConnection',
           'PupyConnectionThread')

import time
import traceback

from rpyc.core import Connection, consts, brine
from threading import Thread, Lock, current_thread
from Queue import Queue, Full, Empty

from network.lib import getLogger

logger = getLogger('pconn')
synclogger = getLogger('sync')
syncqueuelogger = getLogger('syncqueue')

from network.lib.ack import Ack
from network.lib.buffer import Buffer

############# Monkeypatch brine to be buffer firendly #############


def stream_dump(obj):
    buf = Buffer()
    brine._dump(obj, buf)
    return buf


@brine.register(brine._dump_registry, str)
def _dump_str_to_buffer(obj, stream):
    obj_len = len(obj)
    if obj_len == 0:
        stream.append(brine.TAG_EMPTY_STR)
        return
    elif obj_len < 5:
        if obj_len == 1:
            stream.append(brine.TAG_STR1)
        elif obj_len == 2:
            stream.append(brine.TAG_STR2)
        elif obj_len == 3:
            stream.append(brine.TAG_STR3)
        elif obj_len == 4:
            stream.append(brine.TAG_STR4)
    else:
        if obj_len < 256:
            stream.append(brine.TAG_STR_L1 + brine.I1.pack(obj_len))
        else:
            stream.append(brine.TAG_STR_L4 + brine.I4.pack(obj_len))

    stream.append(obj)


@brine.register(brine._dump_registry, Buffer)
def _dump_buffer_to_buffer(obj, stream):
    stream.append(brine.TAG_STR_L4 + brine.I4.pack(len(obj)))
    stream.append(obj)


brine.simple_types = list(brine.simple_types)
brine.simple_types.append(Buffer)
brine.dump = stream_dump

################################################################


class SyncRequestDispatchQueue(object):
    MAX_TASK_ACK_TIME = 0.5

    instance = None

    def __init__(self):
        self._queue = Queue(maxsize=256)
        self._workers = 1
        self._pending_workers = 0
        self._workers_lock = Lock()
        self._primary_worker = Thread(
            target=self._dispatch_request_worker,
            name="Primary SyncQueue Dispatcher"
        )
        self._primary_worker.daemon = True
        self._primary_worker.start()
        self._closed = False
        self._max_workers = 1
        self._promise = 0

    @staticmethod
    def get_queue():
        if not SyncRequestDispatchQueue.instance:
            SyncRequestDispatchQueue.instance = SyncRequestDispatchQueue()

        return SyncRequestDispatchQueue.instance

    def _dispatch_request_worker(self):

        name = current_thread().name

        if __debug__:
            syncqueuelogger.debug('New Worker({})'.format(name))

        task = self._queue.get()
        while task and not self._closed:
            ack, on_error, func, args = task

            with self._workers_lock:
                ack.set()
                self._pending_workers += 1

            try:
                if __debug__:
                    syncqueuelogger.debug('Process task({}) - start'.format(name))

                func(*args)

                if __debug__:
                    syncqueuelogger.debug('Process task({}) - complete'.format(name))

            except Exception, e:
                if __debug__:
                    syncqueuelogger.debug(
                        'Process task({}) - exception: func={} args={} exc:{}/{}'.format(
                            name, func, args, type(e), e))

                on_error(e)

            del func, args

            with self._workers_lock:
                self._queue.task_done()
                self._pending_workers -= 1

            again = False
            task = None

            try:
                task = self._queue.get_nowait()
                if __debug__:
                    syncqueuelogger.debug('Task acquired({}) (no wait)'.format(name))

            except Empty:
                with self._workers_lock:
                    if not self._closed and (self._promise or self._workers <= self._pending_workers + 1):
                        again = True
                    else:
                        self._workers -= 1

            if again:
                if __debug__:
                    syncqueuelogger.debug('Wait for task to be queued({})'.format(name))

                task = self._queue.get()

                if __debug__:
                    syncqueuelogger.debug('Task acquired({})'.format(name))

        if __debug__:
            if not task:
                syncqueuelogger.debug('Worker({}) closed by explicit request'.format(name))

    def __call__(self, on_error, func, *args):
        with self._workers_lock:
            self._promise += 1

        ack = Ack()
        queued = False

        while not ack.is_set():
            if not queued:
                try:
                    if __debug__:
                        syncqueuelogger.debug('Queue task')

                    self._queue.put_nowait((ack, on_error, func, args))

                    if __debug__:
                        syncqueuelogger.debug('Task queued')

                    with self._workers_lock:
                        self._promise -= 1

                    queued = True

                except Full:
                    if __debug__:
                        syncqueuelogger.debug(
                            'Task not queued - no empty slots. Launch new worker'.format(self,
                                                                                         self._pending_workers))

                        pass

            if not queued or not ack.wait(timeout=self.MAX_TASK_ACK_TIME, probe=0.1):
                with self._workers_lock:
                    self._workers += 1
                    if self._workers > self._max_workers:
                        self._max_workers = self._workers

                        if __debug__:
                            syncqueuelogger.info(
                                'Max workers({}): {}'.format(self, self._max_workers))

                thread = Thread(
                    target=self._dispatch_request_worker,
                    name="SyncQueue Dispatcher"
                )
                thread.daemon = True
                thread.start()

    def close(self):
        self._closed = True
        while True:
            try:
                self._queue.put_nowait(None)
            except Full:
                break


class PupyConnection(Connection):
    __slots__ = (
        '_close_lock', '_sync_events_lock',
        '_async_events_lock', '_sync_events',
        '_sync_raw_replies', '_sync_raw_exceptions',
        '_last_recv', '_ping', '_ping_timeout',
        '_serve_timeout', '_last_ping', '_default_serve_timeout',
        '_queue', '_config', '_timer_event', '_timer_event_last'
    )

    def __repr__(self):
        return 'PC:{}'.format(self._config['connid'])

    def __init__(self, pupy_srv, *args, **kwargs):
        self._close_lock = Lock()
        self._sync_events_lock = Lock()
        self._async_events_lock = Lock()

        self._sync_events = {}
        self._sync_raw_replies = {}
        self._sync_raw_exceptions = {}

        self._last_recv = time.time()
        self._ping = False
        self._ping_timeout = 30
        self._serve_timeout = 10
        self._last_ping = None
        self._default_serve_timeout = 5
        self._queue = SyncRequestDispatchQueue.get_queue()

        self._timer_event = None
        self._timer_event_last = None

        if 'ping' in kwargs:
            ping = kwargs.pop('ping')
        else:
            ping = None

        if 'timeout' in kwargs:
            timeout = kwargs.pop('timeout')
        else:
            timeout = None

        if 'timer_event' in kwargs:
            self._timer_event = kwargs.pop('timer_event')

        if ping or timeout:
            self.set_pings(ping, timeout)

        kwargs['_lazy'] = True
        Connection.__init__(self, *args, **kwargs)
        if pupy_srv:
            self._local_root.pupy_srv = pupy_srv

        if 'config' in kwargs:
            self._config.update(kwargs['config'])

        next(self._seqcounter)

        logger.debug('New PupyConnection: ({})'.format(self))

    def _on_sync_request_exception(self, exc):
        if not isinstance(exc, EOFError):
            logger.exception('{}: {}'.format(self, exc))

        self.close()

    def consume(self):
        return self._channel.consume()

    def wake(self):
        self._channel.wake()

    def set_pings(self, ping=None, timeout=None):
        if ping is not None:
            try:
                self._serve_timeout = int(ping)
            except:
                self._serve_timeout = 10

                self._ping = ping and ping not in (
                    '0', '-1', 'N', 'n', 'false', 'False', 'no', 'No'
                )

            self._ping = bool(ping)

        if timeout:
            try:
                self._ping_timeout = int(timeout)
            except:
                self._ping_timeout = 2

        return self.get_pings()

    def get_pings(self):
        if self._ping:
            return self._serve_timeout, self._ping_timeout
        else:
            return None, None

    def sync_request(self, handler, *args):
        seq = self._send_request(handler, args)
        if __debug__:
            synclogger.debug('Sync request wait({}): {} / {}:{} {} ({})'.format(
                self, seq, *traceback.extract_stack()[-4]))

        self._sync_events[seq].wait()

        if __debug__:
            synclogger.debug(
                'Sync request wait({}): {} - complete'.format(self, seq))

        del self._sync_events[seq]

        if __debug__:
            synclogger.debug('Sync request process({}): {}'.format(self, seq))

        is_response = False
        is_exception = False

        with self._sync_events_lock:
            is_response = seq in self._sync_raw_replies
            is_exception = seq in self._sync_raw_exceptions

        if is_response:
            if __debug__:
                synclogger.debug(
                    'Dispatch sync reply({}): {} - start'.format(self, seq))

            Connection._dispatch_reply(
                self, seq, self._sync_raw_replies.pop(seq))

            if __debug__:
                synclogger.debug(
                    'Dispatch sync reply({}): {} - complete'.format(self, seq))

        if is_exception:
            if __debug__:
                synclogger.debug(
                    'Dispatch sync exception({}): {} - start'.format(self, seq))
                synclogger.debug(
                    'Dispatch sync exception({}): handler = {}({}) args = {}'.format(
                        self, seq,
                        self._HANDLERS[handler], handler,
                        repr(args)))

            Connection._dispatch_exception(
                self, seq, self._sync_raw_exceptions.pop(seq))

            if __debug__:
                synclogger.debug(
                    'Dispatch sync exception({}): {} - complete'.format(self, seq))

        if __debug__:
            synclogger.debug(
                'Sync request({}): {} - complete'.format(self, seq))

        if self.closed:
            raise EOFError(
                'Connection was closed, seq({}): {}'.format(self, seq))

        isexc, obj = self._sync_replies.pop(seq)
        if isexc:
            raise obj
        else:
            return obj

    def _send_request(self, handler, args, async=None):
        seq = next(self._seqcounter)
        if async:
            if __debug__:
                logger.debug('Async request({}): {}'.format(self, seq))

            self._async_callbacks[seq] = async
        else:
            if __debug__:
                synclogger.debug('Sync request({}): {}'.format(self, seq))

            self._sync_events[seq] = Ack()

        self._send(consts.MSG_REQUEST, seq, (handler, self._box(args)))

        if __debug__:
            synclogger.debug('Request submitted({}): {}'.format(self, seq))

        return seq

    def _async_request(self, handler, args=(), callback=(lambda a, b: None)):
        self._send_request(handler, args, async=callback)

    def _dispatch_reply(self, seq, raw):
        if __debug__:
            logger.debug('Dispatch reply({}): {} - start'.format(self, seq))

        self._last_recv = time.time()

        is_sync = False
        with self._async_events_lock:
            is_sync = seq not in self._async_callbacks

        if is_sync:
            self._sync_raw_replies[seq] = raw
            if __debug__:
                logger.debug(
                    'Dispatch sync reply({}): {} - pass'.format(self, seq))
            self._sync_events[seq].set()

        else:
            # We hope here that this request will not block x_x
            if __debug__:
                logger.debug(
                    'Dispatch async reply({}): {} - start'.format(self, seq))

            Connection._dispatch_reply(self, seq, raw)

            if __debug__:
                logger.debug(
                    'Dispatch async reply({}): {} - complete'.format(self, seq))

    def _dispatch_exception(self, seq, raw):
        if __debug__:
            logger.debug('Dispatch exception({}): {}'.format(self, seq))

        self._last_recv = time.time()

        is_sync = False
        with self._async_events_lock:
            is_sync = seq not in self._async_callbacks

        if is_sync:
            self._sync_raw_exceptions[seq] = raw
            if __debug__:
                logger.debug(
                    'Dispatch sync exception({}): {} - pass'.format(
                        self, seq))
            self._sync_events[seq].set()
        else:
            if __debug__:
                logger.debug(
                    'Dispatch async reply({}): {} - start'.format(self, seq))
            Connection._dispatch_exception(self, seq, raw)
            if __debug__:
                logger.debug(
                    'Dispatch async reply({}): {} - complete'.format(self, seq))

    def close(self, _catchall=True):
        with self._close_lock:
            if self._closed:
                return

            self._closed = True

        if __debug__:
            logger.debug('Connection({}) - close - start (at: {}:{} {}({}))'.format(
                self, *traceback.extract_stack()[-2]))

        try:
            self.buf_in.wake()

            self._async_request(consts.HANDLE_CLOSE)
        except EOFError, e:
            logger.info(
                'Connection({}) - close - notification failed '
                'because of EOF ({})'.format(self, e))

        except Exception:
            if not _catchall:
                raise
        finally:
            try:
                self._cleanup(_anyway=True)
            except Exception, e:
                if __debug__:
                    logger.debug('Cleanup exception({}): {}'.format(self, e))

                pass

        _sync_events = self._sync_events.keys()
        for lock in _sync_events:
            lock = self._sync_events.get(lock)
            if lock:
                lock.set()

        if __debug__:
            logger.debug('Connection({}) - closed'.format(self))

    @property
    def inactive(self):
        return time.time() - self._last_recv

    def serve(self, timeout=None):
        raise NotImplementedError('Serve method should not be used!')

    def _init_service_with_notify(self):
        self._init_service()

    def init(self, timeout=60):

        def check_timeout():
            now = time.time()

            logger.debug('Check timeout({}) - start'.format(self))

            while (time.time() - now < timeout) and not self._last_ping and not self.closed:
                time.sleep(1)

            if not self._last_ping:
                logger.info('Check timeout({}) - failed'.format(self))
                if not self.closed:
                    self.close()
            else:
                logger.debug('Check timeout({}) - ok'.format(self))

        t = Thread(
            target=check_timeout,
            name="PupyConnection({}) Timeout check".format(self)
        )
        t.daemon = True
        t.start()

        self._queue(
            self._on_sync_request_exception,
            self._init_service_with_notify)

    def loop(self):
        if __debug__:
            logger.debug('Serve loop({}) started'.format(self))

        if not self._timer_event_last:
            self._timer_event_last = time.time()

        while not self.closed:
            if self._timer_event:
                period, callback = self._timer_event

                if self._timer_event_last + period < time.time():
                    try:
                        callback()
                    except Exception, e:
                        logger.exception('Callback exception({}): {}: {}'.format(self,
                                                                                 type(e), e))

            try:
                data = self._serve()
                self._dispatch(data)
                continue

            except EOFError, e:
                logger.info('Serve loop({}) - EOF ({})'.format(self, e))

            except Exception, e:
                logger.exception(
                    'Exception({}): {}: {}'.format(self, type(e), e))

            break

        if __debug__:
            logger.debug('Serve loop({}) completed'.format(self))

        self.close()

    def _serve(self, timeout=None):
        ''' Check timeouts every serve cycle '''

        interval, ping_timeout = self.get_pings()

        if timeout is None:
            timeout = interval or self._default_serve_timeout

        now = time.time()
        mintimeout = timeout

        data = None

        _async_callbacks = self._async_callbacks.keys()
        for async_event_id in _async_callbacks:
            async_event = self._async_callbacks.get(async_event_id, None)
            if not async_event:
                continue

            if not hasattr(async_event, '_ttl') or not async_event._ttl:
                continue

            if async_event._ttl < now:
                raise EOFError('Async timeout! ({})'.format(self))

            etimeout = async_event._ttl - now

            if __debug__:
                logger.debug('Check timeouts: ({}) etimeout = {} / mintimeout = {} / ttl = {}'.format(
                    self, etimeout, mintimeout, async_event._ttl))

            if mintimeout is None or etimeout < mintimeout:
                mintimeout = etimeout

        timeout = mintimeout

        if __debug__:
            logger.debug('Serve({}): start / timeout = {} / interval = {} / ping = {} / {}'.format(
                self, timeout, interval, ping_timeout, self._last_ping))

        data = self._recv(timeout, wait_for_lock=False)

        if __debug__:
            logger.debug(
                'Serve({}): complete / data = {}'.format(self, len(data) if data else None))

        if not data and interval and ping_timeout:
            if not self._last_ping or now > self._last_ping + interval:
                if __debug__:
                    logger.debug('Send ping, interval({}): {}, timeout: {}'.format(
                        self, interval, ping_timeout))

                self._last_ping = self.ping(timeout=ping_timeout, now=now)
            else:
                if __debug__:
                    logger.debug('Ping not required({}): {} < {}'.format(
                        self, self._last_ping + interval))

        return data

    def _dispatch(self, data):
        if __debug__:
            logger.debug('Dispatch({}) start'.format(self))

        now = time.time()

        if data:
            if __debug__:
                logger.debug('Dispatch({}) - data ({})'.format(self, len(data)))

            msg, seq, args = brine._load(data)
            if msg == consts.MSG_REQUEST:
                if __debug__:
                    logger.debug('Processing message request, type({}): {} seq: {} - started'.format(
                        self, args[0], seq))

                self._queue(
                    self._on_sync_request_exception,
                    self._dispatch_request, seq, args)

            else:
                if __debug__:
                    logger.debug(
                        'Processing message response, seq({}): {} - started'.format(self, seq))

                if msg == consts.MSG_REPLY:
                    self._dispatch_reply(seq, args)
                elif msg == consts.MSG_EXCEPTION:
                    self._dispatch_exception(seq, args)
                else:
                    raise ValueError("invalid message type: %r" % (msg,))

                if __debug__:
                    logger.debug(
                        'Processing message, seq({}): {} - completed'.format(self, seq))

            self._last_ping = now

        elif self.closed:
            if __debug__:
                logger.debug('Dispatch interrupt({}) - closed'.format(self))

            return
        else:
            if __debug__:
                logger.debug('Dispatch({}) - no data'.format(self))

        _async_callbacks = self._async_callbacks.keys()
        for async_event_id in _async_callbacks:
            async_event = self._async_callbacks.get(async_event_id)
            if not async_event:
                continue

            if not hasattr(async_event, '_ttl'):
                continue

            if async_event._ttl and async_event._ttl < now:
                raise EOFError(
                    'Async timeout! ({}, event={})'.format(self, async_event),
                    async_event)

    def ping(self, timeout=30, now=None, block=False):
        ''' RPyC do not have any PING handler. So.. why to wait? '''
        now = now or time.time()
        promise = self.async_request(
            consts.HANDLE_PING, 'ping', timeout=timeout)
        if block:
            promise.wait()

        return now


class PupyConnectionThread(Thread):
    def __init__(self, *args, **kwargs):
        if __debug__:
            logger.debug('Create connection thread')

        self.pupy_srv = args[0]
        self.connection = PupyConnection(*args, **kwargs)

        Thread.__init__(self)
        self.daemon = True
        self.name = 'PupyConnection({}) Thread'.format(self.connection)

        if __debug__:
            logger.debug('Create connection({}) thread completed'.format(self.connection))

    def run(self):
        if __debug__:
            logger.debug('Run connection thread')

        self.connection.init()
        self.connection.loop()

        if __debug__:
            logger.debug('Connection thread closed')
