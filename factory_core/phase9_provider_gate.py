"""Hold the native child at exec-stop until its kernel identity is committed.

This fixed helper runs inside the provider PID namespace. The control socket
is private to the outer supervisor; no provider code executes before GO.
"""
from __future__ import annotations
import ctypes
import json
import os
import signal
import socket
import sys


def main():
    endpoint, *argv = sys.argv[1:]
    libc = ctypes.CDLL(None, use_errno=True)
    pid = os.fork()
    if pid == 0:
        if libc.ptrace(0, 0, None, None) != 0:  # PTRACE_TRACEME
            os._exit(125)
        os.execvpe(argv[0], argv, os.environ)
    waited, status = os.waitpid(pid, 0)
    if waited != pid or not os.WIFSTOPPED(status) or os.WSTOPSIG(status) != signal.SIGTRAP:
        return 125
    if libc.ptrace(0x4200, pid, None, ctypes.c_void_p(0x00100000)) != 0:  # PTRACE_O_EXITKILL
        os.kill(pid, signal.SIGKILL)
        return 125
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(15)
        channel.connect(b'\0' + endpoint[1:].encode())
        channel.sendall(json.dumps({"native_namespace_pid": pid,
                                  "pid_namespace_inode": os.stat('/proc/self/ns/pid').st_ino}).encode() + b'\n')
        if channel.recv(16) != b'GO\n':
            os.kill(pid, signal.SIGKILL)
            return 125
    if libc.ptrace(17, pid, None, None) != 0:  # PTRACE_DETACH resumes after exec
        os.kill(pid, signal.SIGKILL)
        return 125
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == '__main__':
    raise SystemExit(main())
