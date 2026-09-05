"""Sealed execution bytes and an exec-stop native identity handshake."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import uuid
from .canonical import canonical_sha256
from .phase9_provider_identity import _file, configuration_observation, expected_configuration_states


class ProviderSandbox:
    def __init__(self, scratch, profile, argv, project):
        self.scratch, self.profile, self.native_argv = scratch, profile, argv
        self.fds = []
        self.mounts = []
        self.views = []
        self.private_home = scratch / 'codex_home'
        self.private_home.mkdir(mode=0o700)
        self.endpoint = '@phase9-gate-' + uuid.uuid4().hex
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(b'\0' + self.endpoint[1:].encode())
        self.server.listen(1)
        self.server.settimeout(10)
        self.channel = None
        self.closed = False
        self._snapshot(profile['native'], profile['native']['path'], executable=True)
        # Pin every existing configuration file at its original pathname. Also
        # install the approved home config in a separate writable runtime home.
        original_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
        for row in profile['configuration']:
            path = Path(row.get('load_path', row['path']))
            if not row.get('absent'):
                self._snapshot(row, str(path))
            if path.parent == original_home:
                destination = self.private_home / path.name
                destination.touch(mode=0o600)
                if row.get('absent'):
                    self._sealed(b'', str(destination), source=str(path))
                else:
                    self._snapshot(row, str(destination))
        # Authentication bytes stay in this private test/runtime directory and
        # are never serialized in execution records or printed.
        auth = original_home / 'auth.json'
        if auth.is_file():
            destination = self.private_home / 'auth.json'
            with destination.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(auth.read_bytes())
        helper = Path(__file__).with_name('phase9_provider_gate.py')
        self._snapshot(_file(helper), str(helper))
        self.command = [sys.executable, '-I', '-S', '-B', str(helper), str(self.endpoint), *argv]
        self.namespace_mounts, self.readonly_mounts = self._configuration_namespaces()
        self.execution_view = {'working_directory': str(project), 'configuration_states': expected_configuration_states(profile),
            'protected_directories': self.protected_directories, 'schema': 'phase9-sealed-provider-view-v1',
            'files': self.views, 'private_runtime_home': str(self.private_home),
            'helper': _file(helper), 'helper_interpreter': _file(sys.executable),
            'native_sha256': profile['native']['sha256']}

    def _configuration_namespaces(self):
        """Freeze directory entries in private tmpfs mounts, including absences.

        O_PATH descriptors preserve non-configuration children without copying
        database bytes. No writable host directory backs these namespace views.
        """
        critical = {Path(row.get('load_path', row['path'])): row for row in self.profile['configuration']}
        directories = {parent for path in critical for parent in path.parents}
        self.protected_directories = [str(path) for path in sorted(directories, key=lambda p: (len(p.parts), str(p)))]
        options = []
        for directory in map(Path, self.protected_directories):
            options.extend(['--tmpfs', str(directory)])
            try:
                entries = list(directory.iterdir()) if directory.is_dir() else []
            except FileNotFoundError:
                entries = []
            children = {path.name: path for path in entries}
            children.update({path.name: path for path in directories if path.parent == directory and path != directory})
            for name, path in sorted(children.items()):
                if path in critical:
                    # Present files are installed later from sealed descriptors;
                    # absent ones never acquire a directory entry in this view.
                    continue
                if path in directories:
                    options.extend(['--dir', str(path)])
                    continue
                try:
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        options.extend(['--symlink', os.readlink(path), str(path)])
                    elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                        descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
                        self.fds.append(descriptor)
                        options.extend(['--ro-bind-fd', str(descriptor), str(path)])
                    # Host sockets/devices are not provider configuration inputs.
                    # /proc and /dev are mounted explicitly by the supervisor.
                except FileNotFoundError:
                    continue
        readonly = [part for path in reversed(self.protected_directories) for part in ('--remount-ro', path)]
        return options, readonly

    def _snapshot(self, row, destination, executable=False):
        raw = Path(row['path']).read_bytes()
        if len(raw) != row['byte_length'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('provider bytes changed before sealed snapshot')
        self._sealed(raw, destination, source=row['path'], executable=executable)

    def _sealed(self, raw, destination, *, source, executable=False):
        descriptor = os.memfd_create('phase9-execution-view', os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
        self.fds.append(descriptor)
        with os.fdopen(os.dup(descriptor), 'wb') as stream:
            stream.write(raw)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SEAL
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        self.mounts.extend(['--perms', '0555' if executable else '0400', '--ro-bind-data', str(descriptor), destination])
        self.views.append({'source': source, 'destination': destination, 'sha256': hashlib.sha256(raw).hexdigest(),
                           'byte_length': len(raw), 'seals': seals})

    def handshake(self, wrapper_pid, intent, sandbox_argv):
        channel, _ = self.server.accept()
        self.channel = channel
        channel.settimeout(10)
        peer, _, _ = struct.unpack('3i', channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        message = channel.recv(4096)
        if not message.endswith(b'\n'):
            raise ValueError('provider launch handshake is incomplete')
        ready = json.loads(message)
        children = Path(f'/proc/{peer}/task/{peer}/children').read_text().split()
        if len(children) != 1:
            raise ValueError('provider gate must own exactly one native child')
        native_pid = int(children[0])
        pid = peer
        while pid != wrapper_pid:
            raw = Path(f'/proc/{pid}/stat').read_text()
            parent = int(raw[raw.rfind(')') + 2:].split()[1])
            if parent <= 1 or parent == pid:
                raise ValueError('native gate is outside the owned wrapper scope')
            pid = parent
        status = Path(f'/proc/{native_pid}/status').read_text()
        ns_pid = next(line.split()[1:] for line in status.splitlines() if line.startswith('NSpid:'))
        if int(ns_pid[-1]) != ready['native_namespace_pid'] or os.stat(f'/proc/{native_pid}/ns/pid').st_ino != ready['pid_namespace_inode']:
            raise ValueError('native namespace handshake differs')
        native_stat = Path(f'/proc/{native_pid}/stat').read_text()
        native_fields = native_stat[native_stat.rfind(')') + 2:].split()
        return {'native_process_start_ticks': native_fields[19], 'pid_namespace_inode': ready['pid_namespace_inode'],
                'provider_call_sha256': canonical_sha256(intent['provider_call']),
                'native_process_pid': native_pid, 'gate_process_pid': peer,
                'kernel_executable_sha256': hashlib.sha256(Path(f'/proc/{native_pid}/exe').read_bytes()).hexdigest(),
                'kernel_cmdline_sha256': hashlib.sha256(Path(f'/proc/{native_pid}/cmdline').read_bytes()).hexdigest(),
                'sandbox_argv': sandbox_argv, 'sandbox_argv_sha256': canonical_sha256(sandbox_argv),
                'execution_view': self.execution_view, 'execution_view_sha256': canonical_sha256(self.execution_view),
                'configuration_observation': configuration_observation(native_pid, self.profile)}

    def release(self):
        self.channel.sendall(b'GO\n')

    def close(self):
        if getattr(self, "closed", False):
            return
        self.closed = True
        if getattr(self, "channel", None) is not None:
            self.channel.close()
        self.server.close()
        for descriptor in self.fds:
            os.close(descriptor)
        self.fds.clear()

    def __del__(self):
        try:
            self.close()
        except (AttributeError, OSError):
            pass
