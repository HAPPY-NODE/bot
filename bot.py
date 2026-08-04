import discord
from discord.ext import commands
import asyncio
import subprocess
import json
from datetime import datetime, timedelta, timezone
import shlex
import logging
import shutil
import os
from typing import Optional, List, Dict, Any
import threading
import time
import sqlite3
import random
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Load environment variables - SECURITY: Never hardcode tokens!
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is required! Set it in .env file or environment.")

BOT_NAME = os.getenv('BOT_NAME', 'INFINITY_CLOUD')
# LXC container names only allow alphanumeric and hyphen - sanitize bot name
LXC_PREFIX = ''.join(c if c.isalnum() else '-' for c in BOT_NAME.lower()).strip('-')
PREFIX = os.getenv('PREFIX', '!')
YOUR_SERVER_IP = os.getenv('YOUR_SERVER_IP', '127.0.0.1')
MAIN_ADMIN_ID = int(os.getenv('MAIN_ADMIN_ID', '0'))
VPS_USER_ROLE_ID = int(os.getenv('VPS_USER_ROLE_ID', '0'))
DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', 'default')
BOT_VERSION = os.getenv('BOT_VERSION', '8.0-PRO')
BOT_DEVELOPER = os.getenv('BOT_DEVELOPER', 'HAPPY_FF')

# OS Options for VPS Creation and Reinstall
OS_OPTIONS = [
    {"label": "Ubuntu 20.04 LTS", "value": "ubuntu:20.04"},
    {"label": "Ubuntu 22.04 LTS", "value": "ubuntu:22.04"},
    {"label": "Ubuntu 24.04 LTS", "value": "ubuntu:24.04"},
    {"label": "Debian 10 (Buster)", "value": "images:debian/10"},
    {"label": "Debian 11 (Bullseye)", "value": "images:debian/11"},
    {"label": "Debian 12 (Bookworm)", "value": "images:debian/12"},
    {"label": "Debian 13 (Trixie)", "value": "images:debian/13"},
]

# Configure logging to file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(f'{BOT_NAME.lower()}_vps_bot')

# Database setup
def get_db():
    """Get database connection with proper timeout and WAL mode"""
    conn = sqlite3.connect('vps.db', timeout=60.0, check_same_thread=False, isolation_level=None)  # 60 second timeout, autocommit mode
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")  # 60 second busy timeout
    conn.execute("PRAGMA synchronous=NORMAL")  # Faster writes
    conn.row_factory = sqlite3.Row
    return conn

# Async database wrapper to prevent blocking
async def run_in_executor(func, *args):
    """Run blocking database operations in thread executor"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY
    )''')
    cur.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (str(MAIN_ADMIN_ID),))
    cur.execute('''CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        location TEXT,
        total_vps INTEGER,
        tags TEXT DEFAULT '[]',
        api_key TEXT,
        url TEXT,
        is_local INTEGER DEFAULT 0
    )''')
    # Add local node if not exists
    cur.execute('SELECT COUNT(*) FROM nodes WHERE is_local = 1')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('Local Node', 'Local', 100, '[]', None, None, 1))  # Default capacity 100
    cur.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        node_id INTEGER NOT NULL DEFAULT 1,
        container_name TEXT UNIQUE NOT NULL,
        ram TEXT NOT NULL,
        cpu TEXT NOT NULL,
        storage TEXT NOT NULL,
        config TEXT NOT NULL,
        os_version TEXT DEFAULT 'ubuntu:22.04',
        status TEXT DEFAULT 'stopped',
        suspended INTEGER DEFAULT 0,
        whitelisted INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        shared_with TEXT DEFAULT '[]',
        suspension_history TEXT DEFAULT '[]'
    )''')
    # Migrations
    cur.execute('PRAGMA table_info(vps)')
    info = cur.fetchall()
    columns = [col[1] for col in info]
    if 'os_version' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN os_version TEXT DEFAULT 'ubuntu:22.04'")
    if 'node_id' not in columns:
        cur.execute("ALTER TABLE vps ADD COLUMN node_id INTEGER DEFAULT 1")
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    settings_init = [
        ('cpu_threshold', '90'),
        ('ram_threshold', '90'),
    ]
    for key, value in settings_init:
        cur.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    cur.execute('''CREATE TABLE IF NOT EXISTS port_allocations (
        user_id TEXT PRIMARY KEY,
        allocated_ports INTEGER DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS port_forwards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        vps_container TEXT NOT NULL,
        vps_port INTEGER NOT NULL,
        host_port INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )''')
    
    # Add expiration columns to VPS table if not exists
    cur.execute('PRAGMA table_info(vps)')
    vps_columns = [col[1] for col in cur.fetchall()]
    if 'expires_at' not in vps_columns:
        cur.execute("ALTER TABLE vps ADD COLUMN expires_at TEXT")
    if 'duration_days' not in vps_columns:
        cur.execute("ALTER TABLE vps ADD COLUMN duration_days INTEGER DEFAULT 7")
    if 'auto_renew' not in vps_columns:
        cur.execute("ALTER TABLE vps ADD COLUMN auto_renew INTEGER DEFAULT 0")
    
    conn.commit()
    conn.close()

def get_setting(key: str, default: Any = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def get_nodes() -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes')
    rows = cur.fetchall()
    conn.close()
    nodes = [dict(row) for row in rows]
    for node in nodes:
        node['tags'] = json.loads(node['tags'])
    return nodes

def get_node(node_id: int) -> Optional[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM nodes WHERE id = ?', (node_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        node = dict(row)
        node['tags'] = json.loads(node['tags'])
        return node
    return None

def get_current_vps_count(node_id: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM vps WHERE node_id = ?', (node_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count

def get_vps_data() -> Dict[str, List[Dict[str, Any]]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps')
    rows = cur.fetchall()
    conn.close()
    data = {}
    for row in rows:
        user_id = row['user_id']
        if user_id not in data:
            data[user_id] = []
        vps = dict(row)
        vps['shared_with'] = json.loads(vps['shared_with'])
        vps['suspension_history'] = json.loads(vps['suspension_history'])
        vps['suspended'] = bool(vps['suspended'])
        vps['whitelisted'] = bool(vps['whitelisted'])
        vps['os_version'] = vps.get('os_version', 'ubuntu:22.04')
        data[user_id].append(vps)
    return data

def get_admins() -> List[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM admins')
    rows = cur.fetchall()
    conn.close()
    return [row['user_id'] for row in rows]

def save_vps_data():
    conn = get_db()
    cur = conn.cursor()
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            shared_json = json.dumps(vps['shared_with'])
            history_json = json.dumps(vps['suspension_history'])
            suspended_int = 1 if vps['suspended'] else 0
            whitelisted_int = 1 if vps.get('whitelisted', False) else 0
            os_ver = vps.get('os_version', 'ubuntu:22.04')
            created_at = vps.get('created_at', datetime.now().isoformat())
            node_id = vps.get('node_id', 1)
            if 'id' not in vps or vps['id'] is None:
                cur.execute('''INSERT INTO vps (user_id, node_id, container_name, ram, cpu, storage, config, os_version, status, suspended, whitelisted, created_at, shared_with, suspension_history)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int,
                             created_at, shared_json, history_json))
                vps['id'] = cur.lastrowid
            else:
                cur.execute('''UPDATE vps SET user_id = ?, node_id = ?, container_name = ?, ram = ?, cpu = ?, storage = ?, config = ?, os_version = ?, status = ?, suspended = ?, whitelisted = ?, shared_with = ?, suspension_history = ?
                               WHERE id = ?''',
                            (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'], vps['storage'], vps['config'],
                             os_ver, vps['status'], suspended_int, whitelisted_int, shared_json, history_json, vps['id']))
    conn.commit()
    conn.close()

def save_admin_data():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM admins')
    for admin_id in admin_data['admins']:
        cur.execute('INSERT INTO admins (user_id) VALUES (?)', (admin_id,))
    conn.commit()
    conn.close()

# Port forwarding functions
def get_user_allocation(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT allocated_ports FROM port_allocations WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def get_user_used_ports(user_id: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM port_forwards WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0]

def allocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO port_allocations (user_id, allocated_ports) VALUES (?, COALESCE((SELECT allocated_ports FROM port_allocations WHERE user_id = ?), 0) + ?)', (user_id, user_id, amount))
    conn.commit()
    conn.close()

def deallocate_ports(user_id: str, amount: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE port_allocations SET allocated_ports = GREATEST(0, allocated_ports - ?) WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def get_available_host_port(node_id: int) -> Optional[int]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT host_port FROM port_forwards WHERE vps_container IN (SELECT container_name FROM vps WHERE node_id = ?)', (node_id,))
    used_ports = set(row[0] for row in cur.fetchall())
    conn.close()
    for _ in range(100):
        port = random.randint(20000, 50000)
        if port not in used_ports:
            return port
    return None

async def create_port_forward(user_id: str, container: str, vps_port: int, node_id: int) -> Optional[int]:
    host_port = get_available_host_port(node_id)
    if not host_port:
        return None
    try:
        await execute_lxc(container, f"config device add {container} tcp_proxy_{host_port} proxy listen=tcp:0.0.0.0:{host_port} connect=tcp:127.0.0.1:{vps_port}", node_id=node_id)
        await execute_lxc(container, f"config device add {container} udp_proxy_{host_port} proxy listen=udp:0.0.0.0:{host_port} connect=udp:127.0.0.1:{vps_port}", node_id=node_id)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, created_at) VALUES (?, ?, ?, ?, ?)',
                    (user_id, container, vps_port, host_port, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return host_port
    except Exception as e:
        logger.error(f"Failed to create port forward: {e}")
        return None

async def remove_port_forward(forward_id: int, is_admin: bool = False):
    """Remove a port forward - Returns (success, error_message)"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id, vps_container, host_port FROM port_forwards WHERE id = ?', (forward_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, None
    user_id, container, host_port = row
    node_id = find_node_id_for_container(container)
    try:
        await execute_lxc(container, f"config device remove {container} tcp_proxy_{host_port}", node_id=node_id)
        await execute_lxc(container, f"config device remove {container} udp_proxy_{host_port}", node_id=node_id)
        cur.execute('DELETE FROM port_forwards WHERE id = ?', (forward_id,))
        conn.commit()
        conn.close()
        return True, user_id
    except Exception as e:
        logger.error(f"Failed to remove port forward {forward_id}: {e}")
        conn.close()
        return False, None

def get_user_forwards(user_id: str) -> List[Dict]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM port_forwards WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]

async def recreate_port_forwards(container_name: str) -> int:
    node_id = find_node_id_for_container(container_name)
    readded_count = 0
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT vps_port, host_port FROM port_forwards WHERE vps_container = ?', (container_name,))
    rows = cur.fetchall()
    for row in rows:
        vps_port = row['vps_port']
        host_port = row['host_port']
        try:
            await execute_lxc(container_name, f"config device add {container_name} tcp_proxy_{host_port} proxy listen=tcp:0.0.0.0:{host_port} connect=tcp:127.0.0.1:{vps_port}", node_id=node_id)
            await execute_lxc(container_name, f"config device add {container_name} udp_proxy_{host_port} proxy listen=udp:0.0.0.0:{host_port} connect=udp:127.0.0.1:{vps_port}", node_id=node_id)
            logger.info(f"Re-added port forward {host_port}->{vps_port} for {container_name}")
            readded_count += 1
        except Exception as e:
            logger.error(f"Failed to re-add port forward {host_port}->{vps_port} for {container_name}: {e}")
    conn.close()
    return readded_count

