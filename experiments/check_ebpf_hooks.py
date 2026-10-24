"""Verify real eBPF TC network hook (packet accounting + port isolation) and LSM security audit."""
import json
import os
import socket
import socketserver
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oeagent.runtime import ResourceLock
from oeagent.tools import create_registry


class _SilentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok-ebpf-hook"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        del args


class _ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def _start_http(port):
    server = _ReusableHTTPServer(("127.0.0.1", port), _SilentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    registry = create_registry()
    port_target = 18091
    port_other = 18092
    evidence = {"schema_version": 1, "status": "running", "target_port": port_target, "other_port": port_other}
    s_target = None
    s_other = None
    try:
        with ResourceLock():
            net_desc = registry.call("network.ebpf.describe", {})
            lsm_desc = registry.call("security.lsm.describe", {})
            lsm_audit = registry.call("security.lsm.audit", {"pid": os.getpid()})
            evidence.update(network_describe=net_desc, security_describe=lsm_desc, security_audit=lsm_audit)
            if not net_desc["ok"] or not net_desc["data"]["available"]:
                raise RuntimeError("eBPF network hook unavailable on guest")
            if not lsm_desc["ok"] or not lsm_audit["ok"] or not lsm_desc["data"]["bpf_lsm_enabled"]:
                raise RuntimeError("LSM audit or BPF LSM check failed")

            s_target = _start_http(port_target)
            s_other = _start_http(port_other)

            attached_obs = registry.call("network.ebpf.attach", {
                "interface": "lo", "target_port": port_target, "drop_target": False})
            evidence["attach_observe"] = attached_obs
            if not attached_obs["ok"] or not attached_obs["data"]["active"]:
                raise RuntimeError("Failed to attach eBPF TC hook in observe mode")

            for _ in range(5):
                with urllib.request.urlopen(f"http://127.0.0.1:{port_target}/", timeout=2) as resp:
                    if resp.read() != b"ok-ebpf-hook":
                        raise RuntimeError("Unexpected HTTP body on target port")

            status_obs = registry.call("network.ebpf.status", {})
            evidence["status_after_traffic"] = status_obs
            stats_obs = status_obs["data"]["stats"]
            if stats_obs["target_packets"] < 5 or stats_obs["target_bytes"] < 200 or stats_obs["dropped_packets"] != 0:
                raise RuntimeError(f"Unexpected eBPF stats in observe mode: {stats_obs}")

            attached_drop = registry.call("network.ebpf.attach", {
                "interface": "lo", "target_port": port_target, "drop_target": True})
            evidence["attach_drop"] = attached_drop
            if not attached_drop["ok"]:
                raise RuntimeError("Failed to update eBPF TC hook to drop_target=True")

            blocked = False
            try:
                sock = socket.create_connection(("127.0.0.1", port_target), timeout=0.6)
                sock.close()
            except OSError:
                blocked = True
            if not blocked:
                raise RuntimeError("Target port connection succeeded despite drop_target=True")

            with urllib.request.urlopen(f"http://127.0.0.1:{port_other}/", timeout=2) as resp:
                if resp.read() != b"ok-ebpf-hook":
                    raise RuntimeError("Non-target port traffic was unexpectedly disrupted")

            status_drop = registry.call("network.ebpf.status", {})
            evidence["status_after_drop"] = status_drop
            stats_drop = status_drop["data"]["stats"]
            if stats_drop["dropped_packets"] < 1:
                raise RuntimeError(f"Expected dropped_packets >= 1, got {stats_drop}")

            detached = registry.call("network.ebpf.detach", {})
            evidence["detach"] = detached
            if not detached["ok"] or detached["data"]["after"]["active"]:
                raise RuntimeError("eBPF network hook did not detach cleanly")

            with urllib.request.urlopen(f"http://127.0.0.1:{port_target}/", timeout=2) as resp:
                if resp.read() != b"ok-ebpf-hook":
                    raise RuntimeError("Target port did not recover after detach")

            evidence["status"] = "passed"
    except BaseException as exc:
        evidence.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        try:
            registry.call("network.ebpf.detach", {})
        except BaseException:
            pass
        for srv in (s_target, s_other):
            if srv is not None:
                srv.shutdown()
                srv.server_close()
        (ROOT / "state/ebpf-hooks-validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
