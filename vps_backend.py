import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone

BASE_DIR = '/var/lib/vps-bot'
INSTANCES_DIR = os.path.join(BASE_DIR, 'instances')
ROOTFS_DIR = os.path.join(BASE_DIR, 'rootfs')

SUITES = {
    'ubuntu': ('jammy', 'http://archive.ubuntu.com/ubuntu'),
    'debian': ('bookworm', 'http://deb.debian.org/debian'),
}

_setup_lock = asyncio.Lock()


def _total_cpu():
    return os.cpu_count() or 1


def _total_mem_gb():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    kb = float(line.split()[1])
                    return kb / (1024 * 1024)
    except Exception:
        pass
    return 8.0


class DockerBackend:
    name = 'docker'

    def __init__(self):
        self.host_cpus = _total_cpu()
        self.host_mem_gb = _total_mem_gb()

    async def _run(self, args, timeout=120):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, out.decode(errors='replace'), err.decode(errors='replace')
        except asyncio.TimeoutError:
            return -1, '', 'timeout'
        except Exception as e:
            return -1, '', str(e)

    async def run(self, image, hostname, ram, cpu, disk, container_name):
        code, out, err = await self._run([
            'docker', 'run', '-d',
            '--privileged', '--cap-add=ALL',
            '--restart', 'unless-stopped',
            f'--memory={ram}',
            f'--cpus={cpu}',
            f'--hostname={hostname}',
            f'--name={container_name}',
            image,
            'tail', '-f', '/dev/null'
        ], timeout=120)
        if code != 0:
            return None
        return out.strip()

    async def start(self, cid):
        code, _, _ = await self._run(['docker', 'start', cid], timeout=60)
        return code == 0

    async def stop(self, cid):
        code, _, _ = await self._run(['docker', 'stop', cid], timeout=60)
        if code != 0:
            await self._run(['docker', 'kill', cid], timeout=30)
        return code == 0

    async def restart(self, cid):
        code, _, _ = await self._run(['docker', 'restart', cid], timeout=60)
        return code == 0

    async def rm(self, cid):
        code, _, _ = await self._run(['docker', 'rm', '-f', cid], timeout=60)
        return code == 0

    async def install_tmate(self, cid, os_type):
        code, _, err = await self._run([
            'docker', 'exec', cid, 'bash', '-c',
            'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y tmate curl wget sudo openssh-client'
        ], timeout=300)

    async def exec_tmate(self, cid):
        try:
            return await asyncio.create_subprocess_exec(
                'docker', 'exec', cid, 'tmate', '-F',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except Exception:
            return None

    def get_uptime(self, cid):
        try:
            out = subprocess.check_output(
                ['docker', 'inspect', '-f', '{{.State.StartedAt}}', cid],
                stderr=subprocess.STDOUT
            ).decode().strip()
            if not out or out == '<no value>':
                return 'Not running'
            start = datetime.fromisoformat(out.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            d = (now - start).days
            h, rem = divmod((now - start).seconds, 3600)
            m, _ = divmod(rem, 60)
            return f'{d}d {h}h {m}m'
        except Exception:
            return 'Unknown'

    def get_stats(self, cid):
        try:
            out = subprocess.check_output([
                'docker', 'stats', '--no-stream', '--format',
                '{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}',
                cid
            ], stderr=subprocess.STDOUT).decode().strip()
            parts = out.split('\t')
            if len(parts) == 3:
                return {'cpu': parts[0], 'mem': parts[1], 'net': parts[2]}
        except Exception:
            pass
        return {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}

    def get_logs(self, cid, lines=50):
        try:
            out = subprocess.check_output(
                ['docker', 'logs', '--tail', str(lines), cid],
                stderr=subprocess.STDOUT
            ).decode(errors='replace')
            return out[-2000:]
        except Exception:
            return 'Failed to fetch logs'

    def get_status(self, cid):
        try:
            out = subprocess.check_output(
                ['docker', 'inspect', '-f', '{{.State.Status}}', cid],
                stderr=subprocess.STDOUT
            ).decode().strip()
            if out == 'exited':
                return 'stopped'
            return out
        except Exception:
            return 'stopped'


class ProotBackend:
    name = 'proot'

    def __init__(self):
        self.host_cpus = _total_cpu()
        self.host_mem_gb = _total_mem_gb()
        self._tools_checked = False

    async def _run(self, args, timeout=300):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, out.decode(errors='replace'), err.decode(errors='replace')
        except asyncio.TimeoutError:
            return -1, '', 'timeout'
        except Exception as e:
            return -1, '', str(e)

    def _ensure_tools(self):
        if self._tools_checked:
            return
        missing = []
        for tool in ('proot', 'debootstrap'):
            if shutil.which(tool) is None:
                missing.append(tool)
        if missing:
            subprocess.run(
                ['apt-get', 'update', '-y'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            subprocess.run(
                ['apt-get', 'install', '-y'] + missing,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        self._tools_checked = True

    def _rootfs_path(self, os_type):
        return os.path.join(ROOTFS_DIR, os_type)

    def _instance_path(self, cid):
        return os.path.join(INSTANCES_DIR, cid)

    def _meta_path(self, cid):
        return os.path.join(self._instance_path(cid), 'meta.json')

    def _log_path(self, cid):
        return os.path.join(self._instance_path(cid), 'vps.log')

    async def _prepare_rootfs(self, os_type):
        async with _setup_lock:
            self._ensure_tools()
            suite, mirror = SUITES[os_type]
            rootfs = self._rootfs_path(os_type)
            if os.path.isdir(rootfs) and os.path.isfile(os.path.join(rootfs, '.ready')):
                return True
            os.makedirs(ROOTFS_DIR, exist_ok=True)
            if os.path.isdir(rootfs):
                subprocess.run(['rm', '-rf', rootfs])
            code, _, err = await self._run([
                'debootstrap', '--variant=minimal', '--arch=amd64',
                suite, rootfs, mirror
            ], timeout=1800)
            if code != 0:
                return False
            with open(os.path.join(rootfs, '.ready'), 'w') as f:
                f.write('ok')
            return True

    def _bind_args(self, rootfs):
        return [
            'proot', '-0', '-R', rootfs,
            '-b', '/proc:/proc',
            '-b', '/dev:/dev',
            '-b', '/sys:/sys',
            '-b', f'/etc/resolv.conf:/etc/resolv.conf'
        ]

    def _spawn(self, cid, os_type):
        inst = self._instance_path(cid)
        log_file = open(self._log_path(cid), 'ab')
        proc = subprocess.Popen(
            self._bind_args(inst) + ['/bin/bash', '-c', 'exec tail -f /dev/null'],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True
        )
        meta = {
            'pid': proc.pid,
            'os': os_type,
            'started_at': datetime.now(timezone.utc).isoformat()
        }
        with open(self._meta_path(cid), 'w') as f:
            json.dump(meta, f)
        return meta

    async def run(self, image, hostname, ram, cpu, disk, container_name):
        os_type = image.split(':')[0]
        if os_type not in SUITES:
            return None
        if not await self._prepare_rootfs(os_type):
            return None
        cid = uuid.uuid4().hex[:12]
        inst = self._instance_path(cid)
        os.makedirs(INSTANCES_DIR, exist_ok=True)
        code, _, err = await self._run(['cp', '-a', self._rootfs_path(os_type), inst], timeout=900)
        if code != 0:
            return None
        self._spawn(cid, os_type)
        return cid

    async def start(self, cid):
        if not self._instance_path(cid):
            return False
        meta = self._load_meta(cid)
        if meta and self._is_alive(meta.get('pid')):
            return True
        if not meta:
            meta = {'os': 'debian'}
        self._spawn(cid, meta.get('os', 'debian'))
        return True

    async def stop(self, cid):
        meta = self._load_meta(cid)
        if not meta:
            return False
        pid = meta.get('pid')
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if not os.path.isdir(self._instance_path(cid)):
            return True
        for _ in range(20):
            if not self._is_alive(pid):
                break
            await asyncio.sleep(0.5)
        return True

    async def restart(self, cid):
        await self.stop(cid)
        await asyncio.sleep(1)
        meta = self._load_meta(cid)
        if not meta:
            return False
        self._spawn(cid, meta.get('os', 'debian'))
        return True

    async def rm(self, cid):
        await self.stop(cid)
        meta = self._load_meta(cid)
        if meta and meta.get('pid'):
            try:
                os.kill(meta['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.sleep(1)
        subprocess.run(['rm', '-rf', self._instance_path(cid)])
        return True

    async def install_tmate(self, cid, os_type):
        inst = self._instance_path(cid)
        code, _, err = await self._run(
            self._bind_args(inst) + ['/bin/bash', '-c',
             'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y tmate curl wget sudo openssh-client'],
            timeout=600
        )

    async def exec_tmate(self, cid):
        try:
            return await asyncio.create_subprocess_exec(
                *self._bind_args(self._instance_path(cid)),
                '/bin/bash', '-c', 'tmate -F',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except Exception:
            return None

    def _load_meta(self, cid):
        try:
            with open(self._meta_path(cid)) as f:
                return json.load(f)
        except Exception:
            return None

    def _is_alive(self, pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def get_uptime(self, cid):
        meta = self._load_meta(cid)
        if not meta or not self._is_alive(meta.get('pid')):
            return 'Not running'
        try:
            start = datetime.fromisoformat(meta['started_at'])
            now = datetime.now(timezone.utc)
            uptime = now - start
            days = uptime.days
            hours, rem = divmod(uptime.seconds, 3600)
            minutes, _ = divmod(rem, 60)
            return f'{days}d {hours}h {minutes}m'
        except Exception:
            return 'Unknown'

    def get_stats(self, cid):
        return {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}

    def get_logs(self, cid, lines=50):
        try:
            with open(self._log_path(cid), 'rb') as f:
                data = f.read()
            text = data.decode(errors='replace')
            return text[-2000:]
        except Exception:
            return 'Failed to fetch logs'

    def get_status(self, cid):
        meta = self._load_meta(cid)
        if not meta:
            return 'stopped'
        return 'running' if self._is_alive(meta.get('pid')) else 'stopped'


def _probe_docker():
    try:
        r = subprocess.run(['unshare', '--mount', 'true'], capture_output=True, timeout=10)
        if r.returncode != 0:
            return False
    except Exception:
        return False
    if not _docker_alive():
        _start_dockerd()
    if not _docker_alive():
        return False
    return _run_docker(['hello-world']) == 0


def _docker_alive():
    try:
        r = subprocess.run(['docker', 'info'], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def _start_dockerd():
    try:
        log = open('/var/log/dockerd.log', 'ab')
        subprocess.Popen(
            ['dockerd'],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        for _ in range(20):
            time.sleep(1)
            if _docker_alive():
                return True
    except Exception:
        pass
    return _docker_alive()


def _run_docker(args):
    try:
        return subprocess.run(
            ['docker', 'run', '--rm'] + args,
            capture_output=True,
            timeout=60
        ).returncode
    except Exception:
        return -1


def get_backend():
    if os.environ.get('VPS_BACKEND', 'auto').lower() == 'docker':
        return DockerBackend()
    if os.environ.get('VPS_BACKEND', 'auto').lower() == 'proot':
        return ProotBackend()
    if _probe_docker():
        return DockerBackend()
    return ProotBackend()