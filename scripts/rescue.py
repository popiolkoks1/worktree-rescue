#!/usr/bin/env python3
"""Worktree Rescue: local-only lifecycle management for registered Git worktrees."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

VERSION = '0.2.0'
DEFAULTS = {
    'enabled': True,
    'grace_seconds': 600,
    'archive_after_days': 7,
    'poll_seconds': 60,
    'minimum_free_bytes': 1024 ** 3,
    'archive_budget_bytes': 10 * 1024 ** 3,
}


class RescueError(Exception):
    pass


def run(args, cwd=None, check=True, input=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(args, cwd=cwd, env=env, input=input, capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RescueError(result.stderr.strip() or 'Command failed: ' + args[0])
    return result.stdout.strip() if check else result


def git(path, *args, **kwargs):
    return run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-C', str(path), *args], **kwargs)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(temp, 'x', encoding='utf-8') as file:
            os.chmod(temp, 0o600)
            json.dump(value, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def process_token(pid):
    """PID plus process start time avoids treating a reused PID as the owner."""
    result = run(['ps', '-p', str(pid), '-o', 'lstart='], check=False)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise RescueError('Cannot inspect process identity')
    return result.stdout.strip() or None


def owner_process():
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        row = run(['ps', '-p', str(pid), '-o', 'ppid=', '-o', 'command='], check=False)
        fields = row.stdout.strip().split(None, 1)
        if len(fields) != 2:
            break
        parent, command = int(fields[0]), fields[1]
        # Match the executable/CLI path, never a word inside the user's prompt.
        executable = command.split()[:2]
        if any(re.search(r'(?:^|/)(?:claude|claude-code)(?:$|/)', part) for part in executable):
            return {'pid': pid, 'token': process_token(pid)}
        pid = parent
    return None


def ancestors():
    """Ignore the requesting Claude and its hook wrappers, not the user's parent shell."""
    owner = owner_process()
    if not owner:
        return {os.getpid()}
    found = {os.getpid()}
    pid = os.getppid()
    for _ in range(20):
        if pid <= 1 or pid in found:
            break
        found.add(pid)
        if pid == owner['pid']:
            break
        result = run(['ps', '-p', str(pid), '-o', 'ppid='], check=False)
        try:
            pid = int(result.stdout.strip())
        except ValueError:
            break
    return found


def busy_processes(path, exclude=()):
    """Fail closed if cwd inspection is unavailable; never kill another process."""
    excluded = set(exclude) | {os.getpid()}
    path = Path(path).resolve()
    found = []
    if sys.platform.startswith('linux'):
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit() or int(entry.name) in excluded:
                continue
            try:
                owner = entry.stat().st_uid
                if owner != os.getuid():
                    continue
                cwd = (entry / 'cwd').resolve(strict=True)
                if inside(cwd, path):
                    found.append(int(entry.name))
            except FileNotFoundError:
                continue
            except PermissionError as error:
                raise RescueError('Cannot inspect a same-user process; preserving worktree') from error
    elif sys.platform == 'darwin':
        result = run(['lsof', '-a', '-u', str(os.getuid()), '-d', 'cwd', '-Fpn'], check=False)
        if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr.strip()):
            raise RescueError('Cannot inspect working directories; preserving worktree')
        pid = None
        for line in result.stdout.splitlines():
            if line.startswith('p'):
                pid = int(line[1:])
            elif line.startswith('n') and pid not in excluded and inside(line[1:], path):
                found.append(pid)
    else:
        raise RescueError('Automatic cleanup currently supports macOS and Linux only')
    commands = run(['ps', '-axo', 'pid=,uid=,command='], check=False)
    if commands.returncode != 0:
        raise RescueError('Cannot inspect process commands; preserving worktree')
    for row in commands.stdout.splitlines():
        parts = row.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            pid, uid = int(parts[0]), int(parts[1])
            if pid not in excluded and uid == os.getuid() and str(path) in parts[2]:
                found.append(pid)
    return sorted(set(found))


