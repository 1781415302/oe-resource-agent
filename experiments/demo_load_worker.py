#!/usr/bin/env python3
"""Bounded, reproducible demo loads; no third-party dependencies.

EPS units differ between modes and must only be compared within one scenario.
CPU affinity is assigned by the parent with taskset and inherited by threads.
"""
import argparse
import ctypes
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit


def name(value):
    ctypes.CDLL(None).prctl(15, value.encode()[:15], 0, 0, 0)


def compute_server(port):
    name("oe-compute")
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def do_GET(self):
            digest = hashlib.pbkdf2_hmac("sha256", b"oe-demo-fixed-input", b"oe-demo-fixed-salt", 4000)
            body = digest.hex().encode().ljust(4096, b".")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()


def periodic_load(mode, threads):
    name("oe-load-" + mode)
    counters = [0] * threads
    started = time.monotonic()
    def worker(index):
        name("oe-load-" + mode)
        block = bytes([index + 1]) * (32 * 1024 * 1024) if mode == "cache" else None
        while True:
            if mode == "cache":
                hashlib.sha256(block).digest()
            else:
                hashlib.pbkdf2_hmac("sha256", b"oe-demo", b"fixed", 300)
                time.sleep(.002)
            counters[index] += 1
    for index in range(threads):
        threading.Thread(target=worker, args=(index,), daemon=True).start()
    previous, last = 0, started
    while True:
        time.sleep(1)
        now, count = time.monotonic(), sum(counters)
        print(f"[ {now-started:.6f}s ] threads: {threads} eps: {(count-previous)/(now-last):.6f}", flush=True)
        previous, last = count, now


def burst_load(url):
    name("oe-burst-http")
    target = urlsplit(url)
    if target.hostname != "127.0.0.1" or target.port != 18091:
        raise ValueError("Burst target must be the owned loopback Nginx")
    started = time.monotonic()
    completed, errors = [0] * 8, [0] * 8
    def worker(index):
        connection = None
        while True:
            if (time.monotonic() - started) % 1.0 >= .2:
                time.sleep(.005)
                continue
            try:
                if connection is None:
                    connection = http.client.HTTPConnection("127.0.0.1", 18091, timeout=.5)
                connection.request("GET", target.path or "/index.html")
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    completed[index] += 1
                else:
                    errors[index] += 1
            except (OSError, http.client.HTTPException):
                errors[index] += 1
                if connection:
                    connection.close()
                connection = None
    for index in range(8):
        threading.Thread(target=worker, args=(index,), daemon=True).start()
    while True:
        time.sleep(1)
        print(json.dumps({"elapsed_seconds": time.monotonic()-started, "completed": sum(completed),
                          "errors": sum(errors), "period_seconds": 1, "active_seconds": .2,
                          "concurrency": 8}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("compute", "cache", "wakeup", "burst"))
    parser.add_argument("--threads", type=int, choices=(1, 2, 3, 4), default=1)
    parser.add_argument("--port", type=int, choices=(18093,), default=18093)
    parser.add_argument("--url", default="http://127.0.0.1:18091/index.html")
    args = parser.parse_args()
    if args.mode == "compute":
        compute_server(args.port)
    elif args.mode == "burst":
        burst_load(args.url)
    else:
        periodic_load(args.mode, args.threads)
