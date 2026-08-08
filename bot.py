import random
import logging
import subprocess
import sys
import os
import re
import time
import discord
from discord.ext import commands, tasks
import asyncio
from discord import app_commands
import sqlite3
from dotenv import load_dotenv
from datetime import datetime, timezone

# ============ VPS BACKEND (Docker + Proot auto-detect) ============
import json
import shutil
import signal
import tarfile
import urllib.request
import uuid
import socket

logger = logging.getLogger('vps_backend')

BBASE_DIR = '/var/lib/vps-bot'
BINSTANCES_DIR = os.path.join(BBASE_DIR, 'instances')
BROOTFS_DIR = os.path.join(BBASE_DIR, 'rootfs')

BSUITES = {
    'ubuntu': ('jammy', 'http://archive.ubuntu.com/ubuntu'),
    'debian': ('bookworm', 'http://deb.debian.org/debian'),
}

BIMAGES = {
    'ubuntu': 'ubuntu:22.04',
    'debian': 'debian:bookworm',
}

BUBUNTU_BASE_URLS = [
    'http://cdimage.ubuntu.com/ubuntu-base/releases/jammy/release/ubuntu-base-22.04.4-base-amd64.tar.gz',
    'http://cdimage.ubuntu.com/ubuntu-base/releases/jammy/release/ubuntu-base-22.04.3-base-amd64.tar.gz',
    'http://cdimage.ubuntu.com/ubuntu-base/releases/jammy/release/ubuntu-base-22.04.2-base-amd64.tar.gz',
    'http://cdimage.ubuntu.com/ubuntu-base/releases/jammy/release/ubuntu-base-22.04.1-base-amd64.tar.gz',
]

BTMATE_STATIC_URL = 'https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-amd64.tar.xz'
BUSYBOX_STATIC_URL = 'https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox'
BTTYD_URL = 'https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64'
BCLOUDFLARED_URL = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64'
BNEOFETCH_URL = 'https://raw.githubusercontent.com/dylanaraps/neofetch/master/neofetch'

