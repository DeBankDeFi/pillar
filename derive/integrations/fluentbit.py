import json
import logging as builtin_logging
import os
import socket
import typing
from logging import LogRecord
from logging.handlers import QueueHandler
from queue import Queue, Empty
from threading import Thread, Lock

from configalchemy import BaseConfig

import derive
from derive import logging
from derive.integrations import BaseIntegration


class DefaultConfig(BaseConfig):
    ENABLE = False
    TCP_HOST = "0.0.0.0"
    TCP_PORT = 5170
    THREAD_TERMINATE_TIMEOUT = 5
    PUT_LOG_INTERVAL = 5
    BATCH_SIZE = 512 * 1024
    BUILTIN_LOGGER_LEVEL = logging.INFO


class FluentBitLoggingQueueListener:
    SEPARATOR = "\r\n"
    BUILTIN_RECORD_ATTRS = frozenset(
        (
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "lineno",
            "module",
            "name",
            "pathname",
            "process",
            "processName",
            "thread",
            "threadName",
            "stack_info",
        )
    )

    _sentinel = None
    log_buffer: str

    def __init__(self, queue: Queue, config: DefaultConfig):
        self.queue = queue
        self.config = config
        self.reset_log_buffer()

        self.logger = builtin_logging.getLogger(__name__)
        self.logger.addHandler(logging.stderr_stream_handler)
        self.logger.setLevel(self.config.BUILTIN_LOGGER_LEVEL)

        self._thread = None
        self._lock = Lock()
        self._thread_for_pid = None

    def reset_log_buffer(self):
        self.log_buffer = ""

    def check_log_size(self) -> bool:
        return len(self.log_buffer) >= self.config.BATCH_SIZE

    def format(self, record: logging.DeriveLogRecord) -> str:
        return json.dumps(record.to_log_data(), indent=None, separators=(",", ":"))

    def handle(self, record: logging.DeriveLogRecord) -> None:
        self.log_buffer += self.format(record) + self.SEPARATOR
        if self.check_log_size():
            self.put_logs()

    def put_logs(self):
        if not len(self.log_buffer):
            return
        self._send()
        self.reset_log_buffer()

    def _send(self) -> None:
        self.logger.debug("sending logs to fluentbit")
        with socket.create_connection(
            (self.config.TCP_HOST, self.config.TCP_PORT), 0.5
        ) as conn:
            conn.sendall(self.log_buffer.encode("utf-8"))
        self.logger.debug("sent logs to fluentbit")

    def _monitor(self):
        while True:
            try:
                record = self.queue.get(True, self.config.PUT_LOG_INTERVAL)
            except Empty:
                self.put_logs()
            else:
                try:
                    if record is self._sentinel:
                        self.put_logs()
                        break
                    self.handle(record)
                except Exception:
                    self.logger.exception("error handling log record")
                    if len(self.log_buffer) > self.config.BATCH_SIZE * 4:
                        self.reset_log_buffer()

    def start(self):
        with self._lock:
            if not self.is_alive:
                self._thread = Thread(
                    target=self._monitor,
                    name="derive.logging.FluentBitLoggingQueueListener",
                )
                self._thread.setDaemon(True)
                self._thread.start()
                self._thread_for_pid = os.getpid()

    def kill(self):
        with self._lock:
            if self._thread:
                self.queue.put_nowait(self._sentinel)
                self._thread.join(self.config.THREAD_TERMINATE_TIMEOUT)
                self._thread = None
                self._thread_for_pid = None

    def __del__(self):
        self.kill()

    @property
    def is_alive(self) -> bool:
        if self._thread_for_pid != os.getpid():
            return False
        if not self._thread:
            return False
        return self._thread.is_alive()

    def ensure_thread(self):
        if not self.is_alive:
            self.start()


class FluentBitLoggingQueueHandler(QueueHandler):
    def __init__(self, listener: FluentBitLoggingQueueListener):
        super().__init__(listener.queue)
        self.listener = listener

    def flush(self) -> None:
        self.listener.kill()

    def prepare(self, record: LogRecord) -> LogRecord:
        return record


class Integration(BaseIntegration):
    def __init__(self, config: DefaultConfig):
        self.handler: typing.Optional[FluentBitLoggingQueueHandler] = None
        self.ql: typing.Optional[FluentBitLoggingQueueListener] = None
        self.config = config

    @property
    def identifier(self) -> str:
        return "fluentbit"

    def setup(self):
        if not self.config.ENABLE:
            return
        root = logging.getLogger()
        queue = Queue()
        self.ql = FluentBitLoggingQueueListener(queue, self.config)
        self.handler = FluentBitLoggingQueueHandler(self.ql)
        root.addHandler(self.handler)
        self.ql.start()
        derive.register_after_fork(lambda: self.ql.ensure_thread())
