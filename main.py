# -*- coding: utf-8 -*-
import logging
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
from datetime import datetime, timedelta
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
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

CLIENT_VERSION = "4.3.7"

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
    """同服务端定义"""
    c_num, c_suf = parse_version(client_ver)
    s_num, s_suf = parse_version(server_ver)
    if c_num is None or s_num is None:
        return False
    if c_num != s_num:
        return False
    if c_suf is not None:
        return c_suf == s_suf
    return True

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
    QMenu::item{
        padding: 6px 20px;
        margin: 2px 4px;
        border-radius: 4px;
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
    defaults ={
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
        "volume": 0.5
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

def send_mail(user_data, title, content, items):
    ensure_user_fields(user_data)
    clean_expired_mails(user_data)
    mail = {
        "id": str(uuid.uuid4()),
        "title": title,
        "content": content,
        "sender": "系统",
        "timestamp": datetime.now().isoformat(),
        "read": False,
        "claimed": False,
        "items": items
    }
    user_data["mailbox"].append(mail)
    return mail

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
    connection_success = pyqtSignal(str, str, str, str)  # server_uid, permission, inventory_json, server_version
    connection_failed = pyqtSignal(str)
    connection_closed = pyqtSignal()
    message_received = pyqtSignal(str)  # 改为传递 JSON 字符串

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
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                break
            except Exception as e:
                print(f"[ConnectionThread] 监听异常: {e}")
                break
        self._running = False

    def _process_line(self, line):
        try:
            msg = json.loads(line)
            mtype = msg['type']
            data = msg.get('data', {})
            if mtype == 'login_ok':
                server_uid = data['server_uid']
                permission = data['permission']
                inventory = data.get('inventory')
                server_version = data.get('server_version')
                # 将 inventory 转为 JSON 字符串
                inventory_json = json.dumps(inventory) if inventory else 'null'
                self.connection_success.emit(server_uid, permission, inventory_json, server_version)
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
    login_success = pyqtSignal(str, str, object, str)
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
        self._process_message(msg)

    def _on_connection_success(self, server_uid, permission, inventory_json, server_version):
        inventory = json.loads(inventory_json) if inventory_json != 'null' else None
        self.connected = True
        self.server_uid = server_uid
        self.server_permission = permission
        self.firefly.online_inventory = inventory
        if inventory is not None:
            self.firefly.online_signed_today = inventory.get('last_sign_date') == datetime.now().strftime('%Y-%m-%d')
        else:
            self.firefly.online_signed_today = False
        self.login_success.emit(server_uid, permission, inventory, server_version)

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

    def _process_message(self, msg_or_line):
        try:
            if isinstance(msg_or_line, dict):
                msg = msg_or_line
            else:
                msg = json.loads(msg_or_line)
            mtype = msg['type']
            data = msg.get('data', {})
        except Exception:
            return
        mtype = msg['type']
        if mtype == 'player_list':
            self.online_list_updated.emit(msg['data']['players'])
        elif mtype == 'chat_broadcast':
            data = msg.get('data', {})
            # 如果是系统消息且包含封禁关键词
            if data.get('is_system', False) and '你已被服务器禁止！' in data.get('text', ''):
                self.banned_by_server.emit(data['text'])
            self.message_received.emit(msg)
        if mtype == 'mention':
            from_name = data.get('from_name', '')
            if not from_name:
                return
            if data.get('all'):  # 服务端发的是 all: true
                self.mentioned_all.emit(from_name)  # 发射 @所有人的信号
            else:
                self.mentioned.emit(from_name)  # 发射普通 @的信号
        elif mtype == 'system_message':
            text = msg['data'].get('text', '')
            # 新增：检测封禁关键词
            if '封禁' in text:
                self.banned_by_server.emit(text)
            self.message_received.emit({
                'type': 'chat_broadcast',
                'data': {
                    'from_name': '系统',
                    'text': text,
                    'is_system': True
                }
            })
        elif mtype == 'login_fail':
            reason = msg['data'].get('reason', '未知原因')
            self.login_failed.emit(reason)
            self.disconnect()
        elif mtype == 'login_ok':
            self.server_uid = msg['data']['server_uid']
            self.server_permission = msg['data']['permission']
            inv = msg['data'].get('inventory', None)
            server_version = msg['data'].get('server_version', None)  # 获取服务端版本
            self.firefly.online_signed_today = msg['data'].get('signed_today', False)
            self.firefly.online_redeemed_codes = msg['data'].get('redeemed_codes', [])
            self.firefly.chat_history = msg['data'].get('chat_history', [])
            server_name = msg['data'].get('server_name', '')
            if server_name:
                self.firefly.online_name = server_name
            self.firefly.create_menu()
            # 发射信号时增加 server_version 参数
            self.login_success.emit(self.server_uid, self.server_permission, inv, server_version)
        elif mtype == 'server_shutdown':
            reason = data.get('reason', '服务器已关闭')
            self.server_shutdown.emit(reason)
            self.disconnect()  # 主动断开
        elif mtype == 'permission_changed':
            self.server_permission = msg['data']['permission']
            self.server_uid = msg['data'].get('server_uid', self.server_uid)
            self.permission_changed.emit()
        elif mtype == 'chat_history_data':
            self.firefly.chat_history = msg['data']['history']
            # 如果刷新请求标志为真，则重新打开聊天窗口
            if self.firefly._refresh_chat_pending:
                self.firefly._refresh_chat_pending = False
                self.firefly.refresh_chat_window()
        elif mtype == 'mails_data':
            self.mails_ready.emit(msg['data']['mails'])
        elif mtype == 'claim_mail_result':
            self.claim_mail_result_signal.emit(msg['data']['success'], msg['data'].get('reason', ''))
        elif mtype == 'redeem_result':
            self.redeem_result_signal.emit(msg['data']['success'], msg['data']['message'])
        elif mtype == 'chat_history_data':
            self.chat_history_ready.emit(msg['data']['history'])
        elif mtype == 'command_result':
            self.command_result_signal.emit(msg['data']['success'], msg['data']['message'])
        elif mtype == 'sign_result':
            self.sign_result_signal.emit(msg['data']['success'], msg['data']['message'])
        elif mtype == 'inventory_update':
            inv = msg['data']['inventory']
            self.firefly.online_inventory = inv
            today = datetime.now().strftime('%Y-%m-%d')
            self.firefly.online_signed_today = (inv.get('last_sign_date') == today)
            self.firefly.update_bag_display()


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
        else:
            self.mails = firefly.current_user.get("mailbox", [])

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
        self.init_ui()
        self.load_mails()
        self.apply_rounded_mask()
        if self.mail_list.count() > 0:
            self.mail_list.setCurrentRow(0)
            self.show_detail_for_current()
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    # 鼠标事件、遮罩等保持不变（同原代码）
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self.drag_pos)
            event.accept()
    def create_rounded_mask(self):
        radius = 15
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), radius, radius)
        return QRegion(path.toFillPolygon().toPolygon())
    def apply_rounded_mask(self):
        self.setMask(self.create_rounded_mask())

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
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.claim_all_btn)
        btn_layout.addWidget(self.read_all_btn)
        btn_layout.addWidget(self.close_btn)
        main_layout.addLayout(btn_layout)

        self.setLayout(main_layout)

    def load_mails(self):
        self.mail_list.blockSignals(True)
        self.mail_list.clear()
        for i, mail in enumerate(reversed(self.mails)):
            title = mail["title"]
            if not mail.get("read"):
                title = "● " + title
            if mail.get("claimed"):
                title += " [已领取]"
            item = QListWidgetItem(title)
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

    def on_mail_clicked(self, item):
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
            return

        if not mail.get("read"):
            mail["read"] = True
            if self.online_mails is None:
                self.firefly._save_current_user()
            # 更新列表项
            title = mail["title"]
            if mail.get("claimed"):
                title += " [已领取]"
            self.mail_list.blockSignals(True)
            item.setText(title)
            self.mail_list.blockSignals(False)

        # 构建详情...
        items_str = []
        has_ticket = False
        for it in mail["items"]:
            if it["type"] == "cake":
                items_str.append(f"橡木蛋糕卷 x{it['amount']}")
            elif it["type"] == "credit":
                items_str.append(f"信用点 x{it['amount']}")
            elif it["type"] == "clothes":
                clothes_info = AVAILABLE_CLOTHES.get(it["id"], {})
                items_str.append(f"时装 [{clothes_info.get('name', it['id'])}]")
            elif it["type"] == "star_rail_ticket":
                has_ticket = True
                items_str.append(f"星铁专票 x{it['amount']}")

        items_text = ', '.join(items_str) if items_str else "无"
        status = "已领取" if mail.get("claimed") else "未领取"
        detail = (
            f"发件人：{mail['sender']}\n"
            f"时间：{mail['timestamp'][:19]}\n"
            f"状态：{status}\n"
            f"标题：{mail['title']}\n"
            f"──────────────────\n"
            f"{mail['content']}\n"
            f"──────────────────\n"
            f"物品：{items_text}"
        )
        self.detail_text.setPlainText(detail)

        if has_ticket:
            self.ticket_label.setText("星铁专票 x1")
            self.ticket_label.show()
        else:
            self.ticket_label.hide()

        self.claim_btn.setEnabled(not mail.get("claimed"))

    def claim_current_mail(self):
        item = self.mail_list.currentItem()
        mail = self.get_mail_by_item(item)
        if mail is None or mail.get("claimed"):
            return

        if self.online_mails is not None:
            # 联机：仅发送领取请求
            self.firefly.mp_client._send({'type': 'claim_mail', 'data': {'mail_id': mail['id']}})
        else:
            success, desc = claim_mail_items(self.firefly.current_user, mail)
            if success:
                self.firefly._save_current_user()
                self.firefly.update_bag_display()
                title = mail["title"] + " [已领取]"
                self.mail_list.blockSignals(True)
                item.setText(title)
                self.mail_list.blockSignals(False)
                self.show_detail_for_item(item)
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "成功", f"物品已领取！获得 {', '.join(desc)}"))

    def claim_all(self):
        for mail in self.mails:
            if not mail.get("claimed"):
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
            self.close()

    def read_all(self):
        for mail in self.mails:
            mail["read"] = True
        if self.online_mails is None:
            self.firefly._save_current_user()
        self.load_mails()
        self.show_detail_for_current()

    def closeEvent(self, event):
        # 直接接受关闭，不执行任何额外操作
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

        label = QLabel("请输入兑换码：")
        layout.addWidget(label)

        self.code_edit = QLineEdit()
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
        self.style().unpolish(self)
        self.style().polish(self)
        super().showEvent(event)


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
        # 扣款
        inv["credit"] = credit - total_credit
        inv["balance"] = inv.get("balance", 0) + amount

        # 保存
        if self.firefly.mp_client.connected and self.firefly.online_inventory is not None:
            self.firefly.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.firefly.online_inventory}})
        else:
            self.firefly._save_current_user()

        history = self.firefly.current_user.get("redeem_history", [])
        history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 商店购买 {amount} 个橡木蛋糕卷，花费 {total_credit} 信用点")
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
        msg = ctypes.wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY and msg.wParam == self.id:
            if self.firefly.mp_client.connected:
                self.firefly.toggle_online_list()
            return True, 0
        return False, 0

    def unregister(self):
        ctypes.windll.user32.UnregisterHotKey(None, self.id)

class ChangeNameDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        set_window_emoji_icon(self, "✏️")
        self.setWindowTitle("修改昵称")
        self.setFixedSize(350, 180)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; border-radius: 10px; }
            QLabel { color: #888888; font-size: 14px; }
            QLineEdit { background-color: #2D2D2D; color: white; border: 1px solid #555; border-radius: 5px; padding: 8px; }
            QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 5px; padding: 8px 16px; }
            QPushButton:hover { background-color: #45A049; }
        """)
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(QLabel("请输入新昵称 (最多16字符):"))
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(16)
        layout.addWidget(self.name_edit)
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定")
        cancel_btn = QPushButton("取消")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        screen_center = QApplication.primaryScreen().availableGeometry().center()
        self.move(screen_center - self.rect().center())

    def get_new_name(self):
        return self.name_edit.text().strip()

import socket, time, threading, json, os
from PyQt5.QtCore import QTimer, pyqtSignal

class AddServerDialog(QDialog):
    """添加/编辑自定义服务器的对话框"""
    def __init__(self, parent=None, name='', addr=''):
        super().__init__(parent)
        set_window_emoji_icon(self, "➕")
        self.setWindowTitle("编辑服务器")
        self.setFixedSize(480, 300)
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
        send_btn = QPushButton("发送")
        send_btn.clicked.connect(self.send_message)
        input_layout.addWidget(self.input_edit)
        input_layout.addWidget(send_btn)
        main_layout.addLayout(input_layout)

        self.firefly.mp_client.message_received.connect(self.append_message)
        if not self.firefly.chat_history:
            self.firefly.mp_client._send({'type': 'get_chat_history', 'data': {}})
            self.firefly._refresh_chat_pending = True

        # 加载历史
        if self.firefly.chat_history:
            for msg in self.firefly.chat_history:
                self.add_message(
                    sender=msg.get('from_name', '?'),
                    text=msg.get('text', ''),
                    time_str=self._format_time(msg.get('time', '')),
                    is_system=msg.get('is_system', False)
                )

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

    def send_message(self):
        if self.firefly.banned:
            QMessageBox.warning(self, "提示", "你已被封禁，无法发送消息")
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        if text.startswith('/'):
            self.firefly.mp_client.send_command(text)
        else:
            self.firefly.mp_client.send_chat(text)
        self.input_edit.clear()

    def append_message(self, msg):
        data = msg['data']
        self.add_message(
            sender=data.get('from_name', '?'),
            text=data['text'],
            time_str=self._format_time(data.get('time', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))),
            is_system=data.get('is_system', False)
        )

    def add_message(self, sender, text, time_str, is_system=False):
        if self.msg_layout.count() > 0:
            last_item = self.msg_layout.itemAt(self.msg_layout.count() - 1)
            if last_item.spacerItem():
                self.msg_layout.removeItem(last_item)

        if is_system:
            lbl = QLabel(f"<div align='center' style='color:#AAAAAA; font-size:12px;'>{text}</div>")
            lbl.setWordWrap(True)
            self.msg_layout.addWidget(lbl)
        else:
            is_self = (sender == self.get_my_name())

            name_lbl = QLabel(sender)
            name_lbl.setStyleSheet("font-size:12px; color:#CCCCCC; background:transparent; font-weight:bold; font-family: 'HarmonyOS Sans SC','Microsoft YaHei',sans-serif;")
            name_lbl.setAlignment(Qt.AlignRight if is_self else Qt.AlignLeft)

            bubble_widget = QWidget()
            bubble_layout = QVBoxLayout(bubble_widget)
            bubble_layout.setContentsMargins(0,0,0,0)
            bubble_layout.setSpacing(2)

            bubble = QLabel(text)
            bubble.setWordWrap(True)
            bubble_bg = '#DCF8C6' if is_self else '#FFFFFF'
            bubble.setStyleSheet(f"background-color:{bubble_bg}; border-radius:14px; padding:10px 14px; font-size:14px; color:#000000; font-family: 'HarmonyOS Sans SC','Microsoft YaHei',sans-serif;")
            bubble.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

            time_lbl = QLabel(time_str)
            time_lbl.setStyleSheet("color:#888888; font-size:10px; background:transparent;")
            time_lbl.setAlignment(Qt.AlignRight if is_self else Qt.AlignLeft)

            bubble_layout.addWidget(bubble)
            bubble_layout.addWidget(time_lbl)

            if is_self:
                name_row = QHBoxLayout()
                name_row.addStretch()
                name_row.addWidget(name_lbl)
                bubble_row = QHBoxLayout()
                bubble_row.addStretch()
                bubble_row.addWidget(bubble_widget)
                self.msg_layout.addLayout(name_row)
                self.msg_layout.addLayout(bubble_row)
            else:
                name_row = QHBoxLayout()
                name_row.addWidget(name_lbl)
                name_row.addStretch()
                bubble_row = QHBoxLayout()
                bubble_row.addWidget(bubble_widget)
                bubble_row.addStretch()
                self.msg_layout.addLayout(name_row)
                self.msg_layout.addLayout(bubble_row)

        self.msg_layout.addStretch()
        QTimer.singleShot(50, self.scroll_to_bottom)

    def scroll_to_bottom(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


# -------------------------- 核心桌宠类 --------------------------
class Firefly(QWidget):
    def __init__(self, user_data=None, parent=None):
        super(Firefly, self).__init__(parent)
        self.current_user = user_data
        self.online_name = None
        self.label = QLabel("", self)
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
        self.hotkey_filter = GlobalHotkeyFilter(self)
        app.installNativeEventFilter(self.hotkey_filter)
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
            self.apply_cursor_style()
            self.apply_pet_size()
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
            if self.single_player_state['current_animation_state'] == "Discomfort":
                self.changeToDiscomfort()
            else:
                self.changeToStandby()
            self.single_player_state = None
        if self.local_inventory_backup:
            self.current_user['balance'] = self.local_inventory_backup['balance']
            self.current_user['credit'] = self.local_inventory_backup['credit']
            self.current_user['owned_clothes'] = self.local_inventory_backup['owned_clothes']
            self.current_user['star_rail_tickets'] = self.local_inventory_backup['star_rail_tickets']
            self._save_current_user()
            self.local_inventory_backup = None
        self.update_bag_display()
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
        """将聊天窗口置顶并激活"""
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
                # 可选：显示错误并退出

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
            self.mp_client._send({'type': 'get_mails'})  # 刷新邮箱
            QTimer.singleShot(0, lambda: self.show_centered_message("邮件", "物品已领取成功！"))
        else:
            QTimer.singleShot(0, lambda: self.show_centered_message("领取失败", message or "未知错误",
                                                                    QMessageBox.Warning))

    def on_sign_result(self, success, message):
        if success:
            self.show_centered_message("签到成功", message)
            self.online_signed_today = True
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
            QTimer.singleShot(0, lambda: self.show_centered_message("命令结果", message, QMessageBox.Information))
            if '改名成功' in message and self._pending_new_name:
                # 立即更新本地显示的名字
                self.online_name = self._pending_new_name
                self._pending_new_name = None
                self.create_menu()  # 立即刷新右键菜单
                # 仍然请求玩家列表，确保与服务端最终状态同步（如果服务端未改，后续会覆盖回来）
                self.mp_client.request_player_list()
                self.mp_client._send({'type': 'get_chat_history'})
        else:
            QTimer.singleShot(0, lambda: self.show_centered_message("命令失败", message, QMessageBox.Warning))
            self._pending_new_name = None  # 失败时清空暂存

    def refresh_chat_window(self):
        if self.chat_window is None:
            return
        # 清空现有消息
        for i in reversed(range(self.chat_window.msg_layout.count())):
            item = self.chat_window.msg_layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                # 递归清空，但简单起见，重新加载历史即可
                pass
        # 重新加载历史消息
        for msg in self.chat_history:
            self.chat_window.add_message(
                sender=msg.get('from_name', '?'),
                text=msg.get('text', ''),
                time_str=self.chat_window._format_time(msg.get('time', '')),
                is_system=msg.get('is_system', False)
            )
        self.chat_window.scroll_to_bottom()

    def _safe_open_chat(self):
        try:
            self.open_chat_window()
        except Exception as e:
            print(f"重新打开聊天窗口失败: {e}")

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
        data = msg['data']
        text = data.get('text', '')

        # 以下保留：管理指令触发的列表刷新
        if '管理员' in text or '改名为' in text or '封禁' in text:
            self.mp_client.request_player_list()
            self.mp_client._send({'type': 'get_chat_history'})

    def get_display_uid(self):
        if self.mp_client.connected and self.online_name:
            return f"{self.online_name} (UID:{self.mp_client.server_uid})"
        return f"{self.current_user['username']} (UID:{self.current_user['uid']})"

    def change_online_name(self):
        if not self.mp_client.connected:
            self.show_centered_message("提示", "请先加入服务器", QMessageBox.Warning)
            return
        if self.chat_window and self.chat_window.isVisible():
            QTimer.singleShot(0,
                              lambda: self.show_centered_message("提示", "请先关闭聊天窗口再改名", QMessageBox.Warning))
            return
        dlg = ChangeNameDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            name = dlg.get_new_name()
            if name:
                self._pending_new_name = name  # ← 暂存新名字
                self.mp_client._send({'type': 'change_name', 'data': {'new_name': name}})

    def get_active_inventory(self):
        if self.mp_client.connected and self.online_inventory is not None:
            return self.online_inventory
        return self.current_user

    def on_login_success_mp(self, uid, perm, inv, server_version=None):
        try:
            # 版本二次校验
            if server_version and not versions_compatible(CLIENT_VERSION, server_version):
                self._is_manual_disconnect = True
                self.show_centered_message("版本不兼容",
                                           f"客户端版本 {CLIENT_VERSION} 与服务器版本 {server_version} 不匹配",
                                           QMessageBox.Warning)
                self.mp_client.disconnect()
                return

            self.connecting = False
            # 保存单机状态
            self.single_player_state = {
                'persistent_state': self.persistent_state,
                'current_animation_state': self.current_animation_state,
                'last_interaction_time': self.last_interaction_time,
                'emo_random_delay': self.emo_random_delay,
                'last_feed_time': self.current_user.get('last_feed_time')
            }
            self.changeToStandby()
            self.persistent_state = "Standby"
            self.last_interaction_time = time.time()
            self.emo_random_delay = random.randint(0, 15 * 60)
            self.current_user['last_feed_time'] = None

            # 确保 inv 是字典
            if inv is None:
                inv = {'balance': 0, 'credit': 0, 'owned_clothes': ['normal'], 'star_rail_tickets': 0}
            self.online_inventory = inv
            self.update_bag_display()

            QTimer.singleShot(100, lambda: self.show_centered_message("成功", f"已连接到服务器\n你的服务器UID: {uid}"))

            # ★ 关键修改：请求玩家列表，等待真实名字，不再立即 create_menu()
            self._waiting_for_name = True
            self.mp_client.request_player_list()

        except Exception as e:
            print(f"[on_login_success_mp] 异常: {e}", file=sys.stderr)
            traceback.print_exc()
            self.show_centered_message("错误", f"联机初始化失败：{str(e)}", QMessageBox.Warning)
            self.disconnect_server()

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
            # 如果是非主动断开且之前处于连接状态，则弹窗并恢复单机
            if self._was_connected and not self._is_manual_disconnect:
                QTimer.singleShot(0, lambda: self.show_centered_message("连接断开",
                                                                        "与服务器断开连接！"))
                self._restore_single_player_state()
            self.server_uid = None
            self.online_name = None
            self.online_inventory = None
            self._was_connected = False
        else:
            self._was_connected = True
        # 重置手动断开标志
        self._is_manual_disconnect = False
        self.create_menu()

    def open_chat_window(self):
        if self.banned:
            return
        if not self.mp_client.connected:
            return
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

        # 在列表中查找自己（通过 userid）
        my_id = self.current_user['userid']
        found_name = None
        found_uid = None
        for p in players:
            if p.get('id') == my_id:
                found_name = p['name']
                found_uid = p.get('uid', '')
                break

        if found_name is not None:
            changed = False
            if found_name != self.online_name:
                self.online_name = found_name
                changed = True
            if found_uid and found_uid != getattr(self.mp_client, 'server_uid', ''):
                self.mp_client.server_uid = found_uid
                self.server_uid = found_uid
                changed = True

            if changed:
                # ★ 关键修改：根据标志决定是否立即创建菜单
                if self._waiting_for_name:
                    self._waiting_for_name = False
                    self.create_menu()  # 首次获取名字，创建菜单
                else:
                    self.create_menu()  # 后续普通刷新（如改名后）
        else:
            # 未找到自己（可能服务端未返回完整列表），但不要阻塞菜单创建
            if self._waiting_for_name:
                # 等待超时后备会处理，这里不做额外操作
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
        def check_process_running(process_name):
            for process in psutil.process_iter(['name']):
                if process.info['name'] == process_name:
                    return True
            return False
        process_name = '工具组件.exe'
        if not check_process_running(process_name):
            subprocess.Popen(["./tools/工具组件.exe"])

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

    def apply_clothes(self, clothes_id):
        info = AVAILABLE_CLOTHES.get(clothes_id)
        if not info:
            self.show_centered_message("错误", f"未知时装ID: {clothes_id}", QMessageBox.Warning)
            return
        source_dir = info["source_dir"]
        target_dir = "assets"
        if not os.path.exists(source_dir):
            self.show_centered_message("错误", f"时装资源目录不存在：{source_dir}\n请确保程序目录下有该文件夹。",
                                       QMessageBox.Warning)
            return
        try:
            # 统计复制文件数
            copied_count = 0
            for root, dirs, files in os.walk(source_dir):
                relative_path = os.path.relpath(root, source_dir)
                target_root = os.path.join(target_dir, relative_path)
                os.makedirs(target_root, exist_ok=True)
                for file in files:
                    src_file = os.path.join(root, file)
                    dst_file = os.path.join(target_root, file)
                    shutil.copy2(src_file, dst_file)
                    copied_count += 1
            self.show_centered_message("成功", f"时装已切换为：{info['name']}")
            # 刷新当前动画
            self._refresh_animation_after_clothes_change()
        except Exception as e:
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
        if not self.current_user:
            return
        userid = self.current_user.get("userid")
        if not userid:
            print("[ERROR] 当前用户缺少 userid，无法保存")
            return
        filepath = os.path.join(USER_DATA_DIR, f"{userid}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.current_user, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] 用户数据已保存到 {filepath}")

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

    def disconnect_server(self):
        self._is_manual_disconnect = True  # 标记为主动断开
        self.banned = False
        self._waiting_for_name = False
        if self.mp_client.connected:
            # 主动断开时先提示（可选）
            self.show_centered_message("提示", "已断开与服务器的连接", QMessageBox.Information)
            if self.online_inventory:
                self.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.online_inventory}})
            self.mp_client.disconnect()
        # 恢复单机状态（因为 on_mp_status_changed 会跳过恢复）
        self._restore_single_player_state()
        # 清理其他联机相关变量（可选，但 _restore_single_player_state 已处理大部分）
        self.server_uid = None
        self.server_permission = 'user'
        self.online_name = None
        self.online_redeemed_codes = []
        self.online_signed_today = False
        self._pending_mail_request = None
        # 确保菜单刷新
        self.create_menu()

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
        is_admin = uid_prefix.startswith("admin")
        if is_admin:
            help_text = (
                "【联机管理命令】（与单机格式相同）\n"
                "/give <数量> <物品类型> [id=时装ID] [for <用户列表>]\n"
                "    给予物品，示例：/give 100 cake for Gubwin,Firefly\n"
                "    信用点物品类型为 credit，示例：/give 5000 credit for @a\n"
                "/clear <物品类型> [for <用户列表>]\n"
                "    清空物品，示例：/clear cake for Gubwin\n"
                "/set cake <数量> [for <用户列表>]\n"
                "    设置蛋糕卷数量，示例：/set cake 50 for Gubwin\n"
                "/set credit <数量> [for <用户列表>]\n"
                "    设置信用点数量（覆盖），示例：/set credit 100000 for @a\n"
                "/set newcode <兑换码> <物品列表> [for <用户列表>] [有效期]\n"
                "    创建兑换码，物品列表如 cake:5,credit:1000,26_Newyear\n"
                "    有效期格式 YYYY-MM-DD 或 1Y2M3D\n"
                "/revoke code <兑换码> [for <用户列表>]\n"
                "    撤销兑换码，不指定用户则全局撤销\n"
                "/set user <标识> Admin\n"
                "    提升用户为管理员\n"
                "/ban <标识>[,<标识2>...] [时长]\n"
                "    封禁用户，时长如 30D、1Y6M15D\n"
                "/unban <标识>[,<标识2>...]\n"
                "    解封用户\n"
                "/help\n"
                "    显示此帮助"
            )
        else:
            help_text = """【普通用户命令】
直接输入兑换码字符串即可兑换。
可用命令：
/help  - 显示此帮助

签到：右键菜单 → 每日签到
商店：右键菜单 → 商店
邮箱：右键菜单 → 📬 邮箱
"""
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
        if not code.startswith('/'):
            return False
        cmd_line = code[1:].strip()
        if not cmd_line:
            return False
        if cmd_line == "help" or cmd_line.startswith("help"):
            self.show_help()
            return True
        if cmd_line.startswith("give "):
            # 尝试匹配新格式：/give clothes <clothes_id> [for ...]（数量默认为1）
            new_pattern = r'give\s+clothes\s+(\S+)(?:\s+for\s+(.+))?$'
            new_match = re.match(new_pattern, cmd_line, re.IGNORECASE)
            if new_match:
                clothes_id = new_match.group(1)
                target = new_match.group(2) if new_match.group(2) else None
                self.give_item(1, "clothes", target, clothes_id=clothes_id)
                return True

            # 原格式：/give <数量> <物品类型> [id=时装ID] [for <用户列表>]
            pattern = r'give\s+(\d+)\s+(\w+)(?:\s+id=(\S+))?(?:\s+for\s+(.+))?$'
            match = re.match(pattern, cmd_line, re.IGNORECASE)
            if not match:
                self.show_centered_message("错误",
                                           "命令格式：/give <数量> <物品类型> [id=时装ID] [for <用户列表>] 或 /give clothes <时装ID> [for ...]",
                                           QMessageBox.Warning)
                return True
            amount = match.group(1)
            item_type = match.group(2).lower()
            clothes_id = match.group(3) if match.group(3) else None
            target = match.group(4) if match.group(4) else None
            if item_type == "credit":
                self.give_credit(amount, target)
            elif item_type == "cake":
                self.give_item(amount, "cake", target)
            elif item_type == "clothes":
                if not clothes_id:
                    self.show_centered_message("错误", "时装必须指定 id= 参数", QMessageBox.Warning)
                else:
                    self.give_item(1, "clothes", target, clothes_id=clothes_id)
            else:
                self.show_centered_message("错误", f"未知物品类型: {item_type}", QMessageBox.Warning)
            return True

        if cmd_line.startswith("clear "):
            # 正则支持：clear <all/cake/credit> for @a
            pattern = r'clear\s+(\w+)(?:\s+for\s+(.+))?$'
            match = re.match(pattern, cmd_line, re.IGNORECASE)
            if not match:
                self.show_centered_message("错误", "命令格式：/clear <all/cake/credit> [for <用户列表>]\n示例：/clear all for @a", QMessageBox.Warning)
                return True
            item_type = match.group(1).lower()
            target = match.group(2) if match.group(2) else None

            # 核心逻辑：清空所有 / 单个物品
            if item_type == "all":
                self.clear_all_items(target)  # 清空所有物品
            elif item_type in ["cake", "credit"]:
                self.clear_item(item_type, target)  # 清空单个物品
            else:
                self.show_centered_message("错误", "支持类型：all(全部)、cake(蛋糕卷)、credit(信用点)", QMessageBox.Warning)
            return True

        if cmd_line.startswith("set newcode "):
            # 新格式：/set newcode <兑换码> <物品1>[,<物品2>,...] [for <用户列表>] [有效期]
            rest = cmd_line[len("set newcode "):].strip()
            # 用正则拆分：兑换码名称、物品列表、可选 for 和有效期
            # 物品列表可能包含逗号，且 for 和有效期相对固定位置
            # 简单方法：先按空格分割，然后处理
            parts = rest.split()
            if len(parts) < 2:
                self.show_centered_message("错误",
                                           "命令格式：/set newcode <兑换码> <物品列表> [for <用户列表>] [有效期]",
                                           QMessageBox.Warning)
                return True

            code = parts[0]
            items_str = parts[1]  # 例如 "cake:5,credit:1000,26_Newyear"
            idx = 2
            user_spec = None
            expire_str = None

            # 解析后续可选参数
            if idx < len(parts):
                if parts[idx].lower() == "for":
                    if idx + 1 >= len(parts):
                        self.show_centered_message("错误", "缺少用户列表", QMessageBox.Warning)
                        return True
                    user_spec = parts[idx + 1]
                    idx += 2
                # 剩余的是有效期
                if idx < len(parts):
                    expire_str = parts[idx]
                    idx += 1
                    if idx < len(parts):
                        # 多余参数
                        self.show_centered_message("错误", "命令参数过多", QMessageBox.Warning)
                        return True

            # 解析物品列表
            items_list = []
            for item_spec in items_str.split(','):
                item_spec = item_spec.strip()
                if not item_spec:
                    continue
                if ':' in item_spec:
                    # cake:数量 或 credit:数量
                    try:
                        itype, amount_str = item_spec.split(':', 1)
                        itype = itype.lower()
                        amount = int(amount_str)
                        if itype == "cake":
                            if amount <= 0 or amount > 9999:
                                raise ValueError
                        elif itype == "credit":
                            if amount <= 0 or amount > 99999999:
                                raise ValueError
                        else:
                            self.show_centered_message("错误", f"未知物品类型: {itype}", QMessageBox.Warning)
                            return True
                    except:
                        self.show_centered_message("错误", f"物品数量格式错误: {item_spec}，正确格式如 cake:5",
                                                   QMessageBox.Warning)
                        return True
                    items_list.append({"type": itype, "amount": amount})
                else:
                    # 时装ID（无冒号）
                    clothes_id = item_spec
                    if clothes_id not in AVAILABLE_CLOTHES:
                        self.show_centered_message("错误", f"未知时装ID: {clothes_id}", QMessageBox.Warning)
                        return True
                    items_list.append({"type": "clothes", "id": clothes_id})

            if not items_list:
                self.show_centered_message("错误", "物品列表不能为空", QMessageBox.Warning)
                return True

            # 解析有效期
            if expire_str:
                expire_time = parse_expire_time(expire_str)
                if expire_time is None:
                    self.show_centered_message("错误", "有效期格式错误，请使用 YYYY-MM-DD 或时长如 1d/1Y2M3D",
                                               QMessageBox.Warning)
                    return True
            else:
                expire_time = None

            # 验证目标用户
            all_codes = load_all_codes()
            if code in all_codes:
                self.show_centered_message("错误", "兑换码已存在", QMessageBox.Warning)
                return True

            target_identifier_str = None
            if user_spec and user_spec.strip().lower() != "none":
                try:
                    self.find_users_by_identifiers(user_spec)
                    target_identifier_str = user_spec
                except ValueError as e:
                    self.show_centered_message("错误", str(e), QMessageBox.Warning)
                    return True

            # 创建兑换码（固定 max_uses = 1）
            all_codes[code] = {
                "max_uses": 1,
                "used_count": 0,
                "created_by": self.current_user["username"],
                "created_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "target_user": target_identifier_str,
                "items": items_list,
                "expire_time": expire_time,
                "revoked": False,
                "revoked_users": [],
                "revoked_by": None,
                "revoked_time": None
            }
            save_all_codes(all_codes)

            # 记录日志
            history = self.current_user.get("redeem_history", [])
            history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 创建兑换码 {code}")
            self.current_user["redeem_history"] = history
            self._save_current_user()

            self.show_centered_message("成功", f"兑换码 {code} 已创建", QMessageBox.Information)
            return True

        if cmd_line.startswith("set user "):
            pattern = r'set user\s+(\S+)\s+Admin$'
            match = re.match(pattern, cmd_line, re.IGNORECASE)
            if not match:
                self.show_centered_message("错误", "命令格式：/set user <标识> Admin", QMessageBox.Warning)
                return True
            target_identifier = match.group(1)
            target_user = self.find_user_by_identifier(target_identifier)
            if not target_user:
                self.show_centered_message("错误", f"未找到用户: {target_identifier}", QMessageBox.Warning)
                return True
            if target_user["uid"].lower().startswith("admin"):
                self.show_centered_message("错误", "目标用户已是管理员，无法重复设置", QMessageBox.Warning)
                return True
            old_uid = target_user.get("uid", "")
            digits = re.search(r'\d{9}$', old_uid)
            if digits:
                new_uid = f"Admin-{digits.group()}"
            else:
                rand_num = str(random.randint(0, 10 ** 9 - 1)).zfill(9)
                new_uid = f"Admin-{rand_num}"
            target_user["uid"] = new_uid
            self.save_user_data(target_user)
            history = self.current_user.get("redeem_history", [])
            history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [管理员] 将用户 {target_identifier} 的UID改为 {new_uid}")
            self.current_user["redeem_history"] = history
            self._save_current_user()
            self.show_centered_message("成功", f"已将用户 {target_identifier} 的UID修改为 {new_uid}", QMessageBox.Information)
            return True

        if cmd_line.startswith("ban "):
            parts = cmd_line.split(maxsplit=2)
            if len(parts) < 2:
                self.show_centered_message("错误", "命令格式：/ban <标识>[,<标识2>...] [时长]", QMessageBox.Warning)
                return True
            identifiers_part = parts[1]
            duration_str = parts[2] if len(parts) > 2 else None
            try:
                target_users = self.find_users_by_identifiers(identifiers_part)
                if target_users is None:
                    self.show_centered_message("错误", "未指定有效目标用户", QMessageBox.Warning)
                    return True
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return True
            current_uid = self.current_user.get("uid", "")
            is_current_admin = current_uid.lower().startswith("admin")
            for tu in target_users:
                if is_current_admin and tu["uid"].lower().startswith("admin") and tu["userid"] != self.current_user["userid"]:
                    self.show_centered_message("错误", f"不能封禁其他管理员用户: {tu['username']}", QMessageBox.Warning)
                    return True
            delta = None
            if duration_str:
                delta = parse_duration(duration_str)
                if delta is None:
                    self.show_centered_message("错误", "时长格式错误，示例：1Y6M15D（年Y/月M/日D）", QMessageBox.Warning)
                    return True
            success_list = []
            for tu in target_users:
                if delta:
                    until_date = datetime.now() + delta
                    tu["banned"] = True
                    tu["banned_until"] = until_date.isoformat()
                    self.save_user_data(tu)
                    success_list.append(f"{tu['username']} 至 {until_date.strftime('%Y-%m-%d')}")
                else:
                    tu["banned"] = True
                    tu["banned_until"] = None
                    self.save_user_data(tu)
                    success_list.append(f"{tu['username']} 永久")
            history = self.current_user.get("redeem_history", [])
            history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [管理员] 封禁用户: {identifiers_part}，时长 {duration_str if duration_str else '永久'}")
            self.current_user["redeem_history"] = history
            self._save_current_user()
            self.show_centered_message("成功", f"已封禁用户:\n" + "\n".join(success_list), QMessageBox.Information)
            return True

        if cmd_line.startswith("unban "):
            parts = cmd_line.split(maxsplit=1)
            if len(parts) < 2:
                self.show_centered_message("错误", "命令格式：/unban <标识>[,<标识2>...]", QMessageBox.Warning)
                return True
            identifiers_part = parts[1]
            try:
                target_users = self.find_users_by_identifiers(identifiers_part)
                if not target_users:
                    self.show_centered_message("错误", "未找到任何有效用户", QMessageBox.Warning)
                    return True
            except ValueError as e:
                self.show_centered_message("错误", str(e), QMessageBox.Warning)
                return True

            # 解封并同步当前登录用户的内存状态
            for tu in target_users:
                tu["banned"] = False
                tu["banned_until"] = None
                self.save_user_data(tu)
                # 如果解封的是当前用户，立即更新内存中的数据
                if tu.get("userid") == self.current_user.get("userid"):
                    self.current_user["banned"] = False
                    self.current_user["banned_until"] = None
                    self._save_current_user()  # 同步保存到文件

            history = self.current_user.get("redeem_history", [])
            history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [管理员] 解封用户: {identifiers_part}")
            self.current_user["redeem_history"] = history
            self._save_current_user()

            self.show_centered_message("成功", f"已解封用户: {identifiers_part}", QMessageBox.Information)
            return True

        if cmd_line.startswith("revoke code "):
            pattern = r'revoke code\s+(\S+)(?:\s+for\s+(.+))?$'
            match = re.match(pattern, cmd_line, re.IGNORECASE)
            if not match:
                self.show_centered_message("错误", "命令格式：/revoke code <兑换码> [for <用户列表>]", QMessageBox.Warning)
                return True
            code = match.group(1)
            target = match.group(2) if match.group(2) else None
            self.revoke_code(code, target)
            return True

        if cmd_line.startswith("emo"):
            parts = cmd_line.split(maxsplit=2)
            target_str = ""
            if len(parts) == 1:
                target_str = ""
            elif len(parts) >= 2:
                if parts[1].strip() == "@a":
                    target_str = "@a"
                elif parts[1].strip().lower() == "for" and len(parts) == 3:
                    target_str = parts[2].strip()

            if target_str == "":
                self.persistent_state = "Discomfort"
                self.current_user["last_state"] = "Discomfort"
                self._save_current_user()
                self.changeToDiscomfort()
                self.show_centered_message("成功", "已进入 EMO 状态（永久生效）", QMessageBox.Information)

            elif target_str == "@a":
                all_users = []
                for filename in os.listdir(USER_DATA_DIR):
                    if filename == os.path.basename(CODES_FILE):
                        continue
                    if filename.endswith(".json"):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, "r", encoding="utf-8") as f:
                                u = json.load(f)
                            if "uid" in u and "userid" in u:
                                all_users.append(u)
                        except:
                            continue
                for u in all_users:
                    u["last_state"] = "Discomfort"
                    self.save_user_data(u)
                self.persistent_state = "Discomfort"
                self.changeToDiscomfort()
                self.show_centered_message("成功", "全服用户已设置EMO状态", QMessageBox.Information)

            else:
                targets = self.find_users_by_identifiers(target_str)
                if not targets:
                    self.show_centered_message("错误", f"未找到用户：{target_str}", QMessageBox.Warning)
                    return True
                for u in targets:
                    u["last_state"] = "Discomfort"
                    self.save_user_data(u)
                self.show_centered_message("成功", f"已为 {len(targets)} 个用户设置EMO", QMessageBox.Information)
            return True

        if cmd_line.startswith("Standby"):
            parts = cmd_line.split(maxsplit=2)
            target_str = ""
            if len(parts) == 1:
                target_str = ""
            elif len(parts) >= 2:
                if parts[1].strip() == "@a":
                    target_str = "@a"
                elif parts[1].strip().lower() == "for" and len(parts) == 3:
                    target_str = parts[2].strip()

            if target_str == "":
                self.persistent_state = "Standby"
                self.current_user["last_state"] = "Standby"
                self._save_current_user()
                self.changeToStandby()
                self.show_centered_message("成功", "已进入 Standby 待机状态（永久生效）", QMessageBox.Information)

            elif target_str == "@a":
                all_users = []
                for filename in os.listdir(USER_DATA_DIR):
                    if filename == os.path.basename(CODES_FILE):
                        continue
                    if filename.endswith(".json"):
                        filepath = os.path.join(USER_DATA_DIR, filename)
                        try:
                            with open(filepath, "r", encoding="utf-8") as f:
                                u = json.load(f)
                            if "uid" in u and "userid" in u:
                                all_users.append(u)
                        except:
                            continue
                for u in all_users:
                    u["last_state"] = "Standby"
                    self.save_user_data(u)
                self.persistent_state = "Standby"
                self.changeToStandby()
                self.show_centered_message("成功", "全服用户已设置Standby待机状态", QMessageBox.Information)

            else:
                targets = self.find_users_by_identifiers(target_str)
                if not targets:
                    self.show_centered_message("错误", f"未找到用户：{target_str}", QMessageBox.Warning)
                    return True
                for u in targets:
                    u["last_state"] = "Standby"
                    self.save_user_data(u)
                self.show_centered_message("成功", f"已为 {len(targets)} 个用户设置Standby", QMessageBox.Information)
            return True

        if cmd_line.startswith("set "):
            pattern = r'set\s+(\w+)\s+(\d+)(?:\s+for\s+(.+))?$'
            match = re.match(pattern, cmd_line, re.IGNORECASE)
            if not match:
                self.show_centered_message("错误", "命令格式：/set <物品类型> <数量> [for <用户列表>]", QMessageBox.Warning)
                return True
            item_type = match.group(1).lower()
            amount = match.group(2)
            target = match.group(3) if match.group(3) else None
            if item_type == "credit":
                self.set_credit(amount, target)
            elif item_type == "cake":
                try:
                    int_amount = int(amount)
                except:
                    self.show_centered_message("错误", "数量错误", QMessageBox.Warning)
                    return True
                if target:
                    try:
                        target_users = self.find_users_by_identifiers(target)
                        for tu in target_users:
                            tu["balance"] = int_amount
                            self.save_user_data(tu)
                    except ValueError as e:
                        self.show_centered_message("错误", str(e), QMessageBox.Warning)
                else:
                    self.current_user["balance"] = int_amount
                    self._save_current_user()
                self.show_centered_message("成功", "已设置蛋糕卷数量", QMessageBox.Information)
            else:
                self.show_centered_message("错误", "未知物品类型", QMessageBox.Warning)
            return True

        self.show_centered_message("提示", "未知管理员命令", QMessageBox.Warning)
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
        if self.mp_client.connected:
            if code.startswith('/'):
                self.mp_client.send_command(code)
            else:
                self.mp_client._send({'type': 'redeem_code', 'data': {'code': code}})
            # 仅清空输入框，不关闭对话框
            dialog = getattr(self, '_redeem_dialog', None)
            if dialog:
                dialog.code_edit.clear()
            return

        # ===== 以下是单机逻辑，保持原样 =====
        dialog = getattr(self, '_redeem_dialog', None)
        if dialog is None or not dialog.isVisible():
            return

        if not code:
            QMessageBox.warning(dialog, "提示", "请输入兑换码")
            return

        if code.startswith('/'):
            uid_prefix = self.current_user.get("uid", "").lower()
            is_admin = uid_prefix.startswith("admin")
            if is_admin:
                self.process_admin_command(code)
            else:
                if code.strip().lower() == '/help':
                    self.show_help()
                else:
                    QMessageBox.warning(dialog, "提示", "未知兑换码或权限不足")
            return

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

        self.send_mail_to_user(self.current_user, "兑换码奖励",
                               f"成功使用兑换码 {code}，获得以下物品。", items)
        if cinfo["max_uses"] != 0:
            self.current_user["redeemed_codes"] = redeemed + [code]
        cinfo["used_count"] += 1
        save_all_codes(all_codes)
        history = self.current_user.get("redeem_history", [])
        history.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 使用兑换码 {code}")
        self.current_user["redeem_history"] = history
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
        now_ts = time.time()
        # 使用当前生效的库存数据
        inv = self.get_active_inventory()
        last_feed = self.current_user.get("last_feed_time")  # 冷却时间仍然共用（或可独立，此处简单复用）
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

        # 扣除蛋糕卷
        inv['balance'] = balance - 1
        self.current_user["last_feed_time"] = now_ts

        # 联机时保存到服务端
        if self.mp_client.connected and self.online_inventory is not None:
            self.mp_client._send({'type': 'save_inventory', 'data': {'inventory': self.online_inventory}})
        else:
            self._save_current_user()  # 单机时保存本地数据

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

    def sleep(self):
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
        def check_process_running(process_name):
            for process in psutil.process_iter(['name']):
                if process.info['name'] == process_name:
                    return True
            return False

        process_name = 'AI.exe'
        if not check_process_running(process_name):
            subprocess.Popen(["./tools/AI.exe"])

    def open_AI(self):
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
        # 记录今年已祝福
        self.current_user["last_birthday_greet_year"] = datetime.now().year
        self._save_current_user()

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

    def contextMenuEvent(self, event):
        if self.connecting:  # 正在连接，不弹出菜单
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
            action100 = QAction('💬 在线对话', self)  # 添加 emoji

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
            menu.addMenu(mouse_GUI)

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
            mp_menu.setTitle("🌐 多人联机")
            if self.mp_client.connected:
                chat_action = mp_menu.addAction("💬 聊天")
                chat_action.triggered.connect(self.open_chat_window)
                chat_action.setEnabled(self.mp_client.connected)
                rename_action = mp_menu.addAction("✏ 改名")
                rename_action.triggered.connect(self.change_online_name)
                leave_action = mp_menu.addAction("🚪 退出游戏")
                leave_action.triggered.connect(self.disconnect_server)
            else:
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
            action9.triggered.connect(self.open_settings)
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

    def get_server(self):
        webbrowser.open("https://github.com/Jimhow-Gu/Firefly-Table-Pet-Online/releases")

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
        menu.addAction("🔄 检查更新", self.check_new)
        menu.addAction("💬 在线对话", self.AI)
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
    firefly = None
    app = QApplication(sys.argv)
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