bsetup_lock = asyncio.Lock()


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
            logger.error(f"Docker run failed: {err}")
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
        _, _, err = await self._run([
            'docker', 'exec', cid, 'bash', '-c',
            'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y tmate curl wget sudo openssh-client'
        ], timeout=300)
        if err:
            logger.warning(f"Tmate install in {cid}: {err[-300:]}")

    async def exec_tmate(self, cid):
        try:
            return await asyncio.create_subprocess_exec(
                'docker', 'exec', cid, 'tmate', '-F',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
        except Exception as e:
            logger.error(f"Tmate exec error for {cid}: {e}")
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
            uptime = datetime.now(timezone.utc) - start
            d = uptime.days
            h, rem = divmod(uptime.seconds, 3600)
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
            up = subprocess.run(
                ['apt-get', 'update', '-y'],
                capture_output=True,
                text=True
            )
            if up.returncode != 0:
                logger.error(f"apt-get update failed: {up.stderr[-300:]}")
            inst = subprocess.run(
                ['apt-get', 'install', '-y'] + missing,
                capture_output=True,
                text=True
            )
            if inst.returncode != 0:
                logger.error(f"apt-get install {missing} failed: {inst.stderr[-300:]}")
        for tool in ('proot', 'debootstrap'):
            if shutil.which(tool) is None:
                logger.error(f"Required tool not available: {tool}")
        self._tools_checked = True

    def _rootfs_path(self, os_type):
        return os.path.join(BROOTFS_DIR, os_type)

    def _instance_path(self, cid):
        return os.path.join(BINSTANCES_DIR, cid)

    def _meta_path(self, cid):
        return os.path.join(self._instance_path(cid), 'meta.json')

    def _log_path(self, cid):
        return os.path.join(self._instance_path(cid), 'vps.log')

    async def _prepare_rootfs(self, os_type):
        async with bsetup_lock:
            self._ensure_tools()
            rootfs = self._rootfs_path(os_type)
            ready = False
            ready_path = os.path.join(rootfs, '.ready')
            if os.path.isfile(ready_path):
                try:
                    with open(ready_path) as f:
                        ready = f.read().strip() == '4'
                except Exception:
                    ready = False
            if ready:
                self._ensure_ca_certs(rootfs)
                self._ensure_neofetch(rootfs)
                return True
            if os.path.isdir(rootfs):
                subprocess.run(['rm', '-rf', rootfs])
            os.makedirs(BROOTFS_DIR, exist_ok=True)
            makers = ('_rootfs_from_tarball', '_rootfs_from_docker', '_rootfs_from_debootstrap')
            for m in makers:
                ok = await getattr(self, m)(os_type, rootfs)
                if ok:
                    await self._bake_static_tools(rootfs)
                    self._ensure_neofetch(rootfs)
                    with open(ready_path, 'w') as f:
                        f.write('4')
                    logger.info(f"Rootfs for {os_type} ready (static tools baked)")
                    return True
                logger.warning(f"Rootfs method {m} failed for {os_type}")
            subprocess.run(['rm', '-rf', rootfs])
            return False

    def _ensure_neofetch(self, rootfs):
        try:
            dst = os.path.join(rootfs, 'usr', 'local', 'bin', 'neofetch')
            if os.path.isfile(dst) and os.path.getsize(dst) > 10000:
                return
            tmp = os.path.join(BROOTFS_DIR, 'neofetch.dl')
            self._download(BNEOFETCH_URL, tmp)
            shutil.copy2(tmp, dst)
            os.chmod(dst, 0o755)
            os.remove(tmp)
            logger.info("neofetch baked into rootfs")
        except Exception as e:
            logger.error(f"neofetch bake failed: {e}")

    async def _bake_static_tools(self, rootfs):
        try:
            shutil.copy('/etc/resolv.conf', os.path.join(rootfs, 'etc', 'resolv.conf'))
        except Exception as e:
            logger.error(f"Resolv copy failed: {e}")
        try:
            btmp = os.path.join(BROOTFS_DIR, 'busybox.static')
            await asyncio.to_thread(self._download, BUSYBOX_STATIC_URL, btmp)
            shutil.copy2(btmp, os.path.join(rootfs, 'usr', 'local', 'bin', 'busybox'))
            os.chmod(os.path.join(rootfs, 'usr', 'local', 'bin', 'busybox'), 0o755)
            for link in ('wget', 'ping', 'nc', 'tar', 'awk', 'sed', 'top', 'uname'):
                ln = os.path.join(rootfs, 'usr', 'local', 'bin', link)
                if not os.path.exists(ln):
                    os.symlink('busybox', ln)
            os.remove(btmp)
        except Exception as e:
            logger.error(f"Busybox static bake failed: {e}")
        try:
            ttmp = os.path.join(BROOTFS_DIR, 'ttyd.static')
            await asyncio.to_thread(self._download, BTTYD_URL, ttmp)
            shutil.copy2(ttmp, os.path.join(rootfs, 'usr', 'local', 'bin', 'ttyd'))
            os.chmod(os.path.join(rootfs, 'usr', 'local', 'bin', 'ttyd'), 0o755)
            os.remove(ttmp)
        except Exception as e:
            logger.error(f"ttyd bake failed: {e}")
        try:
            ctmp = os.path.join(BROOTFS_DIR, 'cloudflared.static')
            await asyncio.to_thread(self._download, BCLOUDFLARED_URL, ctmp)
            shutil.copy2(ctmp, os.path.join(rootfs, 'usr', 'local', 'bin', 'cloudflared'))
            os.chmod(os.path.join(rootfs, 'usr', 'local', 'bin', 'cloudflared'), 0o755)
            os.remove(ctmp)
        except Exception as e:
            logger.error(f"cloudflared bake failed: {e}")
    def _ensure_ca_certs(self, rootfs):
        try:
            certdir = os.path.join(rootfs, 'etc', 'ssl', 'certs')
            os.makedirs(certdir, exist_ok=True)
            src = '/etc/ssl/certs/ca-certificates.crt'
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(certdir, 'ca-certificates.crt'))
            else:
                subprocess.run(['update-ca-certificates'], capture_output=True, timeout=60)
            logger.info("CA certificates baked into rootfs")
        except Exception as e:
            logger.error(f"CA cert bake failed: {e}")

    @staticmethod
    def _extract_tar_xz(src, dst):
        with tarfile.open(src, 'r:xz') as tar:
            tar.extractall(path=dst)

    async def _rootfs_from_tarball(self, os_type, rootfs):
        try:
            tmp = os.path.join(BROOTFS_DIR, 'download.tmp')
            if os.path.exists(tmp):
                os.remove(tmp)
            for url in BUBUNTU_BASE_URLS:
                try:
                    await asyncio.to_thread(self._download, url, tmp)
                    await asyncio.to_thread(self._extract_tar_gz, tmp, rootfs)
                    os.remove(tmp)
                    logger.info(f"Rootfs for {os_type} extracted from {url}")
                    return True
                except Exception as e:
                    logger.warning(f"Tarball {url} failed: {e}")
                    continue
        except Exception as e:
            logger.error(f"Tarball download error: {e}")
        return False

    @staticmethod
    def _download(url, dest):
        req = urllib.request.Request(url, headers={'User-Agent': 'vps-bot/1.0'})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, 'wb') as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    return
                f.write(chunk)

    @staticmethod
    def _extract_tar_gz(src, dst):
        with tarfile.open(src, 'r:gz') as tar:
            tar.extractall(path=dst)

    async def _rootfs_from_docker(self, os_type, rootfs):
        image = BIMAGES[os_type]
        tmp_name = 'vps-rootfs-exporter'
        try:
            subprocess.run(['docker', 'rm', '-f', tmp_name], capture_output=True, timeout=30)
        except Exception:
            pass
        try:
            r = subprocess.run(
                ['docker', 'create', '--name', tmp_name, image],
                capture_output=True,
                timeout=180
            )
            if r.returncode != 0:
                logger.error(f"docker create for rootfs failed: {r.stderr.decode(errors='replace')[-300:]}")
                return False
            proc = subprocess.Popen(
                ['docker', 'export', tmp_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            try:
                with tarfile.open(fileobj=proc.stdout, mode='r|') as tar:
                    tar.extractall(path=rootfs)
            except Exception as e:
                logger.error(f"rootfs extract error: {e}")
            proc.wait(timeout=300)
            subprocess.run(['docker', 'rm', '-f', tmp_name], capture_output=True, timeout=30)
            if proc.returncode != 0:
                logger.error("docker export failed")
                return False
            return True
        except Exception as e:
            logger.error(f"docker rootfs export error: {e}")
            return False

    async def _rootfs_from_debootstrap(self, os_type, rootfs):
        try:
            suite, mirror = BSUITES[os_type]
        except KeyError:
            return False
        code, out, err = await self._run([
            'debootstrap', '--variant=minimal', '--arch=amd64',
            suite, rootfs, mirror
        ], timeout=1800)
        if code != 0:
            logger.error(f"debootstrap {os_type} failed: {err[-500:]}")
            return False
        return True

    def _bind_args(self, rootfs):
        args = [
            'proot', '-0', '-R', rootfs,
            '-b', '/proc:/proc',
            '-b', '/dev:/dev',
            '-b', '/sys:/sys',
            '-b', '/etc/resolv.conf:/etc/resolv.conf'
        ]
        try:
            cid = os.path.basename(rootfs)
            meta = self._load_meta(cid)
            if meta and meta.get('ram'):
                meminfo, cpuinfo = self._ensure_fakeinfo(cid, meta.get('ram'), meta.get('cpu'))
                args.extend(['-b', f'{meminfo}:/proc/meminfo', '-b', f'{cpuinfo}:/proc/cpuinfo'])
        except Exception as e:
            logger.debug(f"fakeinfo bind skipped: {e}")
        return args

    def _ensure_fakeinfo(self, cid, ram, cpu):
        import re as _re
        fake_dir = os.path.join(BINSTANCES_DIR, 'fakeinfo')
        os.makedirs(fake_dir, exist_ok=True)
        try:
            n = int(_re.search(r'-?\d+', str(ram)).group())
        except Exception:
            n = 8
        try:
            cores = max(1, int(_re.search(r'-?\d+', str(cpu)).group()))
        except Exception:
            cores = 2
        low = str(ram or '').lower()
        mb = n if ('m' in low and 'g' not in low) else n * 1024
        total_kb = mb * 1024
        free_kb = int(total_kb * 0.68)
        avail_kb = int(total_kb * 0.62)
        cache_kb = int(total_kb * 0.25)
        meminfo_path = os.path.join(fake_dir, f'{cid}-meminfo')
        if not os.path.isfile(meminfo_path):
            with open(meminfo_path, 'w') as f:
                f.write(f"""MemTotal:        {total_kb} kB
MemFree:         {free_kb} kB
MemAvailable:    {avail_kb} kB
Buffers:            {cache_kb // 4} kB
Cached:            {cache_kb} kB
SwapCached:             0 kB
Active:                 0 kB
Inactive:               0 kB
Dirty:                  0 kB
Writeback:              0 kB
AnonPages:              0 kB
Mapped:                 0 kB
Shmem:                  0 kB
SwapTotal:             0 kB
SwapFree:              0 kB
DirtyThreshold:         0 kB
DirtyBackgroundThreshold: 0 kB
SlabReclaimable:        0 kB
SlabUnreclaimable:      0 kB
""")
        cpuinfo_path = os.path.join(fake_dir, f'{cid}-cpuinfo')
        if not os.path.isfile(cpuinfo_path):
            blocks = []
            for i in range(cores):
                blocks.append(f"""processor       : {i}
vendor_id       : GenuineIntel
cpu family      : 6
model           : 85
model name      : Intel(R) Xeon(R) CPU @ 2.30GHz
stepping        : 7
cpu MHz         : 2300.000
cache size      : 33792 KB
physical id     : {i // 2}
siblings        : {cores}
core id         : {i % 2}
cpu cores       : {cores}
apicid          : {i}
fpu             : yes
fpu_exception   : yes
cpuid level     : 22
wp              : yes
flags           : fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ss ht syscall nx lm
bogomips        : 4600.00
clflush size    : 64
cache_alignment : 64
address sizes   : 46 bits physical, 48 bits virtual
power management:

""")
            with open(cpuinfo_path, 'w') as f:
                f.write(''.join(blocks))
        return meminfo_path, cpuinfo_path

    def _spawn(self, cid, os_type):
        inst = self._instance_path(cid)
        log_file = open(self._log_path(cid), 'ab')
        try:
            proc = subprocess.Popen(
                self._bind_args(inst) + ['/bin/bash', '-c', 'exec tail -f /dev/null'],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True
            )
        except Exception as e:
            logger.error(f"Spawn failed for {cid}: {e}")
            return None
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
        if os_type not in BSUITES:
            logger.error(f"Unsupported OS type: {os_type}")
            return None
        if not await self._prepare_rootfs(os_type):
            logger.error(f"Rootfs creation failed for {os_type}")
            return None
        cid = uuid.uuid4().hex[:12]
        inst = self._instance_path(cid)
        os.makedirs(BINSTANCES_DIR, exist_ok=True)
        code, out, err = await self._run(['cp', '-a', self._rootfs_path(os_type), inst], timeout=900)
        if code != 0:
            logger.error(f"Rootfs copy failed: {err}")
            return None
        if self._spawn(cid, os_type) is None:
            subprocess.run(['rm', '-rf', inst])
            return None
        meta = self._load_meta(cid)
        if meta:
            meta['ram'] = ram
            meta['cpu'] = cpu
            meta['disk'] = disk
            with open(self._meta_path(cid), 'w') as f:
                json.dump(meta, f)
        logger.info(f"Proot VPS created: {cid} ({os_type})")
        return cid

    async def start(self, cid):
        meta = self._load_meta(cid)
        if not os.path.isdir(self._instance_path(cid)):
            return False
        if meta and self._is_alive(meta.get('pid')):
            return True
        self._spawn(cid, (meta or {}).get('os', 'debian'))
        return True

    async def stop(self, cid):
        meta = self._load_meta(cid)
        if not meta:
            return True
        pid = meta.get('pid')
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for _ in range(20):
            if not self._is_alive(pid):
                break
            await asyncio.sleep(0.5)
        return True

    async def restart(self, cid):
        await self.stop(cid)
        await asyncio.sleep(1)
        return await self.start(cid)

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
        logger.info(f"Tmate removed for {cid} (web terminal only)")

    def _instance_env(self):
        return {**os.environ, 'TERM': 'xterm', 'HOME': '/root'}

    def _proot_base(self, cid):
        return self._bind_args(self._instance_path(cid))

    async def exec_web_terminal(self, cid):
        inst = self._instance_path(cid)
        import random as _r
        cred_u = "happy"
        cred_p = "node"
        base = self._bind_args(inst)
        host_tools = os.path.join(BBASE_DIR, 'hostbin')
        os.makedirs(host_tools, exist_ok=True)
        host_ttyd = os.path.join(host_tools, 'ttyd')
        try:
            rootfs = self._rootfs_path('ubuntu')
            if os.path.isdir(rootfs):
                rbin = os.path.join(rootfs, 'usr', 'local', 'bin', 'ttyd')
                if os.path.isfile(rbin) and not os.path.isfile(host_ttyd):
                    shutil.copy2(rbin, host_ttyd)
                    os.chmod(host_ttyd, 0o755)
        except Exception as e:
            logger.error(f"host ttyd copy failed: {e}")
        for attempt in range(4):
            port = 7681 + _r.randint(0, 999)
            ttyd = None
            cf = None
            ttyd_lines = []
            rb = os.path.join(self._instance_path(cid), 'usr', 'local', 'bin', 'ttyd')
            cmd = None
            ttyd_flags = ['--writable', '--port', str(port), '--credential', f"{cred_u}:{cred_p}"]
            if os.path.isfile(rb):
                cmd = [*base, rb, *ttyd_flags, '/bin/bash']
            elif os.path.isfile(host_ttyd):
                cmd = [host_ttyd, *ttyd_flags, *base, '/bin/bash']
            if not cmd:
                logger.error("ttyd binary not found in rootfs or host")
                return None
            try:
                ttyd = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env=self._instance_env()
                )
            except Exception as e:
                logger.error(f"ttyd start failed: {e}")
                return None

            async def _drain_ttyd():
                try:
                    while True:
                        line_b = await ttyd.stdout.readline()
                        if not line_b:
                            break
                        if len(ttyd_lines) < 50:
                            ttyd_lines.append(line_b.decode(errors='replace').strip()[:220])
                except Exception:
                    pass
            _drain_task = asyncio.create_task(_drain_ttyd())

            alive = False
            for probe_i in range(5):
                if ttyd.returncode is not None:
                    break
                try:
                    s = socket.create_connection(('127.0.0.1', port), timeout=1.5)
                    s.close()
                    alive = True
                    break
                except Exception:
                    await asyncio.sleep(1.0)
            if not alive:
                try:
                    _drain_task.cancel()
                except Exception:
                    pass
                try:
                    ttyd.kill()
                    await asyncio.wait_for(ttyd.wait(), timeout=3)
                except Exception:
                    pass
                logger.error(f"ttyd NOT listening on {port}. Output: {' | '.join(ttyd_lines[:10])}")
                continue
            logger.info(f"ttyd up on port {port}")
            try:
                cf = await asyncio.create_subprocess_exec(
                    *base, '/usr/local/bin/cloudflared', 'tunnel', '--no-autoupdate',
                    '--url', f'http://localhost:{port}',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env=self._instance_env()
                )
            except Exception as e:
                logger.error(f"cloudflared start failed: {e}")
                try:
                    ttyd.kill()
                    await ttyd.wait()
                except Exception:
                    pass
                return None
            TUNNEL_SKIP_HOSTS = {'api', 'www', 'dash', 'developers', 'support', 'community', 'cloudflare'}
            try:
                for _ in range(60):
                    try:
                        line_b = await asyncio.wait_for(cf.stdout.readline(), timeout=2.5)
                    except asyncio.TimeoutError:
                        continue
                    if not line_b:
                        raise RuntimeError('cloudflared exited early')
                    line = ANSI_RE.sub('', line_b.decode(errors='replace')).strip()
                    logger.info(f"cloudflared: {line[:160]}")
                    low = line.lower()
                    if 'trycloudflare.com' in low:
                        m = re.search(r'https://([a-z0-9-]+)\.trycloudflare\.com', low)
                        if m and m.group(1) not in TUNNEL_SKIP_HOSTS:
                            return f"URL: {m.group(0)}\nLogin: {cred_u}\nPassword: {cred_p}"
                raise RuntimeError('cloudflared produced no tunnel URL')
            except Exception as e:
                logger.error(f"Web terminal failed (attempt {attempt + 1}): {e}")
                for p in (cf, ttyd):
                    try:
                        p.kill()
                    except Exception:
                        pass
                await asyncio.sleep(1)
                for p in (cf, ttyd):
                    try:
                        p.kill()
                    except Exception:
                        pass
        logger.error("Web terminal failed after 4 attempts")
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
            uptime = datetime.now(timezone.utc) - start
            h, rem = divmod(uptime.seconds, 3600)
            m, _ = divmod(rem, 60)
            return f"{uptime.days}d {h}h {m}m"
        except Exception:
            return 'Unknown'

    def get_stats(self, cid):
        return {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}

    def get_logs(self, cid, lines=50):
        try:
            with open(self._log_path(cid), 'rb') as f:
                data = f.read()
            return data.decode(errors='replace')[-2000:]
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
    try:
        r = subprocess.run(
            ['docker', 'run', '--rm', 'hello-world'],
            capture_output=True,
            timeout=60
        )
        return r.returncode == 0
    except Exception:
        return False


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


def get_backend():
    mode = os.environ.get('VPS_BACKEND', 'auto').lower()
    if mode == 'docker':
        return DockerBackend()
    if mode == 'proot':
        return ProotBackend()
    if _probe_docker():
        return DockerBackend()
    return ProotBackend()

# ============ END VPS BACKEND ============

# Load environment variables
load_dotenv()

# Configuration from .env
TOKEN = os.getenv('TOKEN', 'DISCORD_BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))  # Admin user ID for checks
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'HappyNodes')
WATERMARK = os.getenv('WATERMARK', 'Powered by HappyNodes VPS Bot')
# VPS Defaults from .env
DEFAULT_RAM = os.getenv('DEFAULT_RAM', '2g')  # e.g., '2g', '4G'
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')  # Lowered default to '1' to avoid common errors
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '5G')  # e.g., '20G' - Note: Disk limit not enforced in container
VPS_HOSTNAME = os.getenv('VPS_HOSTNAME', 'happy-free')  # Base hostname, append user ID
SERVER_LIMIT = int(os.getenv('SERVER_LIMIT', 1))
TOTAL_SERVER_LIMIT = int(os.getenv('TOTAL_SERVER_LIMIT', 50))  # Global total running server limit
DATABASE_FILE = os.getenv('DATABASE_FILE', 'vps_bot.db')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vps_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)
backend = get_backend()
logger.info(f"Backend selected: {backend.name}")

