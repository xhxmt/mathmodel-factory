#!/usr/bin/env python3
"""
Paper Factory Dashboard - Development Server
快速启动开发服务器，用于本地调试
"""
import subprocess
import sys
import time
import signal
import os
from pathlib import Path

def check_port(port):
    """Check if port is available"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) != 0

def main():
    script_dir = Path(__file__).parent.resolve()
    backend_dir = script_dir / "backend"
    frontend_dir = script_dir / "frontend"

    print("=" * 50)
    print("  Paper Factory Dashboard - Dev Server")
    print("=" * 50)
    print()

    # Check ports
    if not check_port(8000):
        print("✗ Port 8000 already in use (backend)")
        print("  Run: lsof -ti:8000 | xargs kill -9")
        sys.exit(1)

    if not check_port(5173):
        print("✗ Port 5173 already in use (frontend)")
        print("  Run: lsof -ti:5173 | xargs kill -9")
        sys.exit(1)

    processes = []

    def cleanup(*args):
        print("\n\nShutting down...")
        for p in processes:
            p.terminate()
        for p in processes:
            p.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # Start backend
    print("Starting backend on http://127.0.0.1:8000 ...")
    backend_venv = backend_dir / "venv" / "bin" / "python3"
    backend_app = backend_dir / "app.py"

    backend_proc = subprocess.Popen(
        [str(backend_venv), str(backend_app)],
        cwd=backend_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    processes.append(backend_proc)

    # Wait for backend
    for i in range(10):
        if check_port(8000):
            time.sleep(1)
        else:
            print("✓ Backend ready")
            break
    else:
        print("✗ Backend failed to start")
        cleanup()

    # Start frontend
    print("Starting frontend on http://localhost:5173 ...")
    frontend_proc = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=frontend_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    processes.append(frontend_proc)

    time.sleep(2)
    print("✓ Frontend ready")

    print()
    print("=" * 50)
    print("  Dashboard is ready!")
    print("=" * 50)
    print()
    print("  Frontend: http://localhost:5173")
    print("  Backend:  http://127.0.0.1:8000")
    print("  API Docs: http://127.0.0.1:8000/docs")
    print()
    print("Press Ctrl+C to stop")
    print()

    # Stream logs
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()

if __name__ == "__main__":
    main()