def inventory(path):
    """Hash actual contents, modes and symlinks, including ignored and untracked files."""
    records = {}
    size = 0
    def walk(folder, prefix=''):
        nonlocal size
        for entry in sorted(os.scandir(folder), key=lambda item: item.name):
            name = prefix + entry.name
            if name == '.git':
                continue
            if entry.name == '.git':
                raise RescueError('Nested repositories/submodules require manual handling')
            info = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                records[name] = ['link', mode, os.readlink(entry.path)]
            elif stat.S_ISDIR(info.st_mode):
                records[name] = ['dir', mode]
                walk(entry.path, name + '/')
            elif stat.S_ISREG(info.st_mode):
                h = hashlib.sha256()
                with open(entry.path, 'rb') as file:
                    for block in iter(lambda: file.read(1024 * 1024), b''):
                        h.update(block)
                size += info.st_size
                records[name] = ['file', mode, info.st_size, h.hexdigest()]
            else:
                raise RescueError('Special files require manual handling: ' + name)
    walk(path)
    return {'entries': records, 'bytes': size}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class Rescue:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('WORKTREE_RESCUE_HOME', '~/.local/share/worktree-rescue')).expanduser().absolute()
        if self.home.is_symlink():
            raise RescueError('State directory must not be a symlink')
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        self.home = self.home.resolve()
        for name in ('events', 'archives', 'worktrees'):
            (self.home / name).mkdir(exist_ok=True, mode=0o700)
        self.config = dict(DEFAULTS, **read_json(self.home / 'config.json', {}))
        for key in ('grace_seconds', 'archive_after_days', 'poll_seconds', 'minimum_free_bytes', 'archive_budget_bytes'):
            if not isinstance(self.config[key], (int, float)) or isinstance(self.config[key], bool) or self.config[key] < 0:
                raise RescueError('Invalid configuration value: ' + key)
        if not isinstance(self.config['enabled'], bool) or self.config['poll_seconds'] < 1:
            raise RescueError('Invalid enabled/poll_seconds setting')

    @contextlib.contextmanager
    def lock(self, name='state.lock', blocking=True):
        with open(self.home / name, 'a') as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise RescueError('Another operation is already running') from error
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def state(self):
        return read_json(self.home / 'registry.json', {'version': 1, 'worktrees': {}, 'sessions': {}})

    def save(self, state):
        atomic_json(self.home / 'registry.json', state)

    def log(self, action, **details):
        with open(self.home / 'history.jsonl', 'a', encoding='utf-8') as file:
            os.chmod(self.home / 'history.jsonl', 0o600)
            file.write(json.dumps({'at': time.time(), 'action': action, **details}) + '\n')

    def validate(self, record):
        path = Path(record['path'])
        if path.is_symlink() or not path.is_dir():
            raise RescueError('Worktree is missing or replaced by a symlink')
        if not record.get('external', False) and not inside(path, self.home / 'worktrees'):
            raise RescueError('Worktree is outside the managed directory')
        if str(path.absolute()) != str(path.resolve()):
            raise RescueError('Worktree path contains a symlink')
        common = Path(git(path, 'rev-parse', '--git-common-dir'))
        if not common.is_absolute():
            common = path / common
        common = common.resolve()
        if str(common) != record['common']:
            raise RescueError('Repository identity changed')
        gitdir = Path(git(path, 'rev-parse', '--absolute-git-dir')).resolve()
        if gitdir == common or not inside(gitdir, common / 'worktrees'):
            raise RescueError('Refusing a primary checkout')
        if str(gitdir) != record['gitdir']:
            raise RescueError('Worktree identity changed')
        marker = read_json(gitdir / 'worktree-rescue.json', {})
        if marker.get('id') != record['id']:
            raise RescueError('Ownership marker is missing or invalid')
        if git(path, 'symbolic-ref', '--quiet', 'HEAD') != 'refs/heads/' + record['branch']:
            raise RescueError('Worktree branch changed; manual review required')
        for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'index.lock'):
            if (gitdir / name).exists():
                raise RescueError('Git operation is in progress: ' + name)
        return path

    def create(self, cwd, name='task', session_id=None, owner=None):
        source = Path(git(cwd, 'rev-parse', '--show-toplevel')).resolve()
        common = Path(git(source, 'rev-parse', '--git-common-dir'))
        if not common.is_absolute():
            common = source / common
        common = common.resolve()
        base = git(source, 'symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD', check=False)
        base_ref = base.stdout.strip() if base.returncode == 0 else None
        if not base_ref:
            for candidate in ('refs/heads/main', 'refs/heads/master'):
                if git(source, 'show-ref', '--verify', '--quiet', candidate, check=False).returncode == 0:
                    base_ref = candidate
                    break
        if not base_ref:
            current = git(source, 'symbolic-ref', '--quiet', 'HEAD', check=False)
            if current.returncode != 0:
                raise RescueError('No integration branch found; start from a named branch')
            base_ref = current.stdout.strip()
        ident = uuid.uuid4().hex[:16]
        slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', name)[:40].strip('-') or 'task'
        path = self.home / 'worktrees' / (slug + '-' + ident)
        branch = 'rescue/' + slug + '-' + ident
        with self.lock():
            state = self.state()
            git(source, 'worktree', 'add', '-b', branch, str(path), 'HEAD')
            try:
                self.copy_environment(source, path)
            except Exception:
                git(source, 'worktree', 'remove', '--force', str(path))
                git(source, 'branch', '-D', branch)
                raise
            gitdir = str(Path(git(path, 'rev-parse', '--absolute-git-dir')).resolve())
            record = {'id': ident, 'path': str(path), 'repo': str(source), 'common': str(common), 'gitdir': gitdir, 'branch': branch, 'base_ref': base_ref, 'created_at': time.time(), 'last_activity': time.time(), 'status': 'registered'}
            atomic_json(Path(gitdir) / 'worktree-rescue.json', {'id': ident})
            state['worktrees'][ident] = record
            if session_id:
                session = state['sessions'].setdefault(session_id, {'paths': [], 'owner': owner, 'active': True})
                session.setdefault('paths', []).append(str(path))
                session.update(owner=owner or session.get('owner'), active=True, seen_at=time.time())
            self.save(state)
            self.log('created', id=ident, path=str(path))
        return record

    def copy_environment(self, source, target):
        """Use Git's ignore-pattern parser; copy only ignored regular files."""
        include = source / '.worktreeinclude'
        if include.is_symlink():
            raise RescueError('.worktreeinclude must not be a symlink')
        if not include.exists():
            return
        if not include.is_file():
            raise RescueError('.worktreeinclude must be a regular file')
        def names(*options):
            result = git(source, 'ls-files', '--others', '--ignored', *options, '-z', check=False)
            if result.returncode:
                raise RescueError(result.stderr.strip() or 'Cannot select environment files')
            return set(result.stdout.split('\0'))
        ignored = names('--exclude-standard')
        selected = names('--exclude-from=' + str(include))
        for name in sorted(ignored.intersection(selected) - {''}):
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts:
                raise RescueError('Unsafe environment path: ' + name)
            original, destination = source / relative, target / relative
            if any((source / Path(*relative.parts[:i])).is_symlink() or
                   (target / Path(*relative.parts[:i])).is_symlink()
                   for i in range(1, len(relative.parts) + 1)):
                raise RescueError('Environment paths must not contain symlinks: ' + name)
            if not original.is_file() or destination.exists():
                raise RescueError('Environment copy requires a regular file and unused target: ' + name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)

    def adopt(self, location, base_ref):
        """Explicitly enroll a linked worktree without owning its branch."""
        path = Path(location).expanduser().absolute()
        if path != path.resolve() or not path.is_dir():
            raise RescueError('Adoption requires a canonical directory without symlinks')
        if Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path:
            raise RescueError('Pass the worktree root, not a subdirectory')
        common = Path(git(path, 'rev-parse', '--git-common-dir'))
        common = (path / common).resolve() if not common.is_absolute() else common.resolve()
        gitdir = Path(git(path, 'rev-parse', '--absolute-git-dir')).resolve()
        if gitdir == common or not inside(gitdir, common / 'worktrees'):
            raise RescueError('Refusing to adopt a primary checkout')
        branch = git(path, 'symbolic-ref', '--quiet', 'HEAD')[len('refs/heads/'):]
        base = git(path, 'rev-parse', '--symbolic-full-name', '--verify', '--end-of-options', base_ref)
        if not base.startswith(('refs/heads/', 'refs/remotes/')) or base == 'refs/heads/' + branch:
            raise RescueError('Choose a different named integration branch')
        ident = uuid.uuid4().hex[:16]
        with self.lock():
            state = self.state()
            existing = next((r for r in state['worktrees'].values()
                             if r['path'] == str(path) and r['status'] == 'registered'), None)
            if existing:
                self.validate(existing)
                return existing
            marker = gitdir / 'worktree-rescue.json'
            if marker.exists():
                raise RescueError('Worktree already has a Rescue marker; use its original state directory')
            record = dict(id=ident, path=str(path), repo=str(path), common=str(common),
                          gitdir=str(gitdir), branch=branch, base_ref=base, external=True,
                          created_at=time.time(), last_activity=time.time(), status='registered')
            atomic_json(marker, {'id': ident})
            try:
                self.validate(record)
                state['worktrees'][ident] = record
                self.save(state)
            except Exception:
                marker.unlink(missing_ok=True)
                raise
            self.log('adopted', id=ident, path=str(path), branch=branch)
        return record

    def enqueue(self, event, owner=None):
        # No global lock: SessionEnd has a short budget and must never wait for an archive.
        atomic_json(self.home / 'events' / (str(time.time_ns()) + '-' + uuid.uuid4().hex + '.json'), {'input': event, 'owner': owner, 'at': time.time()})

    def drain(self, state):
        consumed = []
        for file in sorted((self.home / 'events').glob('*.json')):
            message = read_json(file)
            event, at = message['input'], message['at']
            sid = str(event.get('session_id', ''))
            kind = event.get('hook_event_name')
            if not sid:
                consumed.append(file)
                continue
            if kind == 'SessionEnd':
                if sid in state['sessions']:
                    state['sessions'][sid]['active'] = False
                    state['sessions'][sid]['ended_at'] = at
                    paths = state['sessions'][sid]['paths']
                    for record in state['worktrees'].values():
                        if record['path'] in paths:
                            record['last_activity'] = at
            else:
                cwd = Path(event.get('cwd', '/')).resolve()
                for record in state['worktrees'].values():
                    if record['status'] == 'registered' and inside(cwd, record['path']):
                        session = state['sessions'].setdefault(sid, {'paths': [], 'owner': None})
                        if record['path'] not in session['paths']:
                            session['paths'].append(record['path'])
                        session.update(owner=message['owner'] or session.get('owner'), active=True, seen_at=at)
                        record['last_activity'] = at
                        break
                if sid in state['sessions']:
                    session = state['sessions'][sid]
                    session.update(active=True, seen_at=at)
                    for record in state['worktrees'].values():
                        if record['path'] in session['paths']:
                            record['last_activity'] = at
            consumed.append(file)
        # Persist before deleting inbox messages; replay after a crash is idempotent.
        self.save(state)
        for file in consumed:
            file.unlink(missing_ok=True)

    def leased(self, state, record, ignore_session=None):
        active = []
        for sid, session in state['sessions'].items():
            if sid == ignore_session or not session['active'] or record['path'] not in session['paths']:
                continue
            owner = session.get('owner')
            if not owner or not owner.get('token'):
                active.append(sid)
            elif process_token(owner['pid']) == owner['token']:
                active.append(sid)
            else:
                session['active'] = False
                session['ended_at'] = time.time()
                record['last_activity'] = time.time()
        return active

    def inspect(self, state, record, now=None, ignore_session=None, exclude=(), requested=False):
        now = time.time() if now is None else now
        path = self.validate(record)
        if self.leased(state, record, ignore_session):
            return 'keep', 'An active or unverifiable session owns this worktree'
        processes = busy_processes(path, exclude)
        if processes:
            return 'keep', 'Processes still use this worktree: ' + ', '.join(map(str, processes))
        dirty = bool(git(path, 'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none'))
        head = git(path, 'rev-parse', 'HEAD')
        merged = git(path, 'merge-base', '--is-ancestor', head, record['base_ref'], check=False)
        if merged.returncode not in (0, 1):
            return 'keep', 'Integration reference is unavailable'
        age = now - record['last_activity']
        if requested:
            return 'archive', 'Claude requested removal; preserve a recovery archive first'
        if age < self.config['grace_seconds']:
            return 'keep', 'Grace period has not elapsed'
        if not dirty and merged.returncode == 0:
            return 'archive', 'Clean worktree is integrated into ' + record['base_ref']
        days = self.config['archive_after_days']
        if days and age >= days * 86400:
            return 'archive', 'Inactive unfinished work reached the configured archive age'
        return 'keep', 'Unfinished work is below the archive age'

    def archive(self, state, record, ignore_session=None, exclude=(), requested=False):
        action, reason = self.inspect(state, record, ignore_session=ignore_session, exclude=exclude, requested=requested)
        if action != 'archive':
            raise RescueError(reason)
        path = self.validate(record)
        before = inventory(path)
        used = sum(f.stat().st_size for f in (self.home / 'archives').rglob('*') if f.is_file())
        free = shutil.disk_usage(self.home).free
        # Reserve uncompressed file size plus full Git objects, not an optimistic compression ratio.
        object_bytes = sum(f.stat().st_size for f in (Path(record['common']) / 'objects').rglob('*') if f.is_file())
        reserve = before['bytes'] + object_bytes + 1024 * 1024
        if free < reserve + self.config['minimum_free_bytes'] or used + reserve > self.config['archive_budget_bytes']:
            raise RescueError('Archive budget or free-space reserve would be exceeded; preserving worktree')
        ident = uuid.uuid4().hex[:16]
        temp = self.home / 'archives' / ('.pending-' + ident)
        final = self.home / 'archives' / ident
        temp.mkdir(mode=0o700)
        ref = 'refs/worktree-rescue/snapshots/' + ident
        try:
            head = git(path, 'rev-parse', 'HEAD')
            index_tree = git(path, 'write-tree')
            # Include staged blobs (not reachable from HEAD) in the self-contained bundle.
            env = dict(os.environ, GIT_AUTHOR_NAME='Worktree Rescue', GIT_AUTHOR_EMAIL='rescue@localhost', GIT_COMMITTER_NAME='Worktree Rescue', GIT_COMMITTER_EMAIL='rescue@localhost')
            commit = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(path), 'commit-tree', index_tree, '-p', head], input='Recovery snapshot\n', text=True, env=env, capture_output=True, timeout=120)
            if commit.returncode:
                raise RescueError('Cannot preserve staged changes: ' + commit.stderr.strip())
            snapshot = commit.stdout.strip()
            git(path, 'update-ref', ref, snapshot)
            git(path, 'bundle', 'create', str(temp / 'commits.bundle'), ref)
            git(path, 'bundle', 'verify', str(temp / 'commits.bundle'))
            with tarfile.open(temp / 'files.tar.gz', 'w:gz', dereference=False) as tar:
                for name in before['entries']:
                    tar.add(path / name, arcname=name, recursive=False)
            with tarfile.open(temp / 'files.tar.gz', 'r:gz') as tar:
                # Read every compressed member to detect truncation before removing anything.
                archived_entries = {}
                for member in tar:
                    if member.name in archived_entries:
                        raise RescueError('Duplicate file in generated archive')
                    if member.isfile():
                        stream = tar.extractfile(member)
                        digest = hashlib.sha256()
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            digest.update(block)
                        archived_entries[member.name] = ['file', member.mode, member.size, digest.hexdigest()]
                    elif member.isdir():
                        archived_entries[member.name] = ['dir', member.mode]
                    elif member.issym():
                        archived_entries[member.name] = ['link', member.mode, member.linkname]
                    else:
                        raise RescueError('Unexpected archive entry type')
                if archived_entries != before['entries']:
                    raise RescueError('Archive does not match original contents; preserving worktree')
            if inventory(path) != before or git(path, 'rev-parse', 'HEAD') != head or git(path, 'write-tree') != index_tree:
                raise RescueError('Worktree changed during archiving; preserving it')
            metadata = {'version': 1, 'id': ident, 'created_at': time.time(), 'record': record.copy(), 'head': head, 'index_tree': index_tree, 'snapshot_ref': ref, 'inventory': before, 'files_sha256': sha256(temp / 'files.tar.gz'), 'bundle_sha256': sha256(temp / 'commits.bundle')}
            atomic_json(temp / 'manifest.json', metadata)
            os.replace(temp, final)
            # Recheck ownership, session leases, processes and exact content immediately before removal.
            self.validate(record)
            if (self.leased(state, record, ignore_session) or busy_processes(path, exclude) or inventory(path) != before
                    or git(path, 'rev-parse', 'HEAD') != head or git(path, 'write-tree') != index_tree):
                raise RescueError('Worktree became active; archive retained and original preserved')
            git(path, 'worktree', 'remove', '--force', str(path))
            # Delete only this plugin's branch at the exact archived commit, never other refs.
            deleted = None if record.get('external') else run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '--git-dir', record['common'], 'update-ref', '-d', 'refs/heads/' + record['branch'], head], check=False)
            if deleted is not None and deleted.returncode:
                self.log('branch-retained', id=record['id'], branch=record['branch'])
            record.update(status='archived', archive_id=ident, archived_at=time.time())
            self.save(state)
            self.log('archived', id=record['id'], archive=ident, bytes=before['bytes'], reason=reason)
            return metadata
        finally:
            if temp.exists():
                shutil.rmtree(temp)
            # Worktree may be gone; address Git through the common directory.
            run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '--git-dir', record['common'], 'update-ref', '-d', ref], check=False)

    def sweep(self, dry_run=False):
        results = []
        with self.lock():
            state = self.state()
            self.drain(state)
            for record in state['worktrees'].values():
                if record['status'] != 'registered':
                    continue
                try:
                    action, reason = self.inspect(state, record)
                    if not self.config['enabled']:
                        action, reason = 'keep', 'Automatic cleanup is paused'
                    if action == 'archive' and not dry_run:
                        archived = self.archive(state, record)
                        results.append({'id': record['id'], 'action': 'archived', 'archive': archived['id']})
                    else:
                        results.append({'id': record['id'], 'action': action, 'reason': reason})
                    record.pop('last_error', None)
                except (RescueError, OSError, subprocess.SubprocessError) as error:
                    results.append({'id': record['id'], 'action': 'keep', 'reason': str(error)})
                    if not dry_run and record.get('last_error') != str(error):
                        self.log('preserved', id=record['id'], reason=str(error))
                        record['last_error'] = str(error)
            self.save(state)
        return results

    def restore(self, archive_id, target, repo=None):
        if not re.fullmatch(r'[a-f0-9]{16}', archive_id):
            raise RescueError('Invalid archive ID')
        archive = self.home / 'archives' / archive_id
        metadata = read_json(archive / 'manifest.json')
        if not metadata:
            raise RescueError('Archive not found')
        target = Path(target).expanduser().absolute()
        if target.exists() or target.is_symlink() or inside(target, self.home / 'archives'):
            raise RescueError('Restore target must be a new path outside the archive directory')
        if sha256(archive / 'files.tar.gz') != metadata['files_sha256'] or sha256(archive / 'commits.bundle') != metadata['bundle_sha256']:
            raise RescueError('Archive integrity check failed')
        with self.lock():
            common = metadata['record']['common']
            if repo:
                common_path = Path(git(repo, 'rev-parse', '--git-common-dir'))
                common = str((Path(repo) / common_path).resolve())
            elif not Path(common).is_dir():
                common = str(self.home / 'recovered' / (archive_id + '.git'))
                Path(common).parent.mkdir(exist_ok=True, mode=0o700)
                run(['git', 'init', '--bare', common])
            ref = 'refs/worktree-rescue/restored/' + archive_id
            run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '--git-dir', common, 'fetch', '--no-tags', str(archive / 'commits.bundle'), metadata['snapshot_ref'] + ':' + ref])
            target.parent.mkdir(parents=True, exist_ok=True)
            run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '--git-dir', common, 'worktree', 'add', '--no-checkout', '--detach', str(target), metadata['head']])
            git(target, 'read-tree', metadata['index_tree'])
            try:
                with tarfile.open(archive / 'files.tar.gz', 'r:gz') as tar:
                    for member in tar:
                        parts = Path(member.name).parts
                        if not parts or member.name.startswith('/') or '..' in parts or '.git' in parts:
                            raise RescueError('Unsafe archive member')
                        dest = target / member.name
                        if any((target.joinpath(*parts[:i])).is_symlink() for i in range(1, len(parts))):
                            raise RescueError('Archive member traverses a symlink')
                        if dest.exists() or dest.is_symlink():
                            raise RescueError('Duplicate archive member')
                        if member.isdir():
                            dest.mkdir(mode=0o700)
                        elif member.issym():
                            dest.symlink_to(member.linkname)
                        elif member.isfile():
                            with open(dest, 'xb') as file:
                                shutil.copyfileobj(tar.extractfile(member), file)
                            os.chmod(dest, member.mode)
                        else:
                            raise RescueError('Unsupported archive member')
                    # Restore directory permissions after children have been written.
                    for member in reversed(tar.getmembers()):
                        if member.isdir():
                            os.chmod(target / member.name, member.mode)
                if inventory(target) != metadata['inventory']:
                    raise RescueError('Restored content does not match archive inventory')
                self.log('restored', archive=archive_id, path=str(target))
                return str(target)
            except Exception:
                self.log('restore-incomplete', archive=archive_id, path=str(target))
                raise

    def ensure_worker(self):
        if os.environ.get('WORKTREE_RESCUE_NO_WORKER') == '1':
            return
        worker = read_json(self.home / 'worker.json', {})
        if worker.get('pid') and worker.get('token') and process_token(worker['pid']) == worker['token']:
            return
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--home', str(self.home), 'worker'], cwd=str(self.home), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)

    def worker(self):
        try:
            with self.lock('worker.lock', blocking=False):
                atomic_json(self.home / 'worker.json', {'pid': os.getpid(), 'token': process_token(os.getpid()), 'version': VERSION})
                while True:
                    self.config = dict(DEFAULTS, **read_json(self.home / 'config.json', {}))
                    try:
                        self.sweep()
                    except Exception as error:
                        self.log('worker-error', reason=str(error))
                    time.sleep(max(1, self.config['poll_seconds']))
        except RescueError:
            return


