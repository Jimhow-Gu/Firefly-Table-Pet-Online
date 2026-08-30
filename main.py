# -*- coding: utf-8 -*-
import logging
import math
import os
import shutil
import subprocess
import sys
import traceback
import psutil
import pygame
import webbrowser
import re
import random
import uuid
import urllib.request
from datetime import datetime, timedelta, timezone
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWidgets import *
from PyQt5.QtWebEngineWidgets import QWebEngineView
from lunardate import LunarDate
from PyQt5.QtCore import QAbstractNativeEventFilter, QTimer
import ctypes
from ctypes import wintypes

from win10toast import ToastNotifier

def global_exception_hook(exc_type, exc_value, exc_tb):
    msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    with open("firefly_crash.log", "a", encoding="utf-8") as f:
        f.write(f"\n=== {datetime.now()} ===\n")
        f.write(msg)
    print("严重错误已记录到 firefly_crash.log")
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = global_exception_hook

# Windows 常量
WM_HOTKEY = 0x0312
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_TAB = 0x09

CLIENT_VERSION = "4.4.1-beta"

def parse_version(ver_str):
    """同服务端定义"""
    if not ver_str:
        return None, None
    match = re.search(r'(\d+(?:\.\d+){1,2})', ver_str)
    if not match:
        return None, None
    num_part = match.group(1)
    num_tuple = tuple(map(int, num_part.split('.')))
    suffix = None
    if '-' in ver_str:
        suffix = ver_str.split('-', 1)[1].strip()
        if not suffix:
            suffix = None
    return num_tuple, suffix

def versions_compatible(client_ver, server_ver):
    """
    检查客户端版本与服务端版本是否兼容。
    仅比较数字部分（如 4.4.1），忽略后缀（如 -beta）。
    """
    c_num, _ = parse_version(client_ver)
    s_num, _ = parse_version(server_ver)
    if c_num is None or s_num is None:
        return False
    return c_num == s_num

def show_notification_async(title, message, icon_path=None, duration=3):
    """
    在子线程中弹出 Windows 通知
    """
    def _show():
        toaster = ToastNotifier()
        toaster.show_toast(
            title,
            message,
            icon_path=icon_path,
            duration=duration,
            threaded=True  # win10toast 自带异步，但为确保安全再包一层
        )
    threading.Thread(target=_show, daemon=True).start()

def create_volume_menu(parent):
    widget = QWidget()
    widget.setMinimumWidth(250)
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(12, 4, 12, 4)
    layout.setSpacing(8)

    label = QLabel("🔊")
    slider = QSlider(Qt.Horizontal)
    slider.setRange(0, 100)
    slider.setValue(int(parent.volume * 100))
    slider.setFixedWidth(160)

    value_label = QLabel(f"{int(parent.volume * 100)}%")
    value_label.setFixedWidth(45)
    value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    # 修改颜色为黑色，适配白色菜单背景
    value_label.setStyleSheet("color: #000000; font-size: 18px;")

    slider.valueChanged.connect(lambda v: parent.set_volume(v / 100.0))
    slider.valueChanged.connect(lambda v: value_label.setText(f"{v}%"))

    layout.addWidget(label)
    layout.addWidget(slider)
    layout.addWidget(value_label)

    action = QWidgetAction(parent)
    action.setDefaultWidget(widget)
    return action

def set_window_emoji_icon(window, emoji: str, size: int = 24):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    font = QFont("Segoe UI Emoji", size - 4)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, emoji)
    painter.end()
    window.setWindowIcon(QIcon(pixmap))

pluginsPath = 'plugins'
if os.path.exists(pluginsPath):
    QApplication.addLibraryPath(pluginsPath)

# ====================== 用户数据配置 ======================
USER_DATA_DIR = "user_data"
USER_CONFIG = "user_login.json"
if not os.path.exists(USER_DATA_DIR):
    os.makedirs(USER_DATA_DIR)

CODES_FILE = os.path.join(USER_DATA_DIR, "redeem_codes.json")

# ====================== 时装定义 ======================
AVAILABLE_CLOTHES = {
    "normal": {
        "name": "普通时装",
        "source_dir": "clothes/normal/assets",
        "description": "默认服装"
    },
    "26_Newyear": {
        "name": "柿柿如意",
        "source_dir": "clothes/new_year/assets",
        "description": "2026 春节签到获得"
    }
}

# ====================== 统一菜单样式 ======================
MENU_STYLE = """
    QMenu{
        border: 1px solid #dbdbdb;
        background-color: #ffffff;
        padding: 4px;
        border-radius: 6px;
    }
    QMenu::item {
        padding: 8px 24px;          /* 适当内边距，无外边距 */
        margin: 0px;                /* 取消外边距 */
        border-radius: 4px;
        background-color: transparent;
    }
    QMenu::item:selected{
        background-color: LightSkyBlue;
        color: #1E90FF;
    }
    QMenu::separator{
        height: 1px;
        background-color: #e9ecef;
        margin: 4px 0px;
    }
"""

def ensure_user_fields(user_data, filepath=None):
    modified = False
    defaults = {
        "balance": 20,
        "credit": 99999 if user_data.get("uid", "").lower().startswith("trailblazer") else 0,
        "last_sign_date": None,
        "banned": False,
        "banned_until": None,
        "redeemed_codes": [],
        "redeem_history": [],
        "cursor_style": "None",
        "pet_size": "normal",
        "owned_clothes": ["normal"],
        "last_state": "Standby",
        "mailbox": [],
        "last_x": None,
        "last_y": None,
        "star_rail_tickets": 0,
        "last_birthday_greet_year": None,
        "last_feed_time": None,
        "volume": 0.5,
        "allow_move": False,
        "total_interactions": 0,
        "total_open_count": 0,
        "total_sign_days": 0,
        "total_affection_up": 0,
        "total_sleep_count": 0,
        "total_chat_count": 0,
        "monthly_interactions": 0,
        "monthly_open_count": 0,
        "monthly_sign_days": 0,
        "monthly_affection_up": 0,
        "monthly_sleep_count": 0,
        "monthly_chat_count": 0,
        "monthly_credit_gained": 0,
        "monthly_cake_gained": 0,
        "monthly_ai_click_count": 0,
        "current_month": None,
        "current_clothes": "normal",
        "last_change_name_month": None,
        "change_name_count": 0,
    }

    for key, default_val in defaults.items():
        if key not in user_data:
            user_data[key] = default_val
            modified = True
    if not isinstance(user_data.get("owned_clothes"), list):
        user_data["owned_clothes"] = ["normal"]
        modified = True
    if not isinstance(user_data.get("mailbox"), list):
        user_data["mailbox"] = []
        modified = True
    if modified and filepath:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(user_data, f, ensure_ascii=False, indent=2)
    return user_data

def load_all_codes():
    if not os.path.exists(CODES_FILE):
        default_codes = {
            "Firefly": {
                "reward": 10,
                "max_uses": 1,
                "used_count": 0,
                "created_by": "system",
                "created_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "target_user": None,
                "item_type": "cake",
                "expire_time": None,
                "revoked": False,
                "revoked_users": [],
                "revoked_by": None,
                "revoked_time": None,
                "clothes_id": None
            }
        }
        with open(CODES_FILE, "w", encoding="utf-8") as f:
            json.dump(default_codes, f, indent=2)
        return default_codes
    with open(CODES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        for code, info in data.items():
            if "items" not in info:
                itype = info.get("item_type", "cake")
                reward = info.get("reward", 0)
                cid = info.get("clothes_id")
                if itype == "cake":
                    info["items"] = [{"type": "cake", "amount": reward}]
                elif itype == "clothes":
                    info["items"] = [{"type": "clothes", "id": cid}]
                elif itype == "credit":
                    info["items"] = [{"type": "credit", "amount": reward}]
            info.setdefault("expire_time", None)
            info.setdefault("revoked", False)
            info.setdefault("revoked_users", [])
            info.setdefault("revoked_by", None)
            info.setdefault("revoked_time", None)
        return data

def save_all_codes(codes):
    with open(CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(codes, f, indent=2)

def parse_duration(duration_str):
    pattern = re.compile(r'(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?')
    match = pattern.match(duration_str.upper())
    if not match:
        return None
    years = int(match.group(1)) if match.group(1) else 0
    months = int(match.group(2)) if match.group(2) else 0
    days = int(match.group(3)) if match.group(3) else 0
    total_days = years * 365 + months * 30 + days
    return timedelta(days=total_days)

def parse_expire_time(expire_str):
    if not expire_str:
        return None
    try:
        dt = datetime.strptime(expire_str, "%Y-%m-%d")
        return dt.isoformat()
    except:
        pass
    delta = parse_duration(expire_str)
    if delta is not None:
        expire_dt = datetime.now() + delta
        return expire_dt.isoformat()
    return None

def parse_time_spec(spec):
    """
    解析时间规格，支持：
    - 时长格式（单位可任意组合）：
        年：Y 或 yr
        月：M
        日：D 或 d
        时：H 或 hr
        分：m 或 min
        秒：s 或 sec
        示例：1Y2M3D4H5m6s 或 1yr 2mo ... (但mo未启用，可用M)
    - 纯数字：视为天数
    - 绝对日期：2026-12-31 或 2026-12-31 23:59:59
    返回 (timedelta, None) 或 (None, datetime) 或 (None, None)
    """
    if not spec:
        return None, None
    spec = spec.strip()

    # 1. 尝试绝对日期时间 (YYYY-MM-DD HH:MM:SS)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})$', spec)
    if match:
        y, m, d, h, min, sec = map(int, match.groups())
        try:
            dt = datetime(y, m, d, h, min, sec)
            return None, dt
        except:
            pass
    # 2. 绝对日期 (YYYY-MM-DD)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', spec)
    if match:
        y, m, d = map(int, match.groups())
        try:
            dt = datetime(y, m, d, 23, 59, 59)
            return None, dt
        except:
            pass

    # 3. 时长格式（支持大小写区分版和纯小写备选版）
    units = {
        'Y': 'years',
        'yr': 'years',
        'M': 'months',
        'D': 'days',
        'd': 'days',
        'H': 'hours',
        'hr': 'hours',
        'm': 'minutes',
        'min': 'minutes',
        's': 'seconds',
        'sec': 'seconds'
    }
    # 按长度降序排列，避免 `yr` 被 `y` 误匹配
    unit_pattern = '|'.join(sorted(units.keys(), key=len, reverse=True))
    pattern = re.compile(r'(\d+)(' + unit_pattern + r')')
    matches = pattern.findall(spec)

    if not matches:
        # 纯数字作为天数
        if spec.isdigit():
            return timedelta(days=int(spec)), None
        return None, None

    years = months = days = hours = minutes = seconds = 0
    for num_str, unit in matches:
        num = int(num_str)
        if unit in ('Y', 'yr'):
            years += num
        elif unit == 'M':
            months += num
        elif unit in ('D', 'd'):
            days += num
        elif unit in ('H', 'hr'):
            hours += num
        elif unit in ('m', 'min'):
            minutes += num
        elif unit in ('s', 'sec'):
            seconds += num
        else:
            return None, None

    # 累加为 timedelta（1年=365天，1月=30天）
    total_days = years * 365 + months * 30 + days
    delta = timedelta(days=total_days, hours=hours, minutes=minutes, seconds=seconds)
    return delta, None

def clean_expired_mails(user_data):
    if "mailbox" not in user_data:
        return
    cutoff = datetime.now() - timedelta(days=180)
    new_mailbox = []
    for mail in user_data["mailbox"]:
        try:
            mail_time = datetime.fromisoformat(mail["timestamp"])
            if mail_time >= cutoff:
                new_mailbox.append(mail)
        except:
            new_mailbox.append(mail)
    user_data["mailbox"] = new_mailbox

def send_mail(self, target_uid, title, content, items):
    if not self.connected:
        return
    self._send({'type': 'send_mail', 'data': {'target_uid': target_uid, 'title': title, 'content': content, 'items': items}})

def claim_mail_items(user_data, mail):
    if mail["claimed"]:
        return False, []
    items_desc = []
    for item in mail["items"]:
        itype = item["type"]
        if itype == "cake":
            user_data["balance"] = user_data.get("balance", 0) + item["amount"]
            items_desc.append(f"橡木蛋糕卷 x{item['amount']}")
        elif itype == "credit":
            user_data["credit"] = user_data.get("credit", 0) + item["amount"]
            if user_data["credit"] > 99999999:
                user_data["credit"] = 99999999
            items_desc.append(f"信用点 x{item['amount']}")
        elif itype == "clothes":
            clothes_id = item["id"]
            if clothes_id in AVAILABLE_CLOTHES:
                owned = user_data.get("owned_clothes", ["normal"])
                if clothes_id not in owned:
                    owned.append(clothes_id)
                    user_data["owned_clothes"] = owned
                    items_desc.append(f"时装 [{AVAILABLE_CLOTHES[clothes_id]['name']}]")
        elif itype == "star_rail_ticket":
            user_data["star_rail_tickets"] = user_data.get("star_rail_tickets", 0) + item["amount"]
            items_desc.append(f"星铁专票 x{item['amount']}")
    mail["claimed"] = True
    history = user_data.get("redeem_history", [])
    history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 邮件领取: {', '.join(items_desc) if items_desc else '空邮件'}")
    user_data["redeem_history"] = history
    return True, items_desc

class ConnectionThread(QThread):
    connection_success = pyqtSignal(dict)  # 改为传递整个 data 字典
    connection_failed = pyqtSignal(str)
    connection_closed = pyqtSignal()
    message_received = pyqtSignal(str)

    def __init__(self, host, port, userid, uid, name, country, version):
        super().__init__()
        self.host = host
        self.port = port
        self.userid = userid
        self.uid = uid
        self.name = name
        self.country = country
        self.version = version
        self.sock = None
        self._running = True

    def run(self):
        """子线程入口，执行所有网络操作"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(3)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)

            # 发送登录消息
            login_msg = {
                'type': 'login',
                'data': {
                    'userid': self.userid,
                    'uid': self.uid,
                    'name': self.name,
                    'country': self.country,
                    'version': self.version
                }
            }
            self._send(login_msg)

            # 开始监听（持续）
            self._listen()
        except socket.timeout:
            self.connection_failed.emit("连接超时，请检查服务器地址和网络")
        except ConnectionRefusedError:
            self.connection_failed.emit("服务器拒绝连接（可能未启动或端口错误）")
        except Exception as e:
            self.connection_failed.emit(f"连接失败：{str(e)}")
        finally:
            if self.sock:
                self.sock.close()
            self.connection_closed.emit()

    def _send(self, obj):
        if not self.sock:
            return
        try:
            data = (json.dumps(obj, ensure_ascii=False) + '\n').encode()
            self.sock.sendall(data)
        except Exception as e:
            self.connection_failed.emit(f"发送消息失败：{str(e)}")

    def _listen(self):
        """持续接收服务端消息"""
        buffer = bytearray()
        while self._running:
            try:
                data = self.sock.recv(1024)
                if not data:
                    # 正常关闭，处理剩余缓冲区
                    self._process_buffer(buffer)
                    break
                buffer.extend(data)
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        line_str = line.decode('utf-8')
                    except UnicodeDecodeError:
                        continue
                    if line_str:
                        self._process_line(line_str)
            except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
                # ★ 异常断开，先处理缓冲区中剩余数据
                self._process_buffer(buffer)
                print(f"[ConnectionThread] 连接异常断开: {e}")
                break
            except Exception as e:
                print(f"[ConnectionThread] 监听异常: {e}")
                self._process_buffer(buffer)
                break
        self._running = False

    def _process_buffer(self, buffer):
        """处理缓冲区中剩余的完整行"""
        lines = buffer.split(b'\n')
        for line in lines:
            if not line:
                continue
            try:
                line_str = line.decode('utf-8')
                if line_str:
                    self._process_line(line_str)
            except UnicodeDecodeError:
                continue

    def _process_line(self, line):
        try:
            msg = json.loads(line)
            mtype = msg['type']
            data = msg.get('data', {})
            if mtype == 'login_ok':
                self.connection_success.emit(data)
            elif mtype == 'login_fail':
                reason = data.get('reason', '登录失败')
                self.connection_failed.emit(reason)
                self._running = False  # 登录失败才停止
            elif mtype == 'server_shutdown':
                # 服务端关闭/维护模式踢出
                reason = data.get('reason', '服务器已关闭')
                self.message_received.emit(json.dumps(msg))  # 让上层处理
                self._running = False  # 断开连接
            else:
                # 所有其他消息（聊天、列表、邮件、等）转发给上层
                print(f"[DEBUG] Forwarding message type: {mtype}")
                self.message_received.emit(json.dumps(msg))
        except Exception as e:
            print(f"[ConnectionThread] 处理消息异常: {e}", file=sys.stderr)
            traceback.print_exc()

    def disconnect(self):
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass

class MultiplayerClient(QObject):
    login_success = pyqtSignal(dict)  # 传递完整登录数据
    message_received = pyqtSignal(dict)
    online_list_updated = pyqtSignal(list)
    mentioned = pyqtSignal(str)
    mentioned_all = pyqtSignal(str)
    connection_changed = pyqtSignal(bool)
    login_failed = pyqtSignal(str)
    permission_changed = pyqtSignal()
    command_result_signal = pyqtSignal(bool, str)
    sign_result = pyqtSignal(bool, str)
    redeem_result_signal = pyqtSignal(bool, str)
    sign_result_signal = pyqtSignal(bool, str)
    claim_mail_result_signal = pyqtSignal(bool, str)
    mails_ready = pyqtSignal(list)
    chat_history_ready = pyqtSignal(list)
    banned_by_server = pyqtSignal(str)
    server_shutdown = pyqtSignal(str)

    def __init__(self, firefly, parent=None):
        super().__init__(parent)
        self.firefly = firefly
        self.connected = False
        self.server_uid = None
        self.server_permission = 'user'
        self.conn_thread = None   # 连接线程
        self.stats = None
        self._last_ping_sent = 0
        self._latest_latency_ms = -1

    def ping(self):
        if not self.connected or not self.conn_thread:
            return
        self._last_ping_sent = time.time()
        print(f"[PING] 发送请求，时间 {self._last_ping_sent}")  # 新增
        self._send({'type': 'get_player_list', 'data': {}})

    def connect_to_server(self, host, port, userid, uid, name, country=''):
        self.conn_thread = ConnectionThread(host, port, userid, uid, name, country, CLIENT_VERSION)
        self.conn_thread.connection_success.connect(self._on_connection_success)
        self.conn_thread.connection_failed.connect(self._on_connection_failed)
        self.conn_thread.connection_closed.connect(self._on_connection_closed)
        self.conn_thread.message_received.connect(self._on_message_received)  # 新增
        self.conn_thread.start()

    def _send(self, obj):
        if self.conn_thread:
            self.conn_thread._send(obj)

    def _on_message_received(self, msg_json):
        try:
            msg = json.loads(msg_json)
        except:
            return
        self._process_message(msg)  # 直接传递字典

    def _on_connection_success(self, data):
        # 从字典中提取所有字段
        server_uid = data['server_uid']
        permission = data['permission']
        inventory = data.get('inventory')
        server_version = data.get('server_version')
        stats = data.get('stats', {})
        ai_enabled = data.get('ai_enabled', True)
        muted_until = data.get('muted_until')
        server_timezone_offset = data.get('server_timezone_offset', 0)
        chat_enabled = data.get('chat_enabled', True)
        server_display_name = data.get('server_display_name', '流萤服务器')
        require_rename = data.get('require_rename', False)
        server_name = data.get('server_name', '')
        uid = data.get('uid', '')
        chat_history = data.get('chat_history', [])  # ← 关键：获取聊天历史

        # 更新客户端状态
        self.connected = True
        self.server_uid = server_uid
        self.server_permission = permission
        self.stats = stats

        self.firefly.online_uid = uid
        self.firefly.ai_enabled = ai_enabled
        self.firefly.on_mute_status(muted_until)
        self.firefly.server_timezone_offset = server_timezone_offset
        self.firefly.chat_enabled = chat_enabled
        self.firefly.on_chat_status(chat_enabled)
        self.firefly.server_display_name = server_display_name
        self.firefly.online_name = server_name

        # 保存聊天历史到 Firefly
        self.firefly.chat_history = chat_history

        self.firefly.online_inventory = inventory
        if inventory is not None:
            self.firefly.online_signed_today = inventory.get('last_sign_date') == datetime.now().strftime('%Y-%m-%d')
        else:
            self.firefly.online_signed_today = False
        self.login_success.emit(data)

    def _on_connection_failed(self, reason):
        self.connected = False
        self.login_failed.emit(reason)

    def _on_connection_closed(self):
        self.connected = False
        self.connection_changed.emit(False)


    def _connect_thread(self, host, port):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(3)
            self.sock.connect((host, port))
            self.sock.settimeout(None)
            self.connected = True
            # 发送登录包（新增 country 字段）
            login_msg = {
                'type': 'login',
                'data': {
                    'userid': self.login_userid,
                    'uid': self.login_uid,
                    'name': self.login_name,
                    'country': getattr(self, 'login_country', ''),
                    'version': CLIENT_VERSION  # 新增
                }
            }
            self._send(login_msg)
            self.listener_thread = threading.Thread(target=self._listen, daemon=True)
            self.listener_thread.start()
            self.connection_changed.emit(True)
        except socket.timeout:
            self.login_failed.emit("连接超时，请检查服务器地址和网络")
        except ConnectionRefusedError:
            self.login_failed.emit("服务器拒绝连接（可能未启动或端口错误）")
        except Exception as e:
            self.login_failed.emit(f"连接失败：{str(e)}")

    def send_chat(self, text):
        if not self.connected or not self.conn_thread:
            return
        msg = {'type': 'chat', 'data': {'text': text}}
        self.conn_thread._send(msg)

    def send_command(self, cmd):
        if not self.connected or not self.conn_thread:
            return
        self.conn_thread._send({'type': 'command', 'data': {'command': cmd}})

    def request_player_list(self):
        if not self.connected or not self.conn_thread:
            return
        self.conn_thread._send({'type': 'get_player_list', 'data': {}})

    def disconnect(self):
        if self.conn_thread:
            self.conn_thread.disconnect()
            self.conn_thread = None
        self.connected = False
        self.connection_changed.emit(False)

    def _listen(self):
        """持续接收服务端消息"""
        buffer = bytearray()
        while self._running:
            try:
                data = self.sock.recv(1024)
                if not data:
                    # 对端正常关闭
                    break
                buffer.extend(data)
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        line_str = line.decode('utf-8')
                    except UnicodeDecodeError:
                        continue
                    if line_str:
                        self._process_line(line_str)
            except ConnectionResetError as e:
                print(f"[ConnectionThread] 连接被远程重置: {e}")
                break
            except ConnectionAbortedError as e:
                print(f"[ConnectionThread] 连接被中止: {e}")
                break
            except OSError as e:
                print(f"[ConnectionThread] 网络错误: {e}")
                break
            except Exception as e:
                print(f"[ConnectionThread] 未捕获异常: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                break
        self._running = False

    def _process_line(self, line):
        try:
            msg = json.loads(line)
            mtype = msg['type']
            data = msg.get('data', {})
            if mtype == 'login_ok':
                self.connection_success.emit(data)  # 传递完整数据
            elif mtype == 'login_fail':
                reason = data.get('reason', '登录失败')
                self.connection_failed.emit(reason)
                self._running = False
            elif mtype == 'server_shutdown':
                reason = data.get('reason', '服务器已关闭')
                self.message_received.emit(json.dumps(msg))
                self._running = False
            else:
                self.message_received.emit(json.dumps(msg))
        except Exception as e:
            print(f"[ConnectionThread] 处理消息异常: {e}", file=sys.stderr)
            traceback.print_exc()

    def _process_message(self, msg):
        """
        处理从服务端接收到的消息（字典格式），并转发给 UI 层。
        """
        try:
            mtype = msg['type']
            data = msg.get('data', {})
        except Exception:
            return

        # ---------- 各类消息处理 ----------
        if mtype == 'player_list':
            if self._last_ping_sent > 0:
                latency = (time.time() - self._last_ping_sent) * 1000
                self._latest_latency_ms = int(latency)
                print(f"[PING] 收到响应，延迟 {self._latest_latency_ms}ms，时间 {time.time()}")  # 新增
                self._last_ping_sent = 0
            self.online_list_updated.emit(data.get('players', []))
        elif mtype == 'mute_status':
            print(f"[调试] 收到 mute_status 消息，data={data}")
            try:
                if self.firefly:
                    self.firefly.on_mute_status(data.get('muted_until'))
                else:
                    print("[错误] firefly 为 None，无法处理禁言状态")
            except Exception as e:
                print(f"[调试] 处理 mute_status 异常: {e}")
                import traceback
                traceback.print_exc()
        elif mtype == 'birthday_mail_result':
            success = data.get('success', False)
            message = data.get('message', '')
            if success:
                self.firefly.current_user["last_birthday_greet_year"] = datetime.now().year
                self.firefly._save_current_user()
                # 可选：刷新邮箱
                self.firefly.mp_client._send({'type': 'get_mails'})
                show_notification_async("流萤的祝福", "生日邮件已发送至邮箱！", duration=3)
            else:
                print(f"生日邮件发送失败：{message}")
        elif mtype == 'chat_status':
            enabled = data.get('enabled', True)
            self.firefly.on_chat_status(enabled)
        elif mtype == 'ai_status':
            print("[DEBUG] Processing ai_status message")
            self.firefly.on_ai_status(data.get('enabled', True))
        elif mtype == 'chat_broadcast':
            # 直接转发给上层
            self.message_received.emit(msg)  # 注意：这里直接发射字典，不再是字符串
        elif mtype == 'rename_info':
            remaining = data.get('remaining', 1)
            next_time = data.get('next_time', "")
            if self.firefly and hasattr(self.firefly, '_pending_rename_dialog'):
                dlg = self.firefly._pending_rename_dialog
                dlg.set_remaining_changes(remaining, next_time)
        elif mtype == 'mention':
            from_name = data.get('from_name', '')
            if not from_name:
                return
            if data.get('all'):
                self.mentioned_all.emit(from_name)
            else:
                self.mentioned.emit(from_name)
        elif mtype == 'system_message':
            text = data.get('text', '')
            if '封禁' in text:
                self.banned_by_server.emit(text)
            # 组装成类似聊天广播的结构，方便统一处理
            fake_msg = {
                'type': 'chat_broadcast',
                'data': {
                    'from_name': '系统',
                    'text': text,
                    'is_system': True
                }
            }
            self.message_received.emit(fake_msg)
        elif mtype == 'login_fail':
            reason = data.get('reason', '未知原因')
            self.login_failed.emit(reason)
            self.disconnect()
        elif mtype == 'server_shutdown':
            reason = data.get('reason', '服务器已关闭')
            self.server_shutdown.emit(reason)
            self.disconnect()
        elif mtype == 'permission_changed':
            self.server_permission = data.get('permission', 'user')
            self.server_uid = data.get('server_uid', self.server_uid)
            new_uid = data.get('uid', '')
            print(f"[权限变更] 收到新 UID: {new_uid}")  # 控制台输出
            if new_uid:
                self.online_uid = new_uid  # ← 关键：更新 UID
            self.mp_client.server_uid = self.server_uid
            self.mp_client.server_permission = self.server_permission
            self.permission_changed.emit()
            self.create_menu()  # 刷新右键菜单
        elif mtype == 'chat_history_data':
            self.firefly.chat_history = data.get('history', [])
            if self.firefly._refresh_chat_pending:
                self.firefly._refresh_chat_pending = False
                self.firefly.refresh_chat_window()
        elif mtype == 'mails_data':
            self.mails_ready.emit(data.get('mails', []))
        elif mtype == 'claim_mail_result':
            self.claim_mail_result_signal.emit(data.get('success', False), data.get('reason', ''))
        elif mtype == 'redeem_result':
            self.redeem_result_signal.emit(data.get('success', False), data.get('message', ''))
        elif mtype == 'command_result':
            self.command_result_signal.emit(data.get('success', False), data.get('message', ''))
        elif mtype == 'check_name_result':
            available = data.get('available', False)
            reason = data.get('reason', '')
            # 通知 Firefly 或 ChangeNameDialog
            if self.firefly:
                self.firefly._on_check_name_result(available, reason)
        elif mtype == 'stats_data':
            stats = data.get('stats', {})
            self.stats = stats
            if self.firefly and self.firefly.current_user:
                for key, value in stats.items():
                    if key in self.firefly.current_user:
                        self.firefly.current_user[key] = value
                self.firefly._save_current_user()
            self.firefly.online_stats = stats
        elif mtype == 'sign_result':
            self.sign_result_signal.emit(data.get('success', False), data.get('message', ''))
        elif mtype == 'inventory_update':
            inv = data.get('inventory', {})
            self.firefly.online_inventory = inv
            today = datetime.now().strftime('%Y-%m-%d')
            self.firefly.online_signed_today = (inv.get('last_sign_date') == today)
            self.firefly.update_bag_display()
            # 处理信用点变化统计
            if hasattr(self.firefly, '_before_redeem_credit') and self.firefly._before_redeem_credit is not None:
                old_credit = self.firefly._before_redeem_credit
                new_credit = inv.get('credit', 0)
                diff = new_credit - old_credit
                if diff > 0:
                    self.firefly.check_month_reset()
                    self.firefly.current_user["monthly_credit_gained"] += diff
                    self.firefly._save_current_user()
                self.firefly._before_redeem_credit = None
        elif mtype == 'common_mute_status':
            enabled = data.get('enabled', False)
            self.firefly.on_common_mute_status(enabled)
        elif mtype == 'ai_status':
            self.firefly.ai_enabled = data.get('enabled', True)
        elif mtype == 'kick':
            # 直接转发踢出消息
            self.message_received.emit(msg)
        elif mtype == 'recall_notification':
            # 确保撤回通知直接转发，供 UI 处理
            self.message_received.emit(msg)
        else:
            # 其他未知消息，直接转发
            self.message_received.emit(msg)
        # 强制刷新聊天窗口（如果打开）
        if self.firefly.chat_window and self.firefly.chat_window.isVisible():
            self.firefly.chat_window.refresh_all_messages()


# ====================== 邮箱窗口 ======================
class MailboxWindow(QDialog):
    def __init__(self, firefly, parent=None, online_mails=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "📬")
        self.firefly = firefly
        self.online_mails = online_mails

        # 使用服务端邮件或本地邮件
        if online_mails is not None:
            self.mails = online_mails
            # 联机模式：从本地用户数据读取已读邮件ID列表
            self.local_read_mails = self.firefly.current_user.get("read_mails", [])
        else:
            self.mails = firefly.current_user.get("mailbox", [])
            self.local_read_mails = []

        self.setWindowTitle("邮箱")
        self.setFixedSize(820, 550)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QListWidget { background-color: #2D2D2D; border: 1px solid #555555; border-radius: 8px; color: #CCCCCC; font-size: 13px; padding: 5px; }
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 8px; padding: 8px 16px; font-size: 14px; }
            QPushButton:hover { background-color: #45A049; }
            QPushButton:disabled { background-color: #666666; }
        """)
        self.draggable = False
        self.offset = None
        self.init_ui()
        self.load_mails()
        self.apply_rounded_mask()
        if self.mail_list.count() > 0:
            self.mail_list.setCurrentRow(0)
            self.show_detail_for_current()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    # ---------- 鼠标事件（拖拽） ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.pos().y() <= 30:
            self.draggable = True
            self.offset = event.pos()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.draggable:
            self.move(event.globalPos() - self.offset)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.draggable = False
        else:
            super().mouseReleaseEvent(event)

    # ---------- 圆角遮罩 ----------
    def create_rounded_mask(self):
        radius = 15
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), radius, radius)
        return QRegion(path.toFillPolygon().toPolygon())

    def apply_rounded_mask(self):
        self.setMask(self.create_rounded_mask())

    # ---------- UI 初始化 ----------
    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(10)

        title = QLabel("📬 邮箱")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFFFFF;")
        main_layout.addWidget(title)

        content_layout = QHBoxLayout()
        content_layout.setSpacing(10)

        self.mail_list = QListWidget()
        self.mail_list.itemClicked.connect(self.on_mail_clicked)
        content_layout.addWidget(self.mail_list, 2)

        right_frame = QFrame()
        right_frame.setStyleSheet("""
            QFrame { background-color: #2D2D2D; border: 1px solid #555555; border-radius: 8px; }
            QTextEdit { background-color: #2D2D2D; border: none; color: #CCCCCC; font-size: 13px; padding: 10px; }
        """)
        right_layout_inner = QVBoxLayout(right_frame)
        right_layout_inner.setContentsMargins(0, 0, 0, 0)
        right_layout_inner.setSpacing(0)

        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        right_layout_inner.addWidget(self.detail_text)

        self.ticket_label = QLabel()
        self.ticket_label.setStyleSheet("color: #FFD700; font-size: 14px; margin: 8px;")
        self.ticket_label.setToolTip("来自列车的祝福")
        self.ticket_label.hide()
        right_layout_inner.addWidget(self.ticket_label)

        self.claim_btn = QPushButton("领取")
        self.claim_btn.setObjectName("claim_btn")
        self.claim_btn.setStyleSheet("""
            QPushButton#claim_btn { background-color: #FF9800; color: white; border: none; border-radius: 8px; padding: 8px 16px; font-size: 14px; margin: 8px; }
            QPushButton#claim_btn:hover { background-color: #F57C00; }
            QPushButton#claim_btn:disabled { background-color: #666666; }
        """)
        self.claim_btn.clicked.connect(self.claim_current_mail)
        self.claim_btn.setEnabled(False)
        right_layout_inner.addWidget(self.claim_btn, alignment=Qt.AlignRight)

        content_layout.addWidget(right_frame, 3)
        main_layout.addLayout(content_layout)

        btn_layout = QHBoxLayout()
        self.claim_all_btn = QPushButton("一键领取")
        self.claim_all_btn.clicked.connect(self.claim_all)

        self.read_all_btn = QPushButton("全部已读")
        self.read_all_btn.clicked.connect(self.read_all)

        self.delete_read_btn = QPushButton("删除已读")
        self.delete_read_btn.clicked.connect(self.delete_read_mails)
        # 联机模式禁用删除已读
        if self.online_mails is not None:
            self.delete_read_btn.setEnabled(False)
            self.delete_read_btn.setToolTip("联机模式暂不支持删除邮件")

        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)

        btn_layout.addWidget(self.claim_all_btn)
        btn_layout.addWidget(self.read_all_btn)
        btn_layout.addWidget(self.delete_read_btn)
        btn_layout.addWidget(self.close_btn)
        main_layout.addLayout(btn_layout)

        self.setLayout(main_layout)

    # ---------- 加载邮件列表 ----------
    def load_mails(self):
        self.mail_list.blockSignals(True)
        self.mail_list.clear()
        for i, mail in enumerate(reversed(self.mails)):
            title = mail["title"]

            # 联机模式：从本地已读列表恢复状态
            if self.online_mails is not None and mail["id"] in self.local_read_mails:
                mail["read"] = True

            if not mail.get("read"):
                title = "● " + title
            if mail.get("claimed"):
                title += " [已领取]"

            item = QListWidgetItem(title)
            # 已领取邮件灰色且不可选
            if mail.get("claimed"):
                item.setForeground(QColor(128, 128, 128))
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            item.setData(Qt.UserRole, len(self.mails)-1-i)
            self.mail_list.addItem(item)
        self.mail_list.blockSignals(False)

    def get_mail_by_item(self, item):
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        if index is None or index < 0 or index >= len(self.mails):
            return None
        return self.mails[index]

    # ---------- 点击邮件 ----------
    def on_mail_clicked(self, item):
        mail = self.get_mail_by_item(item)
        if mail and mail.get("claimed"):
            return  # 已领取不显示详情
        self.show_detail_for_item(item)

    def show_detail_for_current(self):
        item = self.mail_list.currentItem()
        self.show_detail_for_item(item)

    def show_detail_for_item(self, item):
        mail = self.get_mail_by_item(item)
        if mail is None:
            self.detail_text.clear()
            self.ticket_label.hide()
            self.claim_btn.setEnabled(False)
            self.claim_btn.setText("领取")
            return

        # 联机模式：记录已读状态
        if self.online_mails is not None and not mail.get("read"):
            mail["read"] = True
            if mail["id"] not in self.local_read_mails:
                self.local_read_mails.append(mail["id"])
                # 保存到用户数据（也可在 closeEvent 统一保存，此处即时保存）
                self.firefly.current_user["read_mails"] = self.local_read_mails
                self.firefly._save_current_user()
            # 更新列表项文字（去除 "●"）
            self.mail_list.blockSignals(True)
            row = self.mail_list.row(item)
            title = mail["title"]
            if mail.get("claimed"):
                title += " [已领取]"
            item.setText(title)
            self.mail_list.blockSignals(False)

        # 构建详情（HTML格式）
        items_html = []
        has_ticket = False
        for it in mail["items"]:
            if it["type"] == "cake":
                items_html.append(f"橡木蛋糕卷 x{it['amount']}")
            elif it["type"] == "credit":
                items_html.append(f"信用点 x{it['amount']}")
            elif it["type"] == "clothes":
                clothes_info = AVAILABLE_CLOTHES.get(it["id"], {})
                name = clothes_info.get('name', it['id'])
                items_html.append(f'时装 [<font color="#FFD700">{name}</font>]')
            elif it["type"] == "star_rail_ticket":
                has_ticket = True
                items_html.append(f'<font color="#FFD700">星铁专票 x{it["amount"]}</font>')

        items_text = ', '.join(items_html) if items_html else "无"
        status = "已领取" if mail.get("claimed") else "未领取"
        detail_html = f"""
        <div style="color: #CCCCCC; font-size: 13px;">
        发件人：{mail['sender']}<br>
        时间：{mail['timestamp'][:19]}<br>
        状态：{status}<br>
        标题：{mail['title']}<br>
        ──────────────────<br>
        {mail['content']}<br>
        ──────────────────<br>
        物品：{items_text}
        </div>
        """
        self.detail_text.setHtml(detail_html)

        if has_ticket:
            self.ticket_label.setText("星铁专票 x1")
            self.ticket_label.show()
        else:
            self.ticket_label.hide()

        # 无物品邮件禁用领取按钮
        if not mail.get("items"):
            self.claim_btn.setEnabled(False)
            self.claim_btn.setText("无需领取")
        else:
            self.claim_btn.setEnabled(not mail.get("claimed"))
            self.claim_btn.setText("领取")

    # ---------- 领取当前邮件 ----------
    def claim_current_mail(self):
        item = self.mail_list.currentItem()
        mail = self.get_mail_by_item(item)
        if mail is None or mail.get("claimed"):
            return

        # 无物品邮件提示
        if not mail.get("items"):
            QMessageBox.information(self, "提示", "该邮件无需领取。")
            return

        if self.online_mails is not None:
            # 联机：发送领取请求
            self.firefly.mp_client._send({'type': 'claim_mail', 'data': {'mail_id': mail['id']}})
            self._pending_credit = sum(item['amount'] for item in mail['items'] if item['type'] == 'credit')
        else:
            # 单机：直接领取
            success, desc = claim_mail_items(self.firefly.current_user, mail)
            if success:
                for item in mail['items']:
                    if item['type'] == 'credit':
                        self.firefly.check_month_reset()
                        self.firefly.current_user["monthly_credit_gained"] += item['amount']
                self.firefly._save_current_user()
                self.firefly.update_bag_display()
                # 更新列表和详情
                title = mail["title"] + " [已领取]"
                self.mail_list.blockSignals(True)
                item.setText(title)
                self.mail_list.blockSignals(False)
                self.show_detail_for_item(item)
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "成功", f"物品已领取！获得 {', '.join(desc)}"))

    # ---------- 一键领取 ----------
    def claim_all(self):
        for mail in self.mails:
            if not mail.get("claimed") and mail.get("items"):
                if self.online_mails is not None:
                    self.firefly.mp_client._send({'type': 'claim_mail', 'data': {'mail_id': mail['id']}})
                else:
                    claim_mail_items(self.firefly.current_user, mail)
        if self.online_mails is None:
            self.firefly._save_current_user()
            self.firefly.update_bag_display()
            self.load_mails()
            self.show_detail_for_current()
            QTimer.singleShot(0, lambda: QMessageBox.information(self, "提示", "已领取所有未领取邮件"))
        else:
            # 联机模式：关闭窗口（由服务端推送更新）
            self.close()

    # ---------- 全部已读 ----------
    def read_all(self):
        for mail in self.mails:
            mail["read"] = True
            # 联机模式同步记录到本地列表
            if self.online_mails is not None and mail["id"] not in self.local_read_mails:
                self.local_read_mails.append(mail["id"])
        if self.online_mails is None:
            self.firefly._save_current_user()
        else:
            self.firefly.current_user["read_mails"] = self.local_read_mails
            self.firefly._save_current_user()
        self.load_mails()
        self.show_detail_for_current()

    # ---------- 删除已读邮件（单机） ----------
    def delete_read_mails(self):
        if self.online_mails is not None:
            QMessageBox.information(self, "提示", "联机模式下邮件由服务器管理，暂不支持删除。")
            return

        to_delete = [mail for mail in self.mails if mail.get("read")]
        if not to_delete:
            QMessageBox.information(self, "提示", "没有已读邮件可删除。")
            return

        reply = QMessageBox.question(self, "确认删除", f"确定要删除 {len(to_delete)} 封已读邮件吗？",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.No:
            return

        for mail in to_delete:
            self.mails.remove(mail)

        self.firefly.current_user["mailbox"] = self.mails
        self.firefly._save_current_user()

        self.load_mails()
        self.detail_text.clear()
        self.ticket_label.hide()
        self.claim_btn.setEnabled(False)
        self.claim_btn.setText("领取")
        QMessageBox.information(self, "成功", f"已删除 {len(to_delete)} 封已读邮件。")

    # ---------- 关闭窗口 ----------
    def closeEvent(self, event):
        # 联机模式保存已读列表
        if self.online_mails is not None:
            self.firefly.current_user["read_mails"] = self.local_read_mails
            self.firefly._save_current_user()
        event.accept()


# ====================== 兑换码输入对话框 ======================
class RedeemCodeDialog(QDialog):
    code_submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🎟️")
        self.setWindowTitle("使用兑换码")
        self.setFixedSize(350, 150)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit {
                border: 1px solid #555555; border-radius: 8px; padding: 8px;
                background-color: #2D2D2D; color: #888888; font-size: 14px;
            }
            QLineEdit:focus { border-color: #4CAF50; }
            QPushButton {
                background-color: #4CAF50; color: white; border: none;
                border-radius: 8px; padding: 8px 16px; font-size: 14px;
            }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        label = QLabel("请输入兑换码或命令：")
        layout.addWidget(label)

        self.code_edit = QLineEdit()
        # ---------- 动态命令补全 ----------
        firefly = parent
        is_admin = False
        is_online = False
        if firefly:
            if hasattr(firefly, 'mp_client') and firefly.mp_client.connected:
                is_online = True
                is_admin = (firefly.mp_client.server_permission == 'admin')
            else:
                uid = firefly.current_user.get("uid", "") if hasattr(firefly, 'current_user') else ""
                is_admin = uid.lower().startswith("admin")

        # 通用命令（所有用户）
        common_cmds = ['/help', '/move']
        # 管理员命令（单机可用）
        admin_cmds = ['/give', '/set', '/clear','revoke','/revoke code', '/set newcode']
        # 联机管理命令（仅联机管理员，不含时区）
        online_cmds = ['/op', '/deop', '/ban', '/unban', '/kick']

        cmd_list = common_cmds[:]
        if is_admin:
            cmd_list.extend(admin_cmds)
            if is_online:
                cmd_list.extend(online_cmds)

        completer = QCompleter(cmd_list)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchStartsWith)
        self.code_edit.setCompleter(completer)
        # ------------------------------------

        layout.addWidget(self.code_edit)

        btn_layout = QHBoxLayout()
        self.ok_btn = QPushButton("确定")
        self.cancel_btn = QPushButton("取消")
        self.ok_btn.clicked.connect(self.on_ok)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def on_ok(self):
        code = self.code_edit.text().strip()
        if not code:
            QMessageBox.warning(self, "提示", "请输入兑换码")
            return
        self.code_submitted.emit(code)
        self.code_edit.clear()  # 清空以便继续输入

class RoundedMenu(QMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(MENU_STYLE)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            super().mouseReleaseEvent(event)

    def showEvent(self, event):
        # 仅调用父类，不处理任何统计逻辑
        super().showEvent(event)
        # 只处理 Firefly 实例，避免被子类或其他对象误调用
        if not isinstance(self, Firefly):
            super().showEvent(event)  # 调用父类（QWidget）的 showEvent
            return

        super().showEvent(event)
        if not self._initialized:
            try:
                self.apply_cursor_style()
                self.apply_pet_size()
                self._init_movie_from_file()
                self.hi_music()
                self._initialized = True
            except Exception as e:
                print(f"初始化资源失败: {e}")

        # 计数逻辑（仅对 Firefly 实例有效）
        self.check_month_reset()
        self.current_user["total_open_count"] += 1
        self.current_user["monthly_open_count"] += 1
        self._save_current_user()

class DarkRoundedMenu(RoundedMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QMenu{
                border: 1px solid #3a3a3a;
                background-color: #2b2b2b;
                padding: 4px;
                border-radius: 8px;
            }
            QMenu::item{
                padding: 6px 20px;
                margin: 2px 4px;
                border-radius: 4px;
                color: #f0f0f0;
                background-color: transparent;
            }
            QMenu::item:selected{
                background-color: #4a6a8a;
                color: #ffffff;
            }
            QMenu::separator{
                height: 1px;
                background-color: #4a4a4a;
                margin: 4px 8px;
            }
        """)

# ====================== 兑换明细窗口 ======================
class HistoryWindow(QDialog):
    def __init__(self, user_data, is_admin=False, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "📋")
        self.user_data = user_data
        self.is_admin = is_admin
        self.setWindowTitle("兑换明细")
        self.setFixedSize(550, 400)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #888888;
                font-size: 14px;
            }
            QListWidget {
                background-color: #2D2D2D;
                border: 1px solid #555555;
                border-radius: 8px;
                color: #CCCCCC;
                font-size: 12px;
                padding: 5px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("历史记录")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

        self.setLayout(layout)
        self.load_history()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def load_history(self):
        print(f"[聊天窗口] 加载历史，共 {len(self.firefly.chat_history)} 条")
        history = self.user_data.get("redeem_history", [])
        for record in history:
            self.list_widget.addItem(record)
        if not history:
            self.list_widget.addItem("暂无记录")


# ====================== 商店窗口 ======================
class ShopWindow(QDialog):
    def __init__(self, firefly, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🛒")
        self.firefly = firefly
        self.setWindowTitle("商店 - 橡木蛋糕卷")
        self.setFixedSize(550, 420)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 12px;
            }
            QLabel {
                color: #888888;
                font-size: 14px;
            }
            QLineEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 8px;
                background-color: #2D2D2D;
                color: #888888;
                font-size: 14px;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QSpinBox {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 5px;
                background-color: #2D2D2D;
                color: #888888;
                font-size: 14px;
            }
        """)
        self.init_ui()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def get_inventory(self):
        """获取当前应显示的库存（单机/联机）"""
        if self.firefly.mp_client.connected and self.firefly.online_inventory is not None:
            return self.firefly.online_inventory
        return self.firefly.current_user

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("信用点商店")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)

        image_label = QLabel()
        possible_paths = [
            "assets/images/foods/Cake.png",
            "./assets/images/foods/Cake.png",
            os.path.join(os.path.dirname(sys.argv[0]), "assets/images/foods/Cake.png")
        ]
        pixmap = None
        for path in possible_paths:
            if os.path.exists(path):
                pixmap = QPixmap(path)
                break
        if pixmap and not pixmap.isNull():
            pixmap = pixmap.scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            image_label.setPixmap(pixmap)
            image_label.setAlignment(Qt.AlignCenter)
        else:
            image_label.setText("📦 橡木蛋糕卷")
            image_label.setAlignment(Qt.AlignCenter)
            image_label.setStyleSheet("font-size: 40px;")
        layout.addWidget(image_label)

        price_label = QLabel("单价：3600 信用点 / 个")
        price_label.setAlignment(Qt.AlignCenter)
        price_label.setStyleSheet("font-size: 16px; color: #FFD700;")
        layout.addWidget(price_label)

        self.credit_label = QLabel()
        self.credit_label.setAlignment(Qt.AlignCenter)
        self.credit_label.setStyleSheet("font-size: 14px; color: #AAAAAA;")
        layout.addWidget(self.credit_label)

        amount_layout = QHBoxLayout()
        amount_label = QLabel("购买数量：")
        self.amount_spin = QSpinBox()
        self.amount_spin.setRange(1, 1)
        self.amount_spin.setValue(1)
        self.amount_spin.setSuffix(" 个")
        self.amount_spin.valueChanged.connect(self.update_total)
        amount_layout.addWidget(amount_label)
        amount_layout.addWidget(self.amount_spin)
        layout.addLayout(amount_layout)

        self.total_price_label = QLabel("总计：3600 信用点")
        self.total_price_label.setAlignment(Qt.AlignCenter)
        self.total_price_label.setStyleSheet("font-size: 14px; color: #AAAAAA;")
        layout.addWidget(self.total_price_label)

        buy_btn = QPushButton("购买")
        buy_btn.clicked.connect(self.buy)
        layout.addWidget(buy_btn)

        self.setLayout(layout)
        self.refresh_display()

    def refresh_display(self):
        inv = self.get_inventory()
        credit = inv.get("credit", 0)
        self.credit_label.setText(f"当前信用点：{credit}")
        max_can_buy = credit // 3600
        if max_can_buy < 1:
            max_can_buy = 1
        self.amount_spin.setMaximum(max_can_buy)
        self.update_total()

    def update_total(self):
        amount = self.amount_spin.value()
        total = amount * 3600
        self.total_price_label.setText(f"总计：{total} 信用点")
        credit = self.get_inventory().get("credit", 0)
        if total > credit:
            self.total_price_label.setStyleSheet("font-size: 14px; color: #FF5555;")
        else:
            self.total_price_label.setStyleSheet("font-size: 14px; color: #AAAAAA;")

    def buy(self):
        amount = self.amount_spin.value()
        total_credit = amount * 3600
        inv = self.get_inventory()
        credit = inv.get("credit", 0)
        if credit < total_credit:
            QMessageBox.warning(self, "提示", f"信用点不足！需要 {total_credit} 信用点，当前拥有 {credit} 点。")
            return
        inv["credit"] = credit - total_credit
        inv["balance"] = inv.get("balance", 0) + amount

        if self.firefly.mp_client.connected and self.firefly.online_inventory is not None:
            self.firefly.mp_client._send(
                {'type': 'save_inventory', 'data': {'inventory': self.firefly.online_inventory}})
        else:
            self.firefly._save_current_user()

        # === 新增统计 ===
        self.firefly.check_month_reset()
        self.firefly.current_user["monthly_cake_gained"] += amount
        self.firefly._save_current_user()

        history = self.firefly.current_user.get("redeem_history", [])
        history.append(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 商店购买 {amount} 个橡木蛋糕卷，花费 {total_credit} 信用点")
        self.firefly.current_user["redeem_history"] = history
        if not self.firefly.mp_client.connected:
            self.firefly._save_current_user()

        QMessageBox.information(self, "成功", f"购买成功！获得 {amount} 个橡木蛋糕卷，剩余信用点：{inv['credit']}")
        self.accept()
        self.firefly.update_bag_display()


# ====================== 重置密码窗口 ======================
class ResetPasswordWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🔒")
        self.setWindowTitle("重置密码")
        self.setFixedSize(400, 520)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                font-size: 14px;
                color: #888888;
                background: transparent;
            }
            QLineEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
                background-color: #2D2D2D;
                color: #888888;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
                outline: none;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px;
                font-size: 15px;
                font-weight: 500;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            QPushButton:hover {
                background-color: #45A049;
                box-shadow: 0 4px 8px rgba(0,0,0,0.15);
            }
            QPushButton:pressed {
                background-color: #3D8B40;
            }
            QDateEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
                background-color: #2D2D2D;
                color: #888888;
            }
            QDateEdit::drop-down {
                border: none;
                subcontrol-origin: padding;
                subcontrol-position: right center;
                width: 20px;
            }
            QDateEdit::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #888888;
                width: 0px;
                height: 0px;
                margin-right: 8px;
            }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(15)
        title = QLabel("重置密码")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:22px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("请输入用户名或用户ID")
        layout.addWidget(QLabel("账号："))
        layout.addWidget(self.user_input)
        self.new_pwd_input = QLineEdit()
        self.new_pwd_input.setPlaceholderText("请输入新密码")
        self.new_pwd_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("新密码："))
        layout.addWidget(self.new_pwd_input)
        self.confirm_pwd_input = QLineEdit()
        self.confirm_pwd_input.setPlaceholderText("请再次输入新密码")
        self.confirm_pwd_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("确认新密码："))
        layout.addWidget(self.confirm_pwd_input)
        reset_btn = QPushButton("重置密码")
        reset_btn.clicked.connect(self.do_reset)
        layout.addWidget(reset_btn)
        spacer = QSpacerItem(20, 80, QSizePolicy.Minimum, QSizePolicy.Expanding)
        layout.addItem(spacer)
        self.setLayout(layout)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def do_reset(self):
        key = self.user_input.text().strip()
        new_pwd = self.new_pwd_input.text().strip()
        confirm_pwd = self.confirm_pwd_input.text().strip()
        if not key or not new_pwd or not confirm_pwd:
            QMessageBox.warning(self, "提示", "请填写完整信息")
            return
        if new_pwd != confirm_pwd:
            QMessageBox.warning(self, "提示", "两次密码输入不一致")
            return
        user_file = None
        user_data = None
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith(".json"):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except:
                    continue
                if not isinstance(data, dict):
                    continue  # 跳过非字典格式的文件
                if isinstance(data, dict) and (data.get("username") == key or data.get("userid") == key):
                    user_file = filepath
                    user_data = data
                    break
        if user_file is None:
            QMessageBox.warning(self, "提示", "账号不存在")
            return
        user_data["password"] = new_pwd
        user_data["reset_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_data["redeem_history"] = user_data.get("redeem_history", [])
        user_data["redeem_history"].append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 重置密码")
        with open(user_file, "w", encoding="utf-8") as f:
            json.dump(user_data, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "成功", "密码重置成功！请使用新密码登录")
        self.accept()


# ====================== 注册窗口 ======================
class RegisterWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "📝")
        self.setWindowTitle("注册账号")
        self.setFixedSize(400, 560)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                font-size: 14px;
                color: #888888;
                background: transparent;
            }
            QLineEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
                background-color: #2D2D2D;
                color: #888888;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
                outline: none;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px;
                font-size: 15px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QDateEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                background-color: #2D2D2D;
                color: #888888;
            }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(12)
        title = QLabel("用户注册")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("用户名（唯一）")
        layout.addWidget(QLabel("用户名："))
        layout.addWidget(self.user_input)
        self.id_input = QLineEdit()
        self.id_input.setPlaceholderText("用户ID（长度<20，仅字母数字下划线）")
        layout.addWidget(QLabel("用户ID："))
        layout.addWidget(self.id_input)
        self.pwd_input = QLineEdit()
        self.pwd_input.setPlaceholderText("密码")
        self.pwd_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("密码："))
        layout.addWidget(self.pwd_input)
        self.pwd2_input = QLineEdit()
        self.pwd2_input.setPlaceholderText("确认密码")
        self.pwd2_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("确认密码："))
        layout.addWidget(self.pwd2_input)
        self.birth_input = QDateEdit()
        self.birth_input.setCalendarPopup(True)
        self.birth_input.setDate(QDate.currentDate().addYears(-18))
        layout.addWidget(QLabel("生日："))
        layout.addWidget(self.birth_input)
        reg_btn = QPushButton("完成注册")
        reg_btn.clicked.connect(self.do_register)
        layout.addWidget(reg_btn)
        self.setLayout(layout)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def generate_unique_uid(self):
        existing_uids = set()
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith(".json"):
                filepath = os.path.join(USER_DATA_DIR, filename)
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "uid" in data:
                        existing_uids.add(data["uid"])
        while True:
            rand_num = str(random.randint(0, 10 ** 9 - 1)).zfill(9)
            uid_candidate = f"user-{rand_num}"
            if uid_candidate not in existing_uids:
                return uid_candidate

    def do_register(self):
        username = self.user_input.text().strip()
        userid = self.id_input.text().strip()
        pwd = self.pwd_input.text().strip()
        pwd2 = self.pwd2_input.text().strip()
        birth = self.birth_input.date().toString("yyyy-MM-dd")
        if not username or not userid or not pwd or not pwd2:
            QMessageBox.warning(self, "提示", "请填写完整信息")
            return
        if pwd != pwd2:
            QMessageBox.warning(self, "提示", "两次密码不一致")
            return
        if len(userid) >= 20:
            QMessageBox.warning(self, "提示", "用户ID长度必须小于20")
            return
        if not re.match(r'^[a-zA-Z0-9_]+$', userid):
            QMessageBox.warning(self, "提示", "用户ID只能包含字母、数字和下划线")
            return
        userid_path = os.path.join(USER_DATA_DIR, f"{userid}.json")
        if os.path.exists(userid_path):
            QMessageBox.warning(self, "提示", "用户ID已存在")
            return
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except:
                    continue
                if not isinstance(existing, dict):
                    continue
                if existing.get("username") == username:
                    QMessageBox.warning(self, "提示", "用户名已存在")
                    return
        uid = self.generate_unique_uid()
        initial_credit = 99999 if uid.lower().startswith("trailblazer") else 0
        data = {
            "uid": uid,
            "username": username,
            "userid": userid,
            "password": pwd,
            "birthday": birth,
            "register_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "balance": 20,
            "credit": initial_credit,
            "last_sign_date": None,
            "banned": False,
            "banned_until": None,
            "redeemed_codes": [],
            "redeem_history": [f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 注册账号，获得20橡木蛋糕卷" + (
                f"，获得{initial_credit}信用点" if initial_credit > 0 else "")],
            "cursor_style": "None",
            "pet_size": "normal",
            "owned_clothes": ["normal"],
            "mailbox": [],
            "star_rail_tickets": 0,
            "last_birthday_greet_year": None,
            "volume": 0.5
        }
        with open(userid_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "成功", f"注册成功！\n用户ID：{userid}\nUID：{uid}")
        self.accept()