def find_node_id_for_container(container_name: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT node_id FROM vps WHERE container_name = ?', (container_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 1  # Default to local


def set_vps_expiration(vps_id: int, duration_days: int):
    """Set VPS expiration date"""
    expires_at = datetime.now() + timedelta(days=duration_days)
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''UPDATE vps 
                   SET expires_at = ?, duration_days = ?
                   WHERE id = ?''', (expires_at.isoformat(), duration_days, vps_id))
    conn.commit()
    conn.close()


def renew_vps(vps_id: int, duration_days: int) -> bool:
    """Renew VPS for additional days"""
    conn = get_db()
    cur = conn.cursor()
    
    # Get current expiration
    cur.execute('SELECT expires_at FROM vps WHERE id = ?', (vps_id,))
    row = cur.fetchone()
    
    if not row:
        conn.close()
        return False
    
    current_expiry = row[0]
    
    # Calculate new expiration
    if current_expiry:
        base_time = datetime.fromisoformat(current_expiry)
        if base_time < datetime.now():
            base_time = datetime.now()
    else:
        base_time = datetime.now()
    
    new_expiry = base_time + timedelta(days=duration_days)
    
    # Update VPS
    cur.execute('''UPDATE vps 
                   SET expires_at = ?, duration_days = duration_days + ?
                   WHERE id = ?''', (new_expiry.isoformat(), duration_days, vps_id))
    
    conn.commit()
    conn.close()
    return True

def check_and_suspend_expired_vps():
    """Check for expired VPS and suspend them"""
    conn = get_db()
    cur = conn.cursor()
    
    # Find expired VPS
    cur.execute('''SELECT id, user_id, container_name, expires_at 
                   FROM vps 
                   WHERE expires_at IS NOT NULL 
                   AND expires_at <= ? 
                   AND suspended = 0
                   AND whitelisted = 0
                   AND status = 'running' ''', (datetime.now().isoformat(),))
    
    expired_vps = cur.fetchall()
    conn.close()
    
    suspended_count = 0
    for vps in expired_vps:
        vps_id, user_id, container_name, expires_at = vps
        node_id = find_node_id_for_container(container_name)
        
        try:
            # Stop the VPS
            asyncio.create_task(execute_lxc(container_name, f"stop {container_name}", node_id=node_id))
            
            # Mark as suspended
            conn = get_db()
            cur = conn.cursor()
            cur.execute('''UPDATE vps 
                           SET suspended = 1, status = 'stopped'
                           WHERE id = ?''', (vps_id,))
            
            # Add to suspension history
            cur.execute('SELECT suspension_history FROM vps WHERE id = ?', (vps_id,))
            history = json.loads(cur.fetchone()[0] or '[]')
            history.append({
                'time': datetime.now().isoformat(),
                'reason': f'VPS expired (was valid until {expires_at})',
                'by': 'Auto-Suspension System'
            })
            cur.execute('UPDATE vps SET suspension_history = ? WHERE id = ?', 
                       (json.dumps(history), vps_id))
            
            conn.commit()
            conn.close()
            
            suspended_count += 1
            logger.info(f"Auto-suspended expired VPS: {container_name} (user: {user_id})")
            
        except Exception as e:
            logger.error(f"Failed to suspend expired VPS {container_name}: {e}")
    
    return suspended_count

def format_expiry_time(expires_at_str: str) -> Dict:
    """Format expiry time into human-readable format with status"""
    if not expires_at_str:
        return {
            'text': '♾️ Never',
            'status': 'permanent',
            'color': 0x00ff88,
            'emoji': '♾️',
            'hours_left': float('inf')
        }
    
    expires_at = datetime.fromisoformat(expires_at_str)
    now = datetime.now()
    time_left = expires_at - now
    
    if time_left.total_seconds() <= 0:
        return {
            'text': '❌ EXPIRED',
            'status': 'expired',
            'color': 0xff0000,
            'emoji': '❌',
            'hours_left': 0
        }
    
    hours_left = time_left.total_seconds() / 3600
    days_left = time_left.days
    
    if days_left > 7:
        status_text = f"✅ {days_left} days"
        status = 'safe'
        color = 0x00ff88
        emoji = '✅'
    elif days_left > 1:
        status_text = f"⚠️ {days_left} days"
        status = 'warning'
        color = 0xffaa00
        emoji = '⚠️'
    elif hours_left > 1:
        status_text = f"🚨 {int(hours_left)} hours"
        status = 'critical'
        color = 0xff6600
        emoji = '🚨'
    else:
        minutes_left = int(time_left.total_seconds() / 60)
        status_text = f"⏰ {minutes_left} minutes"
        status = 'urgent'
        color = 0xff0000
        emoji = '⏰'
    
    return {
        'text': status_text,
        'status': status,
        'color': color,
        'emoji': emoji,
        'hours_left': hours_left,
        'expires_at': expires_at.strftime('%Y-%m-%d %H:%M:%S')
    }


# Initialize database
init_db()

# Load data at startup
vps_data = get_vps_data()
admin_data = {'admins': get_admins()}

# Global settings from DB
CPU_THRESHOLD = int(get_setting('cpu_threshold', 90))
RAM_THRESHOLD = int(get_setting('ram_threshold', 90))

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Resource monitoring settings (logging only)
resource_monitor_active = True

# Helper function to truncate text
def truncate_text(text, max_length=1024):
    if not text:
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

# ============================================
# PREMIUM EMBED SYSTEM - Modern UI/UX Design
# ============================================

class EmbedColors:
    """Premium color palette for modern UI"""
    PRIMARY = 0x5865F2      # Discord Blurple - Primary actions
    SUCCESS = 0x57F287      # Green - Success states
    ERROR = 0xED4245        # Red - Error states
    WARNING = 0xFEE75C      # Yellow - Warning states
    INFO = 0x5865F2         # Blue - Information
    PREMIUM = 0xEB459E      # Pink - Premium features
    GOLD = 0xF1C40F         # Gold
    PURPLE = 0x9B59B6       # Purple - Special features
    DARK = 0x2B2D31         # Dark - Neutral
    LIGHT = 0x99AAB5        # Light Gray - Secondary info

class EmbedIcons:
    """Modern icon system for consistent branding"""
    SUCCESS = "✓"
    ERROR = "✕"
    WARNING = "⚠"
    INFO = "ℹ"
    LOADING = "⟳"
    PREMIUM = "★"
    ARROW = "→"
    BULLET = "•"
    DIVIDER = "─"
    
def create_embed(title, description="", color=EmbedColors.PRIMARY, show_branding=True):
    """
    Create a premium-styled Discord embed with modern design principles
    
    Args:
        title: Main title (clean, no emoji prefix)
        description: Main content
        color: Hex color code
        show_branding: Whether to show footer branding
    """
    # Clean title - no emoji spam
    clean_title = truncate_text(title, 256)
    
    embed = discord.Embed(
        title=clean_title,
        description=truncate_text(description, 4096) if description else None,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    
    # Premium footer with minimal branding
    if show_branding:
        embed.set_footer(
            text=f"{BOT_NAME} v{BOT_VERSION} {EmbedIcons.BULLET} Powered by INFINITY_CLOUD",
            icon_url="https://i.imgur.com/dpatuSj.png"
        )
    
    return embed

def add_field(embed, name, value, inline=False):
    """
    Add a field with clean formatting
    
    Args:
        embed: Discord embed object
        name: Field name (clean, minimal icons)
        value: Field value
        inline: Whether to display inline
    """
    # Clean field name - minimal decoration
    clean_name = truncate_text(name, 256)
    clean_value = truncate_text(value if value else "N/A", 1024)
    
    embed.add_field(
        name=clean_name,
        value=clean_value,
        inline=inline
    )
    return embed

def create_success_embed(title, description="", show_icon=True):
    """
    Success state embed - Clean green design
    
    Usage: Successful operations, confirmations
    """
    icon = f"{EmbedIcons.SUCCESS} " if show_icon else ""
    return create_embed(f"{icon}{title}", description, color=EmbedColors.SUCCESS)

def create_error_embed(title, description="", show_icon=True):
    """
    Error state embed - Clean red design
    
    Usage: Errors, failures, access denied
    """
    icon = f"{EmbedIcons.ERROR} " if show_icon else ""
    return create_embed(f"{icon}{title}", description, color=EmbedColors.ERROR)

def create_info_embed(title, description="", show_icon=True):
    """
    Information embed - Clean blue design
    
    Usage: General information, help text
    """
    icon = f"{EmbedIcons.INFO} " if show_icon else ""
    return create_embed(f"{icon}{title}", description, color=EmbedColors.INFO)

def create_warning_embed(title, description="", show_icon=True):
    """
    Warning state embed - Clean yellow design
    
    Usage: Warnings, cautions, confirmations needed
    """
    icon = f"{EmbedIcons.WARNING} " if show_icon else ""
    return create_embed(f"{icon}{title}", description, color=EmbedColors.WARNING)

def create_premium_embed(title, description=""):
    """
    Premium feature embed - Special pink/purple design
    
    Usage: Premium features, special announcements
    """
    return create_embed(f"{EmbedIcons.PREMIUM} {title}", description, color=EmbedColors.PREMIUM)

def create_loading_embed(title, description="Processing your request..."):
    """
    Loading state embed - Indicates ongoing process
    
    Usage: Long-running operations
    """
    return create_embed(f"{EmbedIcons.LOADING} {title}", description, color=EmbedColors.INFO)

def create_card_embed(title, description="", color=EmbedColors.DARK):
    """
    Card-style embed for displaying structured data
    
    Usage: VPS info, user profiles, statistics
    """
    embed = create_embed(title, description, color)
    return embed

def format_progress_bar(current, maximum, length=10, filled="█", empty="░"):
    """
    Create a modern progress bar
    
    Args:
        current: Current value
        maximum: Maximum value
        length: Bar length in characters
        filled: Character for filled portion
        empty: Character for empty portion
    
    Returns:
        Formatted progress bar string with percentage
    """
    if maximum == 0:
        percentage = 0
    else:
        percentage = min(100, int((current / maximum) * 100))
    
    filled_length = int((percentage / 100) * length)
    bar = filled * filled_length + empty * (length - filled_length)
    
    return f"{bar} {percentage}%"

def format_status_badge(status, online_text="Online", offline_text="Offline"):
    """
    Create a status badge with color indicator
    
    Args:
        status: Boolean or string status
        online_text: Text for online/active state
        offline_text: Text for offline/inactive state
    
    Returns:
        Formatted status string with indicator
    """
    if isinstance(status, bool):
        is_online = status
    else:
        is_online = status.lower() in ['running', 'online', 'active', 'started']
    
    indicator = "🟢" if is_online else "🔴"
    text = online_text if is_online else offline_text
    
    return f"{indicator} **{text}**"

def format_metric(label, value, unit="", icon=""):
    """
    Format a metric with consistent styling
    
    Args:
        label: Metric label
        value: Metric value
        unit: Unit of measurement
        icon: Optional icon
    
    Returns:
        Formatted metric string
    """
    icon_str = f"{icon} " if icon else ""
    unit_str = f" {unit}" if unit else ""
    return f"{icon_str}**{label}:** {value}{unit_str}"

def create_divider(char="─", length=30):
    """Create a visual divider for embed sections"""
    return char * length

def format_list_item(text, bullet=EmbedIcons.BULLET):
    """Format a list item with consistent bullet style"""
    return f"{bullet} {text}"

def create_section_header(text):
    """Create a section header with subtle styling"""
    return f"**{text}**"

# Admin checks
def is_admin():
    async def predicate(ctx):
        user_id = str(ctx.author.id)
        if user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", []):
            return True
        raise commands.CheckFailure("You need admin permissions to use this command. Contact support.")
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) == str(MAIN_ADMIN_ID):
            return True
        raise commands.CheckFailure("Only the main admin can use this command.")
    return commands.check(predicate)

# LXC command execution with multi-node support
async def execute_lxc(container_name: str, command: str, timeout=120, node_id: Optional[int] = None):
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    
    if not node:
        raise Exception(f"Node {node_id} not found")
    
    full_command = f"lxc {command}"
    
    if node['is_local']:
        try:
            cmd = shlex.split(full_command)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise asyncio.TimeoutError(f"Command timed out after {timeout} seconds")
            
            if proc.returncode != 0:
                error = stderr.decode().strip() if stderr else "Command failed with no error output"
                # Add more context to error
                raise Exception(f"Local LXC command failed: {error}\nCommand: {full_command}")
            return stdout.decode().strip() if stdout else True
        except asyncio.TimeoutError as te:
            logger.error(f"LXC command timed out: {full_command} - {str(te)}")
            raise
        except Exception as e:
            logger.error(f"LXC Error: {full_command} - {str(e)}")
            raise
    else:
        url = f"{node['url']}/api/execute"
        data = {"command": full_command}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params, timeout=timeout)
            
            # Try to get detailed error information
            try:
                error_detail = response.json()
                if 'detail' in error_detail:
                    error_msg = error_detail['detail']
                elif 'error' in error_detail:
                    error_msg = error_detail['error']
                else:
                    error_msg = response.text
            except:
                error_msg = response.text
            
            response.raise_for_status()
            
            res = response.json()
            if res.get("returncode", 1) != 0:
                stderr = res.get("stderr", "Command failed")
                # Add more context to remote error
                raise Exception(f"Remote LXC command failed on {node['name']}: {stderr}\nCommand: {full_command}")
            return res.get("stdout", True)
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Remote LXC error on node {node['name']} ({url}): {str(e)}")
            # Include URL and status code if available
            if hasattr(e.response, 'status_code'):
                raise Exception(f"Remote execution failed on {node['name']}: HTTP {e.response.status_code} - {str(e)}")
            else:
                raise Exception(f"Remote execution failed on {node['name']}: {str(e)}")

# Apply LXC config
async def apply_lxc_config(container_name: str, node_id: int):
    try:
        await execute_lxc(container_name, f"config set {container_name} security.nesting true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.privileged true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.syscalls.intercept.mknod true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} security.syscalls.intercept.setxattr true", node_id=node_id)
        await execute_lxc(container_name, f"config set {container_name} linux.kernel_modules overlay,loop,nf_nat,ip_tables,ip6_tables,netlink_diag,br_netfilter", node_id=node_id)
        try:
            await execute_lxc(container_name, f"config device add {container_name} fuse unix-char path=/dev/fuse", node_id=node_id)
        except:
            pass
        raw_lxc_config = (
            "lxc.apparmor.profile = unconfined\n"
            "lxc.apparmor.allow_nesting = 1\n"
            "lxc.apparmor.allow_incomplete = 1\n"
            "\n"
            "lxc.cap.drop =\n"
            "lxc.cgroup.devices.allow = a\n"
            "lxc.cgroup2.devices.allow = a\n"
            "\n"
            "lxc.mount.auto = proc:rw sys:rw cgroup:rw shmounts:rw\n"
            "\n"
            "lxc.mount.entry = /dev/fuse dev/fuse none bind,create=file 0 0\n"
        )
        await execute_lxc(container_name, f"config set {container_name} raw.lxc '{raw_lxc_config}'", node_id=node_id)
        logger.info(f"LXC permissions applied to {container_name} on node {node_id}")
    except Exception as e:
        logger.error(f"Failed to apply LXC config to {container_name}: {e}")

# Apply internal permissions
async def apply_internal_permissions(container_name: str, node_id: int):
    try:
        await asyncio.sleep(5)
        commands = [
            "mkdir -p /etc/sysctl.d/",
            "echo 'net.ipv4.ip_unprivileged_port_start=0' > /etc/sysctl.d/99-custom.conf",
            "echo 'net.ipv4.ping_group_range=0 2147483647' >> /etc/sysctl.d/99-custom.conf",
            "echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.d/99-custom.conf",
            "echo 'kernel.unprivileged_userns_clone=1' >> /etc/sysctl.d/99-custom.conf",
            "sysctl -p /etc/sysctl.d/99-custom.conf || true"
        ]
        for cmd in commands:
            try:
                await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{cmd}\"", node_id=node_id)
            except Exception as cmd_error:
                logger.warning(f"Command failed in {container_name}: {cmd} - {cmd_error}")
        logger.info(f"Internal permissions applied to {container_name}")
    except Exception as e:
        logger.error(f"Failed to apply internal permissions to {container_name}: {e}")

# Get or create VPS role
async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID

    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        return None

    role_name = f"{BOT_NAME} VPS User"

    # Try cached role
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role and role < me.top_role:
            return role
        VPS_USER_ROLE_ID = None

    # Find by name
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        if role >= me.top_role:
            try:
                await role.delete(reason="Role above bot, recreating")
            except discord.Forbidden:
                return None
            role = None
        else:
            VPS_USER_ROLE_ID = role.id
            return role

    # Create safely below bot
    try:
        role = await guild.create_role(
            name=role_name,
            color=discord.Color.dark_purple(),
            permissions=discord.Permissions.none(),
            reason=f"{BOT_NAME} VPS User role"
        )
        await role.edit(position=me.top_role.position - 1)
        VPS_USER_ROLE_ID = role.id
        logger.info(f"Created VPS role: {role.id}")
        return role
    except Exception as e:
        logger.error(f"Failed to create VPS role: {e}")
        return None

# Host resource functions
def get_host_cpu_usage():
    """Get host CPU usage - cross-platform"""
    try:
        # Try using psutil first (cross-platform)
        try:
            import psutil
            return psutil.cpu_percent(interval=1)
        except ImportError:
            pass
        
        # Linux fallback
        if shutil.which("mpstat"):
            result = subprocess.run(['mpstat', '1', '1'], capture_output=True, text=True)
            output = result.stdout
            for line in output.split('\n'):
                if 'all' in line and '%' in line:
                    parts = line.split()
                    idle = float(parts[-1])
                    return 100.0 - idle
        elif shutil.which("top"):
            result = subprocess.run(['top', '-bn1'], capture_output=True, text=True)
            output = result.stdout
            for line in output.split('\n'):
                if '%Cpu(s):' in line:
                    parts = line.split()
                    us = float(parts[1])
                    sy = float(parts[3])
                    ni = float(parts[5])
                    id_ = float(parts[7])
                    wa = float(parts[9])
                    hi = float(parts[11])
                    si = float(parts[13])
                    st = float(parts[15])
                    usage = us + sy + ni + wa + hi + si + st
                    return usage
        return 0.0
    except Exception as e:
        logger.error(f"Error getting CPU usage: {e}")
        return 0.0

def get_host_ram_usage():
    """Get host RAM usage - cross-platform"""
    try:
        # Try using psutil first (cross-platform)
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            pass
        
        # Linux fallback
        if shutil.which("free"):
            result = subprocess.run(['free', '-m'], capture_output=True, text=True)
            lines = result.stdout.splitlines()
            if len(lines) > 1:
                mem = lines[1].split()
                total = int(mem[1])
                used = int(mem[2])
                return (used / total * 100) if total > 0 else 0.0
        return 0.0
    except Exception as e:
        logger.error(f"Error getting RAM usage: {e}")
        return 0.0

async def get_host_stats(node_id: int) -> Dict:
    node = get_node(node_id)
    if node['is_local']:
        return {"cpu": get_host_cpu_usage(), "ram": get_host_ram_usage()}
    else:
        url = f"{node['url']}/api/get_host_stats"
        params = {"api_key": node["api_key"]}
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get host stats from node {node['name']}: {e}")
            return {"cpu": 0.0, "ram": 0.0}

def resource_monitor():
    global resource_monitor_active
    while resource_monitor_active:
        try:
            nodes = get_nodes()
            for node in nodes:
                stats = asyncio.run(get_host_stats(node['id']))
                cpu = stats['cpu']
                ram = stats['ram']
                logger.info(f"Node {node['name']}: CPU {cpu:.1f}%, RAM {ram:.1f}%")
                if cpu > CPU_THRESHOLD or ram > RAM_THRESHOLD:
                    logger.warning(f"Node {node['name']} exceeded thresholds (CPU: {CPU_THRESHOLD}%, RAM: {RAM_THRESHOLD}%). Manual intervention required.")
            time.sleep(60)
        except Exception as e:
            logger.error(f"Error in resource monitor: {e}")
            time.sleep(60)

# Start resource monitoring thread
monitor_thread = threading.Thread(target=resource_monitor, daemon=True)
monitor_thread.start()

# Container stats with multi-node
async def get_container_stats(container_name: str, node_id: Optional[int] = None) -> Dict:
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    if node['is_local']:
        status = await get_container_status_local(container_name)
        cpu = await get_container_cpu_pct_local(container_name)
        ram = await get_container_ram_local(container_name)
        disk = await get_container_disk_local(container_name)
        uptime = await get_container_uptime_local(container_name)
        return {"status": status, "cpu": cpu, "ram": ram, "disk": disk, "uptime": uptime}
    else:
        url = f"{node['url']}/api/get_container_stats"
        data = {"container": container_name}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Failed to get container stats from node {node['name']}: {e}")
            return {"status": "unknown", "cpu": 0.0, "ram": {"used": 0, "total": 0, "pct": 0.0}, "disk": "Unknown", "uptime": "Unknown"}

async def get_container_status_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "info", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        for line in output.splitlines():
            if line.startswith("Status: "):
                return line.split(": ", 1)[1].strip().lower()
        return "unknown"
    except Exception:
        return "unknown"

async def get_container_cpu_pct_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "top", "-bn1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode()
        
        # Try to parse CPU usage from top output
        for line in output.splitlines():
            if '%Cpu(s):' in line or 'Cpu(s):' in line:
                try:
                    # Extract numbers from the line
                    parts = line.split()
                    cpu_total = 0.0
                    
                    # Parse each CPU component
                    for i, part in enumerate(parts):
                        if part in ['us,', 'sy,', 'ni,', 'wa,', 'hi,', 'si,', 'st,', 'st']:
                            # Get the number before this label
                            if i > 0:
                                try:
                                    value = float(parts[i-1].replace('%', '').replace(',', ''))
                                    if part not in ['id,', 'id']:  # Skip idle
                                        cpu_total += value
                                except (ValueError, IndexError):
                                    continue
                    
                    return cpu_total if cpu_total > 0 else 0.0
                except Exception as e:
                    logger.error(f"Error parsing CPU line for {container_name}: {e}")
                    continue
        
        # Fallback: try to get CPU from /proc/stat inside container
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "cat", "/proc/loadavg",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode().strip()
        if output:
            # Use load average as approximation (first value * 100 / num_cpus)
            load = float(output.split()[0])
            return min(load * 25, 100.0)  # Rough approximation
        
        return 0.0
    except Exception as e:
        logger.error(f"Error getting CPU for {container_name}: {e}")
        return 0.0

async def get_container_ram_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "free", "-m",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            total = int(parts[1])
            used = int(parts[2])
            pct = (used / total * 100) if total > 0 else 0.0
            return {'used': used, 'total': total, 'pct': pct}
        return {'used': 0, 'total': 0, 'pct': 0.0}
    except Exception as e:
        logger.error(f"Error getting RAM for {container_name}: {e}")
        return {'used': 0, 'total': 0, 'pct': 0.0}

async def get_container_disk_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "df", "-h", "/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().splitlines()
        for line in lines:
            if '/dev/' in line and ' /' in line:
                parts = line.split()
                if len(parts) >= 5:
                    used = parts[2]
                    size = parts[1]
                    perc = parts[4]
                    return f"{used}/{size} ({perc})"
        return "Unknown"
    except Exception:
        return "Unknown"

async def get_container_uptime_local(container_name: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "lxc", "exec", container_name, "--", "uptime",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() if stdout else "Unknown"
    except Exception:
        return "Unknown"

async def get_container_status(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['status']

async def get_container_cpu(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return f"{stats['cpu']:.1f}%"

async def get_container_cpu_pct(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['cpu']

async def get_container_memory(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    ram = stats['ram']
    return f"{ram['used']}/{ram['total']} MB ({ram['pct']:.1f}%)"

async def get_container_ram_pct(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['ram']['pct']

async def get_container_disk(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['disk']

async def get_container_uptime(container_name: str, node_id: Optional[int] = None):
    stats = await get_container_stats(container_name, node_id)
    return stats['uptime']

def get_uptime():
    """Get system uptime - cross-platform"""
    try:
        # Try using psutil first (cross-platform)
        try:
            import psutil
            boot_time = psutil.boot_time()
            uptime_seconds = time.time() - boot_time
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            
            if days > 0:
                return f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                return f"{hours}h {minutes}m"
            else:
                return f"{minutes}m"
        except ImportError:
            pass
        
        # Linux fallback
        if shutil.which("uptime"):
            result = subprocess.run(['uptime'], capture_output=True, text=True)
            return result.stdout.strip()
        
        return "Unknown"
    except Exception as e:
        logger.error(f"Error getting uptime: {e}")
        return "Unknown"

# Try to detect default storage pool or use common defaults
def get_default_storage_pool():
    try:
        result = subprocess.run(['lxc', 'storage', 'list', '--format', 'csv'], 
                              capture_output=True, text=True)
        lines = result.stdout.strip().split('\n')
        if lines and lines[0]:
            # Get first storage pool
            return lines[0].split(',')[0]
    except:
        pass
    return "default"  # Fallback to 'default'

DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', get_default_storage_pool())

# Bot events
@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"{BOT_NAME} VPS Manager"))
    logger.info(f"{BOT_NAME} Bot is ready!")
    
    # Start expiration checker
    bot.loop.create_task(expiration_checker_loop())


@bot.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        await bot.process_commands(message)
        return
    
    # Process commands first
    await bot.process_commands(message)
    



async def expiration_checker_loop():
    """Background task to check for expired VPS and send warnings"""
    await bot.wait_until_ready()

    # Track which VPS have been warned at each interval
    warned_24h = set()
    warned_12h = set()
    warned_1h = set()

    while not bot.is_closed():
        try:
            # Check for expired VPS
            suspended_count = check_and_suspend_expired_vps()
            if suspended_count > 0:
                logger.info(f"Auto-suspended {suspended_count} expired VPS")

            # Get all VPS with expiration dates
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""SELECT id, user_id, container_name, expires_at, whitelisted
                           FROM vps 
                           WHERE expires_at IS NOT NULL 
                           AND suspended = 0
                           AND whitelisted = 0""")
            all_vps = cur.fetchall()
            conn.close()

            for vps in all_vps:
                vps_id, user_id, container_name, expires_at_str, whitelisted = vps

                if not expires_at_str or whitelisted:
                    continue

                expires_at = datetime.fromisoformat(expires_at_str)
                hours_left = (expires_at - datetime.now()).total_seconds() / 3600

                if hours_left <= 0:
                    continue

                try:
                    user = await bot.fetch_user(int(user_id))

                    # 24 hour warning
                    if 23 <= hours_left <= 25 and vps_id not in warned_24h:
                        embed = create_warning_embed("⚠️ VPS Expiring in 24 Hours!", 
                            f"Your VPS `{container_name}` will expire in **24 hours**!\n\n"
                            f"**Expires:** {expires_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"**VPS ID:** {vps_id}\n\n"
                            f"**Renewal:** Free with `{PREFIX}renew {vps_id} <days>`\n"
                            f"**Renew Now:** `{PREFIX}renew {vps_id} 7`")
                        embed.set_footer(text=f"{BOT_NAME} • VPS Expiration Warning")
                        await user.send(embed=embed)
                        warned_24h.add(vps_id)
                        logger.info(f"Sent 24h warning to user {user_id} for VPS {container_name}")

                    # 12 hour warning
                    elif 11 <= hours_left <= 13 and vps_id not in warned_12h:
                        embed = create_warning_embed("🚨 VPS Expiring in 12 Hours!", 
                            f"**URGENT:** Your VPS `{container_name}` will expire in **12 hours**!\n\n"
                            f"**Expires:** {expires_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"**VPS ID:** {vps_id}\n\n"
                            f"**Renewal:** Free with `{PREFIX}renew {vps_id} <days>`\n"
                            f"**Renew Now:** `{PREFIX}renew {vps_id} 7`")
                        embed.set_footer(text=f"{BOT_NAME} • URGENT Expiration Warning")
                        await user.send(embed=embed)
                        warned_12h.add(vps_id)
                        logger.info(f"Sent 12h warning to user {user_id} for VPS {container_name}")

                    # 1 hour warning
                    elif 0.5 <= hours_left <= 1.5 and vps_id not in warned_1h:
                        embed = create_error_embed("⏰ VPS EXPIRING IN 1 HOUR!", 
                            f"**CRITICAL:** Your VPS `{container_name}` will expire in **1 hour**!\n\n"
                            f"**Expires:** {expires_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"**VPS ID:** {vps_id}\n\n"
                            f"**After expiration, your VPS will be SUSPENDED!**\n\n"
                            f"**Quick Renewal:** `{PREFIX}renew {vps_id} 1` (free)")
                        embed.set_footer(text=f"{BOT_NAME} • CRITICAL Expiration Warning")
                        await user.send(embed=embed)
                        warned_1h.add(vps_id)
                        logger.info(f"Sent 1h warning to user {user_id} for VPS {container_name}")

                except discord.Forbidden:
                    logger.warning(f"Cannot DM user {user_id} - DMs disabled")
                except Exception as e:
                    logger.error(f"Failed to send expiration warning to user {user_id}: {e}")

            # Clean up warning sets for renewed VPS
            # Remove VPS IDs that no longer exist or have been renewed
            current_vps_ids = {vps[0] for vps in all_vps}
            warned_24h = warned_24h & current_vps_ids
            warned_12h = warned_12h & current_vps_ids
            warned_1h = warned_1h & current_vps_ids

        except Exception as e:
            logger.error(f"Error in expiration checker: {e}")

        # Check every 10 minutes for more accurate warnings
        await asyncio.sleep(600)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", "Please check command usage with `!help`."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        error_msg = str(error) if str(error) else "You need admin permissions for this command. Contact support."
        await ctx.send(embed=create_error_embed("Access Denied", error_msg))
    elif isinstance(error, discord.NotFound):
        await ctx.send(embed=create_error_embed("Error", "The requested resource was not found. Please try again."))
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(embed=create_error_embed("System Error", "An unexpected error occurred. Support has been notified."))


# ============================================
# Bot commands
@bot.command(name='ping')
async def ping(ctx):
    """Check bot latency and connection status"""
    latency = round(bot.latency * 1000)
    
    # Determine latency quality
    if latency < 100:
        quality = "Excellent"
        color = EmbedColors.SUCCESS
        emoji = "🟢"
    elif latency < 200:
        quality = "Good"
        color = EmbedColors.INFO
        emoji = "🟡"
    else:
        quality = "Poor"
        color = EmbedColors.WARNING
        emoji = "🔴"
    
    embed = create_embed("Connection Status", color=color)
    
    # Main metrics
    add_field(embed, "Latency", f"{emoji} **{latency}ms** ({quality})", True)
    add_field(embed, "Status", format_status_badge(True, "Operational"), True)
    add_field(embed, "Uptime", f"🕐 {get_uptime()}", True)
    
    await ctx.send(embed=embed)

@bot.command(name='uptime')
async def uptime(ctx):
    """Display system uptime information"""
    up = get_uptime()
    
    embed = create_info_embed("System Uptime", show_icon=False)
    embed.set_thumbnail(url="https://i.imgur.com/dpatuSj.png")
    
    add_field(embed, "🕐 Host Uptime", f"```{up}```", False)
    add_field(embed, "📊 Status", "All systems operational", False)
    
    await ctx.send(embed=embed)

@bot.command(name='thresholds')
@is_admin()
async def thresholds(ctx):
    """View current resource monitoring thresholds"""
    embed = create_card_embed("Resource Thresholds", "Current monitoring limits for auto-suspension")
    
    add_field(embed, "🔴 CPU Threshold", f"```{CPU_THRESHOLD}%```", True)
    add_field(embed, "🔵 RAM Threshold", f"```{RAM_THRESHOLD}%```", True)
    add_field(embed, "⚙️ Action", "Auto-suspend on exceed", True)
    
    await ctx.send(embed=embed)

@bot.command(name='set-threshold')
@is_admin()
async def set_threshold(ctx, cpu: int, ram: int):
    """Set resource monitoring thresholds"""
    global CPU_THRESHOLD, RAM_THRESHOLD
    if cpu < 0 or ram < 0:
        await ctx.send(embed=create_error_embed("Invalid Input", "Thresholds must be non-negative values."))
        return
    CPU_THRESHOLD = cpu
    RAM_THRESHOLD = ram
    set_setting('cpu_threshold', str(cpu))
    set_setting('ram_threshold', str(ram))
    embed = create_success_embed("Thresholds Updated", f"**CPU:** {cpu}%\n**RAM:** {ram}%")
    await ctx.send(embed=embed)

@bot.command(name='set-status')
@is_admin()
async def set_status(ctx, activity_type: str, *, name: str):
    types = {
        'playing': discord.ActivityType.playing,
        'watching': discord.ActivityType.watching,
        'listening': discord.ActivityType.listening,
        'streaming': discord.ActivityType.streaming,
    }
    if activity_type.lower() not in types:
        await ctx.send(embed=create_error_embed("Invalid Type", "Valid types: playing, watching, listening, streaming"))
        return
    await bot.change_presence(activity=discord.Activity(type=types[activity_type.lower()], name=name))
    embed = create_success_embed("Status Updated", f"Set to {activity_type}: {name}")
    await ctx.send(embed=embed)

@bot.command(name='reload-env')
@is_admin()
async def reload_env(ctx):
    """Reload .env configuration without restarting bot"""
    try:
        # Reload .env file
        load_dotenv(override=True)
        
        # Reload all global variables
        global BOT_NAME, PREFIX, BOT_VERSION, BOT_DEVELOPER, MAIN_ADMIN_ID
        global YOUR_SERVER_IP, DEFAULT_STORAGE_POOL, CPU_THRESHOLD, RAM_THRESHOLD
        global VPS_USER_ROLE_ID, LXC_PREFIX
        
        BOT_NAME = os.getenv('BOT_NAME', 'INFINITY_CLOUD')
        LXC_PREFIX = ''.join(c if c.isalnum() else '-' for c in BOT_NAME.lower()).strip('-')
        PREFIX = os.getenv('PREFIX', '!')
        BOT_VERSION = os.getenv('BOT_VERSION', '7.1-PRO')
        BOT_DEVELOPER = os.getenv('BOT_DEVELOPER', 'HAPPY_FF')
        MAIN_ADMIN_ID = int(os.getenv('MAIN_ADMIN_ID', '0'))
        YOUR_SERVER_IP = os.getenv('YOUR_SERVER_IP', '127.0.0.1')
        DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', 'default')
        CPU_THRESHOLD = int(os.getenv('CPU_THRESHOLD', '90'))
        RAM_THRESHOLD = int(os.getenv('RAM_THRESHOLD', '90'))
        VPS_USER_ROLE_ID = int(os.getenv('VPS_USER_ROLE_ID', '0'))
        
        # Update settings in database
        set_setting('cpu_threshold', str(CPU_THRESHOLD))
        set_setting('ram_threshold', str(RAM_THRESHOLD))
        
        embed = create_success_embed("✅ Configuration Reloaded", 
            f"Successfully reloaded .env configuration!\n\n"
            f"**Bot Name:** {BOT_NAME}\n"
            f"**Prefix:** {PREFIX}\n"
            f"**Version:** {BOT_VERSION}\n"
            f"**Server IP:** {YOUR_SERVER_IP}\n"
            f"**CPU Threshold:** {CPU_THRESHOLD}%\n"
            f"**RAM Threshold:** {RAM_THRESHOLD}%")
        embed.set_footer(text="Changes applied without restart!")
        await ctx.send(embed=embed)
        
        logger.info(f"Configuration reloaded by {ctx.author.name}")
        
    except Exception as e:
        await ctx.send(embed=create_error_embed("Reload Failed", f"Error reloading configuration: {str(e)}"))
        logger.error(f"Failed to reload .env: {e}")

@bot.command(name="myvps")
async def my_vps(ctx):
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])

    # ─── No VPS Case ───────────────────────────────────────────
    if not vps_list:
        embed = create_error_embed(
            "❌ No VPS Found",
            f"You don’t have any **{BOT_NAME} VPS** yet."
        )
        embed.add_field(
            name="🚀 Quick Actions",
            value=(
                f"• `{PREFIX}manage` – Manage VPS\n"
                f"• Contact an admin to request a VPS"
            ),
            inline=False
        )
        await ctx.send(embed=embed)
        return

    # ─── Embed ────────────────────────────────────────────────
    embed = create_info_embed(
        title="🖥️ My VPS Dashboard",
        description="Your personal VPS overview"
    )

    total_vps = len(vps_list)
    running = suspended = whitelisted = 0
    vps_cards = []

    # ─── VPS Processing ───────────────────────────────────────
    for i, vps in enumerate(vps_list, start=1):
        node = get_node(vps.get("node_id"))
        node_name = node["name"] if node else "Unknown"

        config = vps.get("config", "Custom")
        ram = vps.get("ram", "0GB")
        cpu = vps.get("cpu", "0")
        storage = vps.get("storage", "0GB")

        if vps.get("suspended"):
            status = "⛔ SUSPENDED"
            suspended += 1
        elif vps.get("status") == "running":
            status = "🟢 RUNNING"
            running += 1
        else:
            status = "🔴 STOPPED"

        if vps.get("whitelisted"):
            whitelisted += 1
        
        # Get expiry info
        expiry_info = format_expiry_time(vps.get('expires_at'))

        vps_cards.append(
            f"**{i}.** `{vps['container_name']}`\n"
            f"{status} • `{config}`\n"
            f"⚙️ `{ram}` RAM • `{cpu}` CPU • `{storage}` Disk\n"
            f"📍 Node: `{node_name}`\n"
            f"⏰ Expires: {expiry_info['text']}"
        )

    # ─── Row 1 : Summary ──────────────────────────────────────
    embed.add_field(
        name="📊 Summary",
        value=(
            f"🖥️ `{total_vps}` VPS\n"
            f"🟢 `{running}` Running\n"
            f"⛔ `{suspended}` Suspended\n"
            f"✅ `{whitelisted}` Whitelisted"
        ),
        inline=True
    )

    embed.add_field(
        name="⚡ Quick Actions",
        value=(
            f"`{PREFIX}manage`\n"
            f"`{PREFIX}reinstall`\n"
            f"`{PREFIX}status`"
        ),
        inline=True
    )

    embed.add_field(
        name="🧭 Tip",
        value="Use **manage** to control your VPS",
        inline=True
    )

    # ─── VPS Cards (Full Width) ───────────────────────────────
    vps_text = "\n\n".join(vps_cards)
    for i in range(0, len(vps_text), 1024):
        embed.add_field(
            name="🖥️ Your VPS",
            value=vps_text[i:i + 1024],
            inline=False
        )

    embed.set_footer(text=f"{BOT_NAME} • VPS Control Panel")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name='lxc-list')
@is_admin()
async def lxc_list(ctx, node_id: int = 1):
    try:
        result = await execute_lxc("", "list", node_id=node_id)
        node = get_node(node_id)
        embed = create_info_embed(f"LXC Containers List on {node['name']}", result)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))

class NodeSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx, days: int = 7):
        super().__init__(timeout=300)
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        self.days = days
        nodes = get_nodes()
        options = []
        for n in nodes:
            current_count = get_current_vps_count(n['id'])
            if current_count < n['total_vps']:
                options.append(discord.SelectOption(label=n['name'], value=str(n['id']), description=f"{n['location']} - Available: {n['total_vps'] - current_count}"))
        if not options:
            self.add_item(discord.ui.Select(placeholder="No available nodes", disabled=True))
        else:
            self.select = discord.ui.Select(placeholder="Select a Node for the VPS", options=options)
            self.select.callback = self.select_node
            self.add_item(self.select)

    async def select_node(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can select."), ephemeral=True)
            return
        node_id = int(self.select.values[0])
        self.select.disabled = True
        await interaction.response.edit_message(view=self)
        os_view = OSSelectView(self.ram, self.cpu, self.disk, self.user, self.ctx, node_id, self.days)
        await interaction.followup.send(embed=create_info_embed("Select OS", "Choose the OS for the VPS."), view=os_view)

class OSSelectView(discord.ui.View):
    def __init__(self, ram: int, cpu: int, disk: int, user: discord.Member, ctx, node_id: int, days: int = 7):
        super().__init__(timeout=300)
        self.ram = ram
        self.cpu = cpu
        self.disk = disk
        self.user = user
        self.ctx = ctx
        self.node_id = node_id
        self.days = days
        self.select = discord.ui.Select(
            placeholder="Select an OS for the VPS",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS]
        )
        self.select.callback = self.select_os
        self.add_item(self.select)

    async def select_os(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the command author can select."), ephemeral=True)
            return
        os_version = self.select.values[0]
        self.select.disabled = True
        creating_embed = create_info_embed("Creating VPS", f"Deploying {os_version} VPS for {self.user.mention} on node {self.node_id}...")
        await interaction.response.edit_message(embed=creating_embed, view=self)
        user_id = str(self.user.id)
        if user_id not in vps_data:
            vps_data[user_id] = []
        vps_count = len(vps_data[user_id]) + 1
        container_name = f"{LXC_PREFIX}-vps-{user_id}-{vps_count}"
        try:
            existing = set()
            try:
                listing = await execute_lxc("", "list --format=json", node_id=self.node_id)
                for c in json.loads(listing):
                    existing.add(c.get("name"))
            except Exception:
                pass
            while container_name in existing:
                vps_count += 1
                container_name = f"{LXC_PREFIX}-vps-{user_id}-{vps_count}"
        except Exception as e:
            await interaction.followup.send(embed=create_error_embed("Deployment Failed", f"Name check error: {str(e)}"), ephemeral=True)
            return
        ram_mb = self.ram * 1024
        try:
            await execute_lxc(container_name, f"init {os_version} {container_name} -s {DEFAULT_STORAGE_POOL}", node_id=self.node_id)
            await execute_lxc(container_name, f"config set {container_name} limits.memory {ram_mb}MB", node_id=self.node_id)
            await execute_lxc(container_name, f"config set {container_name} limits.cpu {self.cpu}", node_id=self.node_id)
            await execute_lxc(container_name, f"config device set {container_name} root size={self.disk}GB", node_id=self.node_id)
            await apply_lxc_config(container_name, self.node_id)
            await execute_lxc(container_name, f"start {container_name}", node_id=self.node_id)
            await apply_internal_permissions(container_name, self.node_id)
            await recreate_port_forwards(container_name)
            config_str = f"{self.ram}GB RAM / {self.cpu} CPU / {self.disk}GB Disk"
            # Set expiration based on specified days
            expires_at = datetime.now() + timedelta(days=self.days)
            
            vps_info = {
                "container_name": container_name,
                "node_id": self.node_id,
                "ram": f"{self.ram}GB",
                "cpu": str(self.cpu),
                "storage": f"{self.disk}GB",
                "config": config_str,
                "os_version": os_version,
                "status": "running",
                "suspended": False,
                "whitelisted": False,
                "suspension_history": [],
                "created_at": datetime.now().isoformat(),
                "shared_with": [],
                "expires_at": expires_at.isoformat(),
                "duration_days": self.days,
                "auto_renew": 0,
                "id": None
            }
            vps_data[user_id].append(vps_info)
            save_vps_data()
            
            # Set expiration in database
            if vps_info.get('id'):
                set_vps_expiration(vps_info['id'], self.days)
            
            if self.ctx.guild:
                vps_role = await get_or_create_vps_role(self.ctx.guild)
                if vps_role:
                    try:
                        await self.user.add_roles(vps_role, reason=f"{BOT_NAME} VPS ownership granted")
                    except discord.Forbidden:
                        logger.warning(f"Failed to assign VPS role to {self.user.name}")
            
            success_embed = create_success_embed("VPS Created Successfully")
            add_field(success_embed, "Owner", self.user.mention, True)
            add_field(success_embed, "VPS ID", f"#{vps_count}", True)
            add_field(success_embed, "Container", f"`{container_name}`", True)
            add_field(success_embed, "Node", get_node(self.node_id)['name'], True)
            add_field(success_embed, "Resources", f"**RAM:** {self.ram}GB\n**CPU:** {self.cpu} Cores\n**Storage:** {self.disk}GB", False)
            add_field(success_embed, "OS", os_version, True)
            add_field(success_embed, "⏰ Expiration", 
                f"**Expires:** {expires_at.strftime('%Y-%m-%d %H:%M')}\n"
                f"**Duration:** {self.days} days\n"
                f"**Renewal:** Free with `{PREFIX}renew`", True)
            add_field(success_embed, "Features", "Nesting, Privileged, FUSE, Kernel Modules (Docker Ready), Unprivileged Ports from 0", False)
            add_field(success_embed, "Disk Note", "Run `sudo resize2fs /` inside VPS if needed to expand filesystem.", False)
            await interaction.followup.send(embed=success_embed)
            dm_embed = create_success_embed("VPS Created!", f"Your VPS has been successfully deployed by an admin!")
            add_field(dm_embed, "VPS Details", f"**VPS ID:** #{vps_count}\n**Container Name:** `{container_name}`\n**Configuration:** {config_str}\n**Status:** Running\n**OS:** {os_version}\n**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", False)
            add_field(dm_embed, "Management", f"• Use `{PREFIX}manage` to start/stop/reinstall your VPS\n• Use `{PREFIX}manage` → SSH for terminal access\n• Contact admin for upgrades or issues", False)
            add_field(dm_embed, "Important Notes", "• Full root access via SSH\n• Docker-ready with nesting and privileged mode\n• Back up your data regularly", False)
            try:
                await self.user.send(embed=dm_embed)
            except discord.Forbidden:
                await self.ctx.send(embed=create_info_embed("Notification Failed", f"Couldn't send DM to {self.user.mention}. Please ensure DMs are enabled."))
        except Exception as e:
            error_embed = create_error_embed("Creation Failed", f"Error: {str(e)}")
            await interaction.followup.send(embed=error_embed)

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, ram: int, cpu: int, disk: int, user: discord.Member, days: int = None):
    """Create a VPS with optional expiry days"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed("Invalid Specs", "RAM, CPU, and Disk must be positive integers."))
        return
    
    # Use default days if not specified
    if days is None:
        days = int(get_setting('default_vps_duration_days', 7))
    elif days < 1 or days > 365:
        await ctx.send(embed=create_error_embed("Invalid Duration", "Days must be between 1 and 365"))
        return
    
    embed = create_info_embed("VPS Creation", 
        f"Creating VPS for {user.mention}\n"
        f"**Specs:** {ram}GB RAM, {cpu} CPU cores, {disk}GB Disk\n"
        f"**Duration:** {days} days\n"
        f"Select node below.")
    view = NodeSelectView(ram, cpu, disk, user, ctx, days)
    await ctx.send(embed=embed, view=view)


class ManageView(discord.ui.View):
    """Interactive VPS control panel with pagination and action buttons"""
    def __init__(self, user_id, vps_list, is_admin=False, owner_id=None, is_shared=False, actual_index=None):
        super().__init__(timeout=600)
        self.user_id = str(user_id)
        self.vps_list = vps_list
        self.is_admin = is_admin
        self.owner_id = str(owner_id) if owner_id else str(user_id)
        self.is_shared = is_shared
        self.selected_index = (actual_index if actual_index is not None else 0)
        if not self.vps_list:
            self.selected_index = 0
        elif self.selected_index >= len(self.vps_list):
            self.selected_index = len(self.vps_list) - 1
        self.msg = None
        self.pending = False
        self._setup_pagination()

    def _setup_pagination(self):
        if len(self.vps_list) > 1:
            self.add_item(ManageNavButton("◀", "prev", row=0, style=discord.ButtonStyle.secondary))
            self.add_item(ManageNavButton("▶", "next", row=0, style=discord.ButtonStyle.secondary))

    def _can_control(self, interaction) -> bool:
        if str(interaction.user.id) == self.user_id:
            return True
        return str(interaction.user.id) == str(MAIN_ADMIN_ID) or str(interaction.user.id) in admin_data.get("admins", [])

    def _current_vps(self):
        return self.vps_list[self.selected_index]

    def _gb(self, value):
        try:
            return int(str(value).upper().replace("GB", "").strip())
        except (ValueError, AttributeError):
            return 0

    async def get_initial_embed(self):
        return await self.create_vps_embed(self.selected_index)

    async def create_vps_embed(self, index):
        vps = self.vps_list[index]
        container = vps["container_name"]
        node_id = vps.get("node_id")

        title = "🖥️ VPS Control Panel"
        if len(self.vps_list) > 1:
            title += f" ({index + 1}/{len(self.vps_list)})"

        embed = create_info_embed(title, f"Container: `{container}`")
        node = get_node(node_id) if node_id else None
        node_name = node["name"] if node else "Unknown"

        try:
            status = await get_container_status(container, node_id)
        except Exception:
            status = None
        status_text = vps.get("suspended") and "⛔ SUSPENDED" or (status or vps.get("status", "UNKNOWN"))
        if vps.get("suspended"):
            status_text = "⛔ SUSPENDED"

        add_field(embed, "Status", str(status_text), True)
        add_field(embed, "Node", node_name, True)
        add_field(embed, "OS", vps.get("os_version", "ubuntu:22.04"), True)

        config = vps.get("config", "Custom")
        add_field(embed, "Configuration", f"`{config}`", True)

        expiry_info = format_expiry_time(vps.get("expires_at"))
        add_field(embed, "Expiration", expiry_info["text"], True)

        add_field(embed, "Quick Actions",
            f"▶️ Start | ⏸️ Stop | 🔄 Restart\n"
            f"🌐 Reinstall | 📊 Stats | 🔑 SSH\n"
            f"◀ ▶ page (agar multiple VPS)", False)

        embed.set_footer(text=f"{BOT_NAME} • VPS Control Panel")
        return embed

    def _action_prefix(self):
        return ("Shared" if self.is_shared else "Admin" if self.is_admin else "User")

    async def run_action(self, interaction, action: str):
        if not self._can_control(interaction):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the authorized user can control this VPS."), ephemeral=True)
            return
        if self.pending:
            await interaction.response.send_message(embed=create_warning_embed("Busy", "An operation is already running. Please wait."), ephemeral=True)
            return
        vps = self._current_vps()
        container = vps["container_name"]
        node_id = vps.get("node_id")
        self.pending = True
        try:
            await interaction.response.defer()
            if action == "start":
                await execute_lxc(container, f"start {container}", node_id=node_id)
                vps["status"] = "running"
            elif action == "stop":
                await execute_lxc(container, f"stop {container} --force", node_id=node_id)
                vps["status"] = "stopped"
            elif action == "restart":
                await execute_lxc(container, f"restart {container}", node_id=node_id)
                vps["status"] = "running"
            elif action == "stats":
                stats = await get_container_stats(container, node_id)
                stats_embed = create_embed(f"📊 Live Stats - {container}",
                    "Real-time resource usage", 0x1abc9c)
                add_field(stats_embed, "Status", str(stats.get("status", "Unknown")), True)
                add_field(stats_embed, "CPU", f"{stats.get('cpu', 0)}%", True)
                add_field(stats_embed, "RAM", stats.get("ram", "N/A"), True)
                add_field(stats_embed, "Disk", stats.get("disk", "N/A"), True)
                add_field(stats_embed, "Uptime", stats.get("uptime", "N/A"), False)
                await interaction.followup.send(embed=stats_embed, ephemeral=True)
                self.pending = False
                return
            elif action == "ssh":
                ssh_embed = create_info_embed("🔑 SSH Access",
                    f"Connect to your VPS with:\n\n"
                    f"`ssh root@{YOUR_SERVER_IP}`\n\n"
                    f"Port forwarding ke liye `{PREFIX}ports list` use karein.\n"
                    f"Agar SSH port forwarded hai: `ssh -p <port> root@{YOUR_SERVER_IP}`")
                await interaction.followup.send(embed=ssh_embed, ephemeral=True)
                self.pending = False
                return
            save_vps_data()
            embed = await self.create_vps_embed(self.selected_index)
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception as e:
            logger.error(f"ManageView {action} error: {e}")
            await interaction.followup.send(embed=create_error_embed("Action Failed", str(e)), ephemeral=True)
        finally:
            self.pending = False

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️", row=1)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.run_action(interaction, "start")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏸️", row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.run_action(interaction, "stop")

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔄", row=1)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.run_action(interaction, "restart")

    @discord.ui.button(label="Reinstall", style=discord.ButtonStyle.primary, emoji="🌐", row=2)
    async def reinstall_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._can_control(interaction):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the authorized user can reinstall this VPS."), ephemeral=True)
            return
        vps = self._current_vps()
        view = ReinstallConfirmView(self, interaction.user.id)
        await interaction.response.send_message(
            embed=create_warning_embed("Reinstall VPS", f"`{vps['container_name']}` reinstall hoga - SAARA DATA DELETE ho jayega!\nOS select karein:"),
            view=view, ephemeral=True)

    @discord.ui.button(label="Stats", style=discord.ButtonStyle.secondary, emoji="📊", row=2)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.run_action(interaction, "stats")

    @discord.ui.button(label="SSH", style=discord.ButtonStyle.secondary, emoji="🔑", row=2)
    async def ssh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.run_action(interaction, "ssh")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="❌", row=3)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("Sirf command chalane wala close kar sakta hai.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        if self.msg:
            try:
                await self.msg.edit(view=None)
            except Exception:
                pass


class ManageNavButton(discord.ui.Button):
    def __init__(self, label, action, row, style=discord.ButtonStyle.secondary, disabled=False):
        super().__init__(label=label, style=style, row=row)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view: ManageView = self.view
        if str(interaction.user.id) != view.user_id:
            await interaction.response.send_message("Sirf command chalane wala page change kar sakta hai.", ephemeral=True)
            return
        if view.pending:
            await interaction.response.send_message("Operation chal rahi hai, thoda ruko.", ephemeral=True)
            return
        if self.action == "prev":
            view.selected_index = (view.selected_index - 1) % len(view.vps_list)
        else:
            view.selected_index = (view.selected_index + 1) % len(view.vps_list)
        embed = await view.create_vps_embed(view.selected_index)
        await interaction.response.edit_message(embed=embed, view=view)


class ReinstallConfirmView(discord.ui.View):
    """OS selection for VPS reinstall"""
    def __init__(self, manage_view: ManageView, user_id: int):
        super().__init__(timeout=120)
        self.manage_view = manage_view
        self.user_id = str(user_id)
        select = discord.ui.Select(
            placeholder="Select new OS",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS]
        )
        select.callback = self.select_os
        self.add_item(select)

    async def select_os(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            return
        await interaction.response.defer(ephemeral=True)
        mv = self.manage_view
        vps = mv._current_vps()
        container = vps["container_name"]
        node_id = vps.get("node_id")
        os_version = self.children[0].values[0]
        await interaction.edit_original_response(
            embed=create_info_embed("🔄 Reinstalling", f"`{container}` reinstall ho raha hai ({os_version})..."),
            view=None)
        try:
            await execute_lxc(container, f"stop {container} --force", node_id=node_id)
            await execute_lxc(container, f"delete {container}", node_id=node_id)
            await execute_lxc(container, f"init {os_version} {container} -s {DEFAULT_STORAGE_POOL}", node_id=node_id)
            ram_mb = mv._gb(vps.get("ram")) * 1024
            cpu = mv._gb(vps.get("cpu"))
            disk = mv._gb(vps.get("storage"))
            if ram_mb >= 1024:
                await execute_lxc(container, f"config set {container} limits.memory {ram_mb}MB", node_id=node_id)
            if cpu > 0:
                await execute_lxc(container, f"config set {container} limits.cpu {cpu}", node_id=node_id)
            if disk > 0:
                await execute_lxc(container, f"config device set {container} root size={disk}GB", node_id=node_id)
            await apply_lxc_config(container, node_id)
            await execute_lxc(container, f"start {container}", node_id=node_id)
            await apply_internal_permissions(container, node_id)
            await recreate_port_forwards(container)
            vps["os_version"] = os_version
            vps["status"] = "running"
            save_vps_data()
            await interaction.followup.send(embed=create_success_embed("Reinstall Complete", f"`{container}` nayi OS ke saath ready hai!"), ephemeral=True)
            embed = await mv.create_vps_embed(mv.selected_index)
            await mv.msg.edit(embed=embed, view=mv)
        except Exception as e:
            logger.error(f"Reinstall error: {e}")
            await interaction.followup.send(embed=create_error_embed("Reinstall Failed", str(e)), ephemeral=True)


@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    if user:
        if str(ctx.author.id) != str(MAIN_ADMIN_ID) and str(ctx.author.id) not in admin_data.get("admins", []):
            await ctx.send(embed=create_error_embed("Access Denied", "Only admins can manage other users' VPS."))
            return
        user_id = str(user.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            await ctx.send(embed=create_error_embed("No VPS Found", f"{user.mention} doesn't have any {BOT_NAME} VPS."))
            return
        view = ManageView(str(ctx.author.id), vps_list, is_admin=True, owner_id=user_id)
        view.msg = await ctx.send(embed=create_info_embed(f"Managing {user.name}'s VPS", f"Managing VPS for {user.mention}"), view=view)
    else:
        user_id = str(ctx.author.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            embed = create_error_embed("No VPS Found", f"You don't have any {BOT_NAME} VPS. Contact an admin to create one.")
            add_field(embed, "Quick Actions", f"• `{PREFIX}manage` - Manage VPS\n• Contact admin for VPS creation", False)
            await ctx.send(embed=embed)
            return
        view = ManageView(user_id, vps_list)
        embed = await view.get_initial_embed()
        view.msg = await ctx.send(embed=embed, view=view)

async def get_node_status(node_id: int) -> str:
    node = get_node(node_id)
    if not node:
        return "❓ Unknown"
    if node['is_local']:
        return "🟢 Online (Local)"
    try:
        response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
        if response.status_code == 200:
            return "🟢 Online"
        else:
            return "🔴 Offline"
    except Exception as e:
        logger.error(f"Failed to ping node {node['name']}: {e}")
        return "🔴 Offline"


def get_host_disk_usage():
    """Get host disk usage - cross-platform"""
    try:
        # Try using psutil first (cross-platform)
        try:
            import psutil
            # Get disk usage for the root/main drive
            if os.name == 'nt':  # Windows
                disk = psutil.disk_usage('C:\\')
            else:  # Linux/Unix
                disk = psutil.disk_usage('/')
            
            total_gb = disk.total / (1024**3)
            used_gb = disk.used / (1024**3)
            percent = disk.percent
            
            return f"{used_gb:.1f}GB/{total_gb:.1f}GB ({percent}%)"
        except ImportError:
            pass
        
        # Linux fallback
        if shutil.which("df"):
            result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True)
            lines = result.stdout.splitlines()
            if len(lines) > 1:
                parts = lines[1].split()
                total = parts[1]
                used = parts[2]
                perc = parts[4]
                return f"{used}/{total} ({perc})"
        
        return "Unknown"
    except Exception as e:
        logger.error(f"Error getting disk usage: {e}")
        return "Unknown"


async def get_host_stats(node_id: int) -> Dict:
    node = get_node(node_id)
    if node['is_local']:
        return {
            "cpu": get_host_cpu_usage(),
            "ram": get_host_ram_usage(),
            "disk": get_host_disk_usage()
        }
    else:
        url = f"{node['url']}/api/get_host_stats"
        params = {"api_key": node["api_key"]}
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            stats = response.json()
            # Fallbacks if remote API doesn't provide
            stats['disk'] = stats.get('disk', 'Unknown')
            return stats
        except Exception as e:
            logger.error(f"Failed to get host stats from node {node['name']}: {e}")
            return {"cpu": 0.0, "ram": 0.0, "disk": "Unknown"}


@bot.command(name='vps-list')
@is_admin()
async def vps_list(ctx, node_id: int = 1):
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"Node ID {node_id} not found."))
        return

    # Get node status
    status = await get_node_status(node_id)
    is_online = status.startswith("🟢")

    # Get node resource stats (will use defaults if offline)
    stats = await get_host_stats(node_id)
    cpu_usage = stats.get('cpu', 0.0)
    ram_usage = stats.get('ram', 0.0)
    disk_usage = stats.get('disk', 'Unknown')

    # Resources field text (modern: compact inline stats with progress-like emojis)
    if is_online:
        resources_text = (
            f"**CPU** {cpu_usage:.0f}% {'█' * int(cpu_usage / 5) + '░' * (20 - int(cpu_usage / 5))} "
            f"\n**RAM** {ram_usage:.0f}% {'█' * int(ram_usage / 5) + '░' * (20 - int(ram_usage / 5))} "
            f"\n**Disk** {disk_usage}"
        )
    else:
        resources_text = "⚠️ Resources unavailable (Offline)"

    # Get VPS capacity
    current_vps = get_current_vps_count(node_id)
    total_capacity = node['total_vps']
    capacity_percent = (current_vps / total_capacity * 100) if total_capacity > 0 else 0
    capacity_text = f"{current_vps}/{total_capacity} ({capacity_percent:.0f}%)"

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM vps WHERE node_id = ?', (node_id,))
    rows = cur.fetchall()
    conn.close()

    total_vps = len(rows)

    # Modern counters: use more intuitive emojis and clean layout
    running = 0
    stopped = 0
    suspended = 0
    other = 0
    vps_info = []
    for i, row in enumerate(rows, 1):
        vps = dict(row)
        user_id = vps['user_id']
        try:
            user = await bot.fetch_user(int(user_id))
            username = user.name
        except:
            username = f"Unknown ({user_id})"

        status = vps.get('status', 'unknown')
        suspended_flag = vps.get('suspended', False)

        # Count logic: suspended first, then status if not suspended
        if suspended_flag:
            suspended += 1
        elif status == 'running':
            running += 1
        elif status == 'stopped':
            stopped += 1
        else:
            other += 1

        # Modern emoji: vibrant and status-specific
        status_emoji = "🟢" if status == 'running' and not suspended_flag else "🟡" if suspended_flag else "🔴"
        vps_status = status.upper()
        if suspended_flag:
            vps_status += " (SUSPENDED)"
        if vps.get('whitelisted', False):
            vps_status += " (WHITELISTED)"
        config = vps.get('config', 'Custom')
        vps_info.append(f"{status_emoji} **{i}.** {username} • `{vps['container_name']}`\n _{vps_status} | {config}_")

    # Create main embed (modern: gradient-inspired colors, clean typography)
    color = 0x10b981 if is_online else 0xef4444  # Teal green / Soft red for modern feel
    embed = create_embed(
        title=f"🖥️ VPS Dashboard - {node['name']}",
        description=f"**ID:** `{node_id}` | **Region:** {node['location']}\n*Updated: <t:{int(datetime.now().timestamp())}:R>*",
        color=color
    )
    embed.set_thumbnail(url=node.get('thumbnail_url', None))

    # Inline status and capacity for compact top row
    add_field(embed, "📡 **Status**", status, True)
    add_field(embed, "🗄️ **Capacity**", capacity_text, True)

    # Resources field with modern bar visualization
    add_field(embed, "📊 **Resources**", resources_text, False)

    # Summary field (modern: compact bullet-like with inline emojis)
    summary_text = (
        f"**Total:** {total_vps} 📊\n"
        f"**Running:** {running} 🟢\n"
        f"**Stopped:** {stopped} ⏸️\n"
        f"**Suspended:** {suspended} 🟡"
    )
    if other > 0:
        summary_text += f"\n**Other:** {other} ⚠️"
    add_field(embed, "📈 **Summary**", summary_text, True)

    # VPS List - chunked embeds with modern pagination
    if vps_info:
        chunk_size = 6  # Smaller chunks for cleaner mobile-friendly embeds
        chunks = [vps_info[i:i + chunk_size] for i in range(0, len(vps_info), chunk_size)]
        first_chunk_text = "\n".join(chunks[0])
        add_field(embed, "📋 **Active VPS (1/{len(chunks)})**", f"```{first_chunk_text}```", False)

        # Paginated follow-ups with consistent styling
        for idx, chunk in enumerate(chunks[1:], 2):
            page_embed = create_embed(
                title=f"🖥️ VPS Dashboard - {node['name']} (Page {idx}/{len(chunks)})",
                description=f"**ID:** `{node_id}` | **Region:** {node['location']}\n*Updated: <t:{int(datetime.now().timestamp())}:R>*",
                color=color
            )
            chunk_text = "\n".join(chunk)
            add_field(page_embed, "📋 **VPS List**", f"```{chunk_text}```", False)
            page_embed.set_footer(text=f"Total: {total_vps} VPS | Powered by INFINITY_CLOUD")
            await ctx.send(embed=page_embed)
    else:
        add_field(embed, "📋 **VPS List**", "No VPS deployed yet.", False)

    embed.set_footer(text=f"Refresh with !vps-list {node_id} | {len(vps_info)} shown")
    await ctx.send(embed=embed)

@bot.command(name='list-all')
@is_admin()
async def list_all_vps(ctx):
    total_vps = 0
    total_users = len(vps_data)
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    whitelisted_vps = 0
    vps_info = []
    user_summary = []
    for user_id, vps_list in vps_data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            user_vps_count = len(vps_list)
            user_running = sum(1 for vps in vps_list if vps.get('status') == 'running' and not vps.get('suspended', False))
            user_stopped = sum(1 for vps in vps_list if vps.get('status') == 'stopped')
            user_suspended = sum(1 for vps in vps_list if vps.get('suspended', False))
            user_whitelisted = sum(1 for vps in vps_list if vps.get('whitelisted', False))
            total_vps += user_vps_count
            running_vps += user_running
            stopped_vps += user_stopped
            suspended_vps += user_suspended
            whitelisted_vps += user_whitelisted
            user_summary.append(f"**{user.name}** ({user.mention}) - {user_vps_count} VPS ({user_running} running, {user_suspended} suspended, {user_whitelisted} whitelisted)")
            for i, vps in enumerate(vps_list):
                node = get_node(vps['node_id'])
                node_name = node['name'] if node else "Unknown"
                status_emoji = "🟢" if vps.get('status') == 'running' and not vps.get('suspended', False) else "🟡" if vps.get('suspended', False) else "🔴"
                status_text = vps.get('status', 'unknown').upper()
                if vps.get('suspended', False):
                    status_text += " (SUSPENDED)"
                if vps.get('whitelisted', False):
                    status_text += " (WHITELISTED)"
                vps_info.append(f"{status_emoji} **{user.name}** - VPS {i+1}: `{vps['container_name']}` - {vps.get('config', 'Custom')} - {status_text} (Node: {node_name})")
        except discord.NotFound:
            vps_info.append(f"❓ Unknown User ({user_id}) - {len(vps_list)} VPS")
    embed = create_embed("All VPS Information", "Complete overview of all VPS deployments and user statistics", 0x1a1a1a)
    add_field(embed, "System Overview", f"**Total Users:** {total_users}\n**Total VPS:** {total_vps}\n**Running:** {running_vps}\n**Stopped:** {stopped_vps}\n**Suspended:** {suspended_vps}\n**Whitelisted:** {whitelisted_vps}", False)
    await ctx.send(embed=embed)
    if user_summary:
        embed = create_embed("User Summary", f"Summary of all users and their VPS", 0x1a1a1a)
        summary_text = "\n".join(user_summary)
        chunks = [summary_text[i:i+1024] for i in range(0, len(summary_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            add_field(embed, f"Users (Part {idx})", chunk, False)
        await ctx.send(embed=embed)
    if vps_info:
        vps_text = "\n".join(vps_info)
        chunks = [vps_text[i:i+1024] for i in range(0, len(vps_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"VPS Details (Part {idx})", "List of all VPS deployments", 0x1a1a1a)
            add_field(embed, "VPS List", chunk, False)
            await ctx.send(embed=embed)

@bot.command(name='manage-shared')
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    owner_id = str(owner.id)
    user_id = str(ctx.author.id)
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or owner doesn't have a VPS."))
        return
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "You do not have access to this VPS."))
        return
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id, actual_index=vps_number - 1)
    embed = await view.get_initial_embed()
    await ctx.send(embed=embed, view=view)

@bot.command(name='share-user')
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or you don't have a VPS."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access to this VPS!"))
        return
    vps["shared_with"].append(shared_user_id)
    save_vps_data()
    await ctx.send(embed=create_success_embed("VPS Shared", f"VPS #{vps_number} shared with {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed("VPS Access Granted", f"You have access to VPS #{vps_number} from {ctx.author.mention}. Use `{PREFIX}manage-shared {ctx.author.mention} {vps_number}`", 0x00ff88))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {shared_user.mention}"))

@bot.command(name='share-ruser')
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    user_id = str(ctx.author.id)
    shared_user_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Invalid VPS number or you don't have a VPS."))
        return
    vps = vps_data[user_id][vps_number - 1]
    if "shared_with" not in vps:
        vps["shared_with"] = []
    if shared_user_id not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access to this VPS!"))
        return
    vps["shared_with"].remove(shared_user_id)
    save_vps_data()
    await ctx.send(embed=create_success_embed("Access Revoked", f"Access to VPS #{vps_number} revoked from {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed("VPS Access Revoked", f"Your access to VPS #{vps_number} by {ctx.author.mention} has been revoked.", 0xff3366))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {shared_user.mention}"))

@bot.command(name='ports-add-user')
@is_admin()
async def ports_add_user(ctx, amount: int, user: discord.Member):
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be a positive integer."))
        return
    user_id = str(user.id)
    allocate_ports(user_id, amount)
    embed = create_success_embed("Ports Allocated", f"Allocated {amount} port slots to {user.mention}.")
    add_field(embed, "Quota", f"Total: {get_user_allocation(user_id)} slots", False)
    await ctx.send(embed=embed)
    try:
        dm_embed = create_info_embed("Port Slots Allocated", f"You have been granted {amount} additional port forwarding slots by an admin.\nUse `{PREFIX}ports list` to view your quota and active forwards.")
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", f"Could not notify {user.mention} via DM."))

@bot.command(name='ports-remove-user')
@is_admin()
async def ports_remove_user(ctx, amount: int, user: discord.Member):
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid Amount", "Amount must be a positive integer."))
        return
    user_id = str(user.id)
    current = get_user_allocation(user_id)
    if amount > current:
        amount = current
    deallocate_ports(user_id, amount)
    remaining = get_user_allocation(user_id)
    embed = create_success_embed("Ports Deallocated", f"Removed {amount} port slots from {user.mention}.")
    add_field(embed, "Remaining Quota", f"{remaining} slots", False)
    await ctx.send(embed=embed)
    try:
        dm_embed = create_warning_embed("Port Slots Reduced", f"Your port forwarding quota has been reduced by {amount} slots by an admin.\nRemaining: {remaining} slots.")
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", f"Could not notify {user.mention} via DM."))

@bot.command(name='ports-revoke')
@is_admin()
async def ports_revoke(ctx, forward_id: int):
    success, user_id = await remove_port_forward(forward_id, is_admin=True)
    if success and user_id:
        try:
            user = await bot.fetch_user(int(user_id))
            dm_embed = create_warning_embed("Port Forward Revoked", f"One of your port forwards (ID: {forward_id}) has been revoked by an admin.")
            await user.send(embed=dm_embed)
        except:
            pass
        await ctx.send(embed=create_success_embed("Revoked", f"Port forward ID {forward_id} revoked."))
    else:
        await ctx.send(embed=create_error_embed("Failed", "Port forward ID not found or removal failed."))

@bot.command(name='ports')
async def ports_command(ctx, subcmd: str = None, *args):
    user_id = str(ctx.author.id)
    allocated = get_user_allocation(user_id)
    used = get_user_used_ports(user_id)
    available = allocated - used
    if subcmd is None:
        embed = create_info_embed("Port Forwarding Help", f"**Your Quota:** Allocated: {allocated}, Used: {used}, Available: {available}")
        add_field(embed, "Commands", f"{PREFIX}ports add <vps_num> <port>\n{PREFIX}ports list\n{PREFIX}ports remove <id>", False)
        await ctx.send(embed=embed)
        return
    if subcmd == 'add':
        if len(args) < 2:
            await ctx.send(embed=create_error_embed("Usage", f"Usage: {PREFIX}ports add <vps_number> <vps_port>"))
            return
        try:
            vps_num = int(args[0])
            vps_port = int(args[1])
            if vps_port < 1 or vps_port > 65535:
                raise ValueError
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid Input", "VPS number and port must be positive integers (port: 1-65535)."))
            return
        vps_list = vps_data.get(user_id, [])
        if vps_num < 1 or vps_num > len(vps_list):
            await ctx.send(embed=create_error_embed("Invalid VPS", f"Invalid VPS number (1-{len(vps_list)}). Use {PREFIX}myvps to list."))
            return
        vps = vps_list[vps_num - 1]
        container = vps['container_name']
        node_id = vps['node_id']
        if used >= allocated:
            await ctx.send(embed=create_error_embed("Quota Exceeded", f"No available slots. Allocated: {allocated}, Used: {used}. Contact admin for more."))
            return
        host_port = await create_port_forward(user_id, container, vps_port, node_id)
        if host_port:
            embed = create_success_embed("Port Forward Created", f"VPS #{vps_num} port {vps_port} (TCP/UDP) forwarded to host port {host_port}.")
            add_field(embed, "Access", f"External: {YOUR_SERVER_IP}:{host_port} → VPS:{vps_port} (TCP & UDP)", False)
            add_field(embed, "Quota Update", f"Used: {used + 1}/{allocated}", False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=create_error_embed("Failed", "Could not assign host port. Try again later."))
    elif subcmd == 'list':
        forwards = get_user_forwards(user_id)
        embed = create_info_embed("Your Port Forwards", f"**Quota:** Allocated: {allocated}, Used: {used}, Available: {available}")
        if not forwards:
            add_field(embed, "Forwards", "No active port forwards.", False)
        else:
            text = []
            for f in forwards:
                vps_num = next((i+1 for i, v in enumerate(vps_data.get(user_id, [])) if v['container_name'] == f['vps_container']), 'Unknown')
                created = datetime.fromisoformat(f['created_at']).strftime('%Y-%m-%d %H:%M')
                created = datetime.fromisoformat(f['created_at']).strftime('%Y-%m-%d %H:%M')
                text.append(f"**ID {f['id']}** - VPS #{vps_num}: {f['vps_port']} (TCP/UDP) → {f['host_port']} (Created: {created})")
            add_field(embed, "Active Forwards", "\n".join(text[:10]), False)
            if len(forwards) > 10:
                add_field(embed, "Note", f"Showing 10 of {len(forwards)}. Remove unused with {PREFIX}ports remove <id>.")
        await ctx.send(embed=embed)
    elif subcmd == 'remove':
        if len(args) < 1:
            await ctx.send(embed=create_error_embed("Usage", f"Usage: {PREFIX}ports remove <forward_id>"))
            return
        try:
            fid = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Forward ID must be an integer."))
            return
        success, _ = await remove_port_forward(fid)
        if success:
            embed = create_success_embed("Removed", f"Port forward {fid} removed (TCP & UDP).")
            add_field(embed, "Quota Update", f"Used: {used - 1}/{allocated}", False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=create_error_embed("Not Found", "Forward ID not found. Use !ports list."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Subcommand", f"Use: add <vps_num> <port>, list, remove <id>"))

@bot.command(name='delete-vps')
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason"):
    user_id = str(user.id)

    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed(
            "Invalid VPS",
            "Invalid VPS number or user doesn't have that VPS."
        ))
        return

    vps = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    node_id = vps.get("node_id", 1)

    await ctx.send(embed=create_info_embed(
        "Deleting VPS",
        f"Removing VPS #{vps_number} for {user.mention}..."
    ))

    node_result = "Not checked"

    # 1️⃣ Try deleting container
    try:
        await execute_lxc(container_name, f"delete {container_name} --force", node_id=node_id)
        node_result = "Container deleted successfully."
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["not found", "does not exist", "no such container"]):
            node_result = "Container not found (force DB cleanup)."
        else:
            node_result = f"Container delete failed: {e}"

    # 2️⃣ DELETE FROM DATABASE (THIS IS THE FIX)
    conn = get_db()
    cur = conn.cursor()

    cur.execute("DELETE FROM vps WHERE container_name = ?", (container_name,))
    cur.execute("DELETE FROM port_forwards WHERE vps_container = ?", (container_name,))

    conn.commit()
    conn.close()

    # 3️⃣ Remove from memory
    del vps_data[user_id][vps_number - 1]
    if not vps_data[user_id]:
        del vps_data[user_id]

        # Remove VPS role if needed
        if ctx.guild:
            role = await get_or_create_vps_role(ctx.guild)
            if role and role in user.roles:
                try:
                    await user.remove_roles(role, reason="No VPS ownership")
                except discord.Forbidden:
                    logger.warning(f"Failed to remove VPS role from {user.name}")

    save_vps_data()

    # 4️⃣ Success embed
    embed = create_success_embed("🌟 INFINITY_CLOUD - VPS Deleted Successfully")
    add_field(embed, "Owner", user.mention, True)
    add_field(embed, "VPS Number", f"#{vps_number}", True)
    add_field(embed, "Container", container_name, False)
    add_field(embed, "Node Result", node_result, False)
    add_field(embed, "Reason", reason, False)

    await ctx.send(embed=embed)

@bot.command(name='add-resources')
@is_admin()
async def add_resources(ctx, vps_id: str, ram: int = None, cpu: int = None, disk: int = None):
    if ram is None and cpu is None and disk is None:
        await ctx.send(embed=create_error_embed("Missing Parameters", "Please specify at least one resource to add (ram, cpu, or disk)"))
        return
    found_vps = None
    user_id = None
    vps_index = None
    for uid, vps_list in vps_data.items():
        for i, vps in enumerate(vps_list):
            if vps['container_name'] == vps_id:
                found_vps = vps
                user_id = uid
                vps_index = i
                break
        if found_vps:
            break
    if not found_vps:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with ID: `{vps_id}`"))
        return
    node_id = found_vps['node_id']
    was_running = found_vps.get('status') == 'running' and not found_vps.get('suspended', False)
    disk_changed = disk is not None
    if was_running:
        await ctx.send(embed=create_info_embed("Stopping VPS", f"Stopping VPS `{vps_id}` to apply resource changes..."))
        try:
            await execute_lxc(vps_id, "stop {vps_id}", node_id=node_id)
            found_vps['status'] = 'stopped'
            save_vps_data()
        except Exception as e:
            await ctx.send(embed=create_error_embed("Stop Failed", f"Error stopping VPS: {str(e)}"))
            return
    changes = []
    try:
        current_ram_gb = int(found_vps['ram'].replace('GB', ''))
        current_cpu = int(found_vps['cpu'])
        current_disk_gb = int(found_vps['storage'].replace('GB', ''))
        new_ram_gb = current_ram_gb
        new_cpu = current_cpu
        new_disk_gb = current_disk_gb
        if ram is not None and ram > 0:
            new_ram_gb += ram
            ram_mb = new_ram_gb * 1024
            await execute_lxc(vps_id, f"config set {vps_id} limits.memory {ram_mb}MB", node_id=node_id)
            changes.append(f"RAM: +{ram}GB (New total: {new_ram_gb}GB)")
        if cpu is not None and cpu > 0:
            new_cpu += cpu
            await execute_lxc(vps_id, f"config set {vps_id} limits.cpu {new_cpu}", node_id=node_id)
            changes.append(f"CPU: +{cpu} cores (New total: {new_cpu} cores)")
        if disk is not None and disk > 0:
            new_disk_gb += disk
            await execute_lxc(vps_id, f"config device set {vps_id} root size={new_disk_gb}GB", node_id=node_id)
            changes.append(f"Disk: +{disk}GB (New total: {new_disk_gb}GB)")
        found_vps['ram'] = f"{new_ram_gb}GB"
        found_vps['cpu'] = str(new_cpu)
        found_vps['storage'] = f"{new_disk_gb}GB"
        found_vps['config'] = f"{new_ram_gb}GB RAM / {new_cpu} CPU / {new_disk_gb}GB Disk"
        vps_data[user_id][vps_index] = found_vps
        save_vps_data()
        if was_running:
            await execute_lxc(vps_id, f"start {vps_id}", node_id=node_id)
            found_vps['status'] = 'running'
            save_vps_data()
            await apply_internal_permissions(vps_id, node_id)
            await recreate_port_forwards(vps_id)
        embed = create_success_embed("Resources Added", f"Successfully added resources to VPS `{vps_id}`")
        add_field(embed, "Changes Applied", "\n".join(changes), False)
        if disk_changed:
            add_field(embed, "Disk Note", "Run `sudo resize2fs /` inside the VPS to expand the filesystem.", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Resource Addition Failed", f"Error: {str(e)}"))


@bot.command(name='status')
@is_admin()
async def system_status(ctx):
    """
    Show complete system status including:
    - Bot uptime
    - Total nodes & their status
    - Running/stopped nodes count
    - Total RAM/CPU/DISK allocated vs free
    - Total VPS & users
    - Running/stopped/suspended VPS counts
    - Total admin users
    - Whitelisted VPS
    """
    
    # Start timing for response time
    start_time = time.time()
    
    # Get bot uptime
    bot_start_time = datetime.now() - datetime.fromtimestamp(start_time - bot.latency)
    bot_uptime = str(bot_start_time).split('.')[0]  # Remove microseconds
    
    # Get total nodes
    nodes = get_nodes()
    total_nodes = len(nodes)
    
    # Node status counters
    running_nodes = 0
    stopped_nodes = 0
    local_nodes = 0
    remote_nodes = 0
    
    # Node resource tracking
    total_node_cpu_allocated = 0
    total_node_ram_allocated = 0
    total_node_disk_allocated = 0
    total_node_cpu_free = 0
    total_node_ram_free = 0
    total_node_disk_free = 0
    
    # VPS counters
    total_vps = 0
    total_users = len(vps_data)
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    whitelisted_vps = 0
    
    # Admin counters
    total_admins = len(admin_data.get("admins", [])) + 1  # +1 for main admin
    
    # Port statistics
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT SUM(allocated_ports) FROM port_allocations")
    total_ports_allocated = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM port_forwards")
    total_ports_used = cur.fetchone()[0] or 0
    conn.close()
    
    # Resource counters for all VPS
    total_ram_allocated = 0
    total_cpu_allocated = 0
    total_disk_allocated = 0
    
    # Process all VPS data
    for user_id, vps_list in vps_data.items():
        total_vps += len(vps_list)
        
        for vps in vps_list:
            # Count status
            if vps.get('suspended', False):
                suspended_vps += 1
            elif vps.get('status') == 'running':
                running_vps += 1
            else:
                stopped_vps += 1
            
            # Count whitelisted
            if vps.get('whitelisted', False):
                whitelisted_vps += 1
            
            # Calculate allocated resources
            try:
                ram_gb = int(vps['ram'].replace('GB', ''))
                total_ram_allocated += ram_gb
            except:
                pass
            
            try:
                cpu_cores = int(vps['cpu'])
                total_cpu_allocated += cpu_cores
            except:
                pass
            
            try:
                disk_gb = int(vps['storage'].replace('GB', ''))
                total_disk_allocated += disk_gb
            except:
                pass
    
    # Check node status and calculate free resources
    node_statuses = []
    
    for node in nodes:
        # Determine node type
        if node['is_local']:
            local_nodes += 1
            node_type = "🖥️ Local"
        else:
            remote_nodes += 1
            node_type = "🌐 Remote"
        
        # Check node status
        if node['is_local']:
            status = "🟢 Online"
            running_nodes += 1
            
            # Get local resources (approximate)
            try:
                # Get system memory
                mem_result = subprocess.run(['free', '-m'], capture_output=True, text=True)
                mem_lines = mem_result.stdout.splitlines()
                if len(mem_lines) > 1:
                    mem = mem_lines[1].split()
                    total_ram_mb = int(mem[1])
                    used_ram_mb = int(mem[2])
                    free_ram_mb = total_ram_mb - used_ram_mb
                    total_ram_gb = total_ram_mb / 1024
                    free_ram_gb = free_ram_mb / 1024
                else:
                    total_ram_gb = 0
                    free_ram_gb = 0
                
                # Get CPU cores
                cpu_result = subprocess.run(['nproc'], capture_output=True, text=True)
                total_cpu = int(cpu_result.stdout.strip()) if cpu_result.stdout.strip() else 0
                
                # Get disk space
                disk_result = subprocess.run(['df', '-h', '/'], capture_output=True, text=True)
                disk_lines = disk_result.stdout.splitlines()
                if len(disk_lines) > 1:
                    disk_parts = disk_lines[1].split()
                    total_disk_str = disk_parts[1]
                    # Convert to GB
                    if 'T' in total_disk_str:
                        total_disk = float(total_disk_str.replace('T', '')) * 1024
                    elif 'G' in total_disk_str:
                        total_disk = float(total_disk_str.replace('G', ''))
                    elif 'M' in total_disk_str:
                        total_disk = float(total_disk_str.replace('M', '')) / 1024
                    else:
                        total_disk = 0
                else:
                    total_disk = 0
                
                # Calculate free resources (simplified - actual would need more complex logic)
                free_cpu = max(0, total_cpu - (total_cpu_allocated // total_nodes))  # Approximate
                free_disk = max(0, total_disk - (total_disk_allocated // total_nodes))  # Approximate
                
                # Update totals
                total_node_ram_allocated += total_ram_gb - free_ram_gb
                total_node_cpu_allocated += total_cpu - free_cpu
                total_node_disk_allocated += total_disk - free_disk
                total_node_ram_free += free_ram_gb
                total_node_cpu_free += free_cpu
                total_node_disk_free += free_disk
                
            except Exception as e:
                logger.error(f"Error getting local node resources: {e}")
                status = "⚠️ Unknown"
                total_node_ram_free = 0
                total_node_cpu_free = 0
                total_node_disk_free = 0
        else:
            # Check remote node status
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
                if response.status_code == 200:
                    status = "🟢 Online"
                    running_nodes += 1
                else:
                    status = "🔴 Offline"
                    stopped_nodes += 1
            except:
                status = "🔴 Offline"
                stopped_nodes += 1
        
        # Get current VPS count on this node
        node_vps_count = get_current_vps_count(node['id'])
        capacity = node['total_vps']
        usage_percentage = (node_vps_count / capacity * 100) if capacity > 0 else 0
        
        node_statuses.append(
            f"**{node['name']}** ({node_type})\n"
            f"📍 {node['location']} • 📊 {node_vps_count}/{capacity} VPS ({usage_percentage:.0f}%)\n"
            f"Status: {status}"
        )
    
    # Calculate response time
    response_time = (time.time() - start_time) * 1000
    
    # Create main embed
    embed = create_embed(
        title="📊 System Status Dashboard",
        description=f"**{BOT_NAME}** - Complete System Overview\n*Generated in {response_time:.0f}ms*",
        color=0x1a1a1a
    )
    
    # Bot & Uptime Section
    add_field(embed, "🤖 Bot Status", 
        f"**Uptime:** {bot_uptime}\n"
        f"**Latency:** {round(bot.latency * 1000)}ms\n"
        f"**Version:** {BOT_VERSION}\n"
        f"**Developer:** {BOT_DEVELOPER}", 
        True)
    
    # Nodes Section
    add_field(embed, "🌐 Nodes Overview",
        f"**Total Nodes:** {total_nodes}\n"
        f"**Running:** {running_nodes} 🟢\n"
        f"**Stopped:** {stopped_nodes} 🔴\n"
        f"**Local/Remote:** {local_nodes}/{remote_nodes}",
        True)
    
    # VPS & Users Section
    add_field(embed, "👥 Users & VPS",
        f"**Total Users:** {total_users}\n"
        f"**Total VPS:** {total_vps}\n"
        f"**Running:** {running_vps} 🟢\n"
        f"**Stopped:** {stopped_vps} 🔴\n"
        f"**Suspended:** {suspended_vps} 🟡\n"
        f"**Whitelisted:** {whitelisted_vps} ✅",
        True)
    
    # Resources Section - Allocated vs Free
    add_field(embed, "💾 Resource Allocation",
        f"**RAM Allocated:** {total_ram_allocated} GB\n"
        f"**RAM Free:** {total_node_ram_free:.1f} GB\n"
        f"**CPU Allocated:** {total_cpu_allocated} Cores\n"
        f"**CPU Free:** {total_node_cpu_free:.1f} Cores\n"
        f"**Disk Allocated:** {total_disk_allocated} GB\n"
        f"**Disk Free:** {total_node_disk_free:.1f} GB",
        True)
    
    # System & Admin Section
    add_field(embed, "⚙️ System Information",
        f"**Total Admins:** {total_admins}\n"
        f"**Main Admin:** <@{MAIN_ADMIN_ID}>\n"
        f"**Ports Allocated:** {total_ports_allocated}\n"
        f"**Ports In Use:** {total_ports_used}\n"
        f"**Ports Available:** {total_ports_allocated - total_ports_used}",
        True)
    
    # Node Details Section (if any nodes exist)
    if node_statuses:
        # Split node statuses into chunks if too long
        node_text = "\n\n".join(node_statuses)
        chunks = [node_text[i:i+1024] for i in range(0, len(node_text), 1024)]
        
        for idx, chunk in enumerate(chunks, 1):
            title = "📡 Node Details" if idx == 1 else f"📡 Node Details (Part {idx})"
            add_field(embed, title, chunk, False)
    
    # System Health Indicator
    health_status = "✅ Excellent"
    health_color = 0x00ff88
    
    if running_nodes == 0:
        health_status = "🔴 Critical - No nodes running"
        health_color = 0xff3366
    elif stopped_nodes > 0:
        health_status = "🟡 Warning - Some nodes offline"
        health_color = 0xffaa00
    elif total_vps == 0:
        health_status = "ℹ️ No VPS deployed"
        health_color = 0x00ccff
    
    add_field(embed, "🏥 System Health", health_status, False)
    
    # Footer with current time
    embed.set_footer(text=f"{BOT_NAME} System Status • Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    icon_url="https://i.imgur.com/dpatuSj.png")
    
    await ctx.send(embed=embed)


@bot.command(name='status-summary')
@is_admin()
async def status_summary(ctx):
    """
    Quick summary of system status
    """
    # Get quick stats
    nodes = get_nodes()
    total_nodes = len(nodes)
    running_nodes = 0
    
    for node in nodes:
        if node['is_local']:
            running_nodes += 1
        else:
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=3)
                if response.status_code == 200:
                    running_nodes += 1
            except:
                pass
    
    total_vps = sum(len(vps_list) for vps_list in vps_data.values())
    total_users = len(vps_data)
    
    # Count VPS status
    running_vps = 0
    stopped_vps = 0
    suspended_vps = 0
    
    for vps_list in vps_data.values():
        for vps in vps_list:
            if vps.get('suspended', False):
                suspended_vps += 1
            elif vps.get('status') == 'running':
                running_vps += 1
            else:
                stopped_vps += 1
    
    embed = create_success_embed(
        "📈 Quick Status Summary",
        f"**Nodes:** {running_nodes}/{total_nodes} 🟢\n"
        f"**VPS:** {total_vps} total\n"
        f"• Running: {running_vps} 🟢\n"
        f"• Stopped: {stopped_vps} 🔴\n"
        f"• Suspended: {suspended_vps} 🟡\n"
        f"**Users:** {total_users} 👥\n"
        f"**Bot Latency:** {round(bot.latency * 1000)}ms"
    )
    
    embed.set_footer(text=f"Use '{PREFIX}status' for detailed information")
    await ctx.send(embed=embed)

@bot.command(name='admin-add')
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id == str(MAIN_ADMIN_ID):
        await ctx.send(embed=create_error_embed("Already Admin", "This user is already the main admin!"))
        return
    if user_id in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Already Admin", f"{user.mention} is already an admin!"))
        return
    admin_data["admins"].append(user_id)
    save_admin_data()
    await ctx.send(embed=create_success_embed("Admin Added", f"{user.mention} is now an admin!"))
    try:
        await user.send(embed=create_embed("🎉 Admin Role Granted", f"You are now an admin by {ctx.author.mention}", 0x00ff88))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {user.mention}"))

@bot.command(name='admin-remove')
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    user_id = str(user.id)
    if user_id == str(MAIN_ADMIN_ID):
        await ctx.send(embed=create_error_embed("Cannot Remove", "You cannot remove the main admin!"))
        return
    if user_id not in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Not Admin", f"{user.mention} is not an admin!"))
        return
    admin_data["admins"].remove(user_id)
    save_admin_data()
    await ctx.send(embed=create_success_embed("Admin Removed", f"{user.mention} is no longer an admin!"))
    try:
        await user.send(embed=create_embed("⚠️ Admin Role Revoked", f"Your admin role was removed by {ctx.author.mention}", 0xff3366))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {user.mention}"))

@bot.command(name='admin-list')
@is_main_admin()
async def admin_list(ctx):
    admins = admin_data.get("admins", [])
    main_admin = await bot.fetch_user(MAIN_ADMIN_ID)
    embed = create_embed("👑 Admin Team", "Current administrators:", 0x1a1a1a)
    add_field(embed, "🔰 Main Admin", f"{main_admin.mention} (ID: {MAIN_ADMIN_ID})", False)
    if admins:
        admin_list = []
        for admin_id in admins:
            try:
                admin_user = await bot.fetch_user(int(admin_id))
                admin_list.append(f"• {admin_user.mention} (ID: {admin_id})")
            except:
                admin_list.append(f"• Unknown User (ID: {admin_id})")
        admin_text = "\n".join(admin_list)
        add_field(embed, "🛡️ Admins", admin_text, False)
    else:
        add_field(embed, "🛡️ Admins", "No additional admins", False)
    await ctx.send(embed=embed)

@bot.command(name="userinfo")
@is_admin()
async def user_info(ctx, user: discord.Member):
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])

    # ─── Embed ─────────────────────────────────────────────────
    embed = create_embed(
        title="👤 User Dashboard",
        description=f"Statistics & resources for {user.mention}",
        color=0x1A1A1A
    )

    # ─── Row 1 : User Info ─────────────────────────────────────
    embed.add_field(
        name="👤 User",
        value=(
            f"**Name:** `{user.name}`\n"
            f"**ID:** `{user.id}`\n"
            f"**Joined:** `{user.joined_at.strftime('%Y-%m-%d') if user.joined_at else 'Unknown'}`"
        ),
        inline=True
    )

    is_admin_user = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    embed.add_field(
        name="🛡️ Admin",
        value="✅ Yes" if is_admin_user else "❌ No",
        inline=True
    )

    embed.add_field(
        name="🖥️ VPS Count",
        value=f"`{len(vps_list)}` VPS",
        inline=True
    )

    # ─── If VPS Exists ─────────────────────────────────────────
    if vps_list:
        total_ram = total_cpu = total_storage = 0
        running = suspended = whitelisted = 0

        vps_lines = []

        for i, vps in enumerate(vps_list, start=1):
            node = get_node(vps.get("node_id"))
            node_name = node["name"] if node else "Unknown"

            ram = int(vps.get("ram", "0GB").replace("GB", ""))
            storage = int(vps.get("storage", "0GB").replace("GB", ""))
            cpu = int(vps.get("cpu", 0))

            total_ram += ram
            total_storage += storage
            total_cpu += cpu

            if vps.get("suspended"):
                status = "⛔ SUSPENDED"
                suspended += 1
            elif vps.get("status") == "running":
                status = "🟢 RUNNING"
                running += 1
            else:
                status = "🔴 STOPPED"

            if vps.get("whitelisted"):
                whitelisted += 1

            vps_lines.append(
                f"**{i}.** `{vps['container_name']}`\n"
                f"{status} | `{ram}GB` RAM • `{cpu}` CPU • `{storage}GB` Disk\n"
                f"📍 Node: `{node_name}`"
            )

        # ─── Row 2 : VPS Summary ────────────────────────────────
        embed.add_field(
            name="📊 VPS Summary",
            value=(
                f"🖥️ `{len(vps_list)}` Total\n"
                f"🟢 `{running}` Running\n"
                f"⛔ `{suspended}` Suspended\n"
                f"✅ `{whitelisted}` Whitelisted"
            ),
            inline=True
        )

        embed.add_field(
            name="📈 Resources",
            value=(
                f"**RAM:** `{total_ram} GB`\n"
                f"**CPU:** `{total_cpu} Cores`\n"
                f"**Disk:** `{total_storage} GB`"
            ),
            inline=True
        )

        port_quota = get_user_allocation(user_id)
        port_used = get_user_used_ports(user_id)

        embed.add_field(
            name="🌐 Ports",
            value=f"`{port_used}/{port_quota}` Used",
            inline=True
        )

        # ─── VPS List (Split if needed) ────────────────────────
        vps_text = "\n\n".join(vps_lines)
        for i in range(0, len(vps_text), 1024):
            embed.add_field(
                name="📋 VPS List",
                value=vps_text[i:i + 1024],
                inline=False
            )

    else:
        embed.add_field(
            name="🖥️ VPS",
            value="❌ No VPS assigned",
            inline=False
        )

    embed.set_footer(text="INFINITY_CLOUD • User Resource Dashboard")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name="serverstats")
@is_admin()
async def server_stats(ctx):
    # ─── Counts ────────────────────────────────────────────────
    total_users = len(vps_data)
    total_admins = len(admin_data.get("admins", [])) + 1
    total_vps = sum(len(vps_list) for vps_list in vps_data.values())

    total_ram = total_cpu = total_storage = 0
    running_vps = suspended_vps = stopped_vps = 0
    whitelisted_vps = 0

    # ─── VPS Data ──────────────────────────────────────────────
    for vps_list in vps_data.values():
        for vps in vps_list:
            total_ram += int(vps.get("ram", "0GB").replace("GB", ""))
            total_storage += int(vps.get("storage", "0GB").replace("GB", ""))
            total_cpu += int(vps.get("cpu", 0))

            if vps.get("status") == "running":
                if vps.get("suspended", False):
                    suspended_vps += 1
                else:
                    running_vps += 1
            else:
                stopped_vps += 1

            if vps.get("whitelisted", False):
                whitelisted_vps += 1

    # ─── Ports ─────────────────────────────────────────────────
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT SUM(allocated_ports) FROM port_allocations")
    total_ports_allocated = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM port_forwards")
    total_ports_used = cur.fetchone()[0] or 0
    conn.close()

    # ─── Embed ─────────────────────────────────────────────────
    embed = create_embed(
        title="📊 Server Statistics",
        description="**Live Infrastructure Dashboard**",
        color=0x1A1A1A
    )

    # ── Row 1 ──────────────────────────────────────────────────
    embed.add_field(
        name="👥 Users",
        value=f"`{total_users}` Users\n`{total_admins}` Admins",
        inline=True
    )

    embed.add_field(
        name="🖥️ VPS",
        value=(
            f"Total: `{total_vps}`\n"
            f"🟢 `{running_vps}` Running\n"
            f"⛔ `{suspended_vps}` Suspended"
        ),
        inline=True
    )

    embed.add_field(
        name="📌 Status",
        value=(
            f"🔴 `{stopped_vps}` Stopped\n"
            f"✅ `{whitelisted_vps}` Whitelisted"
        ),
        inline=True
    )

    # ── Row 2 ──────────────────────────────────────────────────
    embed.add_field(
        name="📈 RAM",
        value=f"`{total_ram} GB`",
        inline=True
    )

    embed.add_field(
        name="⚙️ CPU",
        value=f"`{total_cpu} Cores`",
        inline=True
    )

    embed.add_field(
        name="💾 Storage",
        value=f"`{total_storage} GB`",
        inline=True
    )

    # ── Row 3 ──────────────────────────────────────────────────
    embed.add_field(
        name="🌐 Ports Allocated",
        value=f"`{total_ports_allocated}`",
        inline=True
    )

    embed.add_field(
        name="🔌 Ports In Use",
        value=f"`{total_ports_used}`",
        inline=True
    )

    embed.add_field(
        name="📊 Utilization",
        value=(
            f"`{total_ports_used}/{total_ports_allocated}`"
            if total_ports_allocated else "`N/A`"
        ),
        inline=True
    )

    embed.set_footer(text="INFINITY_CLOUD • Real-Time Monitoring")
    embed.timestamp = ctx.message.created_at

    await ctx.send(embed=embed)

@bot.command(name='vpsinfo')
@is_admin()
async def vps_info(ctx, container_name: str = None):
    if not container_name:
        all_vps = []
        for user_id, vps_list in vps_data.items():
            try:
                user = await bot.fetch_user(int(user_id))
                for i, vps in enumerate(vps_list):
                    node = get_node(vps['node_id'])
                    node_name = node['name'] if node else "Unknown"
                    status_text = vps.get('status', 'unknown').upper()
                    if vps.get('suspended', False):
                        status_text += " (SUSPENDED)"
                    if vps.get('whitelisted', False):
                        status_text += " (WHITELISTED)"
                    all_vps.append(f"**{user.name}** - VPS {i+1}: `{vps['container_name']}` - {status_text} (Node: {node_name})")
            except:
                pass
        vps_text = "\n".join(all_vps)
        chunks = [vps_text[i:i+1024] for i in range(0, len(vps_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"🖥️ All VPS (Part {idx})", f"List of all VPS deployments", 0x1a1a1a)
            add_field(embed, "VPS List", chunk, False)
            await ctx.send(embed=embed)
    else:
        found_vps = None
        found_user = None
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    found_vps = vps
                    found_user = await bot.fetch_user(int(user_id))
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
            return
        node = get_node(found_vps['node_id'])
        node_name = node['name'] if node else "Unknown"
        suspended_text = " (SUSPENDED)" if found_vps.get('suspended', False) else ""
        whitelisted_text = " (WHITELISTED)" if found_vps.get('whitelisted', False) else ""
        embed = create_embed(f"🖥️ VPS Information - {container_name}", f"Details for VPS owned by {found_user.mention}{suspended_text}{whitelisted_text} on node {node_name}", 0x1a1a1a)
        add_field(embed, "👤 Owner", f"**Name:** {found_user.name}\n**ID:** {found_user.id}", False)
        add_field(embed, "📊 Specifications", f"**RAM:** {found_vps['ram']}\n**CPU:** {found_vps['cpu']} Cores\n**Storage:** {found_vps['storage']}", False)
        add_field(embed, "📈 Status", f"**Current:** {found_vps.get('status', 'unknown').upper()}{suspended_text}{whitelisted_text}\n**Suspended:** {found_vps.get('suspended', False)}\n**Whitelisted:** {found_vps.get('whitelisted', False)}\n**Created:** {found_vps.get('created_at', 'Unknown')}", False)
        if 'config' in found_vps:
            add_field(embed, "⚙️ Configuration", f"**Config:** {found_vps['config']}", False)
        if found_vps.get('shared_with'):
            shared_users = []
            for shared_id in found_vps['shared_with']:
                try:
                    shared_user = await bot.fetch_user(int(shared_id))
                    shared_users.append(f"• {shared_user.mention}")
                except:
                    shared_users.append(f"• Unknown User ({shared_id})")
            shared_text = "\n".join(shared_users)
            add_field(embed, "🔗 Shared With", shared_text, False)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM port_forwards WHERE vps_container = ?', (container_name,))
        port_count = cur.fetchone()[0]
        conn.close()
        add_field(embed, "🌐 Active Ports", f"{port_count} forwarded ports (TCP/UDP)", False)
        await ctx.send(embed=embed)

@bot.command(name='restart-vps')
@is_admin()
async def restart_vps(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Restarting VPS", f"Restarting VPS `{container_name}`..."))
    try:
        await execute_lxc(container_name, f"restart {container_name}", node_id=node_id)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['status'] = 'running'
                    save_vps_data()
                    break
        await apply_internal_permissions(container_name, node_id)
        await recreate_port_forwards(container_name)
        await ctx.send(embed=create_success_embed("VPS Restarted", f"VPS `{container_name}` has been restarted successfully!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", f"Error: {str(e)}"))

@bot.command(name='exec')
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Executing Command", f"Running command in VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- bash -c \"{command}\"", node_id=node_id)
        embed = create_embed(f"Command Output - {container_name}", f"Command: `{command}`", 0x1a1a1a)
        if output.strip():
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            add_field(embed, "📤 Output", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", f"Error: {str(e)}"))

@bot.command(name='stop-vps-all')
@is_admin()
async def stop_all_vps(ctx):
    embed = create_warning_embed("Stopping All VPS", "⚠️ **WARNING:** This will stop ALL running VPS on all nodes.\n\nThis action cannot be undone. Continue?")
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.defer()
            try:
                stopped_count = 0
                nodes = get_nodes()
                for node in nodes:
                    if node['is_local']:
                        proc = await asyncio.create_subprocess_exec(
                            "lxc", "stop", "--all", "--force",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        stdout, stderr = await proc.communicate()
                        if proc.returncode != 0:
                            logger.error(f"Failed to stop all on local node: {stderr.decode()}")
                            continue
                    else:
                        url = f"{node['url']}/api/execute"
                        data = {"command": "lxc stop --all --force"}
                        params = {"api_key": node["api_key"]}
                        response = requests.post(url, json=data, params=params)
                        if response.status_code != 200:
                            logger.error(f"Failed to stop all on node {node['name']}")
                            continue
                    for user_id, vps_list in vps_data.items():
                        for vps in vps_list:
                            if vps.get('node_id') == node['id'] and vps.get('status') == 'running':
                                vps['status'] = 'stopped'
                                vps['suspended'] = False
                                stopped_count += 1
                save_vps_data()
                embed = create_success_embed("All VPS Stopped", f"Successfully stopped {stopped_count} VPS across all nodes.")
                await interaction.followup.send(embed=embed)
            except Exception as e:
                embed = create_error_embed("Error", f"Error stopping VPS: {str(e)}")
                await interaction.followup.send(embed=embed)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, item: discord.ui.Button):
            await interaction.response.edit_message(embed=create_info_embed("Operation Cancelled", "The stop all VPS operation has been cancelled."))

    await ctx.send(embed=embed, view=ConfirmView())

@bot.command(name='cpu-monitor')
@is_admin()
async def resource_monitor_control(ctx, action: str = "status"):
    global resource_monitor_active
    if action.lower() == "status":
        status = "Active" if resource_monitor_active else "Inactive"
        embed = create_embed("Resource Monitor Status", f"Resource monitoring is currently **{status}** (logs only; no auto-stop)", 0x00ccff if resource_monitor_active else 0xffaa00)
        add_field(embed, "Thresholds", f"{CPU_THRESHOLD}% CPU / {RAM_THRESHOLD}% RAM usage", True)
        add_field(embed, "Check Interval", f"60 seconds (all nodes)", True)
        await ctx.send(embed=embed)
    elif action.lower() == "enable":
        resource_monitor_active = True
        await ctx.send(embed=create_success_embed("Resource Monitor Enabled", "Resource monitoring has been enabled."))
    elif action.lower() == "disable":
        resource_monitor_active = False
        await ctx.send(embed=create_warning_embed("Resource Monitor Disabled", "Resource monitoring has been disabled."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}cpu-monitor <status|enable|disable>`"))

@bot.command(name='resize-vps')
@is_admin()
async def resize_vps(ctx, container_name: str, ram: int = None, cpu: int = None, disk: int = None):
    if ram is None and cpu is None and disk is None:
        await ctx.send(embed=create_error_embed("Missing Parameters", "Please specify at least one resource to resize (ram, cpu, or disk)"))
        return
    found_vps = None
    user_id = None
    vps_index = None
    for uid, vps_list in vps_data.items():
        for i, vps in enumerate(vps_list):
            if vps['container_name'] == container_name:
                found_vps = vps
                user_id = uid
                vps_index = i
                break
        if found_vps:
            break
    if not found_vps:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
        return
    node_id = found_vps['node_id']
    was_running = found_vps.get('status') == 'running' and not found_vps.get('suspended', False)
    disk_changed = disk is not None
    if was_running:
        await ctx.send(embed=create_info_embed("Stopping VPS", f"Stopping VPS `{container_name}` to apply resource changes..."))
        try:
            await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
            found_vps['status'] = 'stopped'
            save_vps_data()
        except Exception as e:
            await ctx.send(embed=create_error_embed("Stop Failed", f"Error stopping VPS: {str(e)}"))
            return
    changes = []
    try:
        new_ram = int(found_vps['ram'].replace('GB', ''))
        new_cpu = int(found_vps['cpu'])
        new_disk = int(found_vps['storage'].replace('GB', ''))
        if ram is not None and ram > 0:
            new_ram = ram
            ram_mb = ram * 1024
            await execute_lxc(container_name, f"config set {container_name} limits.memory {ram_mb}MB", node_id=node_id)
            changes.append(f"RAM: {ram}GB")
        if cpu is not None and cpu > 0:
            new_cpu = cpu
            await execute_lxc(container_name, f"config set {container_name} limits.cpu {cpu}", node_id=node_id)
            changes.append(f"CPU: {cpu} cores")
        if disk is not None and disk > 0:
            new_disk = disk
            await execute_lxc(container_name, f"config device set {container_name} root size={disk}GB", node_id=node_id)
            changes.append(f"Disk: {disk}GB")
        found_vps['ram'] = f"{new_ram}GB"
        found_vps['cpu'] = str(new_cpu)
        found_vps['storage'] = f"{new_disk}GB"
        found_vps['config'] = f"{new_ram}GB RAM / {new_cpu} CPU / {new_disk}GB Disk"
        vps_data[user_id][vps_index] = found_vps
        save_vps_data()
        if was_running:
            await execute_lxc(container_name, f"start {container_name}", node_id=node_id)
            found_vps['status'] = 'running'
            save_vps_data()
            await apply_internal_permissions(container_name, node_id)
            await recreate_port_forwards(container_name)
        embed = create_success_embed("VPS Resized", f"Successfully resized resources for VPS `{container_name}`")
        add_field(embed, "Changes Applied", "\n".join(changes), False)
        if disk_changed:
            add_field(embed, "Disk Note", "Run `sudo resize2fs /` inside the VPS to expand the filesystem.", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Resize Failed", f"Error: {str(e)}"))

@bot.command(name='clone-vps')
@is_admin()
async def clone_vps(ctx, container_name: str, new_name: str = None):
    if not new_name:
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        new_name = f"{LXC_PREFIX}-{container_name}-clone-{timestamp}"
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Cloning VPS", f"Cloning VPS `{container_name}` to `{new_name}`..."))
    try:
        found_vps = None
        user_id = None
        for uid, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    found_vps = vps
                    user_id = uid
                    break
            if found_vps:
                break
        if not found_vps:
            await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS found with container name: `{container_name}`"))
            return
        await execute_lxc(container_name, f"copy {container_name} {new_name}", node_id=node_id)
        await apply_lxc_config(new_name, node_id)
        await execute_lxc(new_name, f"start {new_name}", node_id=node_id)
        await apply_internal_permissions(new_name, node_id)
        await recreate_port_forwards(new_name)
        if user_id not in vps_data:
            vps_data[user_id] = []
        new_vps = found_vps.copy()
        new_vps['container_name'] = new_name
        new_vps['status'] = 'running'
        new_vps['suspended'] = False
        new_vps['whitelisted'] = False
        new_vps['suspension_history'] = []
        new_vps['created_at'] = datetime.now().isoformat()
        new_vps['shared_with'] = []
        new_vps['id'] = None
        vps_data[user_id].append(new_vps)
        save_vps_data()
        embed = create_success_embed("VPS Cloned", f"Successfully cloned VPS `{container_name}` to `{new_name}`")
        add_field(embed, "New VPS Details", f"**RAM:** {new_vps['ram']}\n**CPU:** {new_vps['cpu']} Cores\n**Storage:** {new_vps['storage']}", False)
        add_field(embed, "Features", "Nesting, Privileged, FUSE, Kernel Modules (Docker Ready), Unprivileged Ports from 0", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Clone Failed", f"Error: {str(e)}"))

@bot.command(name='migrate-vps')
@is_admin()
async def migrate_vps(ctx, container_name: str, target_node_id: int):
    node_id = find_node_id_for_container(container_name)
    target_node = get_node(target_node_id)
    if not target_node:
        await ctx.send(embed=create_error_embed("Invalid Node", "Target node not found."))
        return
    await ctx.send(embed=create_info_embed("Migrating VPS", f"Migrating VPS `{container_name}` to node {target_node['name']}..."))
    try:
        await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
        temp_name = f"{LXC_PREFIX}-{container_name}-temp-{int(time.time())}"
        await execute_lxc(container_name, f"copy {container_name} {temp_name} -s {DEFAULT_STORAGE_POOL}", node_id=target_node_id)
        await execute_lxc(container_name, f"delete {container_name} --force", node_id=node_id)
        await execute_lxc(temp_name, f"rename {temp_name} {container_name}", node_id=target_node_id)
        await apply_lxc_config(container_name, target_node_id)
        await execute_lxc(container_name, f"start {container_name}", node_id=target_node_id)
        await apply_internal_permissions(container_name, target_node_id)
        await recreate_port_forwards(container_name)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['node_id'] = target_node_id
                    vps['status'] = 'running'
                    vps['suspended'] = False
                    save_vps_data()
                    break
        await ctx.send(embed=create_success_embed("VPS Migrated", f"Successfully migrated VPS `{container_name}` to node {target_node['name']}"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Migration Failed", f"Error: {str(e)}"))

@bot.command(name='vps-stats')
@is_admin()
async def vps_stats(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Statistics", f"Collecting statistics for VPS `{container_name}`..."))
    try:
        stats = await get_container_stats(container_name, node_id)
        embed = create_embed(f"📊 VPS Statistics - {container_name}", f"Resource usage statistics", 0x1a1a1a)
        add_field(embed, "📈 Status", f"**{stats['status'].upper()}**", False)
        add_field(embed, "💻 CPU Usage", f"**{stats['cpu']:.1f}%**", True)
        add_field(embed, "🧠 Memory Usage", f"**{stats['ram']['used']}/{stats['ram']['total']} MB ({stats['ram']['pct']:.1f}%)**", True)
        add_field(embed, "💾 Disk Usage", f"**{stats['disk']}**", True)
        add_field(embed, "⏱️ Uptime", f"**{stats['uptime']}**", True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Statistics Failed", f"Error: {str(e)}"))


@bot.command(name='node-check')
@is_admin()
async def node_check(ctx, node_id: int):
    """Check node status and available storage pools"""
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"Node ID {node_id} not found."))
        return
    
    embed = create_info_embed(f"Node Check - {node['name']}", 
                             f"Checking status and configuration of node {node['name']}...")
    
    # Check if node is reachable
    status = await get_node_status(node_id)
    add_field(embed, "📡 Connection Status", status, False)
    
    if status.startswith("🟢"):
        # Try to get storage pools
        try:
            pools_output = await execute_lxc("", "storage list", node_id=node_id, timeout=30)
            add_field(embed, "💾 Available Storage Pools", f"```{pools_output}```", False)
            
            # Try to get default profile
            try:
                profile_output = await execute_lxc("", "profile list", node_id=node_id, timeout=30)
                add_field(embed, "📋 Available Profiles", f"```{profile_output[:500]}...```", False)
            except Exception as e:
                add_field(embed, "📋 Profiles", f"Error: {str(e)[:200]}", False)
                
        except Exception as e:
            add_field(embed, "💾 Storage Pools", f"Error: {str(e)[:200]}", False)
        
        # Check remote API endpoint
        try:
            test_response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
            add_field(embed, "🔌 API Endpoint", f"✅ Reachable\nURL: {node['url']}", False)
        except Exception as e:
            add_field(embed, "🔌 API Endpoint", f"❌ Unreachable\nError: {str(e)[:200]}", False)
    else:
        add_field(embed, "⚠️ Status", "Node is offline or unreachable", False)
    
    await ctx.send(embed=embed)

@bot.command(name='vps-network')
@is_admin()
async def vps_network(ctx, container_name: str, action: str, value: str = None):
    node_id = find_node_id_for_container(container_name)
    if action.lower() not in ["list", "add", "remove", "limit"]:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}vps-network <container> <list|add|remove|limit> [value]`"))
        return
    try:
        if action.lower() == "list":
            output = await execute_lxc(container_name, f"exec {container_name} -- ip addr", node_id=node_id)
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            embed = create_embed(f"🌐 Network Interfaces - {container_name}", "Network configuration", 0x1a1a1a)
            add_field(embed, "Interfaces", f"```\n{output}\n```", False)
            await ctx.send(embed=embed)
        elif action.lower() == "limit" and value:
            await execute_lxc(container_name, f"config device set {container_name} eth0 limits.egress {value}", node_id=node_id)
            await execute_lxc(container_name, f"config device set {container_name} eth0 limits.ingress {value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Limited", f"Set network limit to {value} for `{container_name}`"))
        elif action.lower() == "add" and value:
            await execute_lxc(container_name, f"config device add {container_name} eth1 nic nictype=bridged parent={value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Added", f"Added network interface to VPS `{container_name}` with bridge `{value}`"))
        elif action.lower() == "remove" and value:
            await execute_lxc(container_name, f"config device remove {container_name} {value}", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Removed", f"Removed network interface `{value}` from VPS `{container_name}`"))
        else:
            await ctx.send(embed=create_error_embed("Invalid Parameters", "Please provide valid parameters for the action"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Network Management Failed", f"Error: {str(e)}"))

@bot.command(name='vps-processes')
@is_admin()
async def vps_processes(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Processes", f"Listing processes in VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- ps aux", node_id=node_id)
        if len(output) > 1000:
            output = output[:1000] + "\n... (truncated)"
        embed = create_embed(f"⚙️ Processes - {container_name}", "Running processes", 0x1a1a1a)
        add_field(embed, "Process List", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Process Listing Failed", f"Error: {str(e)}"))

@bot.command(name='vps-logs')
@is_admin()
async def vps_logs(ctx, container_name: str, lines: int = 50):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Logs", f"Fetching last {lines} lines from VPS `{container_name}`..."))
    try:
        output = await execute_lxc(container_name, f"exec {container_name} -- journalctl -n {lines}", node_id=node_id)
        if len(output) > 1000:
            output = output[:1000] + "\n... (truncated)"
        embed = create_embed(f"📋 Logs - {container_name}", f"Last {lines} log lines", 0x1a1a1a)
        add_field(embed, "System Logs", f"```\n{output}\n```", False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Log Retrieval Failed", f"Error: {str(e)}"))

@bot.command(name='vps-uptime')
@is_admin()
async def vps_uptime(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    uptime = await get_container_uptime(container_name, node_id)
    embed = create_info_embed("VPS Uptime", f"Uptime for `{container_name}`: {uptime}")
    await ctx.send(embed=embed)

@bot.command(name='suspend-vps')
@is_admin()
async def suspend_vps(ctx, container_name: str, *, reason: str = "Admin action"):
    node_id = find_node_id_for_container(container_name)
    found = False
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if vps.get('status') != 'running':
                    await ctx.send(embed=create_error_embed("Cannot Suspend", "VPS must be running to suspend."))
                    return
                try:
                    await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
                    vps['status'] = 'stopped'
                    vps['suspended'] = True
                    if 'suspension_history' not in vps:
                        vps['suspension_history'] = []
                    vps['suspension_history'].append({
                        'time': datetime.now().isoformat(),
                        'reason': reason,
                        'by': f"{ctx.author.name} ({ctx.author.id})"
                    })
                    save_vps_data()
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Suspend Failed", str(e)))
                    return
                try:
                    owner = await bot.fetch_user(int(uid))
                    embed = create_warning_embed("🚨 VPS Suspended", f"Your VPS `{container_name}` has been suspended by an admin.\n\n**Reason:** {reason}\n\nContact an admin to unsuspend.")
                    await owner.send(embed=embed)
                except Exception as dm_e:
                    logger.error(f"Failed to DM owner {uid}: {dm_e}")
                await ctx.send(embed=create_success_embed("VPS Suspended", f"VPS `{container_name}` suspended. Reason: {reason}"))
                found = True
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='unsuspend-vps')
@is_admin()
async def unsuspend_vps(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    found = False
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if not vps.get('suspended', False):
                    await ctx.send(embed=create_error_embed("Not Suspended", "VPS is not suspended."))
                    return
                try:
                    vps['suspended'] = False
                    vps['status'] = 'running'
                    await execute_lxc(container_name, f"start {container_name}", node_id=node_id)
                    await apply_internal_permissions(container_name, node_id)
                    await recreate_port_forwards(container_name)
                    save_vps_data()
                    await ctx.send(embed=create_success_embed("VPS Unsuspended", f"VPS `{container_name}` unsuspended and started."))
                    found = True
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Start Failed", str(e)))
                try:
                    owner = await bot.fetch_user(int(uid))
                    embed = create_success_embed("🟢 VPS Unsuspended", f"Your VPS `{container_name}` has been unsuspended by an admin.\nYou can now manage it again.")
                    await owner.send(embed=embed)
                except Exception as dm_e:
                    logger.error(f"Failed to DM owner {uid} about unsuspension: {dm_e}")
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='suspension-logs')
@is_admin()
async def suspension_logs(ctx, container_name: str = None):
    if container_name:
        found = None
        for lst in vps_data.values():
            for vps in lst:
                if vps['container_name'] == container_name:
                    found = vps
                    break
            if found:
                break
        if not found:
            await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))
            return
        history = found.get('suspension_history', [])
        if not history:
            await ctx.send(embed=create_info_embed("No Suspensions", f"No suspension history for `{container_name}`."))
            return
        embed = create_embed("Suspension History", f"For `{container_name}`")
        text = []
        for h in sorted(history, key=lambda x: x['time'], reverse=True)[:10]:
            t = datetime.fromisoformat(h['time']).strftime('%Y-%m-%d %H:%M:%S')
            text.append(f"**{t}** - {h['reason']} (by {h['by']})")
        add_field(embed, "History", "\n".join(text), False)
        if len(history) > 10:
            add_field(embed, "Note", "Showing last 10 entries.")
        await ctx.send(embed=embed)
    else:
        all_logs = []
        for uid, lst in vps_data.items():
            for vps in lst:
                h = vps.get('suspension_history', [])
                for event in sorted(h, key=lambda x: x['time'], reverse=True):
                    t = datetime.fromisoformat(event['time']).strftime('%Y-%m-%d %H:%M')
                    all_logs.append(f"**{t}** - VPS `{vps['container_name']}` (Owner: <@{uid}>) - {event['reason']} (by {event['by']})")
        if not all_logs:
            await ctx.send(embed=create_info_embed("No Suspensions", "No suspension events recorded."))
            return
        logs_text = "\n".join(all_logs)
        chunks = [logs_text[i:i+1024] for i in range(0, len(logs_text), 1024)]
        for idx, chunk in enumerate(chunks, 1):
            embed = create_embed(f"Suspension Logs (Part {idx})", f"Global suspension events (newest first)")
            add_field(embed, "Events", chunk, False)
            await ctx.send(embed=embed)

@bot.command(name='apply-permissions')
@is_admin()
async def apply_permissions(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Applying Permissions", f"Applying advanced permissions to `{container_name}`..."))
    try:
        status = await get_container_status(container_name, node_id)
        was_running = status == 'running'
        if was_running:
            await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
        await apply_lxc_config(container_name, node_id)
        await execute_lxc(container_name, f"start {container_name}", node_id=node_id)
        await apply_internal_permissions(container_name, node_id)
        await recreate_port_forwards(container_name)
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                if vps['container_name'] == container_name:
                    vps['status'] = 'running'
                    vps['suspended'] = False
                    save_vps_data()
                    break
        await ctx.send(embed=create_success_embed("Permissions Applied", f"Advanced permissions applied to VPS `{container_name}`. Docker-ready with unprivileged ports!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Apply Failed", f"Error: {str(e)}"))

@bot.command(name='resource-check')
@is_admin()
async def resource_check(ctx):
    suspended_count = 0
    embed = create_info_embed("Resource Check", "Checking all running VPS for high resource usage...")
    msg = await ctx.send(embed=embed)
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps.get('status') == 'running' and not vps.get('suspended', False) and not vps.get('whitelisted', False):
                container = vps['container_name']
                node_id = vps['node_id']
                stats = await get_container_stats(container, node_id)
                cpu = stats['cpu']
                ram = stats['ram']['pct']
                if cpu > CPU_THRESHOLD or ram > RAM_THRESHOLD:
                    reason = f"High resource usage: CPU {cpu:.1f}%, RAM {ram:.1f}% (threshold: {CPU_THRESHOLD}% CPU / {RAM_THRESHOLD}% RAM)"
                    logger.warning(f"Suspending {container}: {reason}")
                    try:
                        await execute_lxc(container, f"stop {container}", node_id=node_id)
                        vps['status'] = 'stopped'
                        vps['suspended'] = True
                        if 'suspension_history' not in vps:
                            vps['suspension_history'] = []
                        vps['suspension_history'].append({
                            'time': datetime.now().isoformat(),
                            'reason': reason,
                            'by': 'Manual Resource Check'
                        })
                        save_vps_data()
                        try:
                            owner = await bot.fetch_user(int(user_id))
                            warn_embed = create_warning_embed("🚨 VPS Auto-Suspended", f"Your VPS `{container}` has been suspended due to high resource usage.\n\n**Reason:** {reason}\n\nContact admin to unsuspend and address the issue.")
                            await owner.send(embed=warn_embed)
                        except Exception as dm_e:
                            logger.error(f"Failed to DM owner {user_id}: {dm_e}")
                        suspended_count += 1
                    except Exception as e:
                        logger.error(f"Failed to suspend {container}: {e}")
    final_embed = create_info_embed("Resource Check Complete", f"Checked all VPS. Suspended {suspended_count} high-usage VPS.")
    await msg.edit(embed=final_embed)

@bot.command(name='whitelist-vps')
@is_admin()
async def whitelist_vps(ctx, container_name: str, action: str):
    if action.lower() not in ['add', 'remove']:
        await ctx.send(embed=create_error_embed("Invalid Action", f"Use: `{PREFIX}whitelist-vps <container> <add|remove>`"))
        return
    found = False
    for user_id, vps_list in vps_data.items():
        for vps in vps_list:
            if vps['container_name'] == container_name:
                if action.lower() == 'add':
                    vps['whitelisted'] = True
                    msg = "added to whitelist (exempt from auto-suspension)"
                else:
                    vps['whitelisted'] = False
                    msg = "removed from whitelist"
                save_vps_data()
                await ctx.send(embed=create_success_embed("Whitelist Updated", f"VPS `{container_name}` {msg}."))
                found = True
                break
        if found:
            break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"VPS `{container_name}` not found."))

@bot.command(name='snapshot')
@is_admin()
async def snapshot_vps(ctx, container_name: str, snap_name: str = "snap0"):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Creating Snapshot", f"Creating snapshot '{snap_name}' for `{container_name}`..."))
    try:
        await execute_lxc(container_name, f"snapshot {container_name} {snap_name}", node_id=node_id)
        await ctx.send(embed=create_success_embed("Snapshot Created", f"Snapshot '{snap_name}' created for VPS `{container_name}`."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Snapshot Failed", f"Error: {str(e)}"))

@bot.command(name='list-snapshots')
@is_admin()
async def list_snapshots(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    try:
        result = await execute_lxc(container_name, f"snapshot list {container_name}", node_id=node_id)
        embed = create_info_embed(f"Snapshots for {container_name}", result)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=create_error_embed("List Failed", f"Error: {str(e)}"))

@bot.command(name='restore-snapshot')
@is_admin()
async def restore_snapshot(ctx, container_name: str, snap_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_warning_embed("Restore Snapshot", f"Restoring snapshot '{snap_name}' for `{container_name}` will overwrite current state. Continue?"))
    class RestoreConfirm(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Confirm Restore", style=discord.ButtonStyle.danger)
        async def confirm(self, inter: discord.Interaction, item: discord.ui.Button):
            await inter.response.defer()
            try:
                await execute_lxc(container_name, f"stop {container_name}", node_id=node_id)
                await execute_lxc(container_name, f"restore {container_name} {snap_name}", node_id=node_id)
                await execute_lxc(container_name, f"start {container_name}", node_id=node_id)
                await apply_internal_permissions(container_name, node_id)
                await recreate_port_forwards(container_name)
                for uid, lst in vps_data.items():
                    for vps in lst:
                        if vps['container_name'] == container_name:
                            vps['status'] = 'running'
                            vps['suspended'] = False
                            save_vps_data()
                            break
                await inter.followup.send(embed=create_success_embed("Snapshot Restored", f"Restored '{snap_name}' for VPS `{container_name}`."))
            except Exception as e:
                await inter.followup.send(embed=create_error_embed("Restore Failed", f"Error: {str(e)}"))

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, item: discord.ui.Button):
            await inter.response.edit_message(embed=create_info_embed("Cancelled", "Snapshot restore cancelled."))

    await ctx.send(view=RestoreConfirm())

@bot.command(name='repair-ports')
@is_admin()
async def repair_ports(ctx, container_name: str):
    await ctx.send(embed=create_info_embed("Repairing Ports", f"Re-adding port forward devices for `{container_name}`..."))
    try:
        readded = await recreate_port_forwards(container_name)
        await ctx.send(embed=create_success_embed("Ports Repaired", f"Re-added {readded} port forwards for `{container_name}`."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Repair Failed", f"Error: {str(e)}"))

@bot.command(name='about')
async def about(ctx):
    """Display bot information with premium branding"""
    total_users = len(vps_data)
    total_vps = sum(len(vps_list) for vps_list in vps_data.values())
    latency = round(bot.latency * 1000)
    main_admin = await bot.fetch_user(MAIN_ADMIN_ID)
    
    # Premium branded embed
    embed = create_premium_embed(
        f"{BOT_NAME} VPS Manager",
        "Professional VPS management platform for Discord communities"
    )
    
    embed.set_thumbnail(url="https://media.discordapp.net/attachments/1527649627875184712/1534104160323109005/1784744593691.jpg?ex=6a72e956&is=6a7197d6&hm=b2f3c9b5789becf7b2ae04d1597f5a5bcd1b0920ac5b3951bc54775422096a6e&=&format=webp&width=640&height=640")
    
    # Bot information
    add_field(embed, "Platform", f"```{BOT_NAME}```", True)
    add_field(embed, "Version", f"```v{BOT_VERSION}```", True)
    add_field(embed, "Status", format_status_badge(True, "Online"), True)
    
    # Performance metrics
    if latency < 100:
        latency_status = "🟢 Excellent"
    elif latency < 200:
        latency_status = "🟡 Good"
    else:
        latency_status = "🔴 Poor"
    
    add_field(embed, "Latency", f"```{latency}ms``` {latency_status}", True)
    add_field(embed, "Uptime", f"```{get_uptime()}```", True)
    add_field(embed, "Server", f"```{YOUR_SERVER_IP}```", True)
    
    # Statistics
    stats_text = (
        f"{format_list_item(f'Total VPS: {total_vps}')} 🖥️\n"
        f"{format_list_item(f'Active Users: {total_users}')} 👥\n"
        f"{format_list_item(f'Commands: 100+')} ⚡"
    )
    add_field(embed, "Statistics", stats_text, False)
    
    # Team
    add_field(embed, "Owner", main_admin.mention, True)
    add_field(embed, "Developer", f"```{BOT_DEVELOPER}```", True)
    add_field(embed, "Support", f"`{PREFIX}help`", True)
    
    # Features highlight
    features_text = (
        f"{EmbedIcons.BULLET} Multi-node VPS management\n"
        f"{EmbedIcons.BULLET} Port forwarding & networking\n"
        f"{EmbedIcons.BULLET} Real-time monitoring\n"
        f"{EmbedIcons.BULLET} Premium UI/UX design"
    )
    add_field(embed, "Key Features", features_text, False)
    
    await ctx.send(embed=embed)


@bot.command(name='renew')
async def renew_vps_command(ctx, vps_number: int = None, days: int = 1):
    """Renew your VPS to extend its expiration date"""
    user_id = str(ctx.author.id)

    if vps_number is None:
        await ctx.send(embed=create_error_embed("Usage", 
            f"Usage: `{PREFIX}renew <vps_number> [days]`\n"
            f"Example: `{PREFIX}renew 1 7` (renew VPS #1 for 7 days)"))
        return

    if days < 1 or days > 365:
        await ctx.send(embed=create_error_embed("Invalid Duration", 
            "Days must be between 1 and 365"))
        return

    # Get user's VPS
    vps_list = vps_data.get(user_id, [])
    if vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", 
            f"You don't have VPS #{vps_number}. Use `{PREFIX}myvps` to see your VPS."))
        return

    vps = vps_list[vps_number - 1]

    # Confirm renewal
    embed = create_warning_embed("🔁 Confirm VPS Renewal", 
        f"**VPS:** `{vps['container_name']}`\n"
        f"**Duration:** {days} day(s)\n"
        f"**Cost:** Free\n\n"
        f"React with ✅ to confirm or ❌ to cancel")

    msg = await ctx.send(embed=embed)
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")

    def check(reaction, user):
        return user == ctx.author and str(reaction.emoji) in ["✅", "❌"] and reaction.message.id == msg.id

    try:
        reaction, user = await bot.wait_for('reaction_add', timeout=30.0, check=check)

        if str(reaction.emoji) == "❌":
            await msg.edit(embed=create_info_embed("Cancelled", "VPS renewal cancelled."))
            return

        # Renew VPS
        vps_id = vps.get('id')
        if vps_id:
            renew_vps(vps_id, days)

            # Update VPS data
            if vps.get('expires_at'):
                current_expiry = datetime.fromisoformat(vps['expires_at'])
                if current_expiry < datetime.now():
                    current_expiry = datetime.now()
            else:
                current_expiry = datetime.now()

            new_expiry = current_expiry + timedelta(days=days)
            vps['expires_at'] = new_expiry.isoformat()
            vps['duration_days'] = vps.get('duration_days', 0) + days

            # Unsuspend if suspended due to expiration
            if vps.get('suspended'):
                vps['suspended'] = False
                vps['status'] = 'stopped'  # User can start it manually

            save_vps_data()

            success_embed = create_success_embed("✅ VPS Renewed Successfully!", 
                f"**VPS:** `{vps['container_name']}`\n"
                f"**Extended by:** {days} day(s)\n"
                f"**New Expiry:** {new_expiry.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Your VPS has been renewed! 🎉")

            await msg.edit(embed=success_embed)

    except asyncio.TimeoutError:
        await msg.edit(embed=create_info_embed("Timeout", "Renewal request timed out."))

@bot.command(name='admin-renew', aliases=['arenew', 'set-expiry', 'extend-vps'])
@is_admin()
async def admin_renew_vps(ctx, user: discord.Member, vps_number: int, days: int):
    """Admin command to set/extend VPS expiry"""
    
    if days < 1 or days > 365:
        await ctx.send(embed=create_error_embed("Invalid Duration", 
            "Days must be between 1 and 365"))
        return
    
    user_id = str(user.id)
    
    # Get user's VPS
    vps_list = vps_data.get(user_id, [])
    if not vps_list:
        await ctx.send(embed=create_error_embed("No VPS Found", 
            f"{user.mention} doesn't have any VPS."))
        return
    
    if vps_number < 1 or vps_number > len(vps_list):
        await ctx.send(embed=create_error_embed("Invalid VPS", 
            f"{user.mention} doesn't have VPS #{vps_number}.\n"
            f"They have {len(vps_list)} VPS total."))
        return
    
    vps = vps_list[vps_number - 1]
    container_name = vps['container_name']
    
    # Calculate new expiry
    if vps.get('expires_at'):
        current_expiry = datetime.fromisoformat(vps['expires_at'])
        if current_expiry < datetime.now():
            # If expired, start from now
            base_time = datetime.now()
        else:
            # If not expired, extend from current expiry
            base_time = current_expiry
    else:
        # No expiry set, start from now
        base_time = datetime.now()
    
    new_expiry = base_time + timedelta(days=days)
    
    # Update VPS data
    vps['expires_at'] = new_expiry.isoformat()
    vps['duration_days'] = vps.get('duration_days', 0) + days
    
    # Unsuspend if suspended due to expiration
    was_suspended = vps.get('suspended', False)
    if was_suspended:
        vps['suspended'] = False
        vps['status'] = 'stopped'  # User can start it manually
    
    save_vps_data()
    
    # Update in database if VPS has ID
    vps_id = vps.get('id')
    if vps_id:
        try:
            renew_vps(vps_id, days)
        except Exception as e:
            logger.warning(f"Failed to update VPS expiry in database: {e}")
    
    # Get expiry info for display
    expiry_info = format_expiry_time(new_expiry.isoformat())
    
    # Create success embed
    embed = create_success_embed("✅ VPS Expiry Updated (Admin)", 
        f"**User:** {user.mention}\n"
        f"**VPS:** `{container_name}` (#{vps_number})\n"
        f"**Extended by:** {days} day(s)\n"
        f"**New Expiry:** {new_expiry.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"**Status:** {expiry_info['text']}\n"
        f"**Total Duration:** {vps.get('duration_days', 0)} days")
    
    if was_suspended:
        add_field(embed, "🔓 Unsuspended", 
            "VPS was suspended and has been unsuspended.\n"
            f"User can start it with `{PREFIX}manage`", False)
    
    add_field(embed, "💡 Note", 
        "This renewal was done by an administrator", False)
    
    await ctx.send(embed=embed)
    
    # Try to DM the user
    try:
        dm_embed = create_success_embed("🎁 VPS Extended by Admin!", 
            f"An admin has extended your VPS!\n\n"
            f"**VPS:** `{container_name}` (#{vps_number})\n"
            f"**Extended by:** {days} day(s)\n"
            f"**New Expiry:** {new_expiry.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Status:** {expiry_info['text']}")
        
        if was_suspended:
            add_field(dm_embed, "🔓 Unsuspended", 
                f"Your VPS was unsuspended! Use `{PREFIX}manage` to start it.", False)
        
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("DM Failed", 
            f"Couldn't send DM to {user.mention}. They may have DMs disabled."))
    
    logger.info(f"Admin {ctx.author.name} extended VPS {container_name} for {user.name} by {days} days")

@bot.command(name='quickhelp')
async def quick_help(ctx):
    """Show quick reference for common tasks"""
    user_id = str(ctx.author.id)
    is_admin_user = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    
    embed = create_info_embed("🚀 Quick Help Reference", 
        "Quick reference for common tasks. Use `!help` for complete command list.")
    
    # Common user tasks
    add_field(embed, "👤 For Users", 
        "• `!myvps` - List your VPS\n"
        "• `!manage` - Start/stop/manage VPS\n"
        "• `!ports` - Manage port forwarding\n"
        "• `!share-user @user 1` - Share VPS #1\n"
        "• `!about` - Bot information", False)
    
    # VPS management
    add_field(embed, "🖥️ VPS Control", 
        "• In `!manage`: Click ▶ to start VPS\n"
        "• In `!manage`: Click ⏸ to stop VPS\n"
        "• In `!manage`: Click 🔑 for SSH access\n"
        "• In `!manage`: Click 📊 for live stats\n"
        "• In `!manage`: Click 🔄 to reinstall OS", False)
    
    # Troubleshooting
    add_field(embed, "🔧 Common Issues", 
        "• Ports not working? Use `!repair-ports <container>` (admin)\n"
        "• VPS suspended? Contact admin to unsuspend\n"
        "• Need more resources? Contact admin for upgrade\n"
        "• SSH not working? Try reinstall with different OS", False)
    
    if is_admin_user:
        add_field(embed, "🛡️ Admin Quick Actions", 
            "• `!create 2 2 20 @user` - Create 2GB/2CPU/20GB VPS\n"
            "• `!userinfo @user` - Check user details\n"
            "• `!node list` - List all nodes\n"
            "• `!serverstats` - System overview\n"
            "• `!suspend-vps <container> <reason>` - Suspend VPS", False)
    
    embed.set_footer(text=f"{BOT_NAME} VPS Manager • Use !help for complete command list")
    await ctx.send(embed=embed)

@bot.command(name='help-search')
async def help_search(ctx, *, search_term: str = None):
    """Search for commands"""
    if not search_term:
        await show_help(ctx)
        return
    
    search_term = search_term.lower()
    user_id = str(ctx.author.id)
    is_admin_user = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
    is_main_admin_user = user_id == str(MAIN_ADMIN_ID)
    
    # Build complete command list based on permissions
    all_commands = []
    
    # User commands (always available)
    user_categories = ["user", "vps", "ports", "system", "bot"]
    for cat in user_categories:
        all_commands.extend(HelpView(ctx).command_categories[cat]["commands"])
    
    # Admin commands
    if is_admin_user:
        all_commands.extend(HelpView(ctx).command_categories["admin"]["commands"])
        all_commands.extend(HelpView(ctx).command_categories["nodes"]["commands"])
    
    # Main admin commands
    if is_main_admin_user:
        all_commands.extend(HelpView(ctx).command_categories["main_admin"]["commands"])
    
    # Search through commands
    matches = []
    for cmd, desc in all_commands:
        if (search_term in cmd.lower() or search_term in desc.lower()):
            matches.append((cmd, desc))
    
    if not matches:
        embed = create_info_embed("🔍 No Results Found",
            f"No commands found matching '{search_term}'. Try a different search term.")
        await ctx.send(embed=embed)
        return
    
    # Show results
    embed = create_info_embed(f"🔍 Search Results for '{search_term}'",
        f"Found {len(matches)} command(s) matching your search.")
    
    # Group matches by category
    results_text = "\n".join([f"**{cmd}** - {desc}" for cmd, desc in matches[:15]])
    add_field(embed, "Matching Commands", results_text, False)
    
    if len(matches) > 15:
        add_field(embed, "Note", f"Showing 15 of {len(matches)} matches. Try a more specific search.", False)
    
    embed.set_footer(text=f"{BOT_NAME} VPS Manager • Use !help for complete list")
    await ctx.send(embed=embed)    

@bot.command(name='node')
@is_admin()
async def node_cmd(ctx, sub: str, *args):
    if sub == 'create':
        await ctx.send("Enter node name:")
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        name = (await bot.wait_for('message', check=check)).content.strip()
        await ctx.send("Enter location:")
        location = (await bot.wait_for('message', check=check)).content.strip()
        await ctx.send("Enter total VPS capacity:")
        total_vps_str = (await bot.wait_for('message', check=check)).content.strip()
        try:
            total_vps = int(total_vps_str)
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid Input", "Total VPS must be an integer."))
            return
        await ctx.send("Enter tags (comma separated):")
        tags_str = (await bot.wait_for('message', check=check)).content.strip()
        tags = [t.strip() for t in tags_str.split(',') if t.strip()]
        tags_json = json.dumps(tags)
        await ctx.send("Enter node URL (e.g., http://ip:port) or leave blank for local:")
        url_str = (await bot.wait_for('message', check=check)).content.strip()
        url = url_str if url_str else None
        is_local = 1 if not url else 0
        api_key = None if is_local else ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=32))
        conn = get_db()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (name, location, total_vps, tags_json, api_key, url, is_local))
            conn.commit()
            node_id = cur.lastrowid
            embed = create_success_embed("Node Created", f"ID: {node_id}\nName: {name}\nLocation: {location}\nCapacity: {total_vps}\nTags: {', '.join(tags)}")
            if not is_local:
                add_field(embed, "API Key", api_key, False)
                add_field(embed, "URL", url, False)
                add_field(embed, "Setup", f"Run `python node-agent.py --api_key={api_key} --port=PORT` on the node server.")
            await ctx.send(embed=embed)
        except sqlite3.IntegrityError:
            await ctx.send(embed=create_error_embed("Error", "Node name already exists."))
        conn.close()
    elif sub == 'list':
        nodes = get_nodes()
        embed = create_info_embed("Nodes List", "")
        for n in nodes:
            status = "Local" if n['is_local'] else "Down"
            if not n['is_local']:
                try:
                    response = requests.get(f"{n['url']}/api/ping", params={'api_key': n['api_key']}, timeout=5)
                    status = "Up" if response.status_code == 200 else "Down"
                except:
                    pass
            field = f"ID: {n['id']}\nName: {n['name']}\nLocation: {n['location']}\nCapacity: {n['total_vps']}\nTags: {', '.join(n['tags'])}\nStatus: {status}"
            if not n['is_local']:
                field += f"\nURL: {n['url']}"
            add_field(embed, f"Node {n['id']}", field, False)
        await ctx.send(embed=embed)
    elif sub == 'edit':
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node edit <id>"))
            return
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        await ctx.send(f"Editing node {node['name']}. Enter new name ( . to skip):")
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        new_name = (await bot.wait_for('message', check=check)).content.strip()
        if new_name != '.':
            node['name'] = new_name
        await ctx.send("New location ( . to skip):")
        new_loc = (await bot.wait_for('message', check=check)).content.strip()
        if new_loc != '.':
            node['location'] = new_loc
        await ctx.send("New total VPS capacity ( . to skip):")
        new_total = (await bot.wait_for('message', check=check)).content.strip()
        if new_total != '.':
            node['total_vps'] = int(new_total)
        await ctx.send("New tags (comma separated, . to skip):")
        new_tags = (await bot.wait_for('message', check=check)).content.strip()
        if new_tags != '.':
            node['tags'] = json.dumps([t.strip() for t in new_tags.split(',') if t.strip()])
        if not node['is_local']:
            await ctx.send("New URL ( . to skip):")
            new_url = (await bot.wait_for('message', check=check)).content.strip()
            if new_url != '.':
                node['url'] = new_url
            await ctx.send("Regenerate API key? (y/n):")
            regen = (await bot.wait_for('message', check=check)).content.strip().lower()
            if regen == 'y':
                node['api_key'] = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=32))
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE nodes SET name=?, location=?, total_vps=?, tags=?, api_key=?, url=? WHERE id=?',
                    (node['name'], node['location'], node['total_vps'], json.dumps(node['tags']), node.get('api_key'), node.get('url'), node_id))
        conn.commit()
        conn.close()
        embed = create_success_embed("Node Updated", f"ID: {node_id}\nName: {node['name']}\nLocation: {node['location']}\nCapacity: {node['total_vps']}\nTags: {', '.join(node['tags'])}")
        if not node['is_local']:
            add_field(embed, "API Key", node['api_key'], False)
            add_field(embed, "URL", node['url'], False)
        await ctx.send(embed=embed)
    
    # NEW: Add delete subcommand
    elif sub == 'delete':
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node delete <id> [force]"))
            return
        
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        
        force = False
        if len(args) > 1 and args[1].lower() == 'force':
            force = True
        elif len(args) > 1:
            await ctx.send(embed=create_error_embed("Invalid Argument", "Optional argument must be 'force'."))
            return
        
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        
        # Check if this is the local node
        if node['is_local']:
            await ctx.send(embed=create_error_embed("Cannot Delete", "Cannot delete the local node."))
            return
        
        # Check if node has any VPS assigned
        vps_count = get_current_vps_count(node_id)
        if not force and vps_count > 0:
            await ctx.send(embed=create_error_embed("Cannot Delete", 
                f"Node has {vps_count} VPS assigned. Migrate or delete them first, or use 'force' to delete all VPS and the node."))
            return
        
        # Prepare warning message
        warning_msg = f"Are you sure you want to delete node **{node['name']}** (ID: {node_id})?\n\n"
        warning_msg += f"**Location:** {node['location']}\n"
        warning_msg += f"**Tags:** {', '.join(node['tags'])}\n\n"
        if force and vps_count > 0:
            warning_msg += f"**WARNING: Force mode will delete all {vps_count} VPS on this node first!**\n\n"
        warning_msg += "This action cannot be undone!"
        
        embed = create_warning_embed("⚠️ Delete Node", warning_msg)
        
        class ConfirmDelete(discord.ui.View):
            def __init__(self, node_id, node_name, force, vps_count):
                super().__init__(timeout=60)
                self.node_id = node_id
                self.node_name = node_name
                self.force = force
                self.vps_count = vps_count
            
            @discord.ui.button(label="Delete Node", style=discord.ButtonStyle.danger)
            async def confirm(self, inter: discord.Interaction, item: discord.ui.Button):
                if str(inter.user.id) != str(ctx.author.id):
                    await inter.response.send_message(
                        embed=create_error_embed("Access Denied", "Only the command author can confirm."),
                        ephemeral=True
                    )
                    return
                
                await inter.response.defer()
                
                conn = get_db()
                cur = conn.cursor()
                
                if self.force and self.vps_count > 0:
                    # Force delete all VPS on this node
                    cur.execute('DELETE FROM vps WHERE node_id = ?', (self.node_id,))
                
                # Delete the node from database
                cur.execute('DELETE FROM nodes WHERE id = ?', (self.node_id,))
                
                conn.commit()
                conn.close()
                
                msg = f"Node **{self.node_name}** (ID: {self.node_id}) has been deleted."
                if self.force and self.vps_count > 0:
                    msg += f" All {self.vps_count} VPS on the node were also deleted."
                
                success_embed = create_success_embed("Node Deleted", msg)
                await inter.followup.send(embed=success_embed)
                self.stop()
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, inter: discord.Interaction, item: discord.ui.Button):
                if str(inter.user.id) != str(ctx.author.id):
                    await inter.response.send_message(
                        embed=create_error_embed("Access Denied", "Only the command author can cancel."),
                        ephemeral=True
                    )
                    return
                
                await inter.response.edit_message(
                    embed=create_info_embed("Deletion Cancelled", "Node deletion was cancelled."),
                    view=None
                )
                self.stop()
        
        await ctx.send(embed=embed, view=ConfirmDelete(node_id, node['name'], force, vps_count))
    
    elif sub == 'status':
        # New: Check node status
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node status <id>"))
            return
        
        try:
            node_id = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Node ID must be an integer."))
            return
        
        node = get_node(node_id)
        if not node:
            await ctx.send(embed=create_error_embed("Not Found", "Node not found."))
            return
        
        embed = create_info_embed(f"Node Status - {node['name']}")
        
        if node['is_local']:
            status = "🟢 Local Node"
            cpu_usage = get_host_cpu_usage()
            ram_usage = get_host_ram_usage()
            add_field(embed, "Status", status, True)
            add_field(embed, "CPU Usage", f"{cpu_usage:.1f}%", True)
            add_field(embed, "RAM Usage", f"{ram_usage:.1f}%", True)
        else:
            try:
                response = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
                if response.status_code == 200:
                    status = "🟢 Online"
                    try:
                        stats_response = requests.get(f"{node['url']}/api/get_host_stats", 
                                                    params={'api_key': node['api_key']}, 
                                                    timeout=5)
                        if stats_response.status_code == 200:
                            stats = stats_response.json()
                            cpu_usage = stats.get('cpu', 0.0)
                            ram_usage = stats.get('ram', 0.0)
                            add_field(embed, "CPU Usage", f"{cpu_usage:.1f}%", True)
                            add_field(embed, "RAM Usage", f"{ram_usage:.1f}%", True)
                    except:
                        cpu_usage = "Unknown"
                        ram_usage = "Unknown"
                else:
                    status = "🔴 Offline"
            except:
                status = "🔴 Offline"
            
            add_field(embed, "Status", status, True)
        
        vps_count = get_current_vps_count(node_id)
        capacity = node['total_vps']
        usage_percentage = (vps_count / capacity * 100) if capacity > 0 else 0
        
        add_field(embed, "VPS Capacity", f"{vps_count}/{capacity} ({usage_percentage:.1f}%)", True)
        add_field(embed, "Location", node['location'], True)
        add_field(embed, "Tags", ", ".join(node['tags']), True)
        
        if not node['is_local']:
            add_field(embed, "URL", node['url'], False)
        
        await ctx.send(embed=embed)
    
    else:
        # Show help for node command
        embed = create_info_embed("Node Management", 
            f"Manage multi-node infrastructure for {BOT_NAME}")

class HelpView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.current_category = "user"
        # Command categories
        self.command_categories = {
            "user": {
                "name": "👤 User Commands",
                "commands": [
                    (f"{PREFIX}ping", "Check bot latency"),
                    (f"{PREFIX}uptime", "Show host uptime"),
                    (f"{PREFIX}myvps", "List your VPS"),
                    (f"{PREFIX}manage [@user]", "Manage your VPS or another user's VPS (Admin only)"),
                    (f"{PREFIX}share-user @user <vps_number>", "Share VPS access"),
                    (f"{PREFIX}share-ruser @user <vps_number>", "Revoke VPS access"),
                    (f"{PREFIX}manage-shared @owner <vps_number>", "Manage shared VPS")
                ]
            },
            "vps": {
                "name": "🖥️ VPS Management",
                "commands": [
                    (f"{PREFIX}myvps", "List your VPS"),
                    (f"{PREFIX}renew <vps_number> <days>", "Renew VPS to extend expiration (free)"),
                    (f"{PREFIX}admin-renew @user <vps_number> <days>", "Set/extend VPS expiry (Admin only)"),
                    (f"{PREFIX}vpsinfo [container]", "VPS information"),
                    (f"{PREFIX}vps-stats <container>", "VPS stats"),
                    (f"{PREFIX}vps-uptime <container>", "VPS uptime"),
                    (f"{PREFIX}vps-processes <container>", "List processes"),
                    (f"{PREFIX}vps-logs <container> [lines]", "Show logs"),
                    (f"{PREFIX}restart-vps <container>", "Restart VPS"),
                    (f"{PREFIX}clone-vps <container> [new_name]", "Clone VPS"),
                    (f"{PREFIX}snapshot <container> [snap_name]", "Create snapshot"),
                    (f"{PREFIX}list-snapshots <container>", "List snapshots"),
                    (f"{PREFIX}restore-snapshot <container> <snap_name>", "Restore snapshot")
                ]
            },
            "ports": {
                "name": "🔌 Port Forwarding",
                "commands": [
                    (f"{PREFIX}ports [add <vps_num> <port> | list | remove <id>]", "Manage port forwards (TCP/UDP)"),
                    (f"{PREFIX}ports-add-user <amount> @user", "Allocate port slots to user (Admin only)"),
                    (f"{PREFIX}ports-remove-user <amount> @user", "Deallocate port slots from user (Admin only)"),
                    (f"{PREFIX}ports-revoke <id>", "Revoke specific port forward (Admin only)")
                ]
            },
            "system": {
                "name": "⚙️ System Commands",
                "commands": [
                    (f"{PREFIX}serverstats", "Server statistics"),
                    (f"{PREFIX}resource-check", "Check and suspend high-usage VPS (Admin only)"),
                    (f"{PREFIX}cpu-monitor <status|enable|disable>", "Resource monitor control (logging only)"),
                    (f"{PREFIX}thresholds", "View resource thresholds"),
                    (f"{PREFIX}set-threshold <cpu> <ram>", "Set resource thresholds (Admin only)"),
                    (f"{PREFIX}set-status <type> <name>", "Set bot status (Admin only)")
                ]
            },
            "nodes": {
                "name": "🌐 Node Management",
                "commands": [
                    (f"{PREFIX}node create", "Create a new node (Admin only)"),
                    (f"{PREFIX}node list", "List all nodes (Admin only)"),
                    (f"{PREFIX}node status <id>", "Check node status (Admin only)"),
                    (f"{PREFIX}node edit <id>", "Edit node details (Admin only)"),
                    (f"{PREFIX}node delete <id>", "Delete a node (Admin only)"),
                    (f"{PREFIX}node migrate <from> <to>", "Migrate VPS between nodes (Admin only)"),
                    (f"{PREFIX}lxc-list [node_id]", "List LXC containers on node (Admin only)")
                ],
                "admin_only": True
            },
            "bot": {
                "name": "🤖 Bot Control",
                "commands": [
                    (f"{PREFIX}ping", "Check bot latency"),
                    (f"{PREFIX}uptime", "Show host uptime"),
                    (f"{PREFIX}help", "Show this help menu"),
                    (f"{PREFIX}set-status <type> <name>", "Set bot status (Admin only)")
                ]
            },
            "admin": {
                "name": "🛡️ Admin Commands",
                "commands": [
                    (f"{PREFIX}lxc-list", "List all LXC containers"),
                    (f"{PREFIX}create <ram_gb> <cpu_cores> <disk_gb> @user", "Create VPS with OS selection"),
                    (f"{PREFIX}delete-vps @user <vps_number> [reason]", "Delete user's VPS"),
                    (f"{PREFIX}add-resources <container> [ram] [cpu] [disk]", "Add resources to VPS"),
                    (f"{PREFIX}resize-vps <container> [ram] [cpu] [disk]", "Resize VPS resources"),
                    (f"{PREFIX}suspend-vps <container> [reason]", "Suspend VPS"),
                    (f"{PREFIX}unsuspend-vps <container>", "Unsuspend VPS"),
                    (f"{PREFIX}suspension-logs [container]", "View suspension logs"),
                    (f"{PREFIX}whitelist-vps <container> <add|remove>", "Whitelist VPS from auto-suspend"),
                    (f"{PREFIX}userinfo @user", "User information"),
                    (f"{PREFIX}list-all", "List all VPS"),
                    (f"{PREFIX}exec <container> <command>", "Execute command"),
                    (f"{PREFIX}stop-vps-all", "Stop all VPS"),
                    (f"{PREFIX}migrate-vps <container> <pool>", "Migrate VPS"),
                    (f"{PREFIX}vps-network <container> <action> [value]", "Network management"),
                    (f"{PREFIX}apply-permissions <container>", "Apply Docker-ready permissions")
                ],
                "admin_only": True
            },
            "main_admin": {
                "name": "👑 Main Admin Commands",
                "commands": [
                    (f"{PREFIX}admin-add @user", "Add admin"),
                    (f"{PREFIX}admin-remove @user", "Remove admin"),
                    (f"{PREFIX}admin-list", "List admins")
                ],
                "admin_only": True,
                "main_admin_only": True
            }
        }
        self.update_select()
        self.update_embed()
        self.add_item(self.select)

    def update_select(self):
        """Update the category selection dropdown based on user permissions"""
        self.select = discord.ui.Select(placeholder="Select Category", options=[])
        user_id = str(self.ctx.author.id)
        is_admin_user = user_id == str(MAIN_ADMIN_ID) or user_id in admin_data.get("admins", [])
        is_main_admin_user = user_id == str(MAIN_ADMIN_ID)
       
        # Add all categories that user has access to
        options = []
        # Always show basic categories
        basic_categories = ["user", "vps", "ports", "system", "bot"]
        for category in basic_categories:
            options.append(discord.SelectOption(
                label=self.command_categories[category]["name"],
                value=category,
                emoji=self.get_category_emoji(category)
            ))
       
        # Add nodes category if admin
        if is_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["nodes"]["name"],
                value="nodes",
                emoji=self.get_category_emoji("nodes")
            ))
       
        # Add admin categories if user has permissions
        if is_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["admin"]["name"],
                value="admin",
                emoji=self.get_category_emoji("admin")
            ))
       
        if is_main_admin_user:
            options.append(discord.SelectOption(
                label=self.command_categories["main_admin"]["name"],
                value="main_admin",
                emoji=self.get_category_emoji("main_admin")
            ))
       
        self.select.options = options
        self.select.callback = self.select_callback
   
    async def select_callback(self, interaction: discord.Interaction):
        """Handle category selection"""
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            return
        
        try:
            self.current_category = interaction.data['values'][0]
            self.update_embed()
            
            # Respond immediately to prevent timeout
            await interaction.response.edit_message(embed=self.embed, view=self)
        except discord.errors.NotFound:
            # Interaction expired, try to send a new message
            try:
                await interaction.channel.send(embed=self.embed, view=self)
            except:
                pass
        except Exception as e:
            logger.error(f"Error in HelpView select_callback: {e}")
            try:
                await interaction.response.send_message("An error occurred. Please try again.", ephemeral=True)
            except:
                pass

    def get_category_emoji(self, category):
        """Get emoji for each category"""
        emojis = {
            "user": "👤",
            "vps": "🖥️",
            "ports": "🔌",
            "system": "⚙️",
            "bot": "🤖",
            "nodes": "🌐",
            "admin": "🛡️",
            "main_admin": "👑"
        }
        return emojis.get(category, "📁")
   
    def update_embed(self):
        """Update the embed based on current category and user permissions"""
        category_data = self.command_categories[self.current_category]
        # Create embed with category-specific styling
        colors = {
            "user": 0x3498db, # Blue
            "vps": 0x2ecc71, # Green
            "ports": 0xe74c3c, # Red
            "system": 0xf39c12, # Orange
            "bot": 0x9b59b6, # Purple
            "nodes": 0x1abc9c, # Teal
            "admin": 0xe67e22, # Carrot
            "main_admin": 0xf1c40f # Yellow
        }
        color = colors.get(self.current_category, 0x1a1a1a)
       
        title = f"📚 {BOT_NAME} Command Help - {category_data['name']}"
        description = f"**{category_data['name']}**\nUse the dropdown below to switch categories."
       
        # Add helpful tips based on category
        tips = {
            "user": "Tip: Use `!myvps` to see all your VPS and `!manage` to control them.",
            "vps": "Tip: Snapshots are useful before making major changes to your VPS.",
            "ports": "Tip: Port forwards work for both TCP and UDP protocols.",
            "system": "Tip: Set thresholds to monitor resource usage across nodes.",
            "nodes": "Tip: Use `!node list` to see all available nodes and their status.",
            "admin": "Tip: Always check `!userinfo @user` before modifying VPS.",
            "main_admin": "Tip: Be careful when adding/removing admin privileges."
        }
       
        if self.current_category in tips:
            description += f"\n\n💡 {tips[self.current_category]}"
       
        self.embed = create_embed(title, description, color)
       
        # Add commands to embed
        commands_text = "\n".join([f"**{cmd}** - {desc}" for cmd, desc in category_data["commands"]])
        add_field(self.embed, "Commands", commands_text, False)
       
        # Add appropriate footer based on category
        footers = {
            "user": f"{BOT_NAME} VPS Manager • User Commands • Need help? Contact admin",
            "vps": f"{BOT_NAME} VPS Manager • VPS Management • Snapshots • Cloning",
            "ports": f"{BOT_NAME} VPS Manager • Port Forwarding • TCP/UDP Support",
            "system": f"{BOT_NAME} VPS Manager • System Monitoring • Resource Management",
            "nodes": f"{BOT_NAME} VPS Manager • Multi-Node Management • Distributed Infrastructure",
            "bot": f"{BOT_NAME} VPS Manager • Bot Control • Status Management",
            "admin": f"{BOT_NAME} VPS Manager • Admin Panel • Restricted Access",
            "main_admin": f"{BOT_NAME} VPS Manager • Main Admin • Full System Control"
        }
       
        self.embed.set_footer(text=footers.get(self.current_category, f"{BOT_NAME} VPS Manager"))


@bot.command(name='help')
async def show_help(ctx):
    """Display the interactive help menu"""
    view = HelpView(ctx)
    await ctx.send(embed=view.embed, view=view)


# Command aliases for typos and convenience
@bot.command(name='mangage')
async def manage_typo(ctx):
    await ctx.send(embed=create_info_embed("Command Correction", f"Did you mean `{PREFIX}manage`? Use the correct command."))


@bot.command(name='commands')
async def commands_alias(ctx):
    """Alias for help command"""
    await show_help(ctx)


@bot.command(name='stats')
async def stats_alias(ctx):
    if str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", []):
        await server_stats(ctx)
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "This command requires admin privileges."))


@bot.command(name='info')
async def info_alias(ctx, user: discord.Member = None):
    if str(ctx.author.id) == str(MAIN_ADMIN_ID) or str(ctx.author.id) in admin_data.get("admins", []):
        if user:
            await user_info(ctx, user)
        else:
            await ctx.send(embed=create_error_embed("Usage", f"Please specify a user: `{PREFIX}info @user`"))
    else:
        await ctx.send(embed=create_error_embed("Access Denied", "This command requires admin privileges."))
# Run the bot
if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logger.error("No Discord token found in DISCORD_TOKEN environment variable.")