def handle_hook(rescue, payload):
    kind = payload.get('hook_event_name')
    sid = str(payload.get('session_id', ''))
    if kind == 'WorktreeCreate':
        record = rescue.create(payload['cwd'], payload.get('name', 'task'), sid or None, owner_process())
        rescue.ensure_worker()
        print(record['path'])
    elif kind == 'WorktreeRemove':
        requested = str(Path(payload['worktree_path']).resolve())
        with rescue.lock():
            state = rescue.state()
            rescue.drain(state)
            record = next((r for r in state['worktrees'].values() if r['path'] == requested and r['status'] == 'registered'), None)
            if not record:
                raise RescueError('Worktree is not owned by this plugin')
            if not rescue.config['enabled']:
                raise RescueError('Automatic cleanup is paused; worktree preserved')
            rescue.archive(state, record, ignore_session=sid, exclude=ancestors(), requested=True)
    elif kind in ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PostToolUse', 'Stop'):
        rescue.enqueue(payload, None if kind == 'SessionEnd' else owner_process())
        rescue.ensure_worker()
    else:
        raise RescueError('Unsupported hook event')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Quiet worktree cleanup with recoverable archives.')
    parser.add_argument('--home', help='Private state directory (or WORKTREE_RESCUE_HOME)')
    parser.add_argument('--version', action='version', version=VERSION)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('hook', help=argparse.SUPPRESS)
    sub.add_parser('worker', help=argparse.SUPPRESS)
    create = sub.add_parser('create', help='Create a managed worktree for any agent')
    create.add_argument('--repo', default='.')
    create.add_argument('--name', default='task')
    adopt = sub.add_parser('adopt', help='Enroll an existing linked worktree; preserve its branch')
    adopt.add_argument('path')
    adopt.add_argument('--base', required=True, help='Named integration branch, for example main')
    companion = sub.add_parser('companion', help='Build a lifecycle-only plugin for other worktree tools')
    companion.add_argument('--to', required=True)
    sweep = sub.add_parser('sweep', help='Apply cleanup policy or preview decisions')
    sweep.add_argument('--dry-run', action='store_true')
    sub.add_parser('status', help='Show registered worktrees and cleanup decisions')
    sub.add_parser('archives', help='List recovery archives')
    sub.add_parser('history', help='Show the last 30 recorded actions')
    sub.add_parser('stop-worker', help='Stop the verified background worker')
    restore = sub.add_parser('restore', help='Restore files, commits and staging to a new worktree')
    restore.add_argument('id')
    restore.add_argument('--to', required=True)
    restore.add_argument('--repo')
    for name in ('pause', 'resume'):
        sub.add_parser(name, help=name.capitalize() + ' automatic sweeps')
    config = sub.add_parser('config', help='Show or update user-level policy')
    config.add_argument('--set', nargs=2, metavar=('KEY', 'JSON_VALUE'))
    args = parser.parse_args(argv)
    rescue = Rescue(args.home)
    if args.command == 'hook':
        # Discard prompt/tool content: persist only lifecycle metadata.
        data = sys.stdin.read(2 * 1024 * 1024)
        if len(data) >= 2 * 1024 * 1024:
            raise RescueError('Hook input is too large')
        raw = json.loads(data)
        payload = {key: raw[key] for key in ('session_id', 'cwd', 'hook_event_name', 'name', 'worktree_path') if key in raw}
        handle_hook(rescue, payload)
    elif args.command == 'worker':
        rescue.worker()
    elif args.command == 'stop-worker':
        worker = read_json(rescue.home / 'worker.json', {})
        if worker.get('pid') and worker.get('token') and process_token(worker['pid']) == worker['token']:
            os.kill(worker['pid'], signal.SIGTERM)
        print('Worker stopped. Session hooks will start it again unless the plugin is disabled.')
    elif args.command == 'companion':
        target = Path(args.to).expanduser().absolute()
        target.mkdir(parents=True, exist_ok=False)
        (target / 'scripts').mkdir()
        shutil.copy2(Path(__file__), target / 'scripts' / 'rescue.py')
        (target / '.claude-plugin').mkdir()
        atomic_json(target / '.claude-plugin' / 'plugin.json', {
            'name': 'rescue-companion', 'version': VERSION, 'license': 'MIT',
            'author': {'name': 'popiolkoks1'},
            'repository': 'https://github.com/popiolkoks1/worktree-rescue',
            'description': 'Lifecycle protection and recoverable cleanup for explicitly adopted worktrees.'})
        root = Path(__file__).resolve().parents[1]
        hooks = read_json(root / 'hooks' / 'hooks.json')
        if not hooks:
            shutil.rmtree(target)
            raise RescueError('Build the companion from a plugin checkout, not the pip installation')
        hooks['hooks'].pop('WorktreeCreate', None)
        hooks['hooks'].pop('WorktreeRemove', None)
        (target / 'hooks').mkdir()
        atomic_json(target / 'hooks' / 'hooks.json', hooks)
        for resource in ('LICENSE', '.claude-plugin/icon.png'):
            if (root / resource).is_file():
                shutil.copy2(root / resource, target / resource)
        (target / 'README.md').write_text(
            '# Worktree Rescue Companion\n\n'
            'Automatically archive inactive, explicitly adopted Git worktrees and restore unfinished work. '
            'This companion observes Claude Code session lifecycle without replacing another plugin’s '
            'worktree creation or removal hooks. Before starting your agent, enroll its linked worktree '
            'with `python3 scripts/rescue.py adopt /absolute/worktree --base main`. '
            'Launch with `claude --plugin-dir /path/to/this/plugin` from that worktree. '
            'Use `/rescue-companion:status`, `/rescue-companion:archives` and '
            '`/rescue-companion:restore ID --to /new/path`. Adopted branches are retained. '
            'Disable automatic deletion in your other tool: the companion cannot prevent it. '
            'Archives are local and unencrypted, and can contain ignored secrets. '
            'Full policy and setup: https://github.com/popiolkoks1/worktree-rescue#readme\n',
            encoding='utf-8')
        if (root / 'commands').is_dir():
            shutil.copytree(root / 'commands', target / 'commands')
        print(str(target))
    elif args.command == 'adopt':
        print(json.dumps(rescue.adopt(args.path, args.base), indent=2))
    elif args.command == 'create':
        print(json.dumps(rescue.create(args.repo, args.name), indent=2))
    elif args.command in ('sweep', 'status'):
        print(json.dumps(rescue.sweep(dry_run=args.command == 'status' or args.dry_run), indent=2))
    elif args.command == 'archives':
        rows = []
        for file in sorted((rescue.home / 'archives').glob('*/manifest.json')):
            meta = read_json(file)
            rows.append({'id': meta['id'], 'branch': meta['record']['branch'], 'path': meta['record']['path'], 'created_at': meta['created_at'], 'bytes': meta['inventory']['bytes']})
        print(json.dumps(rows, indent=2))
    elif args.command == 'restore':
        print(rescue.restore(args.id, args.to, args.repo))
    elif args.command == 'history':
        file = rescue.home / 'history.jsonl'
        print('\n'.join(file.read_text().splitlines()[-30:]) if file.exists() else 'No actions recorded.')
    elif args.command in ('pause', 'resume', 'config'):
        config = dict(rescue.config)
        if args.command in ('pause', 'resume'):
            config['enabled'] = args.command == 'resume'
        elif args.set:
            key, value = args.set
            if key not in DEFAULTS:
                raise RescueError('Unknown setting: ' + key)
            config[key] = json.loads(value)
        with rescue.lock():
            old = read_json(rescue.home / 'config.json', {})
            atomic_json(rescue.home / 'config.json', config)
            try:
                Rescue(rescue.home)
            except Exception:
                atomic_json(rescue.home / 'config.json', old)
                raise
        print(json.dumps(config, indent=2))


def entrypoint():
    try:
        main()
    except (RescueError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('Worktree Rescue: ' + str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    entrypoint()