# ====================== 登录窗口 ======================
class LoginWindow(QDialog):
    login_success = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🔑")
        self.setWindowTitle("用户登录")
        self.setFixedSize(400, 520)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                font-size: 14px;
                color: #888888;
                background: transparent;
            }
            QLineEdit {
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
                background-color: #2D2D2D;
                color: #888888;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
                outline: none;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 12px;
                font-size: 15px;
                font-weight: 500;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }
            QPushButton:hover {
                background-color: #45A049;
                box-shadow: 0 4px 8px rgba(0,0,0,0.15);
            }
            QPushButton:pressed {
                background-color: #3D8B40;
            }
            QCheckBox {
                font-size: 13px;
                color: #888888;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 8px;
                border: 2px solid #555555;
                background-color: #2D2D2D;
            }
            QCheckBox::indicator:hover {
                border-color: #4CAF50;
            }
            QCheckBox::indicator:checked {
                background-color: #4CAF90;
                border-color: #FFFFFF;
            }
            QLabel#forgetPwdLabel {
                color: #4CAF50;
                font-size: 13px;
                text-decoration: underline;
            }
            QLabel#forgetPwdLabel:hover {
                color: #45A049;
                cursor: pointer;
            }
        """)
        self.current_user = None
        self.init_ui()
        self.load_remember()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())


    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(15)

        title = QLabel("登录")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:22px; font-weight:bold; color:#FFFFFF;")
        layout.addWidget(title)

        logo_label = QLabel()
        pixmap = QPixmap("./assets/images/icon/Login_img.png")
        pixmap = pixmap.scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        transparent_pixmap = QPixmap(pixmap.size())
        transparent_pixmap.fill(Qt.transparent)
        painter = QPainter(transparent_pixmap)
        painter.setOpacity(0.5)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        logo_label.setPixmap(transparent_pixmap)
        logo_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo_label)

        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("用户名 / 用户ID")
        layout.addWidget(QLabel("账号："))
        layout.addWidget(self.user_edit)

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setPlaceholderText("密码")
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        layout.addWidget(QLabel("密码："))
        layout.addWidget(self.pwd_edit)

        remember_forget_layout = QHBoxLayout()
        self.remember = QCheckBox("记住登录状态")
        self.remember.setStyleSheet("color:#888888;")
        self.forget_pwd_label = QLabel("忘记密码？")
        self.forget_pwd_label.setObjectName("forgetPwdLabel")
        self.forget_pwd_label.setAlignment(Qt.AlignRight)
        self.forget_pwd_label.mousePressEvent = self.open_reset_password
        remember_forget_layout.addWidget(self.remember)
        remember_forget_layout.addWidget(self.forget_pwd_label)
        layout.addLayout(remember_forget_layout)

        btn_layout = QHBoxLayout()
        login_btn = QPushButton("登录")
        reg_btn = QPushButton("注册")
        login_btn.clicked.connect(self.do_login)
        reg_btn.clicked.connect(self.open_reg)
        btn_layout.addWidget(login_btn)
        btn_layout.addWidget(reg_btn)
        layout.addLayout(btn_layout)

        spacer = QSpacerItem(20, 60, QSizePolicy.Minimum, QSizePolicy.Expanding)
        layout.addItem(spacer)
        self.setLayout(layout)

    def open_reset_password(self, event):
        reset_win = ResetPasswordWindow(self)
        reset_win.open()

    def closeEvent(self, event):
        event.accept()

    def load_remember(self):
        if not os.path.exists(USER_CONFIG):
            return
        try:
            with open(USER_CONFIG, encoding="utf-8") as f:
                d = json.load(f)
                if d.get("remember"):
                    self.user_edit.setText(d.get("userid", ""))
                    self.pwd_edit.setText(d.get("password", ""))
                    self.remember.setChecked(True)
        except:
            pass

    def save_remember(self):
        d = {
            "userid": self.current_user["userid"],
            "password": self.current_user["password"],
            "remember": self.remember.isChecked()
        }
        with open(USER_CONFIG, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    def open_reg(self):
        reg_win = RegisterWindow(self)
        reg_win.open()

    def do_login(self):
        import traceback
        log_path = os.path.join(os.environ.get('TEMP', '.'), 'firefly_crash.log')
        try:
            key = self.user_edit.text().strip()
            pwd = self.pwd_edit.text().strip()
            if not key or not pwd:
                QMessageBox.warning(self, "提示", "账号和密码不能为空")
                return

            found_user = None
            user_filepath = None
            for filename in os.listdir(USER_DATA_DIR):
                if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                    filepath = os.path.join(USER_DATA_DIR, filename)
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception as e:
                        with open(log_path, 'a') as log:
                            log.write(f"跳过损坏文件 {filename}: {e}\n")
                        continue
                    # 关键修复：只处理字典格式的数据
                    if not isinstance(data, dict):
                        with open(log_path, 'a') as log:
                            log.write(f"跳过非字典文件 {filename}, 类型: {type(data)}\n")
                        continue
                    if data.get("username") == key or data.get("userid") == key:
                        found_user = data
                        user_filepath = filepath
                        break

            if found_user is None:
                QMessageBox.warning(self, "错误", "账号不存在")
                return
            if found_user["password"] != pwd:
                QMessageBox.warning(self, "错误", "密码错误")
                return

            # 修复用户字段（同样确保是字典）
            try:
                found_user = ensure_user_fields(found_user, user_filepath)
            except Exception as e:
                with open(log_path, 'a') as log:
                    log.write(f"ensure_user_fields 失败: {e}\n{traceback.format_exc()}\n")
                QMessageBox.critical(self, "错误", "用户数据修复失败，请联系管理员")
                return

            # 封禁检查（略，保持不变）
            banned_until = found_user.get("banned_until")
            if banned_until:
                try:
                    until_date = datetime.fromisoformat(banned_until)
                    if datetime.now() < until_date:
                        remaining = until_date - datetime.now()
                        days = remaining.days
                        QMessageBox.warning(self, "提示",
                                            f"此用户已被封禁，剩余 {days} 天，解封日期：{until_date.strftime('%Y-%m-%d')}")
                        return
                    else:
                        found_user["banned"] = False
                        found_user["banned_until"] = None
                        with open(user_filepath, "w", encoding="utf-8") as f:
                            json.dump(found_user, f, ensure_ascii=False, indent=2)
                except:
                    pass
            if found_user.get("banned"):
                QMessageBox.warning(self, "提示", "此用户已被禁止使用流萤桌宠")
                return

            self.current_user = found_user
            self.save_remember()
            self.login_success.emit(found_user)
            self.accept()

        except Exception as e:
            with open(log_path, 'a') as log:
                log.write(f"登录过程未捕获异常: {e}\n{traceback.format_exc()}\n")
            QMessageBox.critical(self, "严重错误", f"登录时发生未知错误：{str(e)}\n详情已写入 {log_path}")

class GlobalHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, firefly):
        super().__init__()
        self.firefly = firefly
        # 注册全局热键 Ctrl+Tab（避免与系统 Tab 冲突，改用 Ctrl+Tab，可自行调整为纯 Tab）
        self.id = 1
        ctypes.windll.user32.RegisterHotKey(None, self.id, MOD_CONTROL | MOD_NOREPEAT, VK_TAB)

    def nativeEventFilter(self, eventType, message):
        # 防御性检查：如果 message 为 None，直接返回未处理
        if message is None:
            return False, 0
        try:
            # 将消息指针转换为整数，再构造 MSG 结构
            msg_ptr = int(message)
            msg = ctypes.wintypes.MSG.from_address(msg_ptr)
            if msg.message == WM_HOTKEY and msg.wParam == self.id:
                if self.firefly.mp_client.connected:
                    self.firefly.toggle_online_list()
                return True, 0
        except Exception as e:
            # 记录异常但继续运行，避免程序崩溃
            print(f"[热键] 处理消息异常: {e}")
        # 默认返回未处理
        return False, 0

    def unregister(self):
        ctypes.windll.user32.UnregisterHotKey(None, self.id)

class ChangeNameDialog(QDialog):
    def __init__(self, parent=None, current_name="", remaining_changes=1, server_timezone_offset=0):
        super().__init__(parent)
        self.parent_firefly = parent
        self.current_name = current_name
        self.remaining_changes = remaining_changes
        self.server_timezone_offset = server_timezone_offset
        self.all_names = []  # 从服务端获取的所有在线玩家名字
        self._next_time = ""

        set_window_emoji_icon(self, "✏️")
        self.setWindowTitle("修改昵称")
        self.setFixedSize(460, 300)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit {
                background-color: #2D2D2D;
                color: white;
                border: 1px solid #555;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
            QLineEdit:focus { border-color: #4CAF50; }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #45A049; }
            QPushButton:disabled { background-color: #666666; }
        """)
        self.init_ui()
        self.request_player_list()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def on_check_name_result(self, available, reason):
        if available:
            self.status_label.setText('<span style="color:green;">昵称可用</span>')
            self.ok_btn.setEnabled(True)
        else:
            self.status_label.setText(f'<span style="color:red;">{reason}</span>')
            self.ok_btn.setEnabled(False)

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        # 当前昵称
        layout.addWidget(QLabel("当前昵称:"))
        current_lbl = QLabel(self.current_name)
        current_lbl.setStyleSheet("color: #CCCCCC; font-size: 14px;")
        layout.addWidget(current_lbl)

        # 新昵称输入
        layout.addWidget(QLabel("新昵称 (最多16字符):"))
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(16)
        self.name_edit.setFixedHeight(32)
        self.name_edit.textChanged.connect(self.on_text_changed)
        layout.addWidget(self.name_edit)

        # 状态提示（小字）
        self.status_label = QLabel()
        self.status_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.status_label)

        # 剩余次数（小字）
        self.remaining_label = QLabel()
        self.update_remaining_label()
        layout.addWidget(self.remaining_label)

        # 按钮
        btn_layout = QHBoxLayout()
        self.ok_btn = QPushButton("确定")
        self.ok_btn.clicked.connect(self.on_ok_clicked)
        self.ok_btn.setEnabled(False)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def on_ok_clicked(self):
        """点击确定按钮时的处理：弹出确认框，确认后提交"""
        new_name = self.get_new_name()
        if not new_name:
            return

        # 构建确认消息，新昵称用绿色高亮
        msg = f'是否确定更改为：<span style="color:green; font-weight:bold;">{new_name}</span>'
        reply = QMessageBox.question(
            self,
            "确认改名",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.accept()

    def request_player_list(self):
        """请求在线玩家列表以检查重名"""
        if self.parent_firefly and hasattr(self.parent_firefly, 'mp_client'):
            self.parent_firefly.mp_client.request_player_list()

    def update_player_list(self, players):
        """更新在线玩家列表"""
        # 从 players 中提取名字，排除自己
        self.all_names = [p.get('name', '') for p in players if p.get('name') and p.get('name') != self.current_name]
        # 重新检查当前输入
        self.on_text_changed(self.name_edit.text())

    def update_remaining_label(self):
        """更新剩余状态显示（可修改/冷却中）"""
        if self.remaining_changes >= 1:
            color = "green"
            text = "✅ 昵称可修改"
        else:
            color = "red"
            text = "⏳ 修改冷却中"
        self.remaining_label.setText(f'<span style="color:{color};">{text}</span>')

    def on_text_changed(self, text):
        new_name = text.strip()
        if not new_name:
            self.status_label.setText("")
            self.ok_btn.setEnabled(False)
            return

        if not Firefly.is_valid_nickname(new_name):
            if new_name.startswith("开拓者-") and new_name != "开拓者":
                self.status_label.setText('<span style="color:red;">此昵称不合法</span>')
            else:
                self.status_label.setText('<span style="color:red;">昵称不合法</span>')
            self.ok_btn.setEnabled(False)
            return

        # 异步检查重名（通过父 Firefly 发送）
        if self.parent_firefly and hasattr(self.parent_firefly, 'mp_client'):
            self.ok_btn.setEnabled(False)
            self.status_label.setText('<span style="color:#AAAAAA;">检查中...</span>')
            self.parent_firefly.mp_client._send({'type': 'check_name', 'data': {'new_name': new_name}})

    def get_new_name(self):
        return self.name_edit.text().strip()

    def get_remaining_changes(self):
        return self.remaining_changes

    def set_remaining_changes(self, remaining, next_time_str=""):
        self.remaining_changes = remaining
        self._next_time = next_time_str  # 保存以便后续使用
        self.update_remaining_label()
        if remaining <= 0:
            self.ok_btn.setEnabled(False)
            # 使用换行符 \n 将日期放在第二行
            self.status_label.setText(
                f'<span style="color:red;">冷却期未到！\n下次可修改时间为 {next_time_str}</span>'
            )
        else:
            self.ok_btn.setEnabled(True)
            self.status_label.setText("")

import socket, time, threading, json, os
from PyQt5.QtCore import QTimer, pyqtSignal

class AddServerDialog(QDialog):
    """添加/编辑自定义服务器的对话框"""
    def __init__(self, parent=None, name='', addr=''):
        super().__init__(parent)
        set_window_emoji_icon(self, "➕")
        self.setWindowTitle("编辑服务器")
        self.setFixedSize(480, 320)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 20px; }
            QLabel { color: #CCCCCC; font-size: 20px; }
            QLineEdit { background-color: #2D2D2D; color: white; border:1px solid #555; border-radius:5px; padding:6px; }
            QLineEdit:focus { border-color: #4CAF50; }
            QPushButton { background-color: #4CAF50; color: white; border:none; border-radius:5px; padding:6px 16px; }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        layout.addWidget(QLabel("服务器名称："))
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("例如：流萤桌宠服务器")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("服务器地址 (IP:端口)："))
        self.addr_edit = QLineEdit(addr)
        self.addr_edit.setPlaceholderText("例如：127.0.0.1:20082")
        layout.addWidget(self.addr_edit)

        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def get_server_info(self):
        name = self.name_edit.text().strip() or "自定义服务器"
        addr = self.addr_edit.text().strip()
        if ':' in addr:
            host, port = addr.split(':', 1)
            try:
                port = int(port)
            except:
                port = 20082
        else:
            host = addr
            port = 20082
        return name, host, port

class DirectJoinDialog(QDialog):
    """快速加入服务器对话框（独立弹出，居中父窗口）"""
    join_request = pyqtSignal(str, int)  # 发射 (host, port)

    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "🚀")
        self.setWindowTitle("快速加入服务器")
        self.setFixedSize(420, 230)   # 增加高度，避免底部被遮挡
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #CCCCCC;
                font-size: 14px;
            }
            QLineEdit {
                background-color: #2D2D2D;
                color: white;
                border: 1px solid #555;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
            QLineEdit:focus {
                border-color: #4CAF50;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 8px 20px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton#cancel_btn {
                background-color: #666666;
            }
            QPushButton#cancel_btn:hover {
                background-color: #888888;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(15)

        # 标题
        title = QLabel("🔗 输入服务器地址")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #FFFFFF;")
        layout.addWidget(title)

        # IP地址输入框
        self.addr_edit = QLineEdit()
        self.addr_edit.setPlaceholderText("例如：127.0.0.1:20082")
        layout.addWidget(QLabel("地址 (IP:端口)："))
        layout.addWidget(self.addr_edit)

        # 按钮区域
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("加入")
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("cancel_btn")
        ok_btn.clicked.connect(self._on_ok)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        # 调整位置：相对于父窗口居中，若超出屏幕则自动修正
        self._center_on_parent()

    def _center_on_parent(self):
        """将对话框居中于父窗口（如果存在），否则屏幕居中，并确保完全可见"""
        if self.parent():
            parent_rect = self.parent().geometry()
            x = parent_rect.x() + (parent_rect.width() - self.width()) // 2
            y = parent_rect.y() + (parent_rect.height() - self.height()) // 2
            # 确保不超出屏幕边界
            screen = QApplication.primaryScreen().availableGeometry()
            x = max(screen.x(), min(screen.right() - self.width(), x))
            y = max(screen.y(), min(screen.bottom() - self.height(), y))
            self.move(x, y)
        else:
            screen_center = QApplication.primaryScreen().availableGeometry().center()
            self.move(screen_center - self.rect().center())

    def _on_ok(self):
        addr = self.addr_edit.text().strip()
        if not addr:
            QMessageBox.warning(self, "提示", "请输入服务器地址")
            return

        # 中文冒号转英文冒号
        addr = addr.replace('：', ':')

        if ':' in addr:
            host, port_str = addr.rsplit(':', 1)
            try:
                port = int(port_str)
            except ValueError:
                QMessageBox.warning(self, "错误", "端口号必须是数字")
                return
        else:
            host = addr
            port = 20082

        if not host:
            QMessageBox.warning(self, "错误", "IP地址不能为空")
            return

        self.join_request.emit(host, port)
        self.accept()

class JoinServerDialog(QDialog):
    """服务器选择与加入对话框（支持扫描、自定义、快速加入）"""
    scan_finished = pyqtSignal(dict, bool)

    def __init__(self, firefly, parent=None):
        super().__init__(parent)
        self.firefly = firefly  # 保存 Firefly 实例引用，用于直接加入
        set_window_emoji_icon(self, "🌐")
        self.setWindowTitle("加入服务器")
        self.setFixedSize(700, 500)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #CCCCCC; font-size: 13px; }
            QLineEdit { background-color: #2D2D2D; color: white; border:1px solid #555; border-radius:5px; padding:6px; }
            QLineEdit:focus { border-color: #4CAF50; }
            QPushButton { background-color: #4CAF50; color: white; border:none; border-radius:5px; padding:6px 16px; }
            QPushButton:hover { background-color: #45A049; }
            QPushButton:disabled { background-color: #666666; }
            QListWidget { background-color: #2D2D2D; color: white; border:1px solid #555; border-radius:5px; padding:4px; }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        # ========== 左侧：列表 + 手动输入行 ==========
        left_layout = QVBoxLayout()
        left_layout.setSpacing(10)

        self.server_list = QListWidget()
        self.server_list.itemDoubleClicked.connect(self.join_selected)
        left_layout.addWidget(self.server_list)

        # 手动输入行
        addr_layout = QHBoxLayout()
        addr_layout.addWidget(QLabel("地址："))
        self.addr_edit = QLineEdit()
        self.addr_edit.setPlaceholderText("IP:端口")
        self.addr_edit.setText("")
        addr_layout.addWidget(self.addr_edit)
        left_layout.addLayout(addr_layout)

        # 底部按钮（加入、取消）
        btn_layout = QHBoxLayout()
        self.join_btn = QPushButton("加入")
        self.join_btn.clicked.connect(self.join_selected)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.join_btn)
        btn_layout.addWidget(cancel_btn)
        left_layout.addLayout(btn_layout)

        main_layout.addLayout(left_layout, 2)

        # ========== 右侧：控制面板 ==========
        right_layout = QVBoxLayout()
        right_layout.setSpacing(15)

        # 扫描按钮
        self.scan_btn = QPushButton("🔍 扫描局域网")
        self.scan_btn.clicked.connect(self.scan_lan)
        right_layout.addWidget(self.scan_btn)

        # 信号图标 + 延迟信息
        info_layout = QHBoxLayout()
        self.signal_label = QLabel("📶")
        self.signal_label.setAlignment(Qt.AlignCenter)
        self.signal_label.setStyleSheet("font-size: 24px;")
        info_layout.addWidget(self.signal_label)

        self.latency_label = QLabel("延迟：--")
        self.latency_label.setAlignment(Qt.AlignCenter)
        self.latency_label.setStyleSheet("color: #CCCCCC; font-size: 13px;")
        self.latency_label.setMinimumWidth(100)
        info_layout.addWidget(self.latency_label)
        right_layout.addLayout(info_layout)

        # 直接加入按钮（快速）
        self.direct_join_btn = QPushButton("🚀 直接加入")
        self.direct_join_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                font-weight: bold;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #F57C00;
            }
        """)
        self.direct_join_btn.clicked.connect(self.open_direct_join_dialog)
        right_layout.addWidget(self.direct_join_btn)

        # 添加服务器按钮
        add_btn = QPushButton("➕ 添加服务器")
        add_btn.clicked.connect(self.add_custom_server)
        right_layout.addWidget(add_btn)

        # 编辑服务器按钮
        edit_btn = QPushButton("✏️ 编辑选中")
        edit_btn.clicked.connect(self.edit_selected_server)
        right_layout.addWidget(edit_btn)

        right_layout.addStretch()
        main_layout.addLayout(right_layout, 1)

        # 数据
        self.found_servers = []
        self.custom_servers = []
        self.last_latency = "--"
        self.scan_finished.connect(self.on_scan_finished)

        self.load_custom_servers()
        self.refresh_list()
        self.center_on_screen()
        QTimer.singleShot(100, self.scan_lan)

    # ---------- 窗口辅助方法 ----------
    def center_on_screen(self):
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    # ---------- 自定义服务器持久化 ----------
    def load_custom_servers(self):
        path = os.path.join(USER_DATA_DIR, 'custom_servers.json')
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    self.custom_servers = json.load(f)
            except:
                self.custom_servers = []
        else:
            self.custom_servers = []

    def save_custom_servers(self):
        path = os.path.join(USER_DATA_DIR, 'custom_servers.json')
        with open(path, 'w') as f:
            json.dump(self.custom_servers, f, indent=2)

    def refresh_list(self):
        self.server_list.clear()
        for srv in self.custom_servers:
            name = srv.get('name', '自定义')
            text = f"⭐ {name} - {srv['ip']}:{srv['port']}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, ('custom', self.custom_servers.index(srv)))
            self.server_list.addItem(item)
        for i, srv in enumerate(self.found_servers):
            name = srv.get('name', '服务器')
            text = f"📡 {name} - {srv['ip']}:{srv['port']}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, ('found', i))
            self.server_list.addItem(item)

    # ---------- 加入服务器 ----------
    def get_address(self):
        text = self.addr_edit.text().strip()
        # 处理中文冒号
        text = text.replace('：', ':')
        if ':' in text:
            host, port = text.split(':', 1)
            return host, int(port)
        return text, 20082

    def join_selected(self):
        row = self.server_list.currentRow()
        if row >= 0:
            item = self.server_list.item(row)
            tag, index = item.data(Qt.UserRole)
            if tag == 'found' and 0 <= index < len(self.found_servers):
                srv = self.found_servers[index]
                addr = f"{srv['ip']}:{srv['port']}"
                self.addr_edit.setText(addr)
                self.save_last_addr(addr)
                self.accept()
                return
            elif tag == 'custom' and 0 <= index < len(self.custom_servers):
                srv = self.custom_servers[index]
                addr = f"{srv['ip']}:{srv['port']}"
                self.addr_edit.setText(addr)
                self.save_last_addr(addr)
                self.accept()
                return
        addr = self.addr_edit.text().strip()
        if addr:
            self.save_last_addr(addr)
            self.accept()

    def load_last_addr(self):
        path = os.path.join(USER_DATA_DIR, 'server_ip.json')
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f).get('addr', '')
            except:
                pass
        return ''

    def save_last_addr(self, addr):
        path = os.path.join(USER_DATA_DIR, 'server_ip.json')
        try:
            with open(path, 'w') as f:
                json.dump({'addr': addr}, f)
        except:
            pass

    # ---------- 自定义服务器操作 ----------
    def add_custom_server(self):
        dlg = AddServerDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            name, host, port = dlg.get_server_info()
            self.custom_servers.append({'name': name, 'ip': host, 'port': port})
            self.save_custom_servers()
            self.refresh_list()

    def edit_selected_server(self):
        row = self.server_list.currentRow()
        if row < 0:
            QMessageBox.warning(self, "提示", "请先选中一个服务器")
            return
        item = self.server_list.item(row)
        tag, index = item.data(Qt.UserRole)
        if tag != 'custom':
            QMessageBox.warning(self, "提示", "只能编辑手动添加的服务器")
            return
        if 0 <= index < len(self.custom_servers):
            srv = self.custom_servers[index]
            dlg = AddServerDialog(self, name=srv.get('name', ''), addr=f"{srv['ip']}:{srv['port']}")
            if dlg.exec_() == QDialog.Accepted:
                name, host, port = dlg.get_server_info()
                self.custom_servers[index] = {'name': name, 'ip': host, 'port': port}
                self.save_custom_servers()
                self.refresh_list()

    # ---------- 局域网扫描 ----------
    def scan_lan(self):
        self.server_list.clear()
        self.found_servers.clear()
        self.scan_btn.setEnabled(False)
        self.latency_label.setText("正在扫描...")
        self.signal_label.setText("📶")
        self.repaint()
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        discovered = {}
        local_found = False
        self.last_latency = "--"

        # 测试本地服务器
        t0 = time.time()
        local_ok = self._check_port('127.0.0.1', 20082, timeout=1.0)
        t1 = time.time()
        if local_ok:
            discovered['127.0.0.1:20082'] = {'ip': '127.0.0.1', 'port': 20082, 'name': '本地服务器'}
            local_found = True
            self.last_latency = f"{int((t1 - t0) * 1000)}ms"
        else:
            self.last_latency = "无响应"

        # 扫描局域网
        local_ip = self._get_local_ip()
        if local_ip:
            base = '.'.join(local_ip.split('.')[:3])
            threads = []
            lock = threading.Lock()
            for i in range(1, 255):
                ip = f"{base}.{i}"
                t = threading.Thread(target=self._scan_ip, args=(ip, 20082, discovered, lock))
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=2.0)

        self.scan_finished.emit(discovered, local_found)

    def _check_port(self, ip, port, timeout=0.5):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((ip, port))
            s.close()
            return True
        except:
            return False

    def _scan_ip(self, ip, port, result_dict, lock):
        if self._check_port(ip, port):
            with lock:
                result_dict[f"{ip}:{port}"] = {'ip': ip, 'port': port, 'name': '局域网服务器'}

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith('127.'):
                return None
            return ip
        except:
            return None

    def on_scan_finished(self, discovered, local_found):
        self.found_servers = list(discovered.values())
        self.refresh_list()
        self.scan_btn.setEnabled(True)

        # 更新延迟显示和信号图标
        self.latency_label.setText(self.last_latency)
        if not self.found_servers and not self.custom_servers:
            self.latency_label.setText("无服务器")
            self.signal_label.setText("❌")
        else:
            if local_found:
                self.signal_label.setText("📶")
            else:
                self.signal_label.setText("⚠️")

    # ---------- 直接加入功能 ----------
    def open_direct_join_dialog(self):
        """弹出快速加入对话框"""
        dlg = DirectJoinDialog(self)
        dlg.join_request.connect(self._on_direct_join)
        dlg.exec_()

    def _on_direct_join(self, host, port):
        """直接加入服务器，关闭对话框"""
        self.firefly.join_server(host, port)
        self.accept()

class OnlineListOverlay(QFrame):
    def __init__(self, firefly, parent=None):
        super().__init__(parent)
        self.firefly = firefly
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: rgba(0,0,0,0.7); color: white; border-radius: 10px; padding: 15px;")
        self.layout = QVBoxLayout(self)
        self.label = QLabel("在线玩家")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.layout.addWidget(self.label)
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("background: transparent; color: white;")
        self.layout.addWidget(self.list_widget)
        self.adjustSize()
        self.hide()

    def update_players(self, players):
        self.list_widget.clear()
        for p in players:
            text = f"{p['name']} (ID: {p['id']})"
            if 'uid' in p:
                text += f" [UID: {p['uid']}]"
            self.list_widget.addItem(text)
        self.adjustSize()
        # 移动到屏幕上方居中
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.center().x() - self.width() // 2
        y = screen.top() + 30
        self.move(x, y)

    def show_online(self):
        self.show()

    def hide_online(self):
        self.hide()


class ChatWindow(QDialog):
    def __init__(self, firefly, username, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "💬")
        self.firefly = firefly
        self.username = username
        self.setWindowTitle("服务器聊天")
        self.setFixedSize(650, 550)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 12px; }
            QScrollArea { background: #2D2D2D; border: none; }
            QLineEdit { background: #2D2D2D; border: 1px solid #555555; border-radius: 6px; padding: 8px 12px; color: white; font-size: 14px; font-family: "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif; }
            QLineEdit:focus { border-color: #1AAD19; }
            QPushButton { background: #4CAF50; color: white; border: none; border-radius: 6px; padding: 8px 20px; font-size: 14px; font-weight: bold; font-family: "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif; }
            QPushButton:hover { background: #45A049; }
            QMenu { background-color: #2D2D2D; border: 1px solid #555; color: white; }
            QMenu::item { padding: 5px 20px; }
            QMenu::item:selected { background-color: #4CAF50; }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 5)
        main_layout.setSpacing(8)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.msg_container = QWidget()
        self.msg_container.setStyleSheet("background: #2D2D2D;")
        self.msg_layout = QVBoxLayout(self.msg_container)
        self.msg_layout.setContentsMargins(10, 10, 10, 10)
        self.msg_layout.setSpacing(6)
        self.msg_layout.addStretch()
        self.scroll_area.setWidget(self.msg_container)
        main_layout.addWidget(self.scroll_area)

        input_layout = QHBoxLayout()
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("输入消息，支持 @用户名 或 @所有人")
        self.input_edit.returnPressed.connect(self.send_message)
        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self.send_message)
        input_layout.addWidget(self.input_edit)
        input_layout.addWidget(self.send_btn)
        main_layout.addLayout(input_layout)

        # 禁言相关
        self._muted = False
        self._sending = False
        self._muted_until = None
        self._muted_dt = None
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self._update_countdown)
        self.countdown_timer.setInterval(1000)

        self.common_mute = firefly.common_mute
        self.is_admin = firefly.is_admin

        # 加载历史消息
        self.load_history()
        self.update_mute_status(self.firefly.muted_until)
        self.update_chat_status(self.firefly.chat_enabled)
        self._refresh_ui()
        # 绑定视口右键上下文菜单（标准方案）
        self.scroll_area.viewport().customContextMenuRequested.connect(self._on_viewport_right_click)

    def _on_viewport_right_click(self, view_pos):
        """滚动区域空白处右键回调"""
        # view_pos：相对于viewport坐标
        container = self.scroll_area.widget()
        # 转换坐标到msg_container内部
        local_pos = container.mapFrom(self.scroll_area.viewport(), view_pos)
        # 核心：检测当前坐标下有没有任何子控件
        hit_widget = container.childAt(local_pos)
        if hit_widget and "QScrollBar" in hit_widget.metaObject().className():
            hit_widget = None

        if hit_widget is None:
            # ✅ 空白区域，弹出清空记录菜单
            menu = QMenu(self)
            clear_action = menu.addAction("清空记录")
            clear_action.triggered.connect(self.clear_history)
            menu.exec_(self.scroll_area.viewport().mapToGlobal(view_pos))

    # ---------- 消息加载与显示 ----------
    def load_history(self):
        """从 firefly.chat_history 加载所有历史消息并显示"""
        for msg in self.firefly.chat_history:
            self.add_message_obj(msg)
        self.scroll_to_bottom()

    def add_message_obj(self, msg_obj):
        sender = msg_obj.get('sender') or msg_obj.get('from_name', '?')
        text = msg_obj.get('text')
        time_str = self._format_time(msg_obj.get('time'))
        is_system = msg_obj.get('is_system', False)
        # 重新计算 is_self，不依赖消息中的旧值
        is_self = (sender == self.firefly.online_name) or (sender == self.firefly.current_user.get('username', ''))
        msg_id = msg_obj.get('id')
        self.add_message(sender, text, time_str, is_system, msg_id, is_self)

    def add_message(self, sender, text, time_str, is_system=False, msg_id=None, is_self=False):
        if self.msg_layout.count() > 0:
            last_item = self.msg_layout.itemAt(self.msg_layout.count() - 1)
            if last_item.spacerItem():
                self.msg_layout.removeItem(last_item)

        container = QWidget()
        container.setProperty('msg_id', msg_id)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        if is_system:
            # 系统消息
            lbl = QLabel(f"<div align='center' style='color:#AAAAAA; font-size:12px;'>{text}</div>")
            lbl.setWordWrap(True)
            layout.addWidget(lbl, alignment=Qt.AlignCenter)
        else:
            # ---------- 名字标签 ----------
            name_lbl = QLabel(sender)
            name_lbl.setStyleSheet("font-size:12px; color:#CCCCCC; background:transparent; font-weight:bold;")
            name_lbl.setAlignment(Qt.AlignRight if is_self else Qt.AlignLeft)

            # ---------- @TA 功能：点击名字自动填充 ----------
            # 仅对非自己的消息生效
            if not is_self:
                name_lbl.setCursor(Qt.PointingHandCursor)
                # 保存用户名到闭包
                target_name = sender

                def on_name_click(event):
                    # 填充 @用户名 + 空格，并聚焦输入框
                    self.input_edit.setText(f"@{target_name} ")
                    self.input_edit.setFocus()
                    # 可选：自动将光标移到末尾
                    self.input_edit.setCursorPosition(len(self.input_edit.text()))

                name_lbl.mousePressEvent = on_name_click

            layout.addWidget(name_lbl)

            # ---------- 气泡 ----------
            bubble_widget = QWidget()
            bubble_layout = QVBoxLayout(bubble_widget)
            bubble_layout.setContentsMargins(0, 0, 0, 0)
            bubble_layout.setSpacing(2)

            bubble = QLabel(text)
            bubble.setWordWrap(True)
            bubble_bg = '#DCF8C6' if is_self else '#FFFFFF'
            bubble.setStyleSheet(f"""
                background-color:{bubble_bg};
                border-radius:14px;
                padding:10px 14px;
                font-size:14px;
                color:#000000;
                font-family: 'HarmonyOS Sans SC','Microsoft YaHei',sans-serif;
            """)
            bubble.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            bubble.setContextMenuPolicy(Qt.CustomContextMenu)
            bubble.customContextMenuRequested.connect(
                lambda pos, mid=msg_id, s=is_self, wid=bubble, name=sender:
                self.show_message_menu(pos, mid, s, wid, name)
            )
            bubble_layout.addWidget(bubble)

            time_lbl = QLabel(time_str)
            time_lbl.setStyleSheet("color:#888888; font-size:10px; background:transparent;")
            time_lbl.setAlignment(Qt.AlignRight if is_self else Qt.AlignLeft)
            bubble_layout.addWidget(time_lbl)

            layout.addWidget(bubble_widget)
            if is_self:
                layout.setAlignment(Qt.AlignRight)
            else:
                layout.setAlignment(Qt.AlignLeft)

        self.msg_layout.addWidget(container)
        self.msg_layout.addStretch()
        QTimer.singleShot(0, self.scroll_to_bottom)

    # ---------- 右键菜单 ----------
    def show_message_menu(self, pos, msg_id, is_self, widget, sender):
        menu = DarkRoundedMenu(self)
        # 判断当前用户是否为管理员（联机模式下）
        is_admin = False
        if self.firefly.mp_client.connected:
            is_admin = (self.firefly.mp_client.server_permission == 'admin')

        # ---------- 添加 @TA 选项（仅对非自己的消息） ----------
        if not is_self:
            at_action = menu.addAction("@TA")
            at_action.triggered.connect(
                lambda: self._insert_at_mention(sender)
            )

        # ---------- 撤回选项 ----------
        can_recall = False
        if is_admin or is_self:
            can_recall = True
        if can_recall:
            recall_action = menu.addAction("撤回")
            recall_action.triggered.connect(lambda: self.recall_message(msg_id))


        # ---------- 删除本地 ----------
        delete_action = menu.addAction("删除")
        delete_action.triggered.connect(lambda: self.delete_message_local(msg_id))

        menu.exec_(widget.mapToGlobal(pos))

    def _insert_at_mention(self, name):
        """辅助方法：在输入框插入 @用户名 """
        self.input_edit.setText(f"@{name} ")
        self.input_edit.setFocus()
        self.input_edit.setCursorPosition(len(self.input_edit.text()))

    def recall_message(self, msg_id):
        if not self.firefly.mp_client.connected:
            QMessageBox.warning(self, "提示", "未连接到服务器")
            return
        # 自定义确认对话框
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("撤回消息")
        msg_box.setText("确定要撤回这条消息吗？")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        # 设置样式表使文字白色
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
            }
            QLabel {
                color: white;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton[text="No"] {
                background-color: #666666;
            }
            QPushButton[text="No"]:hover {
                background-color: #888888;
            }
        """)
        # 居中显示
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg_box.move(screen_center - msg_box.rect().center())
        reply = msg_box.exec_()
        if reply == QMessageBox.Yes:
            self.firefly.mp_client.send_command(f"/recall {msg_id}")

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            pos = event.pos()
            viewport = self.scroll_area.viewport()
            viewport_pos = viewport.mapFrom(self, pos)
            if viewport.rect().contains(viewport_pos):
                container = self.scroll_area.widget()
                local_pos = container.mapFrom(viewport, viewport_pos)
                # 获取最底层子控件
                child = container.childAt(local_pos)
                if child is None:
                    # 完全空白
                    hit = False
                else:
                    # 检查 child 是否是消息容器（具有 msg_id 属性）
                    if child.property('msg_id') is not None:
                        # 检查点击是否在该容器的内部子控件上
                        # 将坐标转换到 child
                        child_local = child.mapFrom(container, local_pos)
                        # 检查 child 的所有直接子控件
                        hit = False
                        for sub in child.children():
                            if isinstance(sub, QWidget) and sub.isVisible():
                                sub_pos = sub.mapFrom(child, child_local)
                                if sub.rect().contains(sub_pos):
                                    hit = True
                                    break
                        if not hit:
                            # 点击在消息容器内但没有子控件覆盖（即空白），视为空白
                            hit = False
                        # 否则 hit 保持 True（点击到消息内容）
                    else:
                        # 如果 child 不是消息容器，可能是其他控件，视为非空白（不触发清空）
                        hit = True  # 避免误触
                if not hit:
                    menu = DarkRoundedMenu(self)
                    clear_action = menu.addAction("清空记录")
                    clear_action.triggered.connect(self.clear_history)
                    menu.exec_(event.globalPos())
                    event.accept()
                    return
        super().mousePressEvent(event)

    def delete_message_local(self, msg_id):
        """删除本地消息（整个容器）"""
        self.firefly.chat_history = [m for m in self.firefly.chat_history if m.get('id') != msg_id]
        for i in reversed(range(self.msg_layout.count())):
            item = self.msg_layout.itemAt(i)
            if item.widget() and item.widget().property('msg_id') == msg_id:
                item.widget().deleteLater()
                break
        self.scroll_to_bottom()

    def clear_history(self):
        # 自定义确认对话框（文字白色）
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("确认清空")
        msg_box.setText("确定要清空所有聊天记录吗？")
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
            }
            QLabel {
                color: white;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton[text="No"] {
                background-color: #666666;
            }
            QPushButton[text="No"]:hover {
                background-color: #888888;
            }
        """)
        reply = msg_box.exec_()
        if reply == QMessageBox.No:
            return

        self.firefly.chat_history.clear()
        # 彻底清空布局
        for i in reversed(range(self.msg_layout.count())):
            item = self.msg_layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
            elif item.spacerItem():
                self.msg_layout.removeItem(item)
        self.msg_layout.addStretch()
        self.msg_container.updateGeometry()
        self.scroll_area.update()
        self.scroll_area.verticalScrollBar().setValue(0)
        self.scroll_area.verticalScrollBar().update()

    # ---------- 刷新全部消息（用于撤回后重绘） ----------
    def refresh_all_messages(self):
        # 清空所有消息控件
        for i in reversed(range(self.msg_layout.count())):
            item = self.msg_layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
        # 重新加载所有历史消息
        for msg in self.firefly.chat_history:
            self.add_message_obj(msg)
        self.scroll_to_bottom()

    # ---------- 滚动到底部 ----------
    def scroll_to_bottom(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ---------- 禁言与状态更新 ----------
    def update_chat_status(self, enabled):
        if not enabled:
            self.input_edit.setEnabled(False)
            self.input_edit.setReadOnly(True)
            self.input_edit.setPlaceholderText("❌ 聊天功能已被管理员关闭")
            self.send_btn.setEnabled(False)
            self.send_btn.setText("已关闭")
        else:
            if self._muted:
                pass
            else:
                self.input_edit.setEnabled(True)
                self.input_edit.setReadOnly(False)
                self.input_edit.setPlaceholderText("输入消息，支持 @用户名 或 @所有人")
                self.send_btn.setEnabled(True)
                self.send_btn.setText("发送")

    def update_common_mute_status(self, enabled):
        self.common_mute = enabled
        self._refresh_ui()

    def _update_countdown(self):
        if not self._muted or self._muted_dt is None:
            self.countdown_timer.stop()
            return
        now = datetime.now(self._muted_dt.tzinfo)
        if self._muted_dt <= now:
            self.update_mute_status(None)
        else:
            remaining = self._format_remaining(self._muted_dt)
            self.input_edit.setPlaceholderText(f"你已被禁言，剩余 {remaining}")
            if self.input_edit.isEnabled():
                self.input_edit.setEnabled(False)
                self.input_edit.setReadOnly(True)
                self.send_btn.setEnabled(False)
                self.send_btn.setText("禁言中")

    def update_mute_status(self, muted_until):
        self.countdown_timer.stop()
        print(f"[禁言] update_mute_status 收到: {muted_until}")
        if muted_until is None:
            self._muted = False
            self._muted_until = None
            self._muted_dt = None
        else:
            try:
                dt = datetime.fromisoformat(muted_until)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                now = datetime.now(dt.tzinfo)
                if dt > now:
                    self._muted = True
                    self._muted_until = muted_until
                    self._muted_dt = dt
                    print(f"[禁言] 禁言生效，解封时间: {dt}")
                else:
                    self._muted = False
                    self._muted_until = None
                    self._muted_dt = None
                    print(f"[禁言] 禁言已过期或无效")
            except Exception as e:
                print(f"[禁言] 解析时间失败: {e}")
                self._muted = False
                self._muted_until = None
                self._muted_dt = None
        self._refresh_ui()

    def _refresh_ui(self):
        print(f"[UI刷新] common_mute={self.common_mute}, is_admin={self.is_admin}, _muted={self._muted}, _muted_dt={self._muted_dt}")
        if not self.is_admin and self.common_mute:
            self.input_edit.setEnabled(False)
            self.input_edit.setReadOnly(True)
            self.input_edit.setPlaceholderText("🔇 全体禁言中")
            self.send_btn.setEnabled(False)
            self.send_btn.setText("禁言中")
            self.countdown_timer.stop()
        else:
            if self._muted and self._muted_dt is not None:
                self.input_edit.setEnabled(False)
                self.input_edit.setReadOnly(True)
                self.send_btn.setEnabled(False)
                self.send_btn.setText("禁言中")
                remaining = self._format_remaining(self._muted_dt)
                self.input_edit.setPlaceholderText(f"你已被禁言，剩余 {remaining}")
                self.countdown_timer.start()
            else:
                self.input_edit.setEnabled(True)
                self.input_edit.setReadOnly(False)
                self.input_edit.setPlaceholderText("输入消息，支持 @用户名 或 @所有人")
                self.send_btn.setEnabled(True)
                self.send_btn.setText("发送")
                self.countdown_timer.stop()

    def _format_remaining(self, dt):
        if dt is None:
            return "已过期"
        now = datetime.now(dt.tzinfo)
        if dt <= now:
            return "即将解禁"
        delta = dt - now
        days = delta.days
        seconds = delta.seconds
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if days > 0:
            return f"{days}天{hours}小时"
        elif hours > 0:
            return f"{hours}小时{minutes}分"
        else:
            if minutes > 0:
                if secs > 0:
                    return f"{minutes}分{secs}秒"
                else:
                    return f"{minutes}分"
            else:
                return f"{secs}秒"

    def get_my_name(self):
        if self.firefly.mp_client.connected:
            return self.firefly.online_name or self.firefly.current_user['username']
        return self.firefly.current_user['username']

    def _format_time(self, raw_time):
        try:
            dt = datetime.strptime(raw_time, '%Y-%m-%d %H:%M:%S')
            return dt.strftime('%m-%d %H:%M')
        except:
            return raw_time

    # ---------- 发送消息 ----------
    def send_message(self):
        # 强制同步禁言状态
        self.update_mute_status(self.firefly.muted_until)
        if self.firefly.muted_until is None:
            self._muted = False
            self._muted_until = None
            self._muted_dt = None
            self._refresh_ui()

        if not self.firefly.chat_enabled:
            QMessageBox.warning(self, "提示", "❌ 聊天功能已被管理员关闭")
            return
        if not self.is_admin and self.firefly.common_mute:
            QMessageBox.warning(self, "提示", "❌ 全体禁言中，无法发送消息")
            return
        if self._muted and self._muted_dt and self._muted_dt <= datetime.now(self._muted_dt.tzinfo):
            self.update_mute_status(None)
        if self.firefly.muted_until and not self._muted:
            self.update_mute_status(self.firefly.muted_until)
        elif not self.firefly.muted_until and self._muted:
            self.update_mute_status(None)
        if self._muted:
            if self._muted_until:
                try:
                    muted_dt = datetime.fromisoformat(self._muted_until)
                    if muted_dt > datetime.now().astimezone():
                        remaining = self._format_remaining(muted_dt)
                        if self.input_edit.isEnabled():
                            self.input_edit.setEnabled(False)
                            self.input_edit.setReadOnly(True)
                            self.send_btn.setEnabled(False)
                            self.send_btn.setText("禁言中")
                        QMessageBox.warning(self, "提示", f"你已被禁言，剩余 {remaining}")
                        return
                except:
                    pass
            self.update_mute_status(None)
        if self.firefly.banned:
            QMessageBox.warning(self, "提示", "你已被封禁，无法发送消息")
            return
        if not self.input_edit.isEnabled():
            QMessageBox.warning(self, "提示", "你已被禁言，无法发送消息")
            return
        if self._sending:
            return
        self._sending = True
        try:
            text = self.input_edit.text().strip()
            if not text:
                return
            if text.startswith('/'):
                self.firefly.mp_client.send_command(text)
            else:
                self.firefly.mp_client.send_chat(text)
            self.input_edit.clear()
        finally:
            self._sending = False

    def append_message(self, msg):
        data = msg['data']
        self.add_message(
            sender=data.get('from_name', '?'),
            text=data['text'],
            time_str=self._format_time(data.get('time', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))),
            is_system=data.get('is_system', False)
        )


# -------------------------- 核心桌宠类 --------------------------
class Firefly(QWidget):
    def __init__(self, user_data=None, parent=None):
        super(Firefly, self).__init__(parent)
        self.current_user = user_data
        self.online_name = None
        self.label = QLabel("", self)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.resize(500, 500)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.tray_icon = None
        self.movie = None
        self.draggable = False
        self.offset = None
        self.current_animation_state = "Standby"
        self.last_animation_state = "Standby"
        self.last_update_time = time.time()
        self.settings = QSettings("GUCNMC", "流萤桌宠")
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        windowState = self.settings.value("windowState")
        if windowState is not None:
            self.restoreWindowState(windowState)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.changeToDiscomfort)
        self.timer.start(1800000)
        self.music_file = "./Large_Music/不眠之夜.wav"
        self.music_file2 = "./Large_Music/打上花火.wav"
        self.music_file3 = "./Large_Music/520AM.wav"
        self.music_file4 = "./Large_Music/Dream of firefly.wav"
        self.music = None
        self.possible_file_names = ["流萤指针", "萨姆指针", "无指针"]
        self.GUI_file_names = ["Large", "small", "normal", "medium"]
        self.current_file_name = self.get_existing_file_name()
        self.setMouseTracking(True)
        self.installEventFilter(self)
        self.persistent_state = self.current_user.get("last_state", "Standby") if self.current_user else "Standby"

        if self.current_user:
            self.current_user = ensure_user_fields(self.current_user)
            # ===== 修复时装列表：移除无效时装 =====
            valid_clothes = list(AVAILABLE_CLOTHES.keys())
            owned = self.current_user.get('owned_clothes', ['normal'])
            filtered = [c for c in owned if c in valid_clothes]
            if 'normal' not in filtered:
                filtered.append('normal')
            if filtered != owned:
                self.current_user['owned_clothes'] = filtered
                self._save_current_user()
            self.apply_cursor_style()
            self.apply_pet_size()
            # ★ 添加：应用当前时装（自动修正无效时装）
            self.apply_current_clothes()
        self.last_interaction_time = time.time()
        self.emo_random_delay = random.randint(0, 15 * 60)
        self.sleep_start_time = None
        self.anim_restore_timer = None
        self.wake_action_tray = None
        if hasattr(self, 'timer') and self.timer:
            self.timer.stop()
        self.emo_check_timer = QTimer(self)
        self.emo_check_timer.timeout.connect(self.check_emo)
        self.emo_check_timer.start(1000)
        self.mp_client = MultiplayerClient(self)  # 注意传入 self
        self.mp_client.connection_changed.connect(self.on_mp_status_changed)
        self.mp_client.mentioned.connect(self.on_mentioned)
        self.mp_client.online_list_updated.connect(self.update_online_list_overlay)
        self.mp_client.login_failed.connect(self.on_mp_login_failed)
        self.mp_client.login_success.connect(self.on_login_success_mp)
        self.mp_client.message_received.connect(self.on_global_message)
        self.mp_client.permission_changed.connect(self.on_permission_changed)


        self.online_overlay = OnlineListOverlay(self)
        self.chat_window = None
        self.server_uid = None
        self.server_permission = 'user'
        self.online_inventory = None
        self.local_inventory_backup = None
        self.single_player_state = None
        self.online_mailbox = []
        self._pending_mail_request = None
        self.online_signed_today = False
        self.mp_client.sign_result.connect(self.on_sign_result)
        self.mp_client.command_result_signal.connect(self.on_command_result)
        self.mp_client.redeem_result_signal.connect(self.on_redeem_result)
        self.online_redeemed_codes = []
        self.mp_client.sign_result_signal.connect(self.on_sign_result)
        self.mp_client.claim_mail_result_signal.connect(self.on_claim_mail_result)
        self.mp_client.mails_ready.connect(self.on_mails_ready)
        self.chat_history = []
        self._refresh_chat_pending = False
        self.mp_client.chat_history_ready.connect(self.on_chat_history_ready)
        self.volume = self.current_user.get("volume", 0.5) if self.current_user else 0.5
        pygame.mixer.music.set_volume(self.volume)
        self.connecting = False
        QApplication.instance().focusChanged.connect(self.on_focus_changed)
        self._initialized = False
        self._was_connected = False
        self.mp_client.mentioned_all.connect(self.on_mentioned_all)
        self.banned = False  # 封禁标志
        # 连接封禁信号
        self.mp_client.banned_by_server.connect(self.on_server_banned)
        self._ban_dialog_showing = False
        self._was_connected = False
        self._is_manual_disconnect = False  # 标记是否主动断开
        self.mp_client.server_shutdown.connect(self.on_server_shutdown)
        logging.basicConfig(filename='firefly_client.log', level=logging.DEBUG,
                            format='%(asctime)s %(levelname)s %(message)s')
        self._pending_new_name = None
        self._waiting_for_name = False
        self.rename_required = False
        self._rename_dialog = None
        self.allow_move = False
        self.is_moving = False
        self.move_speed = 100.0  # 像素/秒
        self.move_timer = QTimer(self)
        self.move_timer.timeout.connect(self.start_random_move)

        self.move_animation_timer = QTimer(self)
        self.move_animation_timer.timeout.connect(self.update_move_position)
        self.move_target_x = self.move_target_y = 0
        self.move_start_x = self.move_start_y = 0
        self.move_start_time = 0.0
        self.move_duration = 0.0
        self.allow_move = self.current_user.get("allow_move", False) if self.current_user else False
        self._report_generating = False
        self.online_stats = None
        self._restore_from_backup_if_needed()
        self._suppress_disconnect_message = False
        self.muted_until = None
        if self.chat_window:
            self.chat_window.update_mute_status(self.muted_until)
        self.ai_enabled = True
        self.chat_enabled = True  # 默认开启
        self.common_mute = False
        self.is_admin = False
        self.ping_timer = QTimer(self)
        self.ping_timer.timeout.connect(self._on_ping_timer)
        self.ping_timer.setInterval(10000)  # 10秒
        self.server_display_name = "流萤服务器"  # 默认值
        self.rename_required = False
        self._rename_dialog = None
        self.online_uid = ''  # 新增，存储服务端返回的 uid 字段
        self._secret_action = None
        self._secret_timer = QTimer(self)
        self._secret_timer.timeout.connect(self._update_secret_tooltip)
        self._secret_timer.setInterval(1000)
        self._secret_timer.stop()
        self._secret_hovered = False

    def _on_check_name_result(self, available, reason):
        # 优先处理强制改名对话框
        if self._rename_dialog and self._rename_dialog.isVisible():
            self._rename_status_label.setText(reason if not available else "昵称可用")
            self._rename_status_label.setStyleSheet("color: #FF5555;" if not available else "color: #00FF00;")
            self._rename_ok_btn.setEnabled(available)
        elif self._pending_rename_dialog and self._pending_rename_dialog.isVisible():
            self._pending_rename_dialog.on_check_name_result(available, reason)

    def is_valid_nickname(name):
        import unicodedata
        stripped = name.strip()
        if not stripped:
            return False
        # 检查不可见字符
        for ch in stripped:
            if unicodedata.category(ch).startswith('C'):  # Cc, Cf, Cn, Co, Cs
                return False
            if ord(ch) < 0x20:  # 控制字符
                return False
        # 检查是否以“开拓者”开头且不等于“开拓者”
        if stripped.startswith("开拓者") and stripped != "开拓者":
            return False
        # 只允许中英文、数字、下划线（长度1-16）
        if not re.match(r'^[\w\u4e00-\u9fff]{1,16}$', stripped):
            return False
        return True

    def on_common_mute_status(self, enabled):
        self.common_mute = enabled
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.update_common_mute_status(enabled)

    def on_chat_status(self, enabled):
        """处理聊天功能状态变更"""
        self.chat_enabled = enabled
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.update_chat_status(enabled)

    def on_mute_status(self, muted_until):
        if muted_until == "":
            muted_until = None
        print(f"[禁言] 收到 mute_status: {muted_until}")
        self.muted_until = muted_until
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.update_mute_status(muted_until)

    def on_ai_status(self, enabled):
        print(f"[DEBUG] on_ai_status called with enabled={enabled}")
        self.ai_enabled = enabled
        # 刷新托盘菜单（如果存在）
        if self.tray_icon:
            self.create_menu()

    def _save_single_state_backup(self):
        """将单机状态保存到磁盘，用于强制结束后的恢复"""
        backup = {
            'persistent_state': self.persistent_state,
            'allow_move': self.allow_move,
            'cursor_style': self.current_user.get('cursor_style', 'None'),
            'pet_size': self.current_user.get('pet_size', 'normal'),
            'last_feed_time': self.current_user.get('last_feed_time'),
            'last_interaction_time': self.last_interaction_time,
            'emo_random_delay': self.emo_random_delay,
            'inventory_backup': self.local_inventory_backup  # 加入联机前保存的库存
        }
        path = os.path.join(USER_DATA_DIR, 'single_state_backup.json')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(backup, f, indent=2)
        except Exception as e:
            print(f"[备份] 保存单机状态失败: {e}")

    def _is_expire_format(self, s):
        if not s:
            return False
        s = s.strip()
        if s.isdigit():
            return True
        if re.match(r'^(\d+[YMD])+$', s.upper()):
            return True
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except:
            pass
        return False

    def _parse_items_spec(self, items_spec):
        items = []
        for part in items_spec.split('&'):
            part = part.strip()
            if not part:
                continue
            if ':' in part:
                itype, qty = part.split(':', 1)
                try:
                    qty = int(qty)
                except:
                    self.show_centered_message("错误", f"数量错误: {part}", QMessageBox.Warning)
                    return None
                if qty < 1 or qty > 9999:
                    self.show_centered_message("错误", f"数量必须在1-9999: {part}", QMessageBox.Warning)
                    return None
            else:
                itype = part
                qty = 1
            if itype == 'cake':
                items.append({'type': 'cake', 'amount': qty})
            elif itype == 'credit':
                items.append({'type': 'credit', 'amount': qty})
            elif itype == 'ticket':
                items.append({'type': 'star_rail_ticket', 'amount': qty})
            else:
                items.append({'type': 'clothes', 'id': itype})
        if not items:
            self.show_centered_message("错误", "未指定有效物品", QMessageBox.Warning)
            return None
        return items

    def _restore_from_backup_if_needed(self):
        """程序启动时检查是否存在备份，若有则恢复单机状态（强制结束后）"""
        backup_path = os.path.join(USER_DATA_DIR, 'single_state_backup.json')
        if not os.path.exists(backup_path):
            return
        try:
            with open(backup_path, 'r', encoding='utf-8') as f:
                backup = json.load(f)

            # 恢复状态
            self.persistent_state = backup.get('persistent_state', 'Standby')
            self.allow_move = backup.get('allow_move', False)
            if 'cursor_style' in backup:
                self.current_user['cursor_style'] = backup['cursor_style']
                self.apply_cursor_style()
            if 'pet_size' in backup:
                self.current_user['pet_size'] = backup['pet_size']
                self.apply_pet_size()
            if 'last_feed_time' in backup:
                self.current_user['last_feed_time'] = backup['last_feed_time']
            if 'last_interaction_time' in backup:
                self.last_interaction_time = backup['last_interaction_time']
            if 'emo_random_delay' in backup:
                self.emo_random_delay = backup['emo_random_delay']

            # 恢复库存备份
            inv_backup = backup.get('inventory_backup')
            if inv_backup:
                self.local_inventory_backup = inv_backup
                self.current_user['balance'] = inv_backup['balance']
                self.current_user['credit'] = inv_backup['credit']
                self.current_user['owned_clothes'] = inv_backup['owned_clothes']
                self.current_user['star_rail_tickets'] = inv_backup['star_rail_tickets']
                self.local_inventory_backup = None  # 恢复后清除内存引用

            # 恢复动画
            if self.persistent_state == "Discomfort":
                self.changeToDiscomfort()
            else:
                self.changeToStandby()

            # 保存用户数据
            self._save_current_user()
            # 删除备份文件
            os.remove(backup_path)
            print("[恢复] 已从备份恢复单机状态")
        except Exception as e:
            print(f"[恢复] 从备份恢复失败: {e}")
            # 出错时删除备份文件，避免反复尝试
            try:
                os.remove(backup_path)
            except:
                pass

    def change_username_single(self):
        """单机模式下修改本地账号的用户名"""
        if self.mp_client.connected:
            self.show_centered_message("提示", "请先断开服务器连接再修改用户名", QMessageBox.Warning)
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("✏️修改用户名")
        dialog.setFixedSize(350, 150)
        dialog.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        dialog.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit { background-color: #2D2D2D; color: white; border: 1px solid #555; border-radius: 5px; padding: 8px; }
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 5px; padding: 8px 16px; }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("新用户名:"))
        name_edit = QLineEdit()
        name_edit.setText(self.current_user["username"])
        layout.addWidget(name_edit)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)

        # 使对话框居中于屏幕
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())

        if dialog.exec_() != QDialog.Accepted:
            return

        new_name = name_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self, "提示", "用户名不能为空")
            return
        if new_name == self.current_user["username"]:
            return

        # 唯一性检查（排除当前用户自己的文件）
        current_userid = self.current_user["userid"]
        for filename in os.listdir(USER_DATA_DIR):
            if not filename.endswith(".json"):
                continue
            if filename == f"{current_userid}.json":
                continue
            filepath = os.path.join(USER_DATA_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("username") == new_name:
                    QMessageBox.warning(self, "提示", "用户名已存在")
                    return
            except:
                continue

        # 更新内存中的用户名
        self.current_user["username"] = new_name
        # 保存到文件（文件名不变，仅内部 username 字段变化）
        self._save_current_user()

        # 刷新右键菜单和托盘菜单
        self.create_menu()

        self.show_centered_message("成功", f"用户名已修改为「{new_name}」\n下次登录将使用新名称。")

    def on_server_shutdown(self, reason):
        """服务端关闭或维护模式踢出时调用"""
        # 避免在已经断开的情况下重复弹窗
        if not self.mp_client.connected:
            return
        # 显示弹窗（深色风格，与服务端风格一致）
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("服务器通知")
        msg_box.setText(reason)
        msg_box.setIcon(QMessageBox.Information)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                color: #E0E0E0;
            }
            QMessageBox QLabel {
                color: #E0E0E0;
                background-color: #1E1E1E;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)

        # 居中显示
        msg_box.adjustSize()  # 确保尺寸正确
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg_box.move(screen_center - msg_box.rect().center())

        msg_box.exec_()
        # 断开连接并恢复单机（disconnect_server 内部已包含恢复逻辑）
        self.disconnect_server()

    def _restore_single_player_state(self):
        """从备份恢复单机状态（不发送网络请求）"""
        if self.single_player_state:
            self.persistent_state = self.single_player_state['persistent_state']
            self.last_interaction_time = self.single_player_state['last_interaction_time']
            self.emo_random_delay = self.single_player_state['emo_random_delay']
            self.current_user['last_feed_time'] = self.single_player_state.get('last_feed_time')

            # 恢复动画状态
            if self.single_player_state['current_animation_state'] == "Discomfort":
                self.changeToDiscomfort()
            else:
                self.changeToStandby()

            # 恢复移动开关
            self.allow_move = self.single_player_state.get('allow_move', False)
            if self.allow_move:
                delay = random.randint(5 * 60, 15 * 60) * 1000
                self.move_timer.start(delay)
            else:
                self.move_timer.stop()
                if self.is_moving:
                    self.stop_move()

            # 恢复光标样式
            cursor_style = self.single_player_state.get('cursor_style')
            if cursor_style is not None:
                self.current_user['cursor_style'] = cursor_style
                self.apply_cursor_style()

            # 恢复宠物大小
            pet_size = self.single_player_state.get('pet_size')
            if pet_size is not None:
                self.current_user['pet_size'] = pet_size
                self.apply_pet_size()

            # ★ 恢复单机时装
            if 'current_clothes' in self.single_player_state:
                saved_clothes = self.single_player_state['current_clothes']
                owned = self.current_user.get('owned_clothes', ['normal'])
                # 如果单机不拥有该时装，强制改为 normal
                if saved_clothes not in owned or saved_clothes not in AVAILABLE_CLOTHES:
                    saved_clothes = 'normal'
                self.current_user['current_clothes'] = saved_clothes
                self._save_current_user()
                # 应用时装（静默）
                self.apply_clothes(saved_clothes, silent=True)
            else:
                # 如果没有保存，则执行有效性检查（兜底）
                self.apply_current_clothes()

            self.single_player_state = None
            self._save_current_user()

        if self.local_inventory_backup:
            # 恢复库存...
            self._save_current_user()
            self.local_inventory_backup = None

        self.update_bag_display()
        # ★ 移除或注释掉这行，避免二次覆盖
        # self.apply_current_clothes()
        self.create_menu()

    def on_server_banned(self, message):
        if self._ban_dialog_showing:
            return
        self._ban_dialog_showing = True
        try:
            self.banned = True
            self.show_centered_message("封禁通知", message, QMessageBox.Warning)
            if self.chat_window and self.chat_window.isVisible():
                self.chat_window.close()
                self.chat_window = None
        finally:
            self._ban_dialog_showing = False

    def _bring_chat_to_front(self):
        if not self.chat_enabled:
            self.show_centered_message("提示", "❌ 聊天功能已被管理员关闭", QMessageBox.Warning)
            return
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.raise_()
            self.chat_window.activateWindow()
        else:
            self.open_chat_window()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initialized:
            try:
                self.apply_cursor_style()
                self.apply_pet_size()
                self._init_movie_from_file()
                self.hi_music()
                self._initialized = True
            except Exception as e:
                print(f"初始化资源失败: {e}")

        # === 统计打开次数（仅单机模式统计） ===
        if self.current_user:
            self.check_month_reset()
            self.current_user["total_open_count"] += 1
            # 联机模式下，monthly_open_count 由加入服务器时增加，此处不再增加
            if not self.mp_client.connected:
                self.current_user["monthly_open_count"] += 1
            self._save_current_user()

    def on_focus_changed(self, old, new):
        if old == self and self.draggable:
            self.draggable = False
            # 恢复当前应有的动画
            if self.persistent_state == "Discomfort":
                self.changeToDiscomfort()
            else:
                self.changeToStandby()

    def load_volume(self):
        path = os.path.join(USER_DATA_DIR, 'volume.json')
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f).get('volume', 0.5)
            except:
                pass
        return 0.5

    def save_volume(self):
        path = os.path.join(USER_DATA_DIR, 'volume.json')
        try:
            with open(path, 'w') as f:
                json.dump({'volume': self.volume}, f)
        except:
            pass

    def set_volume(self, value):
        self.volume = max(0.0, min(1.0, value))
        pygame.mixer.music.set_volume(self.volume)

        # 保存到当前用户数据
        if self.current_user is not None:
            self.current_user["volume"] = self.volume
            self._save_current_user()  # 写入用户 JSON 文件
            print(f"[DEBUG] 音量已保存: {self.volume} 到用户 {self.current_user.get('userid')}")
        else:
            print("[WARN] 未登录，无法保存音量")

    def on_chat_history_ready(self, history):
        self.chat_history = history
        QTimer.singleShot(100, self.refresh_chat_window)

    def on_mails_ready(self, mails):
        self.online_mailbox = mails
        if self._pending_mail_request == 'open':
            self._pending_mail_request = None
            # 在主线程中创建邮箱窗口
            mailbox_win = MailboxWindow(self, self, online_mails=mails)
            mailbox_win.exec_()

    def on_claim_mail_result(self, success, message):
        if success:
            self.mp_client._send({'type': 'get_mails'})
            QTimer.singleShot(0, lambda: self.show_centered_message("邮件", "物品已领取成功！"))
            # === 新增：统计领取的信用点（需从之前保存的 _pending_credit 获取） ===
            if hasattr(self, '_pending_credit') and self._pending_credit:
                self.check_month_reset()
                self.current_user["monthly_credit_gained"] += self._pending_credit
                self._save_current_user()
                self._pending_credit = 0
        else:
            QTimer.singleShot(0, lambda: self.show_centered_message("领取失败", message or "未知错误",
                                                                    QMessageBox.Warning))

    def on_sign_result(self, success, message):
        if success:
            self.show_centered_message("签到成功", message)
            self.online_signed_today = True
            # === 新增统计（联机签到） ===
            self.check_month_reset()
            self.current_user["total_sign_days"] += 1
            self.current_user["monthly_sign_days"] += 1
            self.current_user["total_interactions"] += 1
            self.current_user["monthly_interactions"] += 1
            # 联机签到获得 7200 信用点
            self.current_user["monthly_credit_gained"] += 7200
            self._save_current_user()
            self.create_menu()
        else:
            self.show_centered_message("签到提示", message, QMessageBox.Warning)

    def on_redeem_result(self, success, message):
        if success:
            self.show_centered_message("兑换成功", message, QMessageBox.Information)
        else:
            self.show_centered_message("兑换失败", message, QMessageBox.Warning)

    def on_command_result(self, success, message):
        print(f"[DEBUG] on_command_result: success={success}, message={message}")
        if success:
            if '撤回成功' in message:
                return
            if '改名成功' in message:
                import re
                match = re.search(r'你现在的名字是 (.+)$', message)
                if match:
                    new_name = match.group(1).strip()
                else:
                    new_name = self._pending_new_name
                if new_name:
                    self.online_name = new_name
                    # ★ 设置保护标志，防止后续玩家列表更新覆盖新名字
                    self._name_just_changed = True
                    # 关闭强制改名对话框（如果存在）
                    if self._rename_dialog and self._rename_dialog.isVisible():
                        self._rename_dialog.accept()
                        self._rename_dialog = None
                        if hasattr(self, '_rename_list_connection'):
                            self.mp_client.online_list_updated.disconnect(self._rename_list_connection)
                            self._rename_list_connection = None
                        self.rename_required = False
                    self._pending_new_name = None
                    # 刷新托盘菜单
                    self.create_menu()
                    # 弹出成功提示
                    QTimer.singleShot(0, lambda: self.show_centered_message(
                        "改名成功",
                        f"改名成功！你现在的名字是 {new_name}"
                    ))
                    # 请求玩家列表以同步（但会触发覆盖，但保护标志会阻止覆盖）
                    self.mp_client.request_player_list()
                    self.mp_client._send({'type': 'get_chat_history'})
                else:
                    self.create_menu()
            else:
                QTimer.singleShot(0, lambda: self.show_centered_message("命令结果", message, QMessageBox.Information))
        else:
            if self._rename_dialog and self._rename_dialog.isVisible():
                self._rename_dialog.setEnabled(True)
                QMessageBox.warning(self._rename_dialog, "改名失败", message)
            else:
                QTimer.singleShot(0, lambda: self.show_centered_message("命令失败", message, QMessageBox.Warning))
            self._pending_new_name = None

    def check_month_reset(self):
        if not self.current_user:
            return
        now = datetime.now()
        current_month = now.strftime("%Y-%m")
        if self.current_user.get("current_month") != current_month:
            monthly_keys = [
                "monthly_interactions", "monthly_open_count", "monthly_sign_days",
                "monthly_affection_up", "monthly_sleep_count", "monthly_chat_count",
                "monthly_credit_gained", "monthly_cake_gained",
                "monthly_ai_click_count"
            ]
            for key in monthly_keys:
                self.current_user[key] = 0
            self.current_user["current_month"] = current_month
            self._save_current_user()

    def generate_monthly_report(self):
        # 防止重复调用
        if getattr(self, '_report_generating', False):
            return
        self._report_generating = True

        try:
            # 确定使用哪个数据源
            if self.mp_client.connected and self.online_stats:
                stats = self.online_stats
            else:
                if not self.current_user:
                    return
                self.check_month_reset()
                stats = {
                    'monthly_interactions': self.current_user.get('monthly_interactions', 0),
                    'monthly_open_count': self.current_user.get('monthly_open_count', 0),
                    'monthly_sign_days': self.current_user.get('monthly_sign_days', 0),
                    'monthly_affection_up': self.current_user.get('monthly_affection_up', 0),
                    'monthly_sleep_count': self.current_user.get('monthly_sleep_count', 0),
                    'monthly_chat_count': self.current_user.get('monthly_chat_count', 0),
                    'monthly_credit_gained': self.current_user.get('monthly_credit_gained', 0),
                    'monthly_cake_gained': self.current_user.get('monthly_cake_gained', 0),
                }
                if self.mp_client.connected and not self.online_stats:
                    self.mp_client._send({'type': 'get_stats'})
                    pass

            import calendar, re, os
            now = datetime.now()
            days = calendar.monthrange(now.year, now.month)[1]

            interactions = stats.get('monthly_interactions', 0)
            open_count = stats.get('monthly_open_count', 0)
            sign_days = stats.get('monthly_sign_days', 0)
            affection = stats.get('monthly_affection_up', 0)
            sleep_count = stats.get('monthly_sleep_count', 0)
            credit_gained = stats.get('monthly_credit_gained', 0)
            cake_gained = stats.get('monthly_cake_gained', 0)

            # === 根据联机状态决定在线对话次数来源 ===
            if self.mp_client.connected:
                chat_count = stats.get('monthly_chat_count', 0)
                chat_label = "多人对话次数"
            else:
                chat_count = self.current_user.get('monthly_ai_click_count', 0)
                chat_label = "在线对话次数"

            # === 根据联机状态决定打开次数标签 ===
            open_label = "加入服务器次数" if self.mp_client.connected else "本月打开桌宠次数"

            # 计算进度条
            credit_target = 7200 * days
            credit_percent = int((credit_gained / credit_target * 100) if credit_target > 0 else 0)
            credit_percent = min(100, credit_percent)
            credit_display = f"{credit_gained} / {credit_target}（{credit_percent}%）"

            sign_percent = int((sign_days / days * 100) if days > 0 else 0)
            sign_percent = min(100, sign_percent)
            sign_display = f"{sign_days} / {days}（{sign_percent}%）"

            interact_percent = int((interactions / 500 * 100) if interactions > 0 else 0)
            interact_percent = min(100, interact_percent)
            interact_display = f"{interactions} / 500（{interact_percent}%）"

            cake_percent = int((cake_gained / 72 * 100) if cake_gained > 0 else 0)
            cake_percent = min(100, cake_percent)
            cake_display = f"{cake_gained} / 72（{cake_percent}%）"

            # 获取显示名称和UID
            if self.mp_client.connected and self.online_name:
                display_name = self.online_name
            else:
                display_name = self.current_user.get('username', '开拓者') if self.current_user else '开拓者'

            if self.mp_client.connected and self.mp_client.server_uid:
                display_uid = self.mp_client.server_uid
            else:
                display_uid = self.current_user.get('uid', '未知') if self.current_user else '未知'

            data = {
                "monthly_interactions": interactions,
                "monthly_open_count": open_count,
                "open_label": open_label,  # 新增
                "monthly_sign_days": sign_days,
                "monthly_affection_up": affection,
                "monthly_sleep_count": sleep_count,
                "monthly_chat_count": chat_count,
                "chat_label": chat_label,
                "monthly_credit_gained": credit_gained,
                "monthly_cake_gained": cake_gained,
                "days_in_month": days,
                "current_year_month": now.strftime("%Y年%m月"),
                "mode_text": "联机模式" if self.mp_client.connected else "单机模式",
                "credit_display": credit_display,
                "credit_percent": credit_percent,
                "sign_display": sign_display,
                "sign_percent": sign_percent,
                "interact_display": interact_display,
                "interact_percent": interact_percent,
                "cake_display": cake_display,
                "cake_percent": cake_percent,
                "display_name": display_name,
                "display_uid": display_uid,
            }

            template_path = "html/month.html"
            if not os.path.exists(template_path):
                self.show_centered_message("错误", "报告模板文件 month.html 不存在", QMessageBox.Warning)
                return

            with open(template_path, "r", encoding="utf-8") as f:
                html = f.read()

            for key, value in data.items():
                pattern = r'{{\s*' + re.escape(key) + r'\s*}}'
                html = re.sub(pattern, str(value), html)

            report_dir = "report"
            if not os.path.exists(report_dir):
                os.makedirs(report_dir)

            report_path = os.path.join(report_dir, "firefly_report.html")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(html)

            webbrowser.open(report_path)

        except Exception as e:
            import traceback
            print(f"[报告生成] 异常: {e}")
            traceback.print_exc()
            self.show_centered_message("错误", f"生成报告失败：{str(e)}", QMessageBox.Warning)

        finally:
            self._report_generating = False

    def refresh_chat_window(self):
        if self.chat_window is None:
            return
        # 清空现有消息
        for i in reversed(range(self.chat_window.msg_layout.count())):
            item = self.chat_window.msg_layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
        # 重新加载历史消息
        for msg in self.chat_history:
            self.chat_window.add_message_obj(msg)  # 改为调用 add_message_obj
        self.chat_window.scroll_to_bottom()

    def _safe_open_chat(self):
        try:
            self.open_chat_window()
        except Exception as e:
            print(f"重新打开聊天窗口失败: {e}")

    def _handle_kick(self, reason):
        """在主线程中处理踢出（弹窗并断开）"""
        self.show_centered_message("连接断开", f"与服务器断开连接！\n{reason}", QMessageBox.Warning)
        self.disconnect_server(show_message=False)

    def on_permission_changed(self):
        userid = self.current_user['userid']
        filepath = os.path.join(USER_DATA_DIR, f'{userid}.json')
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                self.current_user = json.load(f)
        if self.mp_client.server_uid:
            self.server_uid = self.mp_client.server_uid
        self.create_menu()
        if self.online_overlay and self.online_overlay.isVisible():
            self.mp_client.request_player_list()

    def on_global_message(self, msg):
        """
        处理服务端推送的全局消息（聊天、系统、提及、踢出等）
        """
        # ---- 强制调试：打印所有消息类型 ----
        print(f"[全局消息] 收到类型: {msg.get('type')}")

        if not isinstance(msg, dict):
            return

        mtype = msg.get('type')
        data = msg.get('data', {})

        # ---------- 处理踢出（Kick） ----------
        if mtype == 'kick':
            reason = data.get('reason', '你已被管理员踢出服务器，可重新连接。')
            print("[KICK] 收到踢出消息，将调度主线程弹窗")
            QTimer.singleShot(0, lambda: self._handle_kick(reason))
            return

        # ---------- 禁言状态 ----------
        if mtype == 'mute_status':
            print(f"[禁言] 从 on_global_message 收到 mute_status: {data.get('muted_until')}")
            self.on_mute_status(data.get('muted_until'))
            return

        # ---------- 聊天广播 ----------
        if mtype == 'chat_broadcast':
            print(f"[调试] 收到 chat_broadcast，完整 data: {data}")
            from_name = data.get('from_name', '')
            text = data.get('text', '')
            is_system = data.get('is_system', False)
            # 如果是系统消息且包含“撤回了一条消息”，忽略（因为已在recall_notification中处理）
            if is_system and '撤回了一条消息' in text:
                return
            # 其余聊天广播正常处理
            msg_id = data.get('msg_id', None)
            timestamp = data.get('time', datetime.now().isoformat())
            is_self = (from_name == self.online_name or from_name == self.current_user.get('username', ''))
            msg_obj = {
                'id': msg_id,
                'sender': from_name,
                'text': text,
                'time': timestamp,
                'is_system': is_system,
                'is_self': is_self
            }
            self.chat_history.append(msg_obj)
            if self.chat_window and self.chat_window.isVisible():
                self.chat_window.add_message_obj(msg_obj)
            return

        # ---------- 撤回通知（必须添加） ----------
        if mtype == 'recall_notification':
            msg_id = data.get('msg_id')
            sender = data.get('sender', '某人')
            # 查找并修改原消息
            for msg in self.chat_history:
                if msg.get('id') == msg_id:
                    # 改为系统消息样式
                    msg['text'] = f"{sender}撤回了一条消息"
                    msg['is_system'] = True
                    msg['sender'] = '系统'  # 发件人改为系统
                    msg['revoked'] = True  # 标记已撤回（可选）
                    # 保留原时间，或改为当前时间（可选）
                    break
            # 刷新聊天窗口（如果打开）
            if self.chat_window and self.chat_window.isVisible():
                self.chat_window.refresh_all_messages()
            return

        # ---------- @提及 ----------
        if mtype == 'mention':
            from_name = data.get('from_name', '')
            if not from_name:
                return
            is_all = data.get('all', False)
            if is_all:
                self.mentioned_all.emit(from_name)
            else:
                self.mentioned.emit(from_name)
            # 触发桌面通知
            if is_all:
                show_notification_async(
                    title="流萤桌宠多人聊天",
                    message=f"{from_name} @了所有人",
                    icon_path="./assets/images/firefly/Happy/see.ico",
                    duration=3
                )
            else:
                show_notification_async(
                    title="流萤桌宠多人聊天",
                    message=f"{from_name} @了你",
                    icon_path="./assets/images/firefly/Happy/see.ico",
                    duration=3
                )
            self.open_chat_window()
            return

        # ---------- 玩家列表更新 ----------
        if mtype == 'player_list':
            players = data.get('players', [])
            self.update_online_list_overlay(players)
            return

        # ---------- 库存更新 ----------
        if mtype == 'inventory_update':
            inv = data.get('inventory', {})
            self.online_inventory = inv
            today = datetime.now().strftime('%Y-%m-%d')
            self.online_signed_today = (inv.get('last_sign_date') == today)
            self.update_bag_display()
            if hasattr(self, '_before_redeem_credit') and self._before_redeem_credit is not None:
                old_credit = self._before_redeem_credit
                new_credit = inv.get('credit', 0)
                diff = new_credit - old_credit
                if diff > 0:
                    self.check_month_reset()
                    self.current_user["monthly_credit_gained"] += diff
                    self._save_current_user()
                self._before_redeem_credit = None
            self.apply_current_clothes()
            return

        # ---------- 签到结果 ----------
        if mtype == 'sign_result':
            success = data.get('success', False)
            message = data.get('message', '')
            self.on_sign_result(success, message)
            return

        # ---------- 兑换结果 ----------
        if mtype == 'redeem_result':
            success = data.get('success', False)
            message = data.get('message', '')
            self.on_redeem_result(success, message)
            return

        # ---------- 命令执行结果 ----------
        if mtype == 'command_result':
            success = data.get('success', False)
            message = data.get('message', '')
            self.on_command_result(success, message)
            return

        # ---------- 邮件数据 ----------
        if mtype == 'mails_data':
            mails = data.get('mails', [])
            self.on_mails_ready(mails)
            return

        # ---------- 聊天历史 ----------
        if mtype == 'chat_history_data':
            history = data.get('history', [])
            self.on_chat_history_ready(history)
            return

        # ---------- 领取邮件结果 ----------
        if mtype == 'claim_mail_result':
            success = data.get('success', False)
            reason = data.get('reason', '')
            self.on_claim_mail_result(success, reason)
            return

        # ---------- 统计数据 ----------
        if mtype == 'stats_data':
            stats = data.get('stats', {})
            self.online_stats = stats
            if self.current_user:
                for key, value in stats.items():
                    if key in self.current_user:
                        self.current_user[key] = value
                self._save_current_user()
            return

        # ---------- 权限变更 ----------
        if mtype == 'permission_changed':
            self.server_permission = data.get('permission', 'user')
            self.server_uid = data.get('server_uid', self.server_uid)
            self.on_permission_changed()
            return

        # ---------- 服务器关闭通知 ----------
        if mtype == 'server_shutdown':
            reason = data.get('reason', '服务器已关闭')
            self.show_centered_message("服务器通知", reason, QMessageBox.Information)
            self.disconnect_server(show_message=False)
            return

        # ---------- 系统消息（非聊天广播） ----------
        if mtype == 'system_message':
            text = data.get('text', '')
            # ---------- 新增：检测聊天记录清空通知 ----------
            if "聊天记录已清空" in text:
                self.chat_history.clear()  # 清空本地历史
                if self.chat_window and self.chat_window.isVisible():
                    self.chat_window.refresh_all_messages()  # 刷新窗口（清空显示）
                    # 同时插入一条系统消息显示清空通知
                    self.chat_window.add_message(
                        sender='系统',
                        text=text,
                        time_str=datetime.now().strftime('%m-%d %H:%M'),
                        is_system=True
                    )
                return
            # ---------- 原有封禁检测 ----------
            if '封禁' in text or 'banned' in text.lower():
                self.banned = True
                self.show_centered_message("封禁通知", text, QMessageBox.Warning)
                self.disconnect_server(show_message=False)
                return
            # 检测解除禁言（系统广播）
            if '已被解除禁言' in text:
                import re
                match = re.search(r'(\S+)\s*已被解除禁言', text)
                if match:
                    target = match.group(1)
                    my_name = self.online_name or self.current_user.get('username', '')
                    if target == my_name or self.muted_until is not None:
                        print(f"[禁言] 根据系统广播解除禁言 (target={target})")
                        self.on_mute_status(None)
                        if self.chat_window and self.chat_window.isVisible():
                            self.chat_window.update_mute_status(None)
            # 正常显示系统消息（所有系统消息都显示）
            if self.chat_window and self.chat_window.isVisible():
                self.chat_window.add_message(
                    sender='系统',
                    text=text,
                    time_str=datetime.now().strftime('%m-%d %H:%M'),
                    is_system=True
                )
            return

        # ---------- 其他未知消息 ----------
        print(f"[未处理消息] type={mtype}, data={data}")

    def get_display_uid(self):
        if self.mp_client.connected and self.online_name:
            return f"{self.online_name} (UID:{self.online_uid})"
        return f"{self.current_user['username']} (UID:{self.current_user['uid']})"

    def change_online_name(self):
        if not self.mp_client.connected:
            self.show_centered_message("提示", "请先加入服务器", QMessageBox.Warning)
            return
        if self.chat_window and self.chat_window.isVisible():
            QTimer.singleShot(0,
                              lambda: self.show_centered_message("提示", "请先关闭聊天窗口再改名", QMessageBox.Warning))
            return

        current_name = self.online_name or self.current_user['username']
        # 默认剩余次数为1（服务端会更新）
        dlg = ChangeNameDialog(self, current_name, 1, self.server_timezone_offset)
        self._pending_rename_dialog = dlg  # 保存引用

        # 请求服务端获取剩余次数
        self.mp_client._send({'type': 'get_rename_info', 'data': {}})

        # 连接玩家列表更新信号（用于重名校验）
        self.mp_client.online_list_updated.connect(dlg.update_player_list)

        if dlg.exec_() == QDialog.Accepted:
            name = dlg.get_new_name()
            if name:
                # 检查是否已过期（服务端会再次验证）
                self._pending_new_name = name
                self.mp_client._send({'type': 'change_name', 'data': {'new_name': name}})

        self.mp_client.online_list_updated.disconnect(dlg.update_player_list)
        self._pending_rename_dialog = None

    def get_active_inventory(self):
        if self.mp_client.connected and self.online_inventory is not None:
            return self.online_inventory
        return self.current_user

    def on_login_success_mp(self, data):
        # 提取字段
        server_uid = data.get('server_uid')
        perm = data.get('permission')
        inv = data.get('inventory')
        server_version = data.get('server_version')
        stats = data.get('stats', {})
        require_rename = data.get('require_rename', False)
        uid = data.get('uid', '')
        chat_history = data.get('chat_history', [])

        # ----- 强制设置 online_name -----
        server_name = data.get('server_name') or data.get('name', '')
        self.online_name = server_name if server_name else self.current_user.get('username', '用户')
        print(f"[登录] 设置 online_name = '{self.online_name}' (来自服务端: '{server_name}')")
        # --------------------------------

        print(f"[登录成功] 收到历史消息 {len(chat_history)} 条")
        self.chat_history = chat_history

        # ===== 以下代码完全保持原样，只需将变量名替换为上面提取的变量 =====
        # 版本二次校验
        if server_version and not versions_compatible(CLIENT_VERSION, server_version):
            self._is_manual_disconnect = True
            self.show_centered_message("版本不兼容",
                                       f"客户端版本 {CLIENT_VERSION} 与服务器版本 {server_version} 不匹配",
                                       QMessageBox.Warning)
            self.mp_client.disconnect()
            return

        self.connecting = False
        self.is_admin = (perm == 'admin')
        # 保存单机状态
        self.single_player_state = {
            'persistent_state': self.persistent_state,
            'current_animation_state': self.current_animation_state,
            'last_interaction_time': self.last_interaction_time,
            'emo_random_delay': self.emo_random_delay,
            'last_feed_time': self.current_user.get('last_feed_time'),
            'current_clothes': self.current_user.get('current_clothes', 'normal')
        }
        self.changeToStandby()
        self.persistent_state = "Standby"
        self.last_interaction_time = time.time()
        self.emo_random_delay = random.randint(0, 15 * 60)
        self.current_user['last_feed_time'] = None

        if inv is None:
            inv = {'balance': 0, 'credit': 0, 'owned_clothes': ['normal'], 'star_rail_tickets': 0}
        self.online_inventory = inv
        self.update_bag_display()

        # ===== 处理统计数据 =====
        if stats:
            self.online_stats = stats
            for key, value in stats.items():
                if key in self.current_user:
                    self.current_user[key] = value
            self._save_current_user()

        # ===== 联机模式：加入服务器计数 =====
        self.check_month_reset()
        self.current_user["monthly_open_count"] += 1
        self._save_current_user()
        self.create_menu()
        if self.chat_window:
            self.chat_window.update_mute_status(self.muted_until)
        if require_rename:
            QTimer.singleShot(300, self.show_forced_rename_dialog)
        else:
            QTimer.singleShot(100,
                              lambda: self.show_centered_message("成功", f"已连接到服务器\n你的服务器UID: {uid}"))
        self.create_menu()
        self.apply_current_clothes()
        self._waiting_for_name = True
        self.mp_client.request_player_list()
        self.mp_client.ping()
        self.ping_timer.start()
        self.online_uid = uid
        # 确保打开窗口时加载最新历史
        if hasattr(self, 'chat_window') and self.chat_window and self.chat_window.isVisible():
            self.chat_window.refresh_all_messages()

    def _update_online_players_for_rename(self, players):
        # 如果对话框已关闭，断开连接并返回
        if not self._rename_edit or self._rename_edit is None:
            if hasattr(self, '_rename_list_connection'):
                self.mp_client.online_list_updated.disconnect(self._rename_list_connection)
                self._rename_list_connection = None
            return
        self._online_players = [p.get('name', '') for p in players]
        if self._rename_edit:
            self._on_rename_text_changed(self._rename_edit.text())

    def show_forced_rename_dialog(self):
        # 请求在线玩家列表，用于重名检查
        self._online_players = []
        if hasattr(self, '_rename_list_connection'):
            self.mp_client.online_list_updated.disconnect(self._rename_list_connection)
        self._rename_list_connection = self.mp_client.online_list_updated.connect(
            self._update_online_players_for_rename)
        self.mp_client.request_player_list()

        dialog = QDialog(self)
        dialog.setWindowTitle("设置昵称")
        dialog.setModal(True)
        dialog.setFixedSize(400, 250)
        dialog.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())

        dialog.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit { 
                background-color: #2D2D2D; 
                color: white; 
                border: 1px solid #555; 
                border-radius: 5px; 
                padding: 8px; 
            }
            QLineEdit::placeholder { color: #AAAAAA; }
            QPushButton { 
                background-color: #4CAF50; 
                color: white; 
                border: none; 
                border-radius: 5px; 
                padding: 8px 16px; 
            }
            QPushButton:hover { background-color: #45A049; }
            QPushButton:disabled { background-color: #666666; }
            QLabel#status_label { font-size: 11px; }
        """)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        layout.addWidget(QLabel("欢迎首次加入服务器！请设置你的昵称："))
        self._rename_edit = QLineEdit()
        self._rename_edit.setPlaceholderText("输入昵称（最多16字符）")
        self._rename_edit.setMaxLength(16)
        layout.addWidget(self._rename_edit)

        self._rename_status_label = QLabel()
        self._rename_status_label.setObjectName("status_label")
        self._rename_status_label.setStyleSheet("color: #AAAAAA;")
        layout.addWidget(self._rename_status_label)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok)
        btn_box.button(QDialogButtonBox.Ok).setText("确定")
        btn_box.accepted.connect(self._submit_forced_rename)
        dialog.rejected.connect(self._on_rename_dialog_rejected)
        layout.addWidget(btn_box)

        self._rename_edit.textChanged.connect(self._on_rename_text_changed)
        self._rename_ok_btn = btn_box.button(QDialogButtonBox.Ok)
        self._rename_ok_btn.setEnabled(False)

        self._rename_dialog = dialog
        dialog.exec_()

    def _center_dialog_on_parent_or_screen(self, dialog):
        if self.isVisible() and self.geometry().width() > 0:
            # 居中于当前窗口
            parent_rect = self.geometry()
            x = parent_rect.x() + (parent_rect.width() - dialog.width()) // 2
            y = parent_rect.y() + (parent_rect.height() - dialog.height()) // 2
            # 确保不超出屏幕
            screen = QApplication.primaryScreen().availableGeometry()
            x = max(screen.x(), min(screen.right() - dialog.width(), x))
            y = max(screen.y(), min(screen.bottom() - dialog.height(), y))
            dialog.move(x, y)
        else:
            # 屏幕居中
            screen = QApplication.primaryScreen().availableGeometry()
            x = (screen.width() - dialog.width()) // 2
            y = (screen.height() - dialog.height()) // 2
            dialog.move(x, y)

    def _on_rename_dialog_rejected(self):
        if self._rename_dialog is None:
            return
        dialog = self._rename_dialog
        self._rename_dialog = None
        # 断开玩家列表更新连接
        if hasattr(self, '_rename_list_connection'):
            self.mp_client.online_list_updated.disconnect(self._rename_list_connection)
            self._rename_list_connection = None
        try:
            dialog.done(0)
        except Exception as e:
            print(f"关闭改名对话框异常: {e}")
        self.rename_required = False
        self.disconnect_server(show_message=True)

    def _on_rename_text_changed(self, text):
        new_name = text.strip()
        status_label = self._rename_status_label
        ok_btn = self._rename_ok_btn

        if not new_name:
            status_label.setText("昵称不能为空")
            status_label.setStyleSheet("color: #FF5555;")
            ok_btn.setEnabled(False)
            return

        # 格式校验
        if not Firefly.is_valid_nickname(new_name):
            if new_name.startswith("开拓者-") and new_name != "开拓者":
                status_label.setText("此昵称不合法")
            else:
                status_label.setText("昵称不合法")
            status_label.setStyleSheet("color: #FF5555;")
            ok_btn.setEnabled(False)
            return

        # 异步检查重名
        ok_btn.setEnabled(False)
        status_label.setText("检查中...")
        status_label.setStyleSheet("color: #AAAAAA;")
        self.mp_client._send({'type': 'check_name', 'data': {'new_name': new_name}})

    def _submit_forced_rename(self):
        if not self._rename_dialog:
            return
        new_name = self._rename_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self._rename_dialog, "错误", "昵称不能为空")
            return
        if not Firefly.is_valid_nickname(new_name):
            QMessageBox.warning(self._rename_dialog, "错误",
                                "此昵称不合法")
            return
        self._pending_new_name = new_name
        self.mp_client._send({'type': 'change_name', 'data': {'new_name': new_name}})
        self._rename_dialog.setEnabled(False)

    def on_mp_login_failed(self, reason):
        self.connecting = False
        # 判断是否为版本相关错误
        is_version_error = any(keyword in reason for keyword in ["版本过低", "版本过高", "未知版本号", "版本不兼容"])
        if is_version_error:
            # 对于版本错误，设置主动断开标志，避免显示“与服务器断开连接”弹窗
            self._is_manual_disconnect = True
        if "封禁" in reason:
            self.banned = True
            self.show_centered_message("封禁通知", f"无法加入服务器：{reason}", QMessageBox.Warning)
        else:
            # 非封禁错误（包括版本错误）仍然显示原因，但不额外弹断开连接窗
            self.show_centered_message("连接失败", f"无法加入服务器：{reason}", QMessageBox.Warning)

    def on_mp_status_changed(self, connected):
        self.connecting = False
        if not connected:
            # ----- 清空本地聊天记录缓存（本次连接期间的记录） -----
            self.chat_history = []
            # ----- 关闭聊天窗口（如果打开） -----
            if self.chat_window:
                self.chat_window.close()
                self.chat_window = None
            if self._was_connected and not self._suppress_disconnect_message:
                self.show_centered_message("连接断开", "与服务器断开连接！", QMessageBox.Warning)
                self._restore_single_player_state()
            self._suppress_disconnect_message = False
            self.online_uid = ''
            self.server_uid = None
            self.online_name = None
            self.online_inventory = None
            self._was_connected = False
            self.ai_enabled = True
            self.muted_until = None
            self.ping_timer.stop()  # 断开时停止
            if self.chat_window:
                self.chat_window.update_mute_status(None)
        else:
            self._was_connected = True
            self.ping_timer.start()  # 连接成功时启动
            print("[PING] 定时器已启动 (on_mp_status_changed)")  # 调试
        self._is_manual_disconnect = False
        self.create_menu()

    def _get_mp_menu_text(self):
        if not self.mp_client.connected:
            return "🌐 多人联机"
        latency = self.mp_client._latest_latency_ms
        if latency < 0:
            status = "连接中..."
            color = "#888888"
        elif latency <= 40:
            status = f"已连接  延迟{latency}ms"
            color = "#00FF00"  # 绿
        elif latency <= 119:
            status = f"已连接  延迟{latency}ms"
            color = "#FFD700"  # 黄
        else:
            status = f"已连接  延迟{latency}ms"
            color = "#FF0000"  # 红
        # 信号强度：用点表示
        if latency <= 40:
            signal = "█████"  # 满格
        elif latency <= 80:
            signal = "███░░"
        elif latency <= 119:
            signal = "██░░░"
        else:
            signal = "█░░░░"
        # 但为了简洁，只显示延迟颜色，信号强度可以用文字表示
        # 这里我们返回 HTML 格式的文本（但 QMenu 不支持富文本，需要设置样式）
        # 所以直接返回带颜色标记的文本，实际菜单项可能不支持颜色，但可用 setStyleSheet 或 setIcon
        # 我们使用 setText 加上颜色标记？但 QMenu 的 item 不支持富文本，所以用 setIcon 表示信号？太复杂。
        # 简化：只用文字颜色，用 QAction 的 setStyleSheet 设置前景色，但 QAction 不支持直接设置颜色。
        # 我们可以创建自定义 QAction？但太复杂。
        # 实用方案：在菜单项名称中加 emoji 表示信号，延迟用数字显示，颜色无法实现。
        # 但需求是“带颜色划分”，所以我们可以在菜单项文本中用颜色标记，但 QAction 的文本不支持 HTML。
        # 可通过 setData 等方式，但最好直接用 QWidgetAction 自定义。
        # 为了快速实现，我们可以在菜单项旁边加子菜单显示状态？
        # 简单起见：延迟颜色用不同 emoji 表示：绿色 ✅，黄色 ⚠️，红色 ❌
        if latency <= 40:
            signal_emoji = "🟢"
        elif latency <= 119:
            signal_emoji = "🟡"
        else:
            signal_emoji = "🔴"
        return f"🌐 多人联机 ({signal_emoji} 已连接 延迟{latency}ms)"

    def _on_ping_timer(self):
        print(f"[PING] 触发, active={self.ping_timer.isActive()}, connected={self.mp_client.connected}")
        if self.mp_client.connected:
            self.mp_client.ping()
        else:
            print("[PING] 未连接，停止定时器？")  # 可选

    def open_chat_window(self):
        if self.banned:
            return
        if not self.mp_client.connected:
            return
        if not self.chat_enabled:
            self.show_centered_message("提示", "❌ 聊天功能已被管理员关闭", QMessageBox.Warning)
            return  # 阻止继续执行，避免打开窗口
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.raise_()
            return
        # 请求玩家列表以同步自己的名字
        self.mp_client.request_player_list()
        # 稍微延迟一点确保名字更新后再打开窗口
        QTimer.singleShot(200, self._really_open_chat_window)

    def _really_open_chat_window(self):
        if self.chat_window and self.chat_window.isVisible():
            self.chat_window.raise_()
            self.chat_window.activateWindow()
            return
        if self.chat_window and not self.chat_window.isVisible():
            # 窗口存在但隐藏（例如被 close 后 hide），可以重用
            self.chat_window.show()
            self.chat_window.raise_()
            # ★ 修复：重用窗口时同步最新禁言状态（防止窗口隐藏期间收到禁言消息导致状态不同步）
            self.chat_window.update_mute_status(self.muted_until)
            return
        name = self.online_name or self.current_user['username']
        self.chat_window = ChatWindow(self, name, None)
        self.chat_window.show()

    def on_mentioned(self, who):
        if not who:
            return
        show_notification_async(
            title="流萤桌宠多人聊天",
            message=f"{who} @了你",
            icon_path="./assets/images/firefly/Happy/see.ico",
            duration=3
        )
        self.open_chat_window()
        if self.chat_window:
            self.chat_window.setWindowFlags(self.chat_window.windowFlags() | Qt.WindowStaysOnTopHint)
            self.chat_window.show()
            QTimer.singleShot(5000, lambda: self.chat_window.setWindowFlags(
                self.chat_window.windowFlags() & ~Qt.WindowStaysOnTopHint) if self.chat_window else None)

    # ★ 新增方法
    def on_mentioned_all(self, who):
        if not who:
            return
        show_notification_async(
            title="流萤桌宠多人聊天",
            message=f"{who} @了所有人",
            icon_path="./assets/images/firefly/Happy/see.ico",
            duration=3
        )
        self.open_chat_window()
        if self.chat_window:
            self.chat_window.setWindowFlags(self.chat_window.windowFlags() | Qt.WindowStaysOnTopHint)
            self.chat_window.show()
            QTimer.singleShot(5000, lambda: self.chat_window.setWindowFlags(
                self.chat_window.windowFlags() & ~Qt.WindowStaysOnTopHint) if self.chat_window else None)

    def update_online_list_overlay(self, players):
        # 更新悬浮窗
        self.online_overlay.update_players(players)

        if not self.mp_client.connected:
            return

        # ★ 如果刚改完名，暂时不更新 online_name，防止被旧数据覆盖
        if hasattr(self, '_name_just_changed') and self._name_just_changed:
            # 但可以检查玩家列表中是否已包含新名字，如果包含则解除保护
            my_id = self.current_user['userid']
            for p in players:
                if p.get('id') == my_id:
                    if p.get('name') == self.online_name:
                        # 服务器已确认新名字，解除保护
                        self._name_just_changed = False
                    break
            return  # 本次不更新

        # 在列表中查找自己（通过 userid）
        my_id = self.current_user['userid']
        found_name = None
        found_uid = None
        for p in players:
            if p.get('id') == my_id:
                found_name = p['name']
                found_uid = p.get('uid', '')
                break

        changed = False
        if found_name is not None:
            if found_name != self.online_name:
                self.online_name = found_name
                changed = True
            if found_uid and found_uid != getattr(self.mp_client, 'server_uid', ''):
                self.mp_client.server_uid = found_uid
                self.server_uid = found_uid
                changed = True
            if changed:
                if self._waiting_for_name:
                    self._waiting_for_name = False
                self.create_menu()  # 刷新右键菜单
        else:
            if self._waiting_for_name:
                pass

    def get_active_inventory(self):
        if self.mp_client.connected and self.online_inventory is not None:
            return self.online_inventory
        return self.current_user

    def toggle_online_list(self):
        if self.online_overlay.isVisible():
            self.online_overlay.hide()
        else:
            self.mp_client.request_player_list()
            self.online_overlay.show_online()

    def restore_last_position(self):
        if not self.current_user:
            return
        x = self.current_user.get("last_x")
        y = self.current_user.get("last_y")
        if x is not None and y is not None:
            screen = QApplication.primaryScreen().availableGeometry()
            if x < screen.x():
                x = screen.x()
            if y < screen.y():
                y = screen.y()
            if x > screen.right() - self.width():
                x = screen.right() - self.width()
            if y > screen.bottom() - self.height():
                y = screen.bottom() - self.height()
            self.move(x, y)

    def show_exit_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("退出桌宠")
        dialog.setFixedSize(350, 180)
        dialog.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        dialog.setStyleSheet("""
                    QDialog {
                        background-color: #1E1E1E;
                        border: 2px solid #4CAF50;
                        border-top-left-radius: 0px;
                        border-top-right-radius: 0px;
                        border-bottom-left-radius: 8px;
                        border-bottom-right-radius: 8px;
                    }
                    QLabel {
                        color: #CCCCCC;
                        font-size: 15px;
                        font-weight: bold;
                    }
                    QPushButton {
                        background-color: #3A3A3A;
                        color: #EEEEEE;
                        border: 1px solid #555555;
                        border-radius: 8px;
                        padding: 10px;
                        font-size: 14px;
                    }
                    QPushButton:hover {
                        background-color: #4CAF50;
                        color: white;
                        border: 1px solid #4CAF50;
                    }
                    QPushButton#logout_btn {
                        background-color: #D32F2F;
                        border: 1px solid #D32F2F;
                    }
                    QPushButton#logout_btn:hover {
                        background-color: #F44336;
                    }
                    QPushButton#exit_btn {
                        background-color: #4CAF50;
                        border: 1px solid #4CAF50;
                    }
                    QPushButton#exit_btn:hover {
                        background-color: #45A049;
                    }
                """)

        layout = QVBoxLayout()
        layout.setContentsMargins(30, 30, 30, 20)
        layout.setSpacing(20)

        label = QLabel("确定要退出桌宠吗？")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(15)

        exit_btn = QPushButton("退出")
        exit_btn.setObjectName("exit_btn")
        exit_btn.clicked.connect(lambda: self._handle_exit_choice(dialog, clear_login=False))

        logout_btn = QPushButton("退出并登出")
        logout_btn.setObjectName("logout_btn")
        logout_btn.clicked.connect(lambda: self._handle_exit_choice(dialog, clear_login=True))

        btn_layout.addWidget(exit_btn)
        btn_layout.addWidget(logout_btn)
        layout.addLayout(btn_layout)

        dialog.setLayout(layout)
        set_window_emoji_icon(dialog, "🚪")
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())
        dialog.exec_()

    def _handle_exit_choice(self, dialog, clear_login=False):
        dialog.accept()
        if clear_login:
            self._perform_logout()  # 清除配置并重启
        else:
            self.out_win()  # 仅退出，不重启

    def _perform_logout(self):
        # 保存当前状态和位置
        self.current_user["last_state"] = self.persistent_state
        pos = self.pos()
        self.current_user["last_x"] = pos.x()
        self.current_user["last_y"] = pos.y()
        self._save_current_user()

        # 删除自动登录配置
        if os.path.exists(USER_CONFIG):
            os.remove(USER_CONFIG)

        # 断开服务器连接（如果有）
        self.disconnect_server()

        # 注销全局热键
        if hasattr(self, 'hotkey_filter'):
            self.hotkey_filter.unregister()
            QApplication.instance().removeNativeEventFilter(self.hotkey_filter)

        # 停止音乐播放
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except:
            pass

        # 隐藏窗口和托盘图标
        self.hide()
        if self.tray_icon:
            self.tray_icon.setVisible(False)

        # 确定重启命令
        if getattr(sys, 'frozen', False):
            # 打包成 exe 的情况
            program = sys.executable
            args = []
        else:
            # 开发环境：使用 python 解释器运行当前脚本
            program = sys.executable
            args = [os.path.abspath(sys.argv[0])]

        # 启动新进程，使其独立于当前进程
        if sys.platform == 'win32':
            # Windows：使用 DETACHED_PROCESS 和 CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                [program] + args,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            # Unix-like：使用 start_new_session
            subprocess.Popen([program] + args, start_new_session=True)

        # 延迟退出旧进程，给新进程足够的初始化时间
        QTimer.singleShot(1000, self._quit_app)

    def _quit_app(self):
        """彻底退出当前应用程序"""
        QApplication.quit()
        sys.exit(0)

    def record_interaction(self, extra_seconds=0):
        self.last_interaction_time = time.time() + extra_seconds
        self.emo_random_delay = random.randint(0, 15 * 60)
        if self.current_animation_state == "Discomfort":
            self.changeToStandby()
            self.last_animation_state = "Standby"

    def check_emo(self):
        if self.current_animation_state == "Sleep":
            return
        if self.current_animation_state == "Discomfort":
            return

        elapsed = time.time() - self.last_interaction_time
        threshold = 20 * 60 + self.emo_random_delay
        if elapsed >= threshold:
            self.changeToDiscomfort()

    def apply_cursor_style(self):
        if not self.current_user:
            return
        style = self.current_user.get("cursor_style", "None")
        self._apply_cursor_by_style(style)

    def _apply_cursor_by_style(self, style):
        if style == "Firefly":
            pixmap = QPixmap('mouse/Firefly/p1.gif')
            if not pixmap.isNull():
                self.setCursor(QCursor(pixmap))
            else:
                self.setCursor(QCursor(Qt.ArrowCursor))
        elif style == "Sam":
            pixmap = QPixmap('mouse/Sam/p2.gif')
            if not pixmap.isNull():
                self.setCursor(QCursor(pixmap))
            else:
                self.setCursor(QCursor(Qt.ArrowCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

    def set_cursor_style(self, style):
        if not self.current_user:
            return
        self._apply_cursor_by_style(style)
        self.current_user["cursor_style"] = style
        self._save_current_user()

    def _set_pet_size(self, size, save=True):
        target = size
        existing = None
        for name in ["Large", "small", "normal", "medium"]:
            if os.path.exists(name):
                existing = name
                break
        if existing and existing != target:
            try:
                if os.path.exists(target):
                    os.remove(target)  # 先删除已存在的目标文件
                os.rename(existing, target)
            except Exception as e:
                print(f"重命名大小标记文件失败: {e}")
        elif not existing:
            with open(target, "w") as f:
                f.write("")
        self._init_movie_from_file()
        if save and self.current_user:
            self.current_user["pet_size"] = target
            self._save_current_user()

    def apply_pet_size(self):
        if not self.current_user:
            return
        size = self.current_user.get("pet_size", "normal")
        self._set_pet_size(size, save=False)

    def _init_movie_from_file(self):
        state = "Standby"
        if self.current_user and "last_state" in self.current_user:
            if self.current_user["last_state"] == "Discomfort":
                state = "Discomfort"
        files = os.listdir(".")
        if 'Large' in files:
            self.movie = QMovie(f'./assets/images/firefly/actions/{state}/{state}_Large.gif')
        elif 'small' in files:
            self.movie = QMovie(f'./assets/images/firefly/actions/{state}/{state}_small.gif')
        elif 'normal' in files:
            self.movie = QMovie(f'./assets/images/firefly/actions/{state}/{state}.gif')
        elif 'medium' in files:
            self.movie = QMovie(f'./assets/images/firefly/actions/{state}/{state}_medium.gif')
        else:
            self.movie = QMovie(f'./assets/images/firefly/actions/{state}/{state}.gif')
        self.label.setMovie(self.movie)
        self.movie.start()
        self.current_animation_state = state
        self.persistent_state = state

    def init_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        if os.path.exists('icon/setting.ico'):
            self.tray_icon.setIcon(QIcon('icon/setting.ico'))
        else:
            pixmap = QPixmap(16, 16)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setBrush(QBrush(QColor(0, 123, 255)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(0, 0, 16, 16, 4, 4)
            painter.end()
            self.tray_icon.setIcon(QIcon(pixmap))
        self.create_menu()
        self.tray_icon.show()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and obj is self:
            self.wheelEvent(event)
            return True
        return super().eventFilter(obj, event)

    def get_existing_file_name(self):
        for name in self.possible_file_names:
            if os.path.exists(name):
                return name
        return None

    def play_music(self):
        if pygame.mixer.get_num_channels() > 0:
            if self.music is None:
                self.music = pygame.mixer.music.load(self.music_file)
            pygame.mixer.music.play()
        else:
            print("未检测到扬声器，无法播放音频。")
            return False

    def open_settings(self):
        def check_process_running(process_name):
            for process in psutil.process_iter(['name']):
                if process.info['name'] == process_name:
                    return True
            return False
        process_name = '工具组件.exe'
        if not check_process_running(process_name):
            subprocess.Popen(["./tools/工具组件.exe"])

    def open_settings_tool(self):
        import threading
        import http.server
        import socketserver
        import webbrowser
        import json
        import os
        import sys
        import subprocess
        from urllib.parse import unquote, urlparse, parse_qs

        project_root = os.path.dirname(os.path.abspath(__file__))
        os.chdir(project_root)

        class MyHandler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                # API: 获取音乐列表
                if self.path == '/api/music':
                    music_dir = os.path.join(project_root, 'music')
                    files = []
                    if os.path.exists(music_dir):
                        for f in os.listdir(music_dir):
                            if f.lower().endswith(('.mp3', '.wav', '.ogg')):
                                files.append(f)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(files).encode('utf-8'))
                    return

                # API: 提供音频文件数据
                if self.path.startswith('/music/'):
                    filename = unquote(self.path[7:])
                    filepath = os.path.join(project_root, 'music', filename)
                    if os.path.exists(filepath):
                        self.send_response(200)
                        ext = os.path.splitext(filename)[1].lower()
                        mime = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg'}.get(ext,
                                                                                                    'application/octet-stream')
                        self.send_header('Content-Type', mime)
                        self.end_headers()
                        with open(filepath, 'rb') as f:
                            self.wfile.write(f.read())
                        return
                    self.send_response(404)
                    self.end_headers()
                    return

                # API: 打开文件夹（备用）
                if self.path.startswith('/open_folder?'):
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    folder = params.get('path', [''])[0]
                    if folder:
                        abs_path = os.path.join(project_root, folder)
                        if os.path.exists(abs_path):
                            try:
                                if sys.platform == 'win32':
                                    os.startfile(abs_path)
                                else:
                                    subprocess.Popen(['explorer', abs_path])
                                self.send_response(200)
                                self.send_header('Content-Type', 'application/json')
                                self.end_headers()
                                self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))
                                return
                            except Exception as e:
                                self.send_response(500)
                                self.end_headers()
                                self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))
                                return
                        else:
                            self.send_response(404)
                            self.end_headers()
                            self.wfile.write(json.dumps({'error': 'Folder not found'}).encode('utf-8'))
                            return
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': 'Missing path'}).encode('utf-8'))
                    return

                # API: 安装鼠标主题（使用 ShellExecuteW）
                if self.path.startswith('/install_theme?'):
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    folder = params.get('path', [''])[0]
                    if not folder:
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(json.dumps({'error': 'Missing path'}).encode('utf-8'))
                        return
                    abs_path = os.path.join(project_root, folder)
                    if not os.path.exists(abs_path):
                        self.send_response(404)
                        self.end_headers()
                        self.wfile.write(json.dumps({'error': 'Folder not found'}).encode('utf-8'))
                        return
                    # 查找 .inf 文件
                    inf_files = [f for f in os.listdir(abs_path) if f.lower().endswith('.inf')]
                    if not inf_files:
                        self.send_response(404)
                        self.end_headers()
                        self.wfile.write(json.dumps({'error': 'No .inf file found in theme folder'}).encode('utf-8'))
                        return
                    # 优先使用 "右键安装.inf"
                    inf_file = None
                    for f in inf_files:
                        if f == "右键安装.inf":
                            inf_file = f
                            break
                    if not inf_file:
                        inf_file = inf_files[0]
                    inf_full = os.path.join(abs_path, inf_file)
                    try:
                        # 使用 ShellExecuteW 模拟右键“安装”
                        import ctypes
                        ret = ctypes.windll.shell32.ShellExecuteW(
                            None,
                            "install",
                            inf_full,
                            None,
                            abs_path,
                            1
                        )
                        if ret > 32:
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))
                        else:
                            error_code = ctypes.GetLastError()
                            self.send_response(500)
                            self.end_headers()
                            self.wfile.write(
                                json.dumps({'error': f'ShellExecute failed, error code: {error_code}'}).encode('utf-8'))
                    except Exception as e:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))
                    return

                # 其他静态文件
                return super().do_GET()

        # 端口自动选择
        port = 8765
        while True:
            try:
                httpd = socketserver.TCPServer(("", port), MyHandler)
                break
            except OSError:
                port += 1

        def serve():
            httpd.serve_forever()

        threading.Thread(target=serve, daemon=True).start()
        webbrowser.open(f"http://localhost:{port}/html/tools.html")

    def play_music2(self):
        if pygame.mixer.get_num_channels() > 0:
            if self.music is None:
                self.music = pygame.mixer.music.load(self.music_file2)
            pygame.mixer.music.play()
        else:
            print("未检测到扬声器，无法播放音频。")
            return False

    def play_music3(self):
        if pygame.mixer.get_num_channels() > 0:
            if self.music is None:
                self.music = pygame.mixer.music.load(self.music_file3)
            pygame.mixer.music.play()
        else:
            print("未检测到扬声器，无法播放音频。")
            return False

    def play_music4(self):
        if pygame.mixer.get_num_channels() > 0:
            if self.music is None:
                self.music = pygame.mixer.music.load(self.music_file4)
            pygame.mixer.music.play()
        else:
            print("未检测到扬声器，无法播放音频。")
            return False

    def stop_music(self):
        self.changeToStandby()
        pygame.mixer.music.stop()
        self.music = None

    def change_GUI_to_Large(self):
        self._set_pet_size("Large", save=True)

    def change_GUI_to_normal(self):
        self._set_pet_size("normal", save=True)

    def change_GUI_to_medium(self):
        self._set_pet_size("medium", save=True)

    def change_GUI_to_small(self):
        self._set_pet_size("small", save=True)

    def apply_current_clothes(self):
        """启动时或库存更新后，检查并应用当前有效的时装（静默）"""
        if not self.current_user:
            return
        # 获取当前库存
        if hasattr(self, 'mp_client') and self.mp_client.connected and self.online_inventory is not None:
            inv = self.online_inventory
        else:
            inv = self.current_user
        owned = inv.get('owned_clothes', ['normal'])
        current = self.current_user.get('current_clothes', 'normal')
        # 检查是否有效
        if current not in owned or current not in AVAILABLE_CLOTHES:
            # 无效，切换到 normal
            if 'normal' not in owned:
                owned.append('normal')
                if hasattr(self, 'mp_client') and self.mp_client.connected and self.online_inventory is not None:
                    self.online_inventory['owned_clothes'] = owned
                    self.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.online_inventory}})
                else:
                    self.current_user['owned_clothes'] = owned
                    self._save_current_user()
            current = 'normal'
            self.current_user['current_clothes'] = 'normal'
            self._save_current_user()
        # 静默应用时装
        self.apply_clothes(current, silent=True)

    def apply_clothes(self, clothes_id, silent=False):
        info = AVAILABLE_CLOTHES.get(clothes_id)
        if not info:
            if not silent:
                self.show_centered_message("错误", f"未知时装ID: {clothes_id}", QMessageBox.Warning)
            return
        source_dir = info["source_dir"]
        target_dir = "assets"
        if not os.path.exists(source_dir):
            if not silent:
                self.show_centered_message("错误", f"时装资源目录不存在：{source_dir}\n请确保程序目录下有该文件夹。",
                                           QMessageBox.Warning)
            return
        try:
            # 复制资源
            for root, dirs, files in os.walk(source_dir):
                relative_path = os.path.relpath(root, source_dir)
                target_root = os.path.join(target_dir, relative_path)
                os.makedirs(target_root, exist_ok=True)
                for file in files:
                    src_file = os.path.join(root, file)
                    dst_file = os.path.join(target_root, file)
                    shutil.copy2(src_file, dst_file)
            if not silent:
                self.show_centered_message("成功", f"时装已切换为：{info['name']}")
            self.current_user["current_clothes"] = clothes_id
            self._save_current_user()
            self._refresh_animation_after_clothes_change()
        except Exception as e:
            if not silent:
                self.show_centered_message("错误", f"切换失败：{str(e)}", QMessageBox.Warning)

    def _refresh_animation_after_clothes_change(self):
        """切换时装后重新加载当前动画"""
        state = self.current_animation_state
        if state == "Standby":
            self.changeToStandby()
        elif state == "Discomfort":
            self.changeToDiscomfort()
        elif state == "Sleep":
            self.sleep()  # 重新播放睡眠动画
        else:
            # 处于 eat/love/sing 等临时状态，恢复待机
            self.changeToStandby()

    def copy_normal(self):
        self.apply_clothes("normal")

    def copy_new_year(self):
        self.apply_clothes("26_Newyear")

    # ====================== 用户数据操作 ======================
    def _save_current_user(self):
        """
        保存当前用户数据到本地文件，并在联机时上报统计数据到服务端。
        调用时机：任何用户数据（包括统计字段）发生变化后。
        """
        if not self.current_user:
            return

        userid = self.current_user.get("userid")
        if not userid:
            print("[ERROR] 当前用户缺少 userid，无法保存")
            return

        # ---------- 1. 保存到本地文件 ----------
        filepath = os.path.join(USER_DATA_DIR, f"{userid}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(self.current_user, f, ensure_ascii=False, indent=2)
            print(f"[DEBUG] 用户数据已保存到 {filepath}")
        except Exception as e:
            print(f"[ERROR] 保存用户数据失败: {e}")
            return

        # ---------- 2. 联机时上报统计数据到服务端 ----------
        if self.mp_client.connected and self.current_user:
            try:
                # 构建统计字典（与服务器字段对齐）
                stats = {
                    'total_interactions': self.current_user.get('total_interactions', 0),
                    'total_open_count': self.current_user.get('total_open_count', 0),
                    'total_sign_days': self.current_user.get('total_sign_days', 0),
                    'total_affection_up': self.current_user.get('total_affection_up', 0),
                    'total_sleep_count': self.current_user.get('total_sleep_count', 0),
                    'total_chat_count': self.current_user.get('total_chat_count', 0),
                    'monthly_interactions': self.current_user.get('monthly_interactions', 0),
                    'monthly_open_count': self.current_user.get('monthly_open_count', 0),
                    'monthly_sign_days': self.current_user.get('monthly_sign_days', 0),
                    'monthly_affection_up': self.current_user.get('monthly_affection_up', 0),
                    'monthly_sleep_count': self.current_user.get('monthly_sleep_count', 0),
                    'monthly_chat_count': self.current_user.get('monthly_chat_count', 0),
                    'monthly_credit_gained': self.current_user.get('monthly_credit_gained', 0),
                    'monthly_cake_gained': self.current_user.get('monthly_cake_gained', 0),
                    'current_month': self.current_user.get('current_month', None),
                }
                # 发送到服务端
                self.mp_client._send({'type': 'update_stats', 'data': {'stats': stats}})
                # 更新本地缓存（供报告使用）
                self.online_stats = stats
                print("[DEBUG] 统计数据已上报服务器")
            except Exception as e:
                print(f"[ERROR] 上报统计数据失败: {e}")

    def find_user_by_identifier(self, identifier):
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except:
                    continue
                if not isinstance(data, dict):
                    continue
                if (data.get("username") == identifier or
                        data.get("userid") == identifier or
                        data.get("uid") == identifier):
                    return data
        return None

    def find_users_by_identifiers(self, identifier):
        identifier = str(identifier).strip()
        if not identifier:
            return []
        matched_users = []
        for filename in os.listdir(USER_DATA_DIR):
            if filename == os.path.basename(CODES_FILE):
                continue
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(USER_DATA_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    user = json.load(f)
            except:
                continue
            if not isinstance(user, dict):
                continue
            name_match = str(user.get("name", "")).strip() == identifier
            id_match = str(user.get("userid", "")).strip() == identifier
            uid_match = str(user.get("uid", "")).strip() == identifier
            if name_match or id_match or uid_match:
                matched_users.append(user)
        return matched_users

    def save_user_data(self, user_data):
        userid = user_data.get("userid")
        if userid:
            filepath = os.path.join(USER_DATA_DIR, f"{userid}.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(user_data, f, ensure_ascii=False, indent=2)

    def send_mail_to_user(self, target_user, title, content, items):
        if self.mp_client.connected:
            # 联机模式：通过服务端发送
            target_uid = target_user.get('server_uid') or target_user.get('uid')
            if not target_uid:
                print("联机模式发邮件：缺少目标uid")
                return
            self.mp_client.send_mail(target_uid, title, content, items)
        else:
            # 单机模式：本地发送
            send_mail(target_user, title, content, items)
            self.save_user_data(target_user)
            if target_user["userid"] == self.current_user["userid"]:
                self._save_current_user()

    def show_centered_message(self, title, text, icon=QMessageBox.Information, buttons=QMessageBox.Ok):
        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setText(text)
        msg.setIcon(icon)
        msg.setStandardButtons(buttons)
        msg.setStyleSheet("""
            QMessageBox {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #888888;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)
        if buttons == (QMessageBox.Yes | QMessageBox.No):
            yes_btn = msg.button(QMessageBox.Yes)
            if yes_btn:
                yes_btn.setStyleSheet("background-color: #F44336;")
            no_btn = msg.button(QMessageBox.No)
            if no_btn:
                no_btn.setStyleSheet("background-color: #4CAF50;")
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        msg.move(screen_center - msg.rect().center())
        return msg.exec_()

    # ---------- 信用点相关 ----------
    def give_credit(self, amount, target_identifiers_str=None):
        try:
            amount = int(amount)
            if amount <= 0 or amount > 99999999:
                raise ValueError
        except:
            self.show_centered_message("错误", "数量必须是 1-99999999 的整数", QMessageBox.Warning)
            return False
        if target_identifiers_str:
            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False
                current_uid = self.current_user.get("uid", "")
                is_current_admin = current_uid.lower().startswith("admin")
                for tu in target_users:
                    if is_current_admin and tu["uid"].lower().startswith("admin") and tu["userid"] != self.current_user["userid"]:
                        self.show_centered_message("错误", f"不能操作其他管理员用户: {tu['username']}", QMessageBox.Warning)
                        return False
                for tu in target_users:
                    self.send_mail_to_user(tu, "管理员赠送信用点",
                                           f"管理员 {self.current_user['username']} 赠送了 {amount} 信用点。",
                                           [{"type": "credit", "amount": amount}])
                self.show_centered_message("成功", f"已向 {len(target_users)} 个用户发送信用点邮件", QMessageBox.Information)
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return False
        else:
            self.send_mail_to_user(self.current_user, "管理员添加信用点",
                                   f"给自己添加了 {amount} 信用点。",
                                   [{"type": "credit", "amount": amount}])
            self.show_centered_message("成功", "已给自己发送信用点邮件", QMessageBox.Information)
        return True

    def set_credit(self, amount, target_identifiers_str=None):
        try:
            amount = int(amount)
            if amount < 0 or amount > 99999999:
                raise ValueError
        except:
            self.show_centered_message("错误", "数量必须是 0-99999999 的整数", QMessageBox.Warning)
            return False
        if target_identifiers_str:
            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False
                for tu in target_users:
                    tu["credit"] = amount
                    self.save_user_data(tu)
                self.show_centered_message("成功", f"已设置 {len(target_users)} 个用户的信用点", QMessageBox.Information)
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return False
        else:
            self.current_user["credit"] = amount
            self._save_current_user()
            self.show_centered_message("成功", "已设置自己的信用点", QMessageBox.Information)
        return True

    # ---------- 签到 ----------
    def sign_in(self):
        if self.mp_client.connected:
            self.mp_client._send({'type': 'sign_in', 'data': {}})
            return

        # ---------- 单机签到逻辑（原有代码） ----------
        now = datetime.now()
        today_3am = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now < today_3am:
            reset_time = today_3am - timedelta(days=1)
        else:
            reset_time = today_3am

        last_sign = self.current_user.get("last_sign_date")
        if last_sign:
            try:
                last_sign_dt = datetime.fromisoformat(last_sign)
                if last_sign_dt >= reset_time:
                    self.show_centered_message("提示", "今日已经签到过了，凌晨3点后再来哦！", QMessageBox.Warning)
                    self.create_menu()
                    return
            except:
                pass

        uid = self.current_user.get("uid", "").lower()
        is_trailblazer = uid.startswith("trailblazer")
        base_credit = 36000 if is_trailblazer else 7200
        self.current_user["credit"] = self.current_user.get("credit", 0) + base_credit
        if self.current_user["credit"] > 99999999:
            self.current_user["credit"] = 99999999

        extra_msg = ""
        today_date = now.date()
        try:
            lunar = LunarDate.fromSolarDate(today_date.year, today_date.month, today_date.day)
            if today_date.year == 2026 and lunar.month == 1 and 1 <= lunar.day <= 15:
                self.current_user["credit"] = self.current_user.get("credit", 0) + 888
                if self.current_user["credit"] > 99999999:
                    self.current_user["credit"] = 99999999
                owned = self.current_user.get("owned_clothes", ["normal"])
                if "26_Newyear" not in owned:
                    owned.append("26_Newyear")
                    self.current_user["owned_clothes"] = owned
                    extra_msg = "，并获得新春时装[柿柿如意] + 888信用点"
                else:
                    extra_msg = "，并获得888信用点（已有时装不再赠送）"
        except:
            pass

        self.current_user["last_sign_date"] = now.isoformat()
        self._save_current_user()

        history = self.current_user.get("redeem_history", [])
        history.append(f"{now.strftime('%Y-%m-%d %H:%M:%S')} - 签到获得 {base_credit} 信用点{extra_msg}")
        self.current_user["redeem_history"] = history
        self._save_current_user()

        # === 新增统计 ===
        self.check_month_reset()
        self.current_user["total_sign_days"] += 1
        self.current_user["monthly_sign_days"] += 1
        self.current_user["total_interactions"] += 1
        self.current_user["monthly_interactions"] += 1
        self.current_user["monthly_credit_gained"] += base_credit
        self._save_current_user()

        self.create_menu()
        self.show_centered_message("成功", f"签到成功！获得 {base_credit} 信用点{extra_msg}。", QMessageBox.Information)

    def is_signed_today(self):
        if self.mp_client.connected:
            # 联机时以服务端状态为准
            return self.online_signed_today

        # ---------- 单机模式（原有代码） ----------
        last_sign = self.current_user.get("last_sign_date")
        if not last_sign:
            return False
        try:
            last_sign_dt = datetime.fromisoformat(last_sign)
            now = datetime.now()
            today_3am = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if now < today_3am:
                reset_time = today_3am - timedelta(days=1)
            else:
                reset_time = today_3am
            return last_sign_dt >= reset_time
        except:
            return False

    def update_bag_display(self):
        pass

    def open_shop(self):
        shop = ShopWindow(self, self)
        shop.open()

    def give_item(self, amount, item_type, target_identifiers_str=None, clothes_id=None):
        if item_type not in ("cake", "clothes"):
            self.show_centered_message("错误", f"未知物品类型: {item_type}", QMessageBox.Warning)
            return False
        if item_type == "clothes" and not clothes_id:
            self.show_centered_message("错误", "时装必须指定 clothes_id", QMessageBox.Warning)
            return False
        try:
            amount = int(amount)
            if amount <= 0 or amount > 9999:
                raise ValueError
        except:
            self.show_centered_message("错误", "数量必须是 1-9999 的整数", QMessageBox.Warning)
            return False

        items = []
        if item_type == "cake":
            items.append({"type": "cake", "amount": amount})
        else:
            items.append({"type": "clothes", "id": clothes_id})

        if target_identifiers_str:
            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False
                current_uid = self.current_user.get("uid", "")
                is_current_admin = current_uid.lower().startswith("admin")
                for tu in target_users:
                    if is_current_admin and tu["uid"].lower().startswith("admin") and tu["userid"] != self.current_user["userid"]:
                        self.show_centered_message("错误", f"不能操作其他管理员用户: {tu['username']}", QMessageBox.Warning)
                        return False
                for tu in target_users:
                    self.send_mail_to_user(tu, "管理员赠送物品",
                                           f"管理员 {self.current_user['username']} 赠送了 {'橡木蛋糕卷' if item_type=='cake' else '时装['+AVAILABLE_CLOTHES[clothes_id]['name']+']'}。",
                                           items)
                self.show_centered_message("成功", f"已向 {len(target_users)} 个用户发送物品邮件", QMessageBox.Information)
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return False
        else:
            self.send_mail_to_user(self.current_user, "管理员添加物品",
                                   f"给自己添加了 {'橡木蛋糕卷' if item_type=='cake' else '时装['+AVAILABLE_CLOTHES[clothes_id]['name']+']'}。",
                                   items)
            self.show_centered_message("成功", "已给自己发送物品邮件", QMessageBox.Information)
        return True

    def show_join_dialog(self):
        dlg = JoinServerDialog(self, self)  # 第一个参数是 firefly，第二个是 parent
        if dlg.exec_() == QDialog.Accepted:
            # 原有逻辑：如果通过列表/手动输入加入，会走到这里
            # 但直接加入已在按钮中处理，这里保留原有手动输入的兼容
            host, port = dlg.get_address()
            self.join_server(host, port)

    class CountryFetcher(QThread):
        finished = pyqtSignal(str)

        def run(self):
            country = 'UNKNOWN'
            try:
                with urllib.request.urlopen('https://ipapi.co/country/', timeout=3) as resp:
                    country = resp.read().decode().strip()
                    if not country or len(country) != 2:
                        country = 'UNKNOWN'
            except Exception:
                try:
                    with urllib.request.urlopen('https://ipinfo.io/country', timeout=3) as resp:
                        country = resp.read().decode().strip()
                except Exception:
                    pass
            self.finished.emit(country)

    def join_server(self, host, port):
        """加入服务器（异步获取国家代码）"""
        user = self.current_user
        self.local_inventory_backup = {
            'balance': user.get('balance', 0),
            'credit': user.get('credit', 0),
            'owned_clothes': user.get('owned_clothes', ['normal']),
            'star_rail_tickets': user.get('star_rail_tickets', 0)
        }
        self.connecting = True
        self.country_fetcher = self.CountryFetcher()
        self.country_fetcher.finished.connect(lambda country: self._do_connect(host, port, user, country))
        self.country_fetcher.start()

    def _do_connect(self, host, port, user, country):
        """实际执行连接（已获取到国家代码）"""
        self.mp_client.connect_to_server(host, port, user['userid'], user['uid'], user['username'], country)
        # 可选：清除 fetcher 引用
        self.country_fetcher = None

    def disconnect_server(self, show_message=True):
        if self._rename_dialog:
            try:
                self._rename_dialog.close()
            except:
                pass
            self._rename_dialog = None
        self._is_manual_disconnect = True
        self._suppress_disconnect_message = True
        self.banned = False
        self.is_admin = False  # 默认非管理员，登录后更新
        self._waiting_for_name = False
        self.rename_required = False
        if self.mp_client.connected:
            if show_message:
                self.show_centered_message("提示", "已断开与服务器的连接", QMessageBox.Information)
            if self.online_inventory:
                self.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.online_inventory}})
            self.mp_client.disconnect()
        # 恢复单机状态（内存恢复）
        self._restore_single_player_state()
        # 删除备份文件（正常断开时删除）
        backup_path = os.path.join(USER_DATA_DIR, 'single_state_backup.json')
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except:
                pass
        # 清理其他联机相关变量
        self.server_uid = None
        self.server_permission = 'user'
        self.online_uid = ''
        self.online_name = None
        self.online_redeemed_codes = []
        self.online_signed_today = False
        self._pending_mail_request = None
        # ★ 新增：重置 AI 状态为默认 True（单机可用）
        self.ai_enabled = True
        self.ping_timer.stop()
        self.create_menu()
        # 重置禁言状态
        self.muted_until = None
        if self.chat_window:
            self.chat_window.update_mute_status(None)

    def update_bag_display(self):
        # 这个方法会被背包菜单、商店等调用，需要根据联机状态返回不同数据
        # 由于菜单是动态生成的，每次打开菜单时重新读取 balance/credit 即可
        # 这里只需重写菜单中获取背包数据的方式
        pass

    def clear_item(self, item_type, target_identifiers_str=None):
        if item_type != "cake":
            self.show_centered_message("错误", "当前仅支持清空橡木蛋糕卷", QMessageBox.Warning)
            return False
        if target_identifiers_str:
            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False
                for tu in target_users:
                    tu["balance"] = 0
                    self.save_user_data(tu)
                self.show_centered_message("成功", f"已清空 {len(target_users)} 个用户的蛋糕卷", QMessageBox.Information)
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return False
        else:
            self.current_user["balance"] = 0
            self._save_current_user()
            self.show_centered_message("成功", "已清空自己的蛋糕卷", QMessageBox.Information)
        return True

        # 构建物品描述字符串（用于日志）
        def describe_items(items):
            desc = []
            for item in items:
                t = item["type"]
                if t == "cake":
                    desc.append(f"蛋糕卷x{item['amount']}")
                elif t == "credit":
                    desc.append(f"信用点x{item['amount']}")
                elif t == "clothes":
                    name = AVAILABLE_CLOTHES.get(item["id"], {}).get("name", item["id"])
                    desc.append(f"时装[{name}]")
            return ", ".join(desc) if desc else "无"

        if target_spec is None or target_spec.strip().lower() == "none":
            # 全局撤销
            if cinfo.get("revoked"):
                self.show_centered_message("提示", f"兑换码 {code} 已经被全局撤销过了", QMessageBox.Warning)
                return
            cinfo["revoked"] = True
            cinfo["revoked_by"] = self.current_user["username"]
            cinfo["revoked_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            items_to_remove = cinfo.get("items", [])
            item_desc = describe_items(items_to_remove)

            revoked_users_list = []
            for filename in os.listdir(USER_DATA_DIR):
                if not filename.endswith(".json"):
                    continue
                filepath = os.path.join(USER_DATA_DIR, filename)
                with open(filepath, "r", encoding="utf-8") as f:
                    user_data = json.load(f)
                redeemed = user_data.get("redeemed_codes", [])
                if code not in redeemed:
                    continue

                # 扣除所有物品
                for item in items_to_remove:
                    itype = item["type"]
                    if itype == "cake":
                        user_data["balance"] = user_data.get("balance", 0) - item["amount"]
                        if user_data["balance"] < 0:
                            user_data["balance"] = 0
                    elif itype == "credit":
                        user_data["credit"] = user_data.get("credit", 0) - item["amount"]
                        if user_data["credit"] < 0:
                            user_data["credit"] = 0
                    elif itype == "clothes":
                        owned = user_data.get("owned_clothes", [])
                        cid = item["id"]
                        if cid in owned:
                            owned.remove(cid)
                            user_data["owned_clothes"] = owned

                redeemed.remove(code)
                user_data["redeemed_codes"] = redeemed
                history = user_data.get("redeem_history", [])
                history.append(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [系统] 兑换码 {code} 被管理员 {self.current_user['username']} 撤销，已扣除：{item_desc}"
                )
                user_data["redeem_history"] = history
                self.save_user_data(user_data)
                revoked_users_list.append(user_data["username"])

            history = self.current_user.get("redeem_history", [])
            history.append(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [管理员] 全局撤销兑换码 {code}，影响用户：{', '.join(revoked_users_list) if revoked_users_list else '无'}"
            )
            self.current_user["redeem_history"] = history
            self._save_current_user()
            save_all_codes(all_codes)
            self.show_centered_message("成功", f"已全局撤销兑换码 {code}，影响 {len(revoked_users_list)} 个用户",
                                       QMessageBox.Information)

        else:
            # 指定用户撤销
            try:
                target_users = self.find_users_by_identifiers(target_spec)
                if not target_users:
                    self.show_centered_message("错误", "未找到任何有效用户", QMessageBox.Warning)
                    return
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return

            items_to_remove = cinfo.get("items", [])
            item_desc = describe_items(items_to_remove)

            revoked_count = 0
            for tu in target_users:
                userid = tu["userid"]
                if userid in cinfo.get("revoked_users", []):
                    continue
                redeemed = tu.get("redeemed_codes", [])
                if code not in redeemed:
                    # 该用户并未使用此兑换码，仍标记为已撤销（防止将来使用）
                    cinfo["revoked_users"].append(userid)
                    continue

                # 扣除物品
                for item in items_to_remove:
                    itype = item["type"]
                    if itype == "cake":
                        tu["balance"] = tu.get("balance", 0) - item["amount"]
                        if tu["balance"] < 0:
                            tu["balance"] = 0
                    elif itype == "credit":
                        tu["credit"] = tu.get("credit", 0) - item["amount"]
                        if tu["credit"] < 0:
                            tu["credit"] = 0
                    elif itype == "clothes":
                        owned = tu.get("owned_clothes", [])
                        cid = item["id"]
                        if cid in owned:
                            owned.remove(cid)
                            tu["owned_clothes"] = owned

                redeemed.remove(code)
                tu["redeemed_codes"] = redeemed
                history = tu.get("redeem_history", [])
                history.append(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [系统] 兑换码 {code} 被管理员 {self.current_user['username']} 撤销，已扣除：{item_desc}"
                )
                tu["redeem_history"] = history
                self.save_user_data(tu)
                revoked_count += 1
                cinfo["revoked_users"].append(userid)

            history = self.current_user.get("redeem_history", [])
            history.append(
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [管理员] 撤销用户 {target_spec} 对兑换码 {code} 的使用资格，实际扣除 {revoked_count} 人"
            )
            self.current_user["redeem_history"] = history
            self._save_current_user()
            save_all_codes(all_codes)
            self.show_centered_message("成功", f"已撤销指定用户对兑换码 {code} 的使用资格，扣除 {revoked_count} 个用户",
                                       QMessageBox.Information)

    def show_help(self):
        uid_prefix = self.current_user.get("uid", "").lower()
        is_admin = uid_prefix.startswith("admin") or (
                    self.mp_client.connected and self.mp_client.server_permission == 'admin')
        if is_admin:
            help_text = (
                "【可用命令】\n"
                "/give <用户> <物品id> <数量>\n"
                "    给玩家发放物品（邮件形式），示例：/give @s cake 5\n"
                "/set <用户> <物品id> <数量>\n"
                "    直接设定玩家物品数量（覆盖），示例：/set Gubwin credit 1000\n"
                "/clear <用户> <物品或*>\n"
                "    清空物品（仅支持玩家名或@s），示例：/clear Gubwin cake\n"
                "/revoke <用户> <物品:数量&物品...>\n"
                "    直接扣除玩家物品（允许负值，时装只能移除）\n"
                "    用户: @a(所有人) @s(自己) 或 玩家名，不可对自己使用\n"
                "    示例: /revoke Gubwin cake:5&credit:1000\n"
                "/revoke code <兑换码> <用户>\n"
                "    撤销指定用户对兑换码的使用资格（撤回兑换码）\n"
                "    示例: /revoke code GIFT2026 @a\n"
                "/set newcode <兑换码> <物品列表> [有效期]\n"
                "    创建兑换码，物品列表如 cake:5&credit:1000&26_Newyear\n"
                "    有效期格式 YYYY-MM-DD 或 1Y2M3D\n"
                "/mute <用户> <时长>\n"
                "    禁言玩家，时长同封禁（如 30D 或 2026-12-31）\n"
                "    示例: /mute Gubwin 1D\n"
                "/unmute <用户>\n"
                "    解除禁言，示例: /unmute Gubwin\n"
                "/function AI on|off\n"
                "    启用或禁用服务器的 AI（在线对话）功能\n"
                "/function Chat on|off\n"
                "    启用或禁用服务器的多人聊天功能\n"
                "/ban <玩家> [时长]         封禁玩家\n"
                "/unban <玩家>              解封玩家\n"
                "/op <玩家>                 提升为管理员\n"
                "/deop <玩家>               取消管理员\n"
                "/move                      立即触发一次桌宠移动（需勾选允许移动）\n"
                "/help                      显示此帮助"
            )
        else:
            help_text = "【普通用户命令】\n直接输入兑换码字符串即可兑换。\n可用命令：\n/help  - 显示此帮助\n签到、商店、邮箱请通过右键菜单操作。"

        dialog = QDialog(self)
        dialog.setWindowTitle("帮助")
        dialog.setMinimumSize(550, 400)
        dialog.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 12px;
            }
            QTextEdit {
                background-color: #2D2D2D;
                color: #CCCCCC;
                border: 1px solid #555555;
                border-radius: 8px;
                padding: 10px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        text_edit = QTextEdit()
        text_edit.setPlainText(help_text)
        text_edit.setReadOnly(True)
        layout.addWidget(text_edit)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dialog.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignCenter)
        dialog.setLayout(layout)
        set_window_emoji_icon(dialog, "❓")
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())
        dialog.open()

    def process_admin_command(self, code):
        """单机模式下处理管理员命令（新版格式）"""
        if not code.startswith('/'):
            return False
        cmd_line = code[1:].strip()
        if not cmd_line:
            return False

        parts = cmd_line.split()
        cmd_name = parts[0].lower()

        # ---------- 控制台专用命令黑名单（客户端禁止） ----------
        console_only_commands = ['restart', 'close', 'change', 'fix', 'all_versions']
        if cmd_name in console_only_commands:
            self.show_centered_message("提示", "此命令只能在服务器控制台执行", QMessageBox.Warning)
            return True

        # ---------- 禁止客户端执行时区和地区封禁 ----------
        if cmd_name in ['ban', 'unban'] and len(parts) >= 3:
            sub_cmd = parts[1].lower()
            if sub_cmd in ['area', 'timezone']:
                self.show_centered_message("提示", "此命令只能在服务器控制台执行", QMessageBox.Warning)
                return True

        # ===== 帮助 =====
        if cmd_name == "help":
            self.show_help()
            return True

        # ===== 服务器专属命令（仅联机） =====
        server_only = ['ban', 'unban', 'op', 'deop', 'fix', 'all_versions', 'banarea', 'untimezone']
        if cmd_name in server_only or cmd_name.startswith('ban') or cmd_name.startswith('unban'):
            self.show_centered_message("提示", "该命令仅适用于联机模式，请先加入服务器。", QMessageBox.Warning)
            return True

        # ===== 移动命令 =====
        if cmd_name == "move":
            if not self.allow_move:
                self.show_centered_message("提示", "请先在右键菜单中勾选“允许移动”", QMessageBox.Warning)
            elif self.is_moving:
                self.show_centered_message("提示", "桌宠正在移动中", QMessageBox.Warning)
            elif self.current_animation_state != "Standby":
                self.show_centered_message("提示", "当前状态不是Standby，无法移动", QMessageBox.Warning)
            else:
                self.start_random_move()
            return True

        # ===== /set newcode 创建兑换码 =====
        if cmd_name == "set" and len(parts) >= 3 and parts[1].lower() == "newcode":
            if len(parts) < 4:
                self.show_centered_message("错误", "格式: /set newcode <兑换码> <物品:数量&物品...> [有效期]",
                                           QMessageBox.Warning)
                return True
            code = parts[2]
            args = parts[3:]
            expire_str = None
            # 使用 parse_time_spec 检测最后一个参数是否为有效期
            if args:
                delta, dt = parse_time_spec(args[-1])
                if delta is not None or dt is not None:
                    expire_str = args[-1]
                    items_spec = ' '.join(args[:-1])
                else:
                    items_spec = ' '.join(args)
            else:
                items_spec = ''
            items = self._parse_items_spec(items_spec)
            if items is None:
                self.show_centered_message("错误", "物品列表解析失败", QMessageBox.Warning)
                return True
            expire_time = None
            if expire_str:
                delta, until_dt = parse_time_spec(expire_str)
                if until_dt:
                    expire_time = until_dt.isoformat()
                elif delta:
                    expire_time = (datetime.now() + delta).isoformat()
                else:
                    self.show_centered_message("错误", f"无效的有效期格式: {expire_str}", QMessageBox.Warning)
                    return True
            all_codes = load_all_codes()
            if code in all_codes:
                self.show_centered_message("错误", f"兑换码 {code} 已存在", QMessageBox.Warning)
                return True
            all_codes[code] = {
                "max_uses": 0,
                "used_count": 0,
                "created_by": self.current_user["username"],
                "created_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "target_user": None,
                "items": items,
                "expire_time": expire_time,
                "revoked": False,
                "revoked_users": [],
                "revoked_by": None,
                "revoked_time": None
            }
            save_all_codes(all_codes)
            history = self.current_user.get("redeem_history", [])
            history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 创建兑换码 {code}")
            self.current_user["redeem_history"] = history
            self._save_current_user()
            self.show_centered_message("成功", f"兑换码 {code} 已创建", QMessageBox.Information)
            return True

        # ===== /give 发放物品 =====
        if cmd_name == "give":
            if len(parts) < 3:
                self.show_centered_message("错误", "格式: /give <用户> <物品> [数量] (数量默认为1，时装自动为1)",
                                           QMessageBox.Warning)
                return True
            user_spec = parts[1]
            item_id = parts[2].lower()
            if len(parts) >= 4:
                try:
                    amount = int(parts[3])
                except:
                    self.show_centered_message("错误", "数量必须为整数", QMessageBox.Warning)
                    return True
                if amount <= 0:
                    self.show_centered_message("错误", "数量必须大于0", QMessageBox.Warning)
                    return True
            else:
                amount = 1
            # 时装强制数量为1
            if item_id not in ['cake', 'credit', 'ticket']:
                amount = 1

            # 获取目标用户
            target_users = []
            if user_spec == '@a':
                for filename in os.listdir(USER_DATA_DIR):
                    if filename == os.path.basename(CODES_FILE):
                        continue
                    if filename.endswith(".json"):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("userid"):
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", "没有找到任何用户", QMessageBox.Warning)
                    return True
            elif user_spec == '@s':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            # 构造物品
            items = []
            if item_id == 'cake':
                items.append({'type': 'cake', 'amount': amount})
            elif item_id == 'credit':
                items.append({'type': 'credit', 'amount': amount})
            elif item_id == 'ticket':
                items.append({'type': 'star_rail_ticket', 'amount': amount})
            else:
                items.append({'type': 'clothes', 'id': item_id})

            # 发送邮件给每个目标
            for u in target_users:
                self.send_mail_to_user(u, "管理员赠送物品", f"管理员 {self.current_user['username']} 赠送了你物品。",
                                       items)
            self.show_centered_message("成功", f"已向 {len(target_users)} 个用户发送物品邮件", QMessageBox.Information)
            return True

        # ===== /set 设置物品 =====
        if cmd_name == "set":
            if len(parts) < 4:
                self.show_centered_message("错误", "格式: /set <用户> <物品> <数量>", QMessageBox.Warning)
                return True
            user_spec = parts[1]
            item_id = parts[2].lower()
            try:
                amount = int(parts[3])
            except:
                self.show_centered_message("错误", "数量必须为整数", QMessageBox.Warning)
                return True
            if amount < 0 or amount > 99999999:
                self.show_centered_message("错误", "数量必须在0-99999999之间", QMessageBox.Warning)
                return True

            # 获取目标用户
            target_users = []
            if user_spec == '@a':
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("userid"):
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", "没有找到任何用户", QMessageBox.Warning)
                    return True
            elif user_spec == '@s':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            # 设置物品
            for u in target_users:
                if item_id == 'cake':
                    u['balance'] = amount
                elif item_id == 'credit':
                    u['credit'] = amount
                elif item_id == 'ticket':
                    u['star_rail_tickets'] = amount
                else:
                    self.show_centered_message("错误", f"未知物品: {item_id}", QMessageBox.Warning)
                    return True
                self.save_user_data(u)
            self.show_centered_message("成功", f"已设置 {len(target_users)} 个用户的 {item_id} 为 {amount}",
                                       QMessageBox.Information)
            return True

        # ===== /clear 清空物品 =====
        if cmd_name == "clear":
            if len(parts) < 3:
                self.show_centered_message("错误", "格式: /clear <用户> <物品或*>", QMessageBox.Warning)
                return True
            user_spec = parts[1]
            item_spec = parts[2].lower()

            # 仅支持单个用户或@s，不支持@a
            target_users = []
            if user_spec == '@s':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                                break
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            for u in target_users:
                if item_spec == '*':
                    u['balance'] = 0
                    u['credit'] = 0
                    # 不清除时装和专票
                elif item_spec == 'cake':
                    u['balance'] = 0
                elif item_spec == 'credit':
                    u['credit'] = 0
                else:
                    self.show_centered_message("错误", f"未知物品: {item_spec}", QMessageBox.Warning)
                    return True
                self.save_user_data(u)
            self.show_centered_message("成功", f"已清空 {item_spec}，影响 {len(target_users)} 个用户",
                                       QMessageBox.Information)
            return True

        # ===== /revoke 物品扣除（不带 code），含统计更新 =====
        if cmd_name == "revoke" and not (len(parts) >= 3 and parts[1].lower() == "code"):
            if len(parts) < 3:
                self.show_centered_message("错误", "格式: /revoke <用户> <物品:数量&物品...>", QMessageBox.Warning)
                return True

            user_spec = parts[1]
            items_spec = ' '.join(parts[2:])

            # 解析物品列表
            items = []
            for part in items_spec.split('&'):
                part = part.strip()
                if not part:
                    continue
                if ':' in part:
                    itype, qty = part.split(':', 1)
                    try:
                        qty = int(qty)
                    except:
                        self.show_centered_message("错误", f"数量错误: {part}", QMessageBox.Warning)
                        return True
                else:
                    itype = part
                    qty = 1
                if itype not in ('cake', 'credit', 'ticket') and itype not in AVAILABLE_CLOTHES:
                    self.show_centered_message("错误", f"未知物品: {itype}", QMessageBox.Warning)
                    return True
                items.append({'type': itype, 'amount': qty})

            if not items:
                self.show_centered_message("错误", "未指定有效物品", QMessageBox.Warning)
                return True

            # 获取目标用户（单机支持 @s 或具体用户，@a 仅操作自己）
            target_users = []
            if user_spec == '@s':
                target_users = [self.current_user]
            elif user_spec == '@a':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            for u in target_users:
                inv = u  # 单机用户数据即 inventory（包含 balance/credit等）
                items_desc = []
                # 记录统计变动
                credit_delta = 0
                cake_delta = 0
                stats_delta = {}

                for item in items:
                    itype = item['type']
                    amount = item['amount']
                    if itype == 'cake':
                        inv['balance'] = inv.get('balance', 0) - amount
                        cake_delta -= amount
                        items_desc.append(f"橡木蛋糕卷 x{amount}")
                    elif itype == 'credit':
                        inv['credit'] = inv.get('credit', 0) - amount
                        if inv['credit'] < -99999999:
                            inv['credit'] = -99999999
                        credit_delta -= amount
                        items_desc.append(f"信用点 x{amount}")
                    elif itype == 'ticket':
                        inv['star_rail_tickets'] = inv.get('star_rail_tickets', 0) - amount
                        items_desc.append(f"星铁专票 x{amount}")
                    else:  # 时装
                        if amount > 0:
                            owned = inv.get('owned_clothes', ['normal'])
                            if itype in owned:
                                owned.remove(itype)
                                inv['owned_clothes'] = owned
                                clothes_name = AVAILABLE_CLOTHES.get(itype, {}).get('name', itype)
                                items_desc.append(f"时装 [{clothes_name}]")
                        # 时装扣除不影响统计

                # ★ 更新统计（仅当扣除了 credit 或 cake）
                if credit_delta != 0 or cake_delta != 0:
                    # 月度统计
                    self.check_month_reset()
                    if 'monthly_credit_gained' in u:
                        u['monthly_credit_gained'] += credit_delta
                    if 'monthly_cake_gained' in u:
                        u['monthly_cake_gained'] += cake_delta
                    # redeem_stats
                    stats = u.setdefault('redeem_stats', {})
                    if credit_delta != 0:
                        stats['credit'] = stats.get('credit', 0) + credit_delta
                    if cake_delta != 0:
                        stats['cake'] = stats.get('cake', 0) + cake_delta
                    # 确保 total 统计不会为负（可选，但不强制）
                    # 如果用户是当前用户，更新内存并保存
                    if u.get('userid') == self.current_user.get('userid'):
                        self._save_current_user()
                    else:
                        self.save_user_data(u)

                # 保存用户数据（已经通过上面保存，但若没有统计变动则仍需保存扣除的物品）
                if u.get('userid') == self.current_user.get('userid'):
                    self._save_current_user()
                else:
                    self.save_user_data(u)

                # 发送邮件通知
                if items_desc:
                    mail_content = f"管理员已从您的账户中扣除以下物品：\n" + "\n".join(items_desc)
                else:
                    mail_content = "管理员执行了物品扣除操作，但未影响您的任何物品。"
                send_mail(u, "物品扣除通知", mail_content, [])
                # 如果操作的是自己，刷新界面
                if u.get('userid') == self.current_user.get('userid'):
                    self.update_bag_display()
                    self.show_centered_message("成功", f"已扣除物品，请查收邮箱通知", QMessageBox.Information)
                else:
                    self.show_centered_message("成功", f"已扣除用户 {u.get('username')} 的物品，并发送了邮件通知",
                                               QMessageBox.Information)

            return True

        # ===== /revoke code 撤销兑换码 =====
        if cmd_name == "revoke" and len(parts) >= 3 and parts[1].lower() == "code":
            if len(parts) < 4:
                self.show_centered_message("错误", "格式: /revoke code <兑换码> <用户>", QMessageBox.Warning)
                return True
            code = parts[2]
            user_spec = parts[3]

            all_codes = load_all_codes()
            if code not in all_codes:
                self.show_centered_message("错误", f"兑换码 {code} 不存在", QMessageBox.Warning)
                return True
            cinfo = all_codes[code]
            if cinfo.get("revoked"):
                self.show_centered_message("提示", "该兑换码已被撤销", QMessageBox.Warning)
                return True

            # 获取目标用户
            target_users = []
            if user_spec == '@a':
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("userid"):
                                target_users.append(u)
                        except:
                            continue
            elif user_spec == '@s':
                target_users = [self.current_user]
            else:
                for filename in os.listdir(USER_DATA_DIR):
                    if filename.endswith(".json") and filename != os.path.basename(CODES_FILE):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                u = json.load(f)
                            if u.get("username") == user_spec or u.get("userid") == user_spec:
                                target_users.append(u)
                        except:
                            continue
                if not target_users:
                    self.show_centered_message("错误", f"未找到用户 {user_spec}", QMessageBox.Warning)
                    return True

            # 标记撤销
            for u in target_users:
                userid = u["userid"]
                if userid not in cinfo.get("revoked_users", []):
                    cinfo.setdefault("revoked_users", []).append(userid)
            save_all_codes(all_codes)
            self.show_centered_message("成功", f"已撤销 {len(target_users)} 个用户对兑换码 {code} 的使用资格",
                                       QMessageBox.Information)
            return True

        # ===== 其他未知命令 =====
        self.show_centered_message("提示", f"未知命令或该命令不支持单机模式", QMessageBox.Warning)
        return True

    def clear_all_items(self, target_identifiers_str=None):
            """清空目标用户的所有物品（蛋糕卷+信用点）"""
            if not target_identifiers_str:
                self.show_centered_message("错误", "请指定目标用户（如 @a）", QMessageBox.Warning)
                return False

            try:
                target_users = self.find_users_by_identifiers(target_identifiers_str)
                if target_users is None:
                    self.show_centered_message("错误", "目标用户列表无效", QMessageBox.Warning)
                    return False

                # 遍历清空蛋糕卷 + 信用点
                for user in target_users:
                    user["balance"] = 0  # 清空蛋糕卷
                    user["credit"] = 0   # 清空信用点
                    self.save_user_data(user)

                self.show_centered_message("成功", f"已清空 {len(target_users)} 个用户的所有物品！", QMessageBox.Information)
            except Exception as e:
                self.show_centered_message("错误", f"清空失败：{str(e)}", QMessageBox.Warning)
                return False
            return True

    def redeem_code(self):
        dialog = RedeemCodeDialog(self)
        self._redeem_dialog = dialog
        dialog.code_submitted.connect(self.handle_redeem_code)
        dialog.open()

    def handle_redeem_code(self, code):
        """
        兑换码窗口输入处理（支持命令）
        - 如果输入以 '/' 开头，作为命令处理：
            - /help、/move 本地处理
            - 联机管理员命令发送到服务端
            - /revoke 特殊处理：禁止对自己使用（@s 或自己的标识）
        - 否则作为兑换码处理（联机发送 redeem_code 请求，单机本地验证并发放）
        """
        dialog = getattr(self, '_redeem_dialog', None)
        if dialog is None or not dialog.isVisible():
            return

        if not code:
            QMessageBox.warning(dialog, "提示", "请输入兑换码或命令")
            return

        # ---------- 命令处理 ----------
        if code.startswith('/'):
            parts = code.split()
            cmd = parts[0].lower()

            # ----- 帮助（单机/联机通用） -----
            if cmd == '/help':
                self.show_help()
                return

            # ----- /move（单机本地功能） -----
            if cmd == '/move':
                if not self.allow_move:
                    self.show_centered_message("提示", "请先在右键菜单中勾选“允许移动”", QMessageBox.Warning)
                elif self.is_moving:
                    self.show_centered_message("提示", "桌宠正在移动中", QMessageBox.Warning)
                elif self.current_animation_state != "Standby":
                    self.show_centered_message("提示", "当前状态不是Standby，无法移动", QMessageBox.Warning)
                else:
                    self.start_random_move()
                return

            if cmd in ('/mute', '/unmute', '/function'):
                if not self.mp_client.connected:
                    self.show_centered_message("提示", "该命令仅适用于联机模式", QMessageBox.Warning)
                    return
                if self.mp_client.server_permission != 'admin':
                    self.show_centered_message("提示", "只有管理员可以使用此命令", QMessageBox.Warning)
                    return
                # 禁止对自己操作
                if cmd in ('/mute', '/unmute') and len(parts) >= 2:
                    user = parts[1]
                    if user == '@s' or user == (self.online_name or self.current_user['username']):
                        self.show_centered_message("提示", "不能对自己使用此命令", QMessageBox.Warning)
                        return
                self.mp_client.send_command(code)
                return

            # ----- /revoke 特殊处理（禁止对自己） -----
            if cmd == '/revoke' and not (len(parts) >= 3 and parts[1].lower() == 'code'):
                if len(parts) < 3:
                    self.show_centered_message("错误", "格式: /revoke <用户> <物品:数量&物品...>", QMessageBox.Warning)
                    return
                user_spec = parts[1]

                # 检查是否为自己（@s）
                if user_spec == '@s':
                    self.show_centered_message("提示", "不能对自己使用 /revoke 命令", QMessageBox.Warning)
                    return

                # 联机模式：检查是否匹配自己的标识，并发送到服务端
                if self.mp_client.connected:
                    # 权限检查（联机）
                    if self.mp_client.server_permission != 'admin':
                        self.show_centered_message("提示", "只有管理员可以使用此命令", QMessageBox.Warning)
                        return
                    my_name = self.online_name or self.current_user['username']
                    my_userid = self.current_user['userid']
                    my_server_uid = self.mp_client.server_uid
                    if user_spec == my_name or user_spec == my_userid or user_spec == my_server_uid:
                        self.show_centered_message("提示", "不能对自己使用 /revoke 命令", QMessageBox.Warning)
                        return
                    # 发送到服务端
                    self.mp_client.send_command(code)
                    return

                # 单机模式：调用本地处理（已包含权限检查）
                else:
                    # 单机模式下，检查是否为自己
                    my_userid = self.current_user['userid']
                    my_username = self.current_user['username']
                    if user_spec == '@a':
                        self.show_centered_message("提示", "单机模式下不允许使用 @a，请指定具体用户",
                                                   QMessageBox.Warning)
                        return
                    if user_spec == my_username or user_spec == my_userid:
                        self.show_centered_message("提示", "不能对自己使用 /revoke 命令", QMessageBox.Warning)
                        return
                    # 权限检查
                    if not self.current_user.get('uid', '').lower().startswith('admin'):
                        self.show_centered_message("提示", "只有管理员可以使用此命令", QMessageBox.Warning)
                        return
                    # 调用单机命令处理器（process_admin_command 会处理 /revoke 并扣除物品）
                    self.process_admin_command(code)
                    return

            # ----- 以下命令需要联机模式 -----
            if not self.mp_client.connected:
                self.show_centered_message("提示", "该命令仅适用于联机模式，请先加入服务器。", QMessageBox.Warning)
                return

            # ----- 权限检查（联机） -----
            if self.mp_client.server_permission != 'admin':
                self.show_centered_message("提示", "只有管理员可以使用此命令", QMessageBox.Warning)
                return

            # ----- 不允许对自己的命令列表（如 /op /deop 等） -----
            no_self_cmds = ['/op', '/deop', '/ban', '/unban', '/kick']
            if cmd in no_self_cmds:
                if len(parts) < 2:
                    self.show_centered_message("错误", f"用法: {cmd} <用户>", QMessageBox.Warning)
                    return
                user_spec = parts[1]
                my_name = self.online_name or self.current_user['username']
                my_userid = self.current_user['userid']
                my_server_uid = self.mp_client.server_uid
                if user_spec == '@s' or user_spec == my_name or user_spec == my_userid or user_spec == my_server_uid:
                    self.show_centered_message("提示", "不能对自己使用此命令", QMessageBox.Warning)
                    return

            # ----- 发送命令到服务端（联机） -----
            self.mp_client.send_command(code)
            return

        # ---------- 兑换码处理（非命令） ----------
        # 联机模式：发送兑换码到服务端
        if self.mp_client.connected:
            self._before_redeem_credit = self.current_user.get('credit', 0)
            self.mp_client._send({'type': 'redeem_code', 'data': {'code': code}})
            return

        # ---------- 单机模式兑换码处理（原有逻辑） ----------
        all_codes = load_all_codes()
        redeemed = self.current_user.get("redeemed_codes", [])

        if code not in all_codes:
            QMessageBox.warning(dialog, "提示", "无效的兑换码！")
            return
        cinfo = all_codes[code]
        if cinfo.get("revoked"):
            QMessageBox.warning(dialog, "提示", "该兑换码已被管理员撤销")
            return
        if self.current_user["userid"] in cinfo.get("revoked_users", []):
            QMessageBox.warning(dialog, "提示", "您对该兑换码的使用资格已被撤销")
            return
        expire_time = cinfo.get("expire_time")
        if expire_time:
            try:
                expire_dt = datetime.fromisoformat(expire_time)
                if datetime.now() > expire_dt:
                    QMessageBox.warning(dialog, "提示", "该兑换码已过期")
                    return
            except:
                pass
        if code in redeemed and cinfo.get("max_uses", 1) == 1:
            QMessageBox.warning(dialog, "提示", "您已经使用过该兑换码！")
            return
        target_spec = cinfo.get("target_user")
        if target_spec:
            try:
                target_users = self.find_users_by_identifiers(target_spec)
                if target_users:
                    user_match = any(tu["userid"] == self.current_user["userid"] for tu in target_users)
                    if not user_match:
                        QMessageBox.warning(dialog, "提示", "未知兑换码")
                        return
            except:
                QMessageBox.warning(dialog, "提示", "兑换码配置错误")
                return
        if cinfo["max_uses"] != 0 and cinfo["used_count"] >= cinfo["max_uses"]:
            QMessageBox.warning(dialog, "提示", "该兑换码已失效（达到使用次数上限）")
            return

        # 单机奖励发放
        items = []
        for item in cinfo.get("items", []):
            itype = item["type"]
            if itype == "cake":
                items.append({"type": "cake", "amount": item["amount"]})
            elif itype == "credit":
                items.append({"type": "credit", "amount": item["amount"]})
            elif itype == "clothes":
                items.append({"type": "clothes", "id": item["id"]})
            elif itype == "star_rail_ticket":
                items.append({"type": "star_rail_ticket", "amount": item["amount"]})

        self.send_mail_to_user(self.current_user, "兑换码奖励",
                               f"成功使用兑换码 {code}，获得以下物品。", items)
        if cinfo["max_uses"] != 0:
            self.current_user["redeemed_codes"] = redeemed + [code]
        cinfo["used_count"] += 1
        save_all_codes(all_codes)
        history = self.current_user.get("redeem_history", [])
        history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 使用兑换码 {code}")
        self.current_user["redeem_history"] = history
        self.check_month_reset()
        for item in items:
            if item["type"] == "credit":
                self.current_user["monthly_credit_gained"] += item["amount"]
            elif item["type"] == "cake":
                self.current_user["monthly_cake_gained"] += item["amount"]
        self._save_current_user()

        QMessageBox.information(dialog, "成功", "兑换成功！物品已发送至邮箱，请查收。")

    def show_history(self):
        if self.mp_client.connected:
            # 显示在线兑换记录
            win = HistoryWindow({'redeem_history': self.online_redeemed_codes}, False, self)
        else:
            is_admin = self.current_user.get("uid", "").lower().startswith("admin")
            win = HistoryWindow(self.current_user, is_admin, self)
        win.open()

    def open_mailbox(self):
        if self.mp_client.connected:
            self.mp_client._send({'type': 'get_mails'})
            self._pending_mail_request = 'open'
        else:
            mailbox_win = MailboxWindow(self, self)
            mailbox_win.exec_()

    # ---------- 动作方法 ----------
    def feed(self):
        self.check_month_reset()  # 新增
        now_ts = time.time()
        inv = self.get_active_inventory()
        last_feed = self.current_user.get("last_feed_time")
        cool_down_sec = 10 * 60
        if last_feed is not None:
            diff = now_ts - last_feed
            if diff < cool_down_sec:
                remain = cool_down_sec - diff
                m = int(remain // 60)
                s = int(remain % 60)
                self.show_centered_message("冷却提示", f"流萤已经吃饱啦！\n投喂冷却中！还需等待 {m}分{s}秒",
                                           QMessageBox.Warning)
                return
        balance = inv.get('balance', 0)
        if balance <= 0:
            self.show_centered_message("提示", "橡木蛋糕卷不足，无法投喂！", QMessageBox.Warning)
            return
        inv['balance'] = balance - 1
        self.current_user["last_feed_time"] = now_ts
        if self.mp_client.connected and self.online_inventory is not None:
            self.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.online_inventory}})
        else:
            self._save_current_user()

        # === 新增统计 ===
        self.current_user["total_interactions"] += 1
        self.current_user["monthly_interactions"] += 1
        self.current_user["total_affection_up"] += 1
        self.current_user["monthly_affection_up"] += 1
        self._save_current_user()

        self.last_animation_state = self.current_animation_state
        self.record_interaction(extra_seconds=10 * 60)

        if self.movie is None:
            return
        self.movie.stop()
        gif_path = None
        files = os.listdir(".")
        if 'Large' in files:
            gif_path = './assets/images/firefly/actions/eat/eat_Large.gif'
        elif 'small' in files:
            gif_path = './assets/images/firefly/actions/eat/eat_small.gif'
        elif 'normal' in files:
            gif_path = './assets/images/firefly/actions/eat/eat.gif'
        elif 'medium' in files:
            gif_path = './assets/images/firefly/actions/eat/eat_medium.gif'
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(5000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def heart(self):
        self.check_month_reset()
        if self.movie is None:
            return
        self.last_animation_state = self.current_animation_state
        self.record_interaction(extra_seconds=2 * 60)
        self.movie.stop()
        gif_path = None
        files = os.listdir(".")
        if 'Large' in files:
            gif_path = './assets/images/firefly/actions/Love/Love_Large.gif'
        elif 'small' in files:
            gif_path = './assets/images/firefly/actions/Love/Love_small.gif'
        elif 'normal' in files:
            gif_path = './assets/images/firefly/actions/Love/Love.gif'
        elif 'medium' in files:
            gif_path = './assets/images/firefly/actions/Love/Love_medium.gif'
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(1300, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

        # === 新增统计 ===
        self.current_user["total_interactions"] += 1
        self.current_user["monthly_interactions"] += 1
        self.current_user["total_affection_up"] += 1
        self.current_user["monthly_affection_up"] += 1
        self._save_current_user()

    def sleep(self):
        self.check_month_reset()
        if self.anim_restore_timer:
            self.anim_restore_timer.stop()
            self.anim_restore_timer = None
        if self.movie is None:
            return
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = os.listdir(".")
        if 'Large' in files:
            gif_path = './assets/images/firefly/actions/sleep/sleep_Large.gif'
        elif 'small' in files:
            gif_path = './assets/images/firefly/actions/sleep/sleep_small.gif'
        elif 'normal' in files:
            gif_path = './assets/images/firefly/actions/sleep/sleep.gif'
        elif 'medium' in files:
            gif_path = './assets/images/firefly/actions/sleep/sleep_medium.gif'

        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()

        self.current_animation_state = "Sleep"
        self.sleep_start_time = time.time()
        self.current_user["total_sleep_count"] += 1
        self.current_user["monthly_sleep_count"] += 1
        self._save_current_user()

    def restore_previous_animation(self):
        if self.last_animation_state == "Discomfort":
            self.changeToDiscomfort()
        else:
            self.changeToStandby()

    def changeToStandby(self):
        if self.movie is None:
            return
        self.movie.stop()
        files = os.listdir(".")
        if 'Large' in files:
            self.movie = QMovie('./assets/images/firefly/actions/Standby/Standby_Large.gif')
        elif 'small' in files:
            self.movie = QMovie('./assets/images/firefly/actions/Standby/Standby_small.gif')
        elif 'normal' in files:
            self.movie = QMovie('./assets/images/firefly/actions/Standby/Standby.gif')
        elif 'medium' in files:
            self.movie = QMovie('./assets/images/firefly/actions/Standby/Standby_medium.gif')
        self.label.setMovie(self.movie)
        self.movie.start()
        self.current_animation_state = "Standby"
        self.persistent_state = "Standby"
        if self.current_user:
            self.current_user["last_state"] = "Standby"
            self._save_current_user()

    def changeToDiscomfort(self):
        if self.movie is None:
            return
        self.movie.stop()
        files = os.listdir(".")
        if 'Large' in files:
            self.movie = QMovie('./assets/images/firefly/actions/Discomfort/Discomfort_Large.gif')
        elif 'small' in files:
            self.movie = QMovie('./assets/images/firefly/actions/Discomfort/Discomfort_small.gif')
        elif 'normal' in files:
            self.movie = QMovie('./assets/images/firefly/actions/Discomfort/Discomfort.gif')
        elif 'medium' in files:
            self.movie = QMovie('./assets/images/firefly/actions/Discomfort/Discomfort_medium.gif')
        self.label.setMovie(self.movie)
        self.movie.start()
        self.current_animation_state = "Discomfort"
        self.persistent_state = "Discomfort"
        if self.current_user:
            self.current_user["last_state"] = "Discomfort"
            self._save_current_user()
        show_notification_async(
            title="流萤桌宠",
            message=f"萤宝不开心了，给她投喂或陪她玩玩吧！",
            icon_path="./assets/images/firefly/Sadness/abandoned.ico",
            duration=3
        )

    def sing_and_dance(self):
        self.play_music()
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = os.listdir(".")
        if 'Large' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_Large.gif'
        elif 'small' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_small.gif'
        elif 'normal' in files:
            gif_path = './assets/images/firefly/actions/sing/sing.gif'
        elif 'medium' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_medium.gif'
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(88000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def sing_and_dance2(self):
        self.play_music2()
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = os.listdir(".")
        if 'Large' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_Large.gif'
        elif 'small' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_small.gif'
        elif 'normal' in files:
            gif_path = './assets/images/firefly/actions/sing/sing.gif'
        elif 'medium' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_medium.gif'
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(92000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def sing_and_dance3(self):
        self.play_music3()
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = os.listdir(".")
        if 'Large' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_Large.gif'
        elif 'small' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_small.gif'
        elif 'normal' in files:
            gif_path = './assets/images/firefly/actions/sing/sing.gif'
        elif 'medium' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_medium.gif'
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(155000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def sing_and_dance4(self):
        self.play_music4()
        self.last_animation_state = self.current_animation_state
        self.movie.stop()
        gif_path = None
        files = os.listdir(".")
        if 'Large' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_Large.gif'
        elif 'small' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_small.gif'
        elif 'normal' in files:
            gif_path = './assets/images/firefly/actions/sing/sing.gif'
        elif 'medium' in files:
            gif_path = './assets/images/firefly/actions/sing/sing_medium.gif'
        if gif_path and os.path.exists(gif_path):
            new_movie = QMovie(gif_path)
            self.label.setMovie(new_movie)
            self.movie = new_movie
            self.movie.start()
            QTimer.singleShot(340000, self.restore_previous_animation)
        else:
            self.restore_previous_animation()

    def check_new(self):
        webbrowser.open("https://github.com/Jimhow-Gu/Firefly-Table-Pet-Online/releases")

    def AI(self):
        # 检查是否被服务器禁用
        if not self.ai_enabled:
            self.show_centered_message("提示", "此功能已被服务器禁用", QMessageBox.Warning)
            return

        def check_process_running(process_name):
            for process in psutil.process_iter(['name']):
                if process.info['name'] == process_name:
                    return True
            return False

        process_name = 'AI.exe'
        if not check_process_running(process_name):
            subprocess.Popen(["./tools/AI.exe"])

    def open_AI(self):

        # 检查是否被服务器禁用
        if not self.ai_enabled:
            self.show_centered_message("提示", "此功能已被服务器禁用", QMessageBox.Warning)
            return

        # 单机模式下统计点击次数
        self.check_month_reset()
        if self.current_user:
            self.current_user["monthly_ai_click_count"] += 1
            self._save_current_user()

        def check_process_running(process_name):
            for process in psutil.process_iter(['name']):
                if process.info['name'] == process_name:
                    return True
            return False

        process_name = 'AI.exe'
        if not check_process_running(process_name):
            subprocess.Popen(["./tools/AI.exe"])

    def set_cursor(self, cursor_name):
        if self.current_file_name and os.path.exists(self.current_file_name):
            if not os.path.exists(cursor_name):
                os.rename(self.current_file_name, cursor_name)
            self.current_file_name = cursor_name

    def rename_file(self, new_name):
        if self.current_file_name and os.path.exists(self.current_file_name) and not os.path.exists(new_name):
            os.rename(self.current_file_name, new_name)
            self.current_file_name = new_name

    def set_default_cursor(self):
        self.setCursor(QCursor(Qt.ArrowCursor))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.draggable = True
            self.offset = event.pos()

    def mouseMoveEvent(self, event):
        if self.draggable:
            self.move(event.globalPos() - self.offset)
            self.movie.stop()
            files = os.listdir(".")
            for file in files:
                if 'Large' in file:
                    m = QMovie('./assets/images/firefly/actions/mention/mention_Large.gif')
                elif 'small' in file:
                    m = QMovie('./assets/images/firefly/actions/mention/mention_small.gif')
                elif 'normal' in file:
                    m = QMovie('./assets/images/firefly/actions/mention/mention.gif')
                elif 'medium' in file:
                    m = QMovie('./assets/images/firefly/actions/mention/mention_medium.gif')
            self.label.setMovie(m)
            m.start()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.draggable = False
            if self.movie:
                self.label.setMovie(self.movie)
                self.movie.start()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            try:
                pygame.mixer.music.load("./music/我将，点燃心海！.wav")
                pygame.mixer.music.set_volume(0.5)
                pygame.mixer.music.play()
            except Exception as e:
                print(f"双击播放失败: {e}")

    # ---------- 生日检测与欢迎 ----------
    def is_birthday_today(self):
        if not self.current_user:
            return False
        birthday_str = self.current_user.get("birthday")
        if not birthday_str:
            return False
        try:
            bd = datetime.strptime(birthday_str, "%Y-%m-%d")
            today = datetime.now()
            return (bd.month == today.month and bd.day == today.day)
        except:
            return False

    def has_birthday_greeted_this_year(self):
        if not self.current_user:
            return True
        last_year = self.current_user.get("last_birthday_greet_year")
        return last_year == datetime.now().year

    def send_birthday_mail(self):
        # 单机模式：直接本地发送
        if not self.mp_client.connected:
            # 原逻辑
            uid = self.current_user.get("uid", "").lower()
            if uid.startswith("admin"):
                greeting_name = "管理员"
            elif uid.startswith("trailblazer"):
                greeting_name = "开拓者"
            else:
                greeting_name = "用户"
            items = [
                {"type": "credit", "amount": 5200},
                {"type": "star_rail_ticket", "amount": 1}
            ]
            self.send_mail_to_user(
                self.current_user,
                "流萤的祝福",
                f"{greeting_name}，生日快乐！",
                items
            )
            self.current_user["last_birthday_greet_year"] = datetime.now().year
            self._save_current_user()
            return

        # 联机模式：请求服务端发送
        self.mp_client._send({
            'type': 'request_birthday_mail',
            'data': {
                'display_name': self.online_name or self.current_user.get('username', '用户')
            }
        })

    def hi_music(self):
        def play_welcome():
            try:
                uid = self.current_user.get("uid", "") if self.current_user else ""
                last_state = self.current_user.get("last_state", "Standby")

                # 生日处理
                if self.is_birthday_today() and not self.has_birthday_greeted_this_year():
                    # 尝试播放生日音频
                    birthday_sound = None
                    if uid.lower().startswith("admin"):
                        birthday_sound = "music/管理员生日快乐.wav"
                    elif uid.lower().startswith("trailblazer"):
                        birthday_sound = "music/开拓者生日快乐.wav"
                    else:
                        birthday_sound = "music/生日快乐.wav"
                    if os.path.exists(birthday_sound):
                        pygame.mixer.music.load(birthday_sound)
                        pygame.mixer.music.set_volume(0.5)
                        pygame.mixer.music.play()
                    # 发送生日邮件
                    self.send_birthday_mail()
                    return  # 播放完生日语音后不再播放常规欢迎

                # 常规欢迎流程
                if last_state == "Discomfort":
                    if uid.lower().startswith("admin"):
                        sound_file = "music/Admin_back.wav"
                    elif uid.lower().startswith("trailblazer"):
                        sound_file = "music/Trailblazer_back.wav"
                    else:
                        sound_file = "music/User_back.wav"
                    if os.path.exists(sound_file):
                        pygame.mixer.music.load(sound_file)
                        pygame.mixer.music.set_volume(0.5)
                        pygame.mixer.music.play()
                        return

                if uid.lower().startswith("trailblazer"):
                    sound_file = "music/开拓者！欢迎回来！.wav"
                elif uid.lower().startswith("admin"):
                    sound_file = "music/你好，管理员！欢迎回来！.wav"
                else:
                    sound_file = "./music/我叫流萤，是鸢尾花家系的译者。.wav"
                if os.path.exists(sound_file):
                    pygame.mixer.music.load(sound_file)
                    pygame.mixer.music.set_volume(0.5)
                    pygame.mixer.music.play()
                else:
                    print(f"欢迎音乐文件不存在: {sound_file}")
            except Exception as e:
                print(f"播放欢迎音乐失败: {e}")

        QTimer.singleShot(500, play_welcome)

    def _get_color_icon(self, color):
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        if color == "green":
            painter.setBrush(QBrush(QColor(0, 255, 0)))
        elif color == "yellow":
            painter.setBrush(QBrush(QColor(255, 215, 0)))
        else:  # red
            painter.setBrush(QBrush(QColor(255, 0, 0)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 12, 12)
        painter.end()
        return QIcon(pixmap)

    def create_icon_text_action(self, text, icon_path, parent_menu, enabled=True, tooltip=""):
        widget = QWidget()
        widget.setAttribute(Qt.WA_StyledBackground, True)
        widget.setStyleSheet("""
            QWidget {
                background: transparent;
                border-radius: 4px;
            }
        """)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(25, 7, 4, 7)
        layout.setSpacing(4)

        # 图标
        pixmap = QPixmap(icon_path)
        if not pixmap.isNull():
            pixmap = pixmap.scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_label = QLabel()
            icon_label.setPixmap(pixmap)
            layout.addWidget(icon_label)

        # 文字
        text_label = QLabel(text)
        text_label.setStyleSheet("color: #000000;")
        layout.addWidget(text_label)
        layout.addStretch()

        def on_enter(event):
            # 1. 清除菜单当前激活项（取消普通菜单高亮）
            parent_menu.setActiveAction(None)
            # 2. 手动设置自定义 widget 高亮背景（不受 enabled 影响）
            widget.setStyleSheet("""
                QWidget {
                    background: LightSkyBlue;
                    border-radius: 4px;
                }
            """)
            # 3. 同步重绘整个菜单，立即清除旧高亮
            parent_menu.repaint()
            # 4. 额外延迟更新，以防万一
            QTimer.singleShot(0, parent_menu.update)
            event.accept()

        def on_leave(event):
            # 恢复透明背景
            widget.setStyleSheet("""
                QWidget {
                    background: transparent;
                    border-radius: 4px;
                }
            """)
            event.accept()

        widget.enterEvent = on_enter
        widget.leaveEvent = on_leave

        action = QWidgetAction(parent_menu)
        action.setDefaultWidget(widget)
        action.setEnabled(enabled)  # 控制是否可点击，不影响悬停高亮
        action.setToolTip(tooltip)
        return action

    def contextMenuEvent(self, event):
        if self.is_moving:
            return  # 移动中不弹出菜单
        if self.connecting:  # 正在连接，不弹出菜单
            return
        if self.rename_required:
            QToolTip.showText(event.globalPos(), "请先设置昵称", self)
            return
        current_cursor = self.cursor()
        QApplication.setOverrideCursor(current_cursor)
        # ✅ 根据联机状态获取背包数据
        if self.mp_client.connected and self.online_inventory:
            inv = self.online_inventory
        else:
            inv = self.current_user

        balance = inv.get('balance', 0)
        credit = inv.get('credit', 0)
        tickets = inv.get('star_rail_tickets', 0)
        owned = inv.get('owned_clothes', ['normal'])

        try:
            menu = RoundedMenu(self)
            menu.setToolTipsVisible(True)
            action0 = QAction(f'Firefly_Win64_v{CLIENT_VERSION}_G', self)
            action0.setDisabled(True)
            action1 = QAction('🍖 投喂', self)  # 添加 emoji
            action2 = QAction('💖 比心', self)  # 添加 emoji
            action3 = QAction('😴 睡觉', self)  # 添加 emoji
            action_new = QAction('🔄 检查更新', self)  # 添加 emoji
            action9 = QAction('🔧 更多功能', self)  # 添加 emoji
            action100 = QAction('💬 在线对话', self)
            if not self.ai_enabled:
                action100.setEnabled(False)
                action100.setToolTip("⚠此功能已被服务器禁止！")

            menu.addAction(action0)
            menu.addAction(action1)
            menu.addAction(action2)
            menu.addAction(action3)

            if self.current_animation_state == "Sleep":
                action4 = QAction('✨ 唤醒', self)  # 添加 emoji
                action4.triggered.connect(self.wake_up)
                menu.addAction(action4)

            action_mailbox = QAction('📬 邮箱', self)
            menu.addAction(action_mailbox)
            shop_action = QAction('🛒 商店', self)  # 添加 emoji
            shop_action.triggered.connect(self.open_shop)
            menu.addAction(shop_action)
            bag = RoundedMenu(menu)
            bag.setTitle("🎒 背包")
            bag.setToolTipsVisible(True)

            # 仅添加数量大于0的物品
            items_added = False
            if balance > 0:
                action_backpack = QAction(f'🍰 橡木蛋糕卷：{balance}个', self)
                action_backpack.setDisabled(True)
                action_backpack.setToolTip("流萤最爱吃的食物")
                bag.addAction(action_backpack)
                items_added = True

            if credit > 0:
                action_credit = QAction(f'💰 信用点：{credit}', self)
                action_credit.setDisabled(True)
                action_credit.setToolTip("用于商店购买物品")
                bag.addAction(action_credit)
                items_added = True

            if tickets > 0:
                action_tickets = QAction(f'🎫 星铁专票：{tickets}张', self)
                action_tickets.setDisabled(True)
                action_tickets.setToolTip("来自列车的祝福")
                bag.addAction(action_tickets)
                items_added = True

            if not items_added:
                empty_action = QAction("暂无物品", self)
                empty_action.setDisabled(True)
                bag.addAction(empty_action)

            menu.addMenu(bag)

            clothes_menu = RoundedMenu(menu)
            clothes_menu.setTitle("👗 时装")
            clothes_menu.setToolTipsVisible(True)
            # ❌ 删掉这行：owned = self.current_user.get("owned_clothes", ["normal"])
            for clothes_id, info in AVAILABLE_CLOTHES.items():
                action = QAction(info["name"], self)
                if clothes_id in owned:
                    action.triggered.connect(lambda checked, cid=clothes_id: self.apply_clothes(cid))
                else:
                    action.setEnabled(False)
                    action.setToolTip(info.get("description", "未拥有"))
                clothes_menu.addAction(action)
            menu.addMenu(clothes_menu)

            volume_menu = RoundedMenu("🔊 音量")
            volume_action = create_volume_menu(self)
            volume_menu.addAction(volume_action)
            menu.addMenu(volume_menu)

            if self.is_signed_today():
                sign_action = menu.addAction("✅ 今日已签到")
                sign_action.setDisabled(True)
            else:
                sign_action = menu.addAction("📅 每日签到")
                sign_action.triggered.connect(self.sign_in)
            menu.addAction(sign_action)
            # ---------- 小三月的秘密基地 ----------
            OPEN_TIME = datetime(2026, 8, 6, 12, 0, 0)
            now = datetime.now()
            is_open = now >= OPEN_TIME

            icon_path = os.path.join("assets", "images", "MARCH_7TH.png")
            tooltip = "为所有崩铁玩家打造了一个属于我们的新世界！——小三月日记"

            if os.path.exists(icon_path):
                action_text = "小三月日记" + ("" if is_open else " 🔒")
                self._secret_action = self.create_icon_text_action(
                    action_text, icon_path, menu,
                    enabled=is_open,
                    tooltip=tooltip if is_open else ""
                )
            else:
                # 降级方案：普通 QAction（无图标）
                self._secret_action = QAction(self)
                self._secret_action.setText("小三月的秘密基地" + ("" if is_open else " 🔒"))
                self._secret_action.setEnabled(is_open)
                self._secret_action.setToolTip(tooltip if is_open else "")

            self._secret_action.triggered.connect(lambda: webbrowser.open("https://blog.mar7th.cn/"))
            menu.addAction(self._secret_action)

            # 悬停动态提示（仅当未开放时显示倒计时）
            def on_menu_hovered(action):
                if action is self._secret_action and not is_open:
                    # 未开放，启动计时器更新倒计时
                    if not self._secret_timer.isActive():
                        self._secret_timer.start()
                    self._update_secret_tooltip()
                    self._secret_hovered = True
                else:
                    # 已开放或悬停其他项，停止计时器并隐藏提示
                    if self._secret_timer.isActive():
                        self._secret_timer.stop()
                    QToolTip.hideText()
                    self._secret_hovered = False

            menu.hovered.connect(on_menu_hovered)

            def on_menu_hide():
                # 菜单隐藏时停止计时器并隐藏提示
                if self._secret_timer.isActive():
                    self._secret_timer.stop()
                QToolTip.hideText()
                self._secret_hovered = False

            menu.aboutToHide.connect(on_menu_hide)
            sing = RoundedMenu(menu)
            sing.setTitle("🎤 AI唱歌&二创")  # 添加 emoji
            sing.addAction("🎵 AI合唱-不眠之夜", self.sing_and_dance)
            sing.addAction("🎶 AI独唱-打上花火", self.sing_and_dance2)
            sing.addAction("🎙️ AI独唱-5:20AM", self.sing_and_dance3)
            sing.addAction("✨ 二创-Dream of Firefly", self.sing_and_dance4)
            sing.addAction("⏹️ 停止音乐", self.stop_music)
            menu.addMenu(sing)

            mouse_GUI = RoundedMenu(menu)
            mouse_GUI.setTitle("🐭 更改桌宠大小")  # 添加 emoji
            mouse_GUI.addAction("🔍 更改：大号", self.change_GUI_to_Large)
            mouse_GUI.addAction("🖥️ 更改：默认", self.change_GUI_to_normal)
            mouse_GUI.addAction("📏 更改：中号", self.change_GUI_to_medium)
            mouse_GUI.addAction("🔬 更改：小号", self.change_GUI_to_small)
            move_action = QAction("🚶‍ 允许移动", self)
            move_action.setCheckable(True)
            move_action.setChecked(self.allow_move)
            move_action.triggered.connect(self.toggle_move)
            mouse_GUI.addAction(move_action)
            menu.addMenu(mouse_GUI)
            menu.addSeparator()
            report_action = QAction('📊 当月收获报告', self)
            report_action.triggered.connect(self.generate_monthly_report)
            menu.addAction(report_action)

            mouse = RoundedMenu(menu)
            mouse.setTitle("🖱️ 修改指针")  # 添加 emoji
            mouse.addAction("🪲 流萤指针", lambda: self.set_cursor_style("Firefly"))
            mouse.addAction("🤖 萨姆指针", lambda: self.set_cursor_style("Sam"))
            mouse.addAction("🚫 停用指针", lambda: self.set_cursor_style("None"))
            menu.addMenu(mouse)

            menu.addAction(action9)
            menu.addAction(action_new)

            menu.addAction(action100)

            action_redeem = QAction('🎟️使用兑换码', self)  # 添加 emoji
            if not self.mp_client.connected:
                change_name_action = menu.addAction("✏️ 修改用户名")
                change_name_action.triggered.connect(self.change_username_single)
            action_history = QAction('📋 兑换明细', self)  # 添加 emoji
            menu.addAction(action_redeem)
            menu.addAction(action_history)
            mp_menu = RoundedMenu(menu)
            if self.mp_client.connected:
                latency = self.mp_client._latest_latency_ms
                if latency < 0:
                    latency = 0
                if latency <= 40:
                    icon = self._get_color_icon("green")
                elif latency <= 119:
                    icon = self._get_color_icon("yellow")
                else:
                    icon = self._get_color_icon("red")
                mp_menu.setIcon(icon)
                mp_menu.setTitle(f"🌐 多人联机 (延迟{latency}ms)")

                # ---------- 服务器名称（灰色不可点击） ----------
                name_action = mp_menu.addAction(self.server_display_name)
                name_action.setEnabled(False)
                # 可设置灰色字体，但 QAction 默认 disabled 时即为灰色，无需额外样式

                # ---------- 聊天室 ----------
                chat_action = mp_menu.addAction("💬 聊天室")
                chat_action.triggered.connect(self.open_chat_window)
                chat_action.setEnabled(self.chat_enabled)
                if not self.chat_enabled:
                    chat_action.setToolTip("⚠此功能已被服务器禁止！")
                else:
                    chat_action.setToolTip("")

                # 改名、退出等其它动作保持不变...
                rename_action = mp_menu.addAction("✏ 修改昵称")
                rename_action.triggered.connect(self.change_online_name)
                leave_action = mp_menu.addAction("🚪 退出游戏")
                leave_action.triggered.connect(self.disconnect_server)
            else:
                mp_menu.setTitle("🌐 多人联机")
                join_action = mp_menu.addAction("🎮 加入游戏")
                join_action.triggered.connect(self.show_join_dialog)
                join_action2 = mp_menu.addAction("🖥️ 获取服务端")
                join_action2.triggered.connect(self.get_server)
            menu.addMenu(mp_menu)
            user_info = menu.addAction(f"👤当前登录：{self.get_display_uid()}")
            user_info.setDisabled(True)
            menu.addAction(user_info)

            exit_all_action = QAction('🚪 退出桌宠', self)  # 添加 emoji
            exit_all_action.triggered.connect(self.show_exit_dialog)
            menu.addAction(exit_all_action)

            # 连接信号（保持不变）
            action1.triggered.connect(self.feed)
            action2.triggered.connect(self.heart)
            action3.triggered.connect(self.sleep)
            action9.triggered.connect(self.open_settings_tool)
            action_new.triggered.connect(self.check_new)
            action100.triggered.connect(self.open_AI)
            action_redeem.triggered.connect(self.redeem_code)
            action_history.triggered.connect(self.show_history)
            action_mailbox.triggered.connect(self.open_mailbox)

            pixmap = QPixmap('mouse/Firefly/p1.gif') if os.path.exists('mouse/Firefly/p1.gif') else QPixmap(16, 16)
            cursor = QCursor(pixmap)
            menu.setCursor(cursor)
            menu.exec_(event.globalPos())
        finally:
            QApplication.restoreOverrideCursor()

    def _update_secret_tooltip(self):
        """更新秘密基地菜单项的悬浮提示（动态倒计时）"""
        if not self._secret_action or not self._secret_hovered:
            return
        now = datetime.now()
        OPEN_TIME = datetime(2026, 8, 6, 12, 0, 0)
        if now >= OPEN_TIME:
            # 已开放，更新动作状态并停止定时器
            self._secret_action.setEnabled(True)
            self._secret_action.setText("小三月的秘密基地")  # 去掉🔒
            self._secret_action.setToolTip("为所有崩铁玩家打造了一个属于我们的新世界！——小三月日记")
            if self._secret_timer.isActive():
                self._secret_timer.stop()
            QToolTip.hideText()
            return

        delta = OPEN_TIME - now
        days = delta.days
        hours, rem = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        tooltip = f"距开放还有 {days}天{hours}时{minutes}分{seconds}秒"
        pos = QCursor.pos()
        QToolTip.showText(pos, tooltip, self._secret_action.parentWidget())

    def get_server(self):
        webbrowser.open("https://github.com/Jimhow-Gu/Firefly-Table-Pet-Online/releases")

    def toggle_move(self, checked):
        self.allow_move = checked
        if self.current_user:
            self.current_user["allow_move"] = checked
            self._save_current_user()
        if checked:
            delay = random.randint(5 * 60, 15 * 60) * 1000
            self.move_timer.start(delay)
        else:
            self.move_timer.stop()
            if self.is_moving:
                self.stop_move()

    def start_random_move(self):
        if not self.allow_move or self.is_moving:
            return
        screen = QApplication.primaryScreen().availableGeometry()

        # 水平移动距离范围：屏幕宽度的 1/6 ~ 2.0/6 （最大范围减少0.5/6）
        min_ratio = 1.0 / 6.0
        max_ratio = 2.0 / 6.0
        max_dx = screen.width() * random.uniform(min_ratio, max_ratio)
        min_horizontal = max(30, screen.width() * 0.05)  # 最小水平位移
        if max_dx < min_horizontal:
            max_dx = min_horizontal * 2

        # 强制水平分量不为零
        direction = random.choice([-1, 1])
        dx = direction * random.uniform(min_horizontal, max_dx)

        # 垂直位移范围，可为正、负或零
        min_ratio_v = 1.0 / 6.0
        max_ratio_v = 2.0 / 6.0
        max_dy = screen.height() * random.uniform(min_ratio_v, max_ratio_v)
        dy = random.uniform(-max_dy, max_dy)

        target_x = self.x() + dx
        target_y = self.y() + dy

        # 边界约束
        target_x = max(0, min(screen.width() - self.width(), target_x))
        target_y = max(0, min(screen.height() - self.height(), target_y))

        # 如果水平位移太小（边界导致），放弃此次移动
        if abs(target_x - self.x()) < min_horizontal:
            self.move_timer.start(random.randint(5 * 60, 15 * 60) * 1000)
            return

        self.move_start_x, self.move_start_y = self.x(), self.y()
        self.move_target_x, self.move_target_y = target_x, target_y

        distance = math.hypot(target_x - self.x(), target_y - self.y())
        if distance < 10:
            self.move_timer.start(random.randint(5 * 60, 15 * 60) * 1000)
            return

        self.move_duration = distance / self.move_speed
        self.move_start_time = time.time()
        self.is_moving = True

        # 暂停情感检查计时器
        if self.emo_check_timer.isActive():
            self.emo_check_timer.stop()

        # 播放行走动画
        direction_str = "right" if target_x >= self.x() else "left"
        self._play_walk_animation(direction_str)

        self.move_animation_timer.start(30)

    def _play_walk_animation(self, direction):
        size_suffix = ""
        if os.path.exists("Large"):
            size_suffix = "_Large"
        elif os.path.exists("small"):
            size_suffix = "_small"
        elif os.path.exists("medium"):
            size_suffix = "_medium"
        gif_path = f"./assets/images/firefly/actions/{direction}/{direction}{size_suffix}.gif"
        if not os.path.exists(gif_path):
            gif_path = f"./assets/images/firefly/actions/{direction}/{direction}.gif"
        new_movie = QMovie(gif_path)
        self.label.setMovie(new_movie)
        self.movie = new_movie
        self.movie.start()

    def update_move_position(self):
        elapsed = time.time() - self.move_start_time
        if elapsed >= self.move_duration:
            self.move(int(self.move_target_x), int(self.move_target_y))
            self.is_moving = False
            self.move_animation_timer.stop()
            self.changeToStandby()
            if not self.emo_check_timer.isActive():
                self.emo_check_timer.start(1000)
            if self.allow_move:
                delay = random.randint(5 * 60, 15 * 60) * 1000
                self.move_timer.start(delay)
        else:
            progress = elapsed / self.move_duration
            x = self.move_start_x + (self.move_target_x - self.move_start_x) * progress
            y = self.move_start_y + (self.move_target_y - self.move_start_y) * progress
            self.move(int(x), int(y))

    def wake_up(self):
        if self.anim_restore_timer and self.anim_restore_timer.isActive():
            self.anim_restore_timer.stop()
        self.anim_restore_timer = None

        self.sleep_start_time = None

        last_state = self.current_user.get("last_state", "Standby") if self.current_user else "Standby"

        if last_state == "Discomfort":
            QTimer.singleShot(50, self._do_wake_up_discomfort)
        else:
            QTimer.singleShot(50, self._do_wake_up_standby)

        if hasattr(self, 'wake_action_tray') and self.wake_action_tray:
            QTimer.singleShot(150, lambda: self.wake_action_tray.setVisible(False))

    def _do_wake_up_standby(self):
        self.changeToStandby()
        self.record_interaction()

    def _do_wake_up_discomfort(self):
        self.changeToDiscomfort()

    def create_menu(self):
        if self.connecting or self.is_moving:
            return
        if self.connecting:  # 正在连接，不创建菜单（托盘菜单会因此不可用）
            return
        menu = RoundedMenu()
        menu.setToolTipsVisible(True)
        fuck_Manthe = menu.addAction(f"Firefly_Win64_v{CLIENT_VERSION}_G")
        fuck_Manthe.setDisabled(True)
        feed_action = menu.addAction("🍖 投喂")
        love_action = menu.addAction("💖 比心")
        mailbox_action = menu.addAction("📬 邮箱")
        mailbox_action.triggered.connect(self.open_mailbox)
        volume_menu = RoundedMenu("🔊 音量")
        volume_action = create_volume_menu(self)
        volume_menu.addAction(volume_action)
        menu.addMenu(volume_menu)

        if self.is_signed_today():
            sign_action = menu.addAction("✅ 今日已签到")
            sign_action.setDisabled(True)
        else:
            sign_action = menu.addAction("📅 每日签到")
            sign_action.triggered.connect(self.sign_in)

        menu.addAction("🔧 更多功能", self.open_settings_tool)
        menu.addAction("🚀 脱离卡死", self.teleport_to_center)
        menu.addAction("🔄 检查更新", self.check_new)
        ai_action = QAction("💬 在线对话", self)
        ai_action.triggered.connect(self.AI)
        if not self.ai_enabled:
            ai_action.setEnabled(False)
            ai_action.setToolTip("⚠此功能已被服务器禁止！")
        menu.addAction(ai_action)
        redeem_action = menu.addAction("🎟️使用兑换码")
        redeem_action.triggered.connect(self.redeem_code)
        history_action = menu.addAction("📋 兑换明细")
        history_action.triggered.connect(self.show_history)
        user_info = menu.addAction(f"👤当前登录：{self.get_display_uid()}")
        user_info.setDisabled(True)
        exit_all_action = menu.addAction("🚪 退出桌宠")
        exit_all_action.triggered.connect(self.show_exit_dialog)
        feed_action.triggered.connect(self.feed)
        love_action.triggered.connect(self.heart)
        self.tray_icon.setContextMenu(menu)
        self.set_default_cursor()
        self.tray_icon.show()

    def teleport_to_center(self):
        """确认后移动桌宠窗口到屏幕中央，刷新当前动画，不改变任何状态"""
        # 自定义确认对话框
        dialog = QDialog(self)
        dialog.setWindowTitle("脱离卡死")
        dialog.setFixedSize(400, 150)
        dialog.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #1E1E1E;
                border-radius: 10px;
            }
            QLabel {
                color: #CCCCCC;
                font-size: 14px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 8px 20px;
                font-size: 14px;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #45A049;
            }
            QPushButton#cancel_btn {
                background-color: #666666;
            }
            QPushButton#cancel_btn:hover {
                background-color: #888888;
            }
        """)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(30, 30, 30, 20)
        layout.setSpacing(20)

        label = QLabel("注意！此功能仅限桌宠超出屏幕范围时使用，你确定要使用吗？")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定")
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("cancel_btn")
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        # 对话框居中
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        dialog.move(screen_center - dialog.rect().center())

        if dialog.exec_() == QDialog.Accepted:
            # 1. 移动窗口到屏幕中央
            screen = QApplication.primaryScreen().geometry()
            win_rect = self.geometry()
            if win_rect.width() == 0 or win_rect.height() == 0:
                win_rect.setWidth(self.width())
                win_rect.setHeight(self.height())
            x = screen.x() + (screen.width() - win_rect.width()) // 2
            y = screen.y() + (screen.height() - win_rect.height()) // 2
            self.move(x, y)

            # 保存位置
            if self.current_user:
                self.current_user["last_x"] = x
                self.current_user["last_y"] = y
                self._save_current_user()

            # 2. 刷新当前动画（解决透明问题）
            if self.movie:
                self.movie.stop()
                # 确定尺寸后缀
                size_suffix = ""
                if os.path.exists("Large"):
                    size_suffix = "_Large"
                elif os.path.exists("small"):
                    size_suffix = "_small"
                elif os.path.exists("medium"):
                    size_suffix = "_medium"
                # 根据当前状态构建路径
                state = self.current_animation_state
                if state == "Sleep":
                    gif_path = f"./assets/images/firefly/actions/sleep/sleep{size_suffix}.gif"
                    if not os.path.exists(gif_path):
                        gif_path = "./assets/images/firefly/actions/sleep/sleep.gif"
                elif state == "Discomfort":
                    gif_path = f"./assets/images/firefly/actions/Discomfort/Discomfort{size_suffix}.gif"
                    if not os.path.exists(gif_path):
                        gif_path = "./assets/images/firefly/actions/Discomfort/Discomfort.gif"
                else:
                    # 默认为 Standby（包括行走等临时状态，但脱离卡死时通常不是行走状态）
                    gif_path = f"./assets/images/firefly/actions/Standby/Standby{size_suffix}.gif"
                    if not os.path.exists(gif_path):
                        gif_path = "./assets/images/firefly/actions/Standby/Standby.gif"
                # 如果路径存在则重新加载，否则回退到切换方法
                if os.path.exists(gif_path):
                    new_movie = QMovie(gif_path)
                    self.label.setMovie(new_movie)
                    self.movie = new_movie
                    self.movie.start()
                else:
                    # 后备：使用原有切换函数（极端情况）
                    if state == "Sleep":
                        self.sleep()
                    elif state == "Discomfort":
                        self.changeToDiscomfort()
                    else:
                        self.changeToStandby()

    def logout(self):
        self.current_user["last_state"] = self.persistent_state
        self._save_current_user()
        self.current_user = None
        self.hide()
        self.tray_icon.setVisible(False)
        self.show_login_again()

    def show_login_again(self):
        login_win = LoginWindow()
        login_win.login_success.connect(self.on_login_success)
        login_win.open()

    def on_login_success(user_data):
        if hasattr(app, 'firefly') and app.firefly:
            app.firefly.deleteLater()
            app.firefly = None
            QApplication.processEvents()
        app.firefly = Firefly(user_data=user_data)
        app.firefly.init_tray_icon()
        app.firefly.create_menu()
        app.firefly.show()
        app.firefly.restore_last_position()

    def closeEvent(self, event):
        if self.firefly.current_user:
            self.firefly.current_user["last_state"] = self.firefly.persistent_state
            pos = self.firefly.pos()
            self.firefly.current_user["last_x"] = pos.x()
            self.firefly.current_user["last_y"] = pos.y()
            self.firefly._save_current_user()
        event.accept()

    def kill_self_and_children(self):
        current_pid = os.getpid()
        current_process = psutil.Process(current_pid)
        try:
            for child in current_process.children(recursive=True):
                try:
                    child.kill()
                except:
                    pass
            current_process.kill()
        except:
            sys.exit()

    def __del__(self):
        if hasattr(self, 'hotkey_filter'):
            try:
                self.hotkey_filter.unregister()
                QApplication.instance().removeNativeEventFilter(self.hotkey_filter)
            except:
                pass

    def out_win(self):
        # 注销全局热键
        if hasattr(self, 'hotkey_filter'):
            self.hotkey_filter.unregister()
            QApplication.instance().removeNativeEventFilter(self.hotkey_filter)
        # 停止音频
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except:
            pass
        # 保存状态
        if self.current_user:
            self.current_user["last_state"] = self.persistent_state
            pos = self.pos()
            self.current_user["last_x"] = pos.x()
            self.current_user["last_y"] = pos.y()
            self._save_current_user()
        # 隐藏窗口和托盘
        self.hide()
        if self.tray_icon:
            self.tray_icon.setVisible(False)
        # 可选：启动清理工具（但不要杀自己）
        try:
            subprocess.Popen(["./tools/kill_process.exe"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass
        # 正常退出
        sys.exit(0)


# ====================== 自动登录检查 ======================
def auto_login_check():
    if not os.path.exists(USER_CONFIG):
        return None
    try:
        with open(USER_CONFIG, encoding="utf-8") as f:
            config = json.load(f)
        if config.get("remember") and config.get("userid") and config.get("password"):
            userid = config["userid"]
            pwd = config["password"]
            user_path = os.path.join(USER_DATA_DIR, f"{userid}.json")
            if os.path.exists(user_path):
                try:
                    with open(user_path, encoding="utf-8") as f:
                        user_data = json.load(f)
                except:
                    return None
                if not isinstance(user_data, dict):
                    return None
                if user_data.get("password") == pwd:
                    user_data = ensure_user_fields(user_data, user_path)
                    banned_until = user_data.get("banned_until")
                    if banned_until:
                        try:
                            until_date = datetime.fromisoformat(banned_until)
                            if datetime.now() < until_date:
                                if os.path.exists(USER_CONFIG):
                                    os.remove(USER_CONFIG)
                                return None
                            else:
                                user_data["banned"] = False
                                user_data["banned_until"] = None
                                with open(user_path, "w", encoding="utf-8") as f:
                                    json.dump(user_data, f, ensure_ascii=False, indent=2)
                        except:
                            pass
                    if user_data.get("banned"):
                        if os.path.exists(USER_CONFIG):
                            os.remove(USER_CONFIG)
                        return None
                    return user_data
    except:
        pass
    return None

# ====================== 主程序 ======================
if __name__ == "__main__":
    import signal
    firefly = None
    app = QApplication(sys.argv)
    def sigint_handler(signum, frame):
        print("\n正在退出程序...")
        if hasattr(app, 'firefly') and app.firefly:
            # 调用已有的清理方法（它会保存状态、断开服务器、停止音乐等）
            app.firefly.out_win()
        else:
            QApplication.quit()
    signal.signal(signal.SIGINT, sigint_handler)
    transparent_pixmap = QPixmap(1, 1)
    transparent_pixmap.fill(Qt.transparent)
    app.setWindowIcon(QIcon(transparent_pixmap))
    app.setQuitOnLastWindowClosed(False)

    font_path = "./assets/font/HarmonyOS_Sans_SC_Bold.ttf"
    if os.path.exists(font_path):
        font_id = QFontDatabase.addApplicationFont(font_path)
        if font_id != -1:
            font_families = QFontDatabase.applicationFontFamilies(font_id)
            if font_families:
                harmony_font = QFont(font_families[0])
                harmony_font.setPointSize(10)
                app.setFont(harmony_font)

    pygame.mixer.init()

    current_user = auto_login_check()

    if current_user:
        # 自动登录：清理旧实例（如果有）
        if hasattr(app, 'firefly') and app.firefly:
            app.firefly.deleteLater()
            app.firefly = None
            QApplication.processEvents()  # 确保销毁完成
        app.firefly = Firefly(user_data=current_user)
        app.firefly.init_tray_icon()
        app.firefly.create_menu()
        app.firefly.show()
        app.firefly.restore_last_position()
    else:
        login_win = LoginWindow()


        def on_login_success(user_data):
            # 清理旧实例
            if hasattr(app, 'firefly') and app.firefly:
                app.firefly.deleteLater()
                app.firefly = None
                QApplication.processEvents()
            app.firefly = Firefly(user_data=user_data)
            app.firefly.init_tray_icon()
            app.firefly.create_menu()
            app.firefly.show()
            app.firefly.restore_last_position()


        login_win.login_success.connect(on_login_success)
        login_win.open()

    sys.exit(app.exec_())