def is_admin(member):
    if not isinstance(member, discord.Member):
        logger.warning("is_admin called with non-Member object")
        return False
    # Check user ID for admin access
    return member.id == ADMIN_ID

# Database setup with SQLite3
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    default_ram = DEFAULT_RAM
    default_cpu = DEFAULT_CPU
    default_disk = DEFAULT_DISK
    sql = f'''
        CREATE TABLE IF NOT EXISTS vps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            container_id TEXT UNIQUE NOT NULL,
            container_name TEXT NOT NULL,
            os_type TEXT NOT NULL,
            hostname TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            ssh_command TEXT,
            ram TEXT DEFAULT '{default_ram}',
            cpu TEXT DEFAULT '{default_cpu}',
            disk TEXT DEFAULT '{default_disk}',
            suspended INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    '''
    cursor.execute(sql)
    cursor.execute("PRAGMA table_info(vps)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'suspended' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN suspended INTEGER DEFAULT 0")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def add_user(user_id, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def add_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO bans (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM bans WHERE user_id = ?', (user_id,))
    banned = cursor.fetchone() is not None
    conn.close()
    return banned

def add_vps(user_id, container_id, container_name, os_type, hostname, ssh_command, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, status, ssh_command, ram, cpu, disk, suspended)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 0)
    ''', (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk))
    conn.commit()
    conn.close()

def get_user_vps(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    vps_list = cursor.fetchall()
    conn.close()
    return vps_list

def count_user_vps(user_id):
    return len(get_user_vps(user_id))

def get_vps_by_container_id(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE container_id = ?', (container_id,))
    vps = cursor.fetchone()
    conn.close()
    return vps

def get_vps_by_identifier(user_id, identifier):
    vps_list = get_user_vps(user_id)
    if not identifier:
        return vps_list[0] if vps_list else None
    identifier_lower = identifier.lower()
    for vps in vps_list:
        if (identifier_lower in vps['container_id'].lower() or
            identifier_lower in vps['container_name'].lower()):
            return vps
    return None

def update_vps_status(container_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET status = ? WHERE container_id = ?', (status, container_id))
    conn.commit()
    conn.close()

def update_vps_ssh(container_id, ssh_command):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET ssh_command = ? WHERE container_id = ?', (ssh_command, container_id))
    conn.commit()
    conn.close()

def update_vps_suspended(container_id, suspended):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET suspended = ? WHERE container_id = ?', (suspended, container_id))
    conn.commit()
    conn.close()

def delete_vps(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM vps WHERE container_id = ?', (container_id,))
    conn.commit()
    conn.close()

def get_total_instances():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status = "running"')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def parse_gb(resource_str):
    match = re.match(r'(\d+(?:\.\d+)?)([mMgG])?', resource_str.lower())
    if match:
        num = float(match.group(1))
        unit = match.group(2) or 'g'
        if unit in ['g', '']:
            return num
        elif unit in ['m']:
            return num / 1024.0
    return 0.0

def get_uptime(container_id):
    return backend.get_uptime(container_id)

def get_stats(container_id):
    return backend.get_stats(container_id)

def get_logs(container_id, lines=50):
    return backend.get_logs(container_id, lines)

# Async VPS helpers
async def async_docker_run(image, hostname, ram, cpu, disk, container_name):
    global backend
    if backend.name != 'proot':
        cid = await backend.run(image, hostname, ram, cpu, disk, container_name)
        if cid is None:
            logger.warning("Docker VPS creation failed - switching to Proot backend")
            backend = ProotBackend()
            return await backend.run(image, hostname, ram, cpu, disk, container_name)
        return cid
    return await backend.run(image, hostname, ram, cpu, disk, container_name)

async def async_docker_start(container_id):
    return await backend.start(container_id)

async def async_docker_stop(container_id):
    return await backend.stop(container_id)

async def async_docker_restart(container_id):
    return await backend.restart(container_id)

async def async_docker_rm(container_id):
    return await backend.rm(container_id)

async def async_install_tmate(container_id, os_type):
    await backend.install_tmate(container_id, os_type)

# SSH capture
async def capture_ssh_session_line(process):
    collected = []
    while True:
        try:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            if not output:
                break
            line = clean_line(output.decode('utf-8', errors='replace'))
            if not line:
                continue
            collected.append(line)
            logger.debug(f"Tmate line: {line}")
            if "ssh session:" in line.lower():
                return line.split("ssh session:")[-1].strip()
        except asyncio.TimeoutError:
            break
    if collected:
        logger.warning(f"Tmate produced no session line. Lines: {' | '.join(collected[-6:])}")
    else:
        logger.warning("Tmate produced no output at all")
    return None

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def clean_line(line):
    return ANSI_RE.sub('', line).strip()


async def proot_web_terminal_session(container_id):
    be = globals().get('backend')
    fn = getattr(be, 'exec_web_terminal', None)
    if not fn:
        logger.warning("web terminal not supported by current backend, skipping")
        return None
    try:
        return await fn(container_id)
    except Exception as e:
        logger.error(f"web terminal session error: {e}")
        return None


async def get_ssh_line(container_id):
    web_line = await proot_web_terminal_session(container_id)
    if web_line:
        logger.info("SSH session obtained via cloud web terminal")
        return web_line
    logger.warning("web terminal failed")
    return None


# Generic regen SSH
async def regen_ssh_command(interaction: discord.Interaction, vps_identifier, send_response=True, target_user=None):
    if target_user is None:
        target_user = interaction.user
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No active VPS found.", color=discord.Color.red())
        if send_response:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if vps['status'] != "running":
        embed = discord.Embed(description="VPS must be running to generate SSH.", color=discord.Color.red())
        if send_response:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if send_response:
        await interaction.response.defer(ephemeral=True)
    container_id = vps['container_id']
    ssh_line = await get_ssh_line(container_id)
    if ssh_line:
        update_vps_ssh(container_id, ssh_line)
        embed = discord.Embed(title="New SSH Session Generated", description=f"```{ssh_line}```", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
        try:
            await target_user.send(embed=embed)
        except discord.Forbidden:
            logger.warning(f"Cannot DM user {target_user.id}")
            if send_response:
                embed_dm_fail = discord.Embed(description="New SSH session generated but could not send to DMs (privacy settings).", color=discord.Color.orange())
                await interaction.followup.send(embed=embed_dm_fail, ephemeral=True)
            else:
                return True
        if send_response:
            embed_success = discord.Embed(description="New SSH session sent to your DMs.", color=discord.Color.green())
            await interaction.followup.send(embed=embed_success, ephemeral=True)
        return True
    else:
        embed = discord.Embed(description="Failed to generate SSH session.", color=discord.Color.red())
        if send_response:
            await interaction.followup.send(embed=embed, ephemeral=True)
        return False
# Reinstall helper
async def reinstall_vps(interaction: discord.Interaction, vps_identifier, os_type, target_user=None):
    if target_user is None:
        target_user = interaction.user
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No VPS found.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    user_id = vps['user_id']
    hostname = vps['hostname']
    ram, cpu, disk = vps['ram'], vps['cpu'], vps['disk']
    # Stop and remove
    await async_docker_stop(container_id)
    await asyncio.sleep(2)
    await async_docker_rm(container_id)
    delete_vps(container_id)
    # Create new with unique name
    suffix = random.randint(1000, 9999)
    new_container_name = f"{os_type}-vps-{user_id}-{suffix}"
    image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
    new_container_id = await async_docker_run(image, hostname, ram, cpu, disk, new_container_name)
    if new_container_id:
        await async_install_tmate(new_container_id, os_type)
        await asyncio.sleep(10)  # Wait longer for install
        ssh_line = await get_ssh_line(new_container_id)
        if ssh_line:
            add_vps(user_id, new_container_id, new_container_name, os_type, hostname, ssh_line, ram, cpu, disk)
            os_name = "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"
            embed = discord.Embed(title="VPS Reinstalled Successfully", description=f"OS: {os_name}\n```{ssh_line}```", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
            embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
            try:
                await target_user.send(embed=embed)
            except discord.Forbidden:
                logger.warning(f"Cannot DM user {target_user.id} for reinstall")
            embed_success = discord.Embed(description="VPS has been reinstalled. Check your DMs for details.", color=discord.Color.green())
            await interaction.followup.send(embed=embed_success, ephemeral=True)
        else:
            embed = discord.Embed(description="Reinstall failed: Unable to generate SSH.", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
            await async_docker_rm(new_container_id)
    else:
        embed = discord.Embed(description="Reinstall failed: Docker creation error.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)

# Create VPS helper
async def create_vps(interaction: discord.Interaction, os_type, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK, target_user=None):
    if target_user is None:
        target_user = interaction.user
    user_id = target_user.id
    username = str(target_user)
    add_user(user_id, username)
    if is_banned(user_id):
        embed = discord.Embed(description="You are banned from creating VPS instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if count_user_vps(user_id) >= SERVER_LIMIT:
        embed = discord.Embed(description=f"You have reached the limit of {SERVER_LIMIT} VPS instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if get_total_instances() >= TOTAL_SERVER_LIMIT:
        embed = discord.Embed(description=f"Global server limit reached: {TOTAL_SERVER_LIMIT} total running instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    # Validate resources against host
    try:
        host_cpus = backend.host_cpus
        host_mem_gb = backend.host_mem_gb
        req_cpu = float(cpu)
        req_ram = parse_gb(ram)
        if req_cpu > host_cpus:
            embed = discord.Embed(description=f"Requested CPU ({req_cpu}) exceeds host limit ({host_cpus}).", color=discord.Color.red())
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if req_ram > host_mem_gb:
            embed = discord.Embed(description=f"Requested RAM ({req_ram}GB) exceeds host limit ({host_mem_gb:.1f}GB).", color=discord.Color.red())
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    except Exception as e:
        logger.error(f"Resource validation failed: {e}")
        embed = discord.Embed(description="Resource validation failed. Please contact an admin.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.NotFound:
        logger.warning("Interaction expired for deploy command")
        return
    except Exception as e:
        logger.error(f"Defer failed: {e}")
        return
    try:
        await interaction.followup.send("Creating your VPS instance...", ephemeral=True)
    except discord.NotFound:
        logger.warning("Followup expired, continuing create")
    hostname = f"{VPS_HOSTNAME}-{user_id}"
    suffix = random.randint(1000, 9999)
    container_name = f"{os_type}-vps-{user_id}-{suffix}"
    image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
    container_id = await async_docker_run(image, hostname, ram, cpu, disk, container_name)
    if not container_id:
        embed = discord.Embed(description="Failed to create Docker container.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    await asyncio.sleep(5)  # Wait for container to start
    await async_install_tmate(container_id, os_type)
    await asyncio.sleep(10)  # Wait for install
    ssh_line = await get_ssh_line(container_id)
    if ssh_line:
        add_vps(user_id, container_id, container_name, os_type, hostname, ssh_line, ram, cpu, disk)
        os_name = "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"
        embed = discord.Embed(title="VPS Instance Created", description=f"OS: {os_name}\nRAM: {ram} | CPU: {cpu} | Disk: {disk}\n```{ssh_line}```", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
        try:
            await target_user.send(embed=embed)
        except discord.Forbidden:
            logger.warning(f"Cannot DM user {target_user.id} for creation")
        embed_success = discord.Embed(description="Your VPS is ready! Check your DMs for access details.", color=discord.Color.green())
        await interaction.followup.send(embed=embed_success, ephemeral=True)
    else:
        embed = discord.Embed(description="Creation failed: Unable to generate SSH session.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)

# Admin helpers
async def admin_manage_vps(interaction: discord.Interaction, target_user_id: int, vps_identifier: str, action: str):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    target_user = await bot.fetch_user(target_user_id)
    if not target_user:
        embed = discord.Embed(description="User not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    vps = get_vps_by_identifier(target_user_id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found for this user.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    container_id = vps['container_id']
    success = False
    if action == "delete":
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)
        delete_vps(container_id)
        success = True
        msg = f"Deleted VPS for {target_user}"
    elif action in ["start", "stop", "restart"]:
        if action == "start":
            success = await async_docker_start(container_id)
            update_vps_status(container_id, "running")
        elif action == "stop":
            success = await async_docker_stop(container_id)
            update_vps_status(container_id, "stopped")
        elif action == "restart":
            success = await async_docker_restart(container_id)
            update_vps_status(container_id, "running")
        msg = f"{action.title()}ed VPS for {target_user}"
    elif action == "suspend":
        success = await async_docker_stop(container_id)
        if success:
            update_vps_status(container_id, "stopped")
            update_vps_suspended(container_id, 1)
        msg = f"Suspended VPS for {target_user}"
    elif action == "unsuspend":
        update_vps_suspended(container_id, 0)
        success = True
        msg = f"Unsuspended VPS for {target_user}. You can now start it."
    if success:
        embed = discord.Embed(title="Admin Action Completed", description=msg, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(description="Action failed.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)

async def admin_kill_all(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id FROM vps WHERE status = "running"')
    running = cursor.fetchall()
    conn.close()
    stopped = 0
    for row in running:
        cid = row['container_id']
        if await async_docker_stop(cid):
            update_vps_status(cid, "stopped")
            stopped += 1
            logger.info(f"Stopped {cid}")
    embed = discord.Embed(title="Admin: Kill All Running VPS", description=f"Successfully stopped {stopped} running VPS instances.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="admin-list", description="Admin: List all VPS instances")
@app_commands.guild_only()
async def admin_list(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.username, v.container_id, v.container_name, v.os_type, v.hostname, v.status, v.ram, v.cpu, v.disk, v.suspended
        FROM vps v JOIN users u ON v.user_id = u.user_id
        ORDER BY v.created_at DESC
    ''')
    all_vps = cursor.fetchall()
    conn.close()
    if not all_vps:
        embed = discord.Embed(description="No VPS instances found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    embed = discord.Embed(title="All VPS Instances", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for row in all_vps[:25]:
        username = row['username']
        container_id = row['container_id']
        container_name = row['container_name']
        os_type = row['os_type']
        hostname = row['hostname']
        status = row['status']
        ram = row['ram']
        cpu = row['cpu']
        disk = row['disk']
        suspended = row['suspended']
        status_emoji = "🟢" if status == "running" else "🔴"
        suspended_text = "(Suspended)" if suspended else ""
        embed.add_field(
            name=f"{status_emoji} {username} - {container_name} ({os_type}) {suspended_text}",
            value=f"ID: ```{container_id}```\nHostname: {hostname}\nStatus: {status}\nResources: {ram} RAM | {cpu} CPU | {disk} Disk",
            inline=False
        )
    if len(all_vps) > 25:
        embed.set_footer(text=f"{WATERMARK} | Showing first 25 of {len(all_vps)}", icon_url=bot.user.avatar.url if bot.user.avatar else None)
    else:
        embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-list-users", description="Admin: List users with VPS counts")
@app_commands.guild_only()
async def admin_list_users(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.username, COUNT(v.id) as total_vps,
               SUM(CASE WHEN v.status = 'running' THEN 1 ELSE 0 END) as running_vps
        FROM users u LEFT JOIN vps v ON u.user_id = v.user_id
        GROUP BY u.user_id, u.username
        ORDER BY total_vps DESC
    ''')
    users = cursor.fetchall()
    conn.close()
    if not users:
        embed = discord.Embed(description="No users found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    embed = discord.Embed(title="Users Overview", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for row in users[:25]:
        username = row['username']
        total = row['total_vps']
        running = row['running_vps'] or 0
        embed.add_field(
            name=username,
            value=f"Total VPS: {total} | Running: {running}",
            inline=False
        )
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-stats", description="Admin: View bot statistics")
@app_commands.guild_only()
async def admin_stats(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    num_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM vps')
    num_vps = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status="running"')
    num_running = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM bans')
    num_banned = cursor.fetchone()[0]
    cursor.execute('SELECT ram, cpu, disk FROM vps WHERE status="running"')
    rows = cursor.fetchall()
    total_cpu = sum(float(row['cpu']) for row in rows)
    total_ram = sum(parse_gb(row['ram']) for row in rows)
    total_disk = sum(parse_gb(row['disk']) for row in rows)
    conn.close()
    embed = discord.Embed(title="Bot Statistics", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Total Users", value=num_users, inline=True)
    embed.add_field(name="Banned Users", value=num_banned, inline=True)
    embed.add_field(name="Total VPS", value=num_vps, inline=True)
    embed.add_field(name="Running VPS", value=num_running, inline=True)
    embed.add_field(name="Total CPU Allocated", value=f"{total_cpu} cores", inline=True)
    embed.add_field(name="Total RAM Allocated", value=f"{total_ram:.1f} GB", inline=True)
    embed.add_field(name="Total Disk Allocated", value=f"{total_disk:.1f} GB", inline=True)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-delete-user", description="Admin: Delete all VPS for a user")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_delete_user(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer()
    user_id = target_user.id
    vps_list = get_user_vps(user_id)
    deleted = 0
    for vps in vps_list:
        container_id = vps['container_id']
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)
        delete_vps(container_id)
        deleted += 1
        logger.info(f"Deleted VPS {container_id} for user {user_id}")
    embed = discord.Embed(description=f"Deleted {deleted} VPS instances for {target_user}.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="admin-ban", description="Admin: Ban a user from creating VPS")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_ban(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    add_ban(target_user.id)
    embed = discord.Embed(description=f"Banned {target_user} from creating VPS instances.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-unban", description="Admin: Unban a user")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_unban(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    remove_ban(target_user.id)
    embed = discord.Embed(description=f"Unbanned {target_user}.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-vps-info", description="Admin: View full VPS details for a user")
@app_commands.describe(target_user="The target user", vps_identifier="VPS ID or Name")
@app_commands.guild_only()
async def admin_vps_info(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    container_id = vps['container_id']
    uptime = get_uptime(container_id)
    stats = get_stats(container_id)
    os_name = "Ubuntu 22.04" if vps['os_type'] == "ubuntu" else "Debian 12"
    embed = discord.Embed(title=f"{target_user.name} - VPS Details: {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="OS", value=os_name, inline=True)
    embed.add_field(name="Hostname", value=vps['hostname'], inline=True)
    embed.add_field(name="Status", value=vps['status'], inline=True)
    embed.add_field(name="Suspended", value="Yes" if vps['suspended'] else "No", inline=True)
    embed.add_field(name="Container ID", value=f"```{container_id}```", inline=False)
    embed.add_field(name="Allocated Resources", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
    embed.add_field(name="Current Usage", value=f"CPU: {stats['cpu']} | Mem: {stats['mem']}", inline=False)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Network I/O", value=stats['net'], inline=False)
    embed.add_field(name="Created At", value=vps['created_at'], inline=True)
    if vps['ssh_command']:
        ssh_trunc = vps['ssh_command'][:100] + "..." if len(vps['ssh_command']) > 100 else vps['ssh_command']
        embed.add_field(name="SSH Command", value=f"```{ssh_trunc}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-logs", description="Admin: View logs for a user's VPS")
@app_commands.describe(target_user="The target user", vps_identifier="VPS ID or Name", lines="Number of lines (default 50)")
@app_commands.guild_only()
async def admin_logs(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str, lines: int = 50):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    container_id = vps['container_id']
    logs = get_logs(container_id, lines)
    embed = discord.Embed(title=f"Logs for {target_user.name}'s {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Recent Logs", value=f"```{logs}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

# Show bot & developer information
@bot.tree.command(name="about", description="Show bot & developer information")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 VPS Manager Bot • About",
        description=(
            "**A powerful, fast, and user-friendly Discord bot for managing VPS servers and Docker containers.**\n\n"
            "Designed with **speed**, **stability**, **security**, and **simplicity** in mind 🚀🔒\n"
            "Perfect for server admins, developers, and hosting enthusiasts!"
        ),
        color=discord.Color.from_rgb(88, 101, 242)  # A modern blurple shade
    )

    # Bot Details
    embed.add_field(
        name="📌 Bot Information",
        value=(
            "➜ **Name:** VPS Manager Bot\n"
            "➜ **Version:** v1.0\n"
            "➜ **Framework:** Python • discord.py\n"
            "➜ **Uptime Status:** 🟢 Online & Stable\n"
            "➜ **Features:** VPS control, Docker management, real-time monitoring, and more!"
        ),
        inline=False
    )

    # Developer Section with more details
    embed.add_field(
        name="👨‍💻 Meet the Developer • HAPPY_FF",
        value=(
            "**HAPPY_FF** is a passionate **Full-Stack Developer** and **DevOps Enthusiast** from India 🇮🇳\n\n"
            "🔹 **Specialties:**\n"
            "   • VPS & Server Management\n"
            "   • Docker & Containerization\n"
            "   • Advanced Control Panels\n"
            "   • QEMU Virtual Machines\n"
            "   • High-Performance Discord Bots\n"
            "   • Minecraft Server Hosting & Optimization\n\n"
            "Focused on delivering **clean code**, **optimized performance**, **robust security**, and **beautiful UI/UX** 💎✨"
        ),
        inline=False
    )

    # Social Links
    embed.add_field(
        name="🔗 Connect with HAPPY_FF",
        value=(
            "📺 **YouTube:** [Watch Tutorials & Guides](https://www.youtube.com/@HAPPY_MINE3)\n"
            "💻 **GitHub:** [View Projects & Scripts](https://github.com/)\n"
            "📸 **Instagram:** [Follow for Updates](https://instagram.com/)"
        ),
        inline=False
    )

    # Fun Fact / Extra Touch
    embed.add_field(
        name="🎮 Fun Fact",
        value=(
            "HAPPY_FF is also a big **Minecraft** fan! Many tutorials cover free/paid hosting, "
            "server setups, web stores, and getting powerful VPS resources for gaming servers 🟩"
        ),
        inline=False
    )

    embed.set_footer(
        text="Built with ❤️ and ☕ by HAPPYFF | Thank you for using VPS Manager Bot!",
        icon_url="https://i.postimg.cc/Pr4qmd3q/80598142-526d-4220-b726-464b6ea012cb.jpg"  # Suggested: A profile-related image from YouTube
    )
    embed.set_thumbnail(
        url="https://i.postimg.cc/Pr4qmd3q/80598142-526d-4220-b726-464b6ea012cb.jpg"  # A cool Discord bot / VPS themed thumbnail for better visuals
    )
    embed.timestamp = discord.utils.utcnow()

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="logs", description="View recent logs for your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name", lines="Number of lines (default 50)")
async def user_logs(interaction: discord.Interaction, vps_identifier: str, lines: int = 50):
    vps = get_vps_by_identifier(interaction.user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    logs = get_logs(container_id, lines)
    embed = discord.Embed(title=f"Logs for {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Recent Logs", value=f"```{logs}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Slash Commands
@bot.tree.command(name="deploy", description="Deploy a new VPS instance with default resources")
@app_commands.describe(os_type="The OS type for the VPS")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu", value="ubuntu"),
    app_commands.Choice(name="Debian", value="debian")
])
async def deploy(interaction: discord.Interaction, os_type: str):
    await create_vps(interaction, os_type)

@bot.tree.command(name="admin-create", description="Admin: Create a VPS for a user with optional custom resources")
@app_commands.describe(target_user="The target user", os_type="OS type", ram="RAM e.g. 2g (optional)", cpu="CPU cores (optional)", disk="Disk e.g. 20G (optional)")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu", value="ubuntu"),
    app_commands.Choice(name="Debian", value="debian")
])
async def admin_create(interaction: discord.Interaction, target_user: discord.User, os_type: str, ram: str = None, cpu: str = None, disk: str = None):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    ram = ram or DEFAULT_RAM
    cpu = cpu or DEFAULT_CPU
    disk = disk or DEFAULT_DISK
    if get_total_instances() >= TOTAL_SERVER_LIMIT:
        embed = discord.Embed(description=f"Global server limit reached: {TOTAL_SERVER_LIMIT} total running instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await create_vps(interaction, os_type, ram, cpu, disk, target_user=target_user)

@bot.tree.command(name="vps-info", description="View full details of your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name (defaults to first)")
async def vps_info(interaction: discord.Interaction, vps_identifier: str = None):
    vps = get_vps_by_identifier(interaction.user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No VPS found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    uptime = get_uptime(container_id)
    stats = get_stats(container_id)
    os_name = "Ubuntu 22.04" if vps['os_type'] == "ubuntu" else "Debian 12"
    embed = discord.Embed(title=f"VPS Details: {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="OS", value=os_name, inline=True)
    embed.add_field(name="Hostname", value=vps['hostname'], inline=True)
    embed.add_field(name="Status", value=vps['status'], inline=True)
    embed.add_field(name="Suspended", value="Yes" if vps['suspended'] else "No", inline=True)
    embed.add_field(name="Container ID", value=f"```{container_id}```", inline=False)
    embed.add_field(name="Allocated Resources", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
    embed.add_field(name="Current Usage", value=f"CPU: {stats['cpu']} | Mem: {stats['mem']}", inline=False)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Network I/O", value=stats['net'], inline=False)
    embed.add_field(name="Created At", value=vps['created_at'], inline=True)
    if vps['ssh_command']:
        ssh_trunc = vps['ssh_command'][:100] + "..." if len(vps['ssh_command']) > 100 else vps['ssh_command']
        embed.add_field(name="SSH Command", value=f"```{ssh_trunc}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="regen-ssh", description="Regenerate SSH session for your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name (defaults to first)")
async def regen_ssh(interaction: discord.Interaction, vps_identifier: str = None):
    await regen_ssh_command(interaction, vps_identifier)

@bot.tree.command(name="start", description="Start your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def start_vps(interaction: discord.Interaction, vps_identifier: str):
    await manage_vps(interaction, vps_identifier, "start")

@bot.tree.command(name="stop", description="Stop your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def stop_vps(interaction: discord.Interaction, vps_identifier: str):
    await manage_vps(interaction, vps_identifier, "stop")

@bot.tree.command(name="restart", description="Restart your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def restart_vps(interaction: discord.Interaction, vps_identifier: str):
    await manage_vps(interaction, vps_identifier, "restart")

@bot.tree.command(name="reinstall", description="Reinstall your VPS with a new OS")
@app_commands.describe(vps_identifier="VPS ID or Name", os_type="The new OS type")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu", value="ubuntu"),
    app_commands.Choice(name="Debian", value="debian")
])
async def reinstall(interaction: discord.Interaction, vps_identifier: str, os_type: str = "ubuntu"):
    await reinstall_vps(interaction, vps_identifier, os_type)

@bot.tree.command(name="list", description="List all your VPS instances")
async def list_vps(interaction: discord.Interaction):
    vps_list = get_user_vps(interaction.user.id)
    if not vps_list:
        embed = discord.Embed(description="You have no VPS instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    embed = discord.Embed(title="Your VPS Instances", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    for vps in vps_list[:25]:
        status_emoji = "🟢" if vps['status'] == "running" else "🔴"
        uptime = get_uptime(vps['container_id'])
        suspended_text = "(Suspended)" if vps['suspended'] else ""
        embed.add_field(
            name=f"{status_emoji} {vps['container_name']} ({vps['os_type']}) {suspended_text}",
            value=f"ID: ```{vps['container_id']}```\nHostname: {vps['hostname']}\nStatus: {vps['status']}\nUptime: {uptime}\nResources: {vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk",
            inline=False
        )
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remove", description="Remove your VPS instance")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def remove_vps(interaction: discord.Interaction, vps_identifier: str):
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(interaction.user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    await async_docker_stop(container_id)
    await asyncio.sleep(2)
    await async_docker_rm(container_id)
    delete_vps(container_id)
    embed = discord.Embed(title="VPS Removed Successfully", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed, ephemeral=True)

# Admin commands
@bot.tree.command(name="admin-manage", description="Admin: Manage a user's VPS (start/stop/restart/delete/suspend/unsuspend)")
@app_commands.describe(target_user="The target user", vps_identifier="VPS ID or Name", action="The action to perform")
@app_commands.choices(action=[
    app_commands.Choice(name="start", value="start"),
    app_commands.Choice(name="stop", value="stop"),
    app_commands.Choice(name="restart", value="restart"),
    app_commands.Choice(name="delete", value="delete"),
    app_commands.Choice(name="suspend", value="suspend"),
    app_commands.Choice(name="unsuspend", value="unsuspend")
])
@app_commands.guild_only()
async def admin_manage(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str, action: str):
    await interaction.response.defer()
    await admin_manage_vps(interaction, target_user.id, vps_identifier, action)

@bot.tree.command(name="admin-kill-all", description="Admin: Stop all running VPS instances")
@app_commands.guild_only()
async def admin_kill_all_cmd(interaction: discord.Interaction):
    await admin_kill_all(interaction)

@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", description=f"Latency: {latency}ms", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="help", description="View help and command list")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="VPS Bot Help", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="**User Commands**", value="", inline=False)
    embed.add_field(name="/deploy <os>", value="Deploy a new VPS with default resources (Ubuntu or Debian)", inline=False)
    embed.add_field(name="/list", value="List all your VPS instances with details", inline=False)
    embed.add_field(name="/vps-info [vps_id]", value="View full details of a VPS including usage and SSH", inline=False)
    embed.add_field(name="/start <vps_id>", value="Start a VPS", inline=False)
    embed.add_field(name="/stop <vps_id>", value="Stop a VPS", inline=False)
    embed.add_field(name="/restart <vps_id>", value="Restart a VPS", inline=False)
    embed.add_field(name="/regen-ssh [vps_id]", value="Regenerate SSH session", inline=False)
    embed.add_field(name="/reinstall <vps_id> [os]", value="Reinstall VPS with new OS (keeps resources)", inline=False)
    embed.add_field(name="/remove <vps_id>", value="Remove a VPS", inline=False)
    embed.add_field(name="/about", value="Show bot & developer information", inline=False)
    embed.add_field(name="/logs <vps_id> [lines]", value="View recent VPS logs", inline=False)
    if ADMIN_ID > 0:
        embed.add_field(name="**Admin Commands**", value="", inline=False)
        embed.add_field(name="/admin-create <user> <os> [ram] [cpu] [disk]", value="Create VPS for a user with optional resources", inline=False)
        embed.add_field(name="/admin-manage <user> <vps> <action>", value="Manage user's VPS (start/stop/restart/delete/suspend/unsuspend)", inline=False)
        embed.add_field(name="/admin-list-users", value="List users with VPS counts", inline=False)
        embed.add_field(name="/admin-list", value="List all VPS instances", inline=False)
        embed.add_field(name="/admin-stats", value="View bot statistics", inline=False)
        embed.add_field(name="/admin-vps-info <user> <vps>", value="View full details for a user's VPS", inline=False)
        embed.add_field(name="/admin-logs <user> <vps> [lines]", value="View logs for a user's VPS", inline=False)
        embed.add_field(name="/admin-delete-user <user>", value="Delete all VPS for a user", inline=False)
        embed.add_field(name="/admin-ban <user>", value="Ban a user from creating VPS", inline=False)
        embed.add_field(name="/admin-unban <user>", value="Unban a user", inline=False)
        embed.add_field(name="/admin-kill-all", value="Stop all running VPS instances", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tasks.loop(minutes=5)
async def sync_statuses():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id, status FROM vps')
    for row in cursor.fetchall():
        cid = row['container_id']
        stat = row['status']
        try:
            out = backend.get_status(cid)
            if out != stat:
                update_vps_status(cid, out)
                logger.info(f"Updated status of {cid} to {out}")
        except Exception as e:
            logger.error(f"Status sync error for {cid}: {e}")
    conn.close()

# Events
@bot.event
async def on_ready():
    change_status.start()
    sync_statuses.start()
    logger.info(f'Bot ready: {bot.user}')
    try:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} commands')
    except Exception as e:
        logger.error(f'Sync failed: {e}')

@tasks.loop(seconds=10)
async def change_status():
    try:
        count = get_total_instances()
        status = f"{BOT_STATUS_NAME} | {count} Active"
        await bot.change_presence(activity=discord.Game(name=status))
    except Exception as e:
        logger.error(f"Status update failed: {e}")

if __name__ == "__main__":
    if not TOKEN:
        logger.error("TOKEN not set in .env")
        sys.exit(1)
    bot.run(TOKEN)
