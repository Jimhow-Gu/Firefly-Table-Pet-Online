# -*- coding: utf-8 -*-
import psutil
import time
import os
from win10toast import ToastNotifier

def kill_process_and_children(process_name_list):
    killed_pids = set()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] in process_name_list:
                parent = psutil.Process(proc.info['pid'])
                children = parent.children(recursive=True)
                for child in children:
                    if child.pid not in killed_pids:
                        child.terminate()
                        killed_pids.add(child.pid)
                        print(f"终止子进程: {child.name()} (PID: {child.pid})")
                if proc.info['pid'] not in killed_pids:
                    proc.terminate()
                    killed_pids.add(proc.info['pid'])
                    print(f"终止主进程: {proc.info['name']} (PID: {proc.info['pid']})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(1)
    for pid in killed_pids:
        try:
            p = psutil.Process(pid)
            if p.is_running():
                p.kill()
                print(f"强制终止残留进程: {p.name()} (PID: {pid})")
        except:
            pass

def send_notification(title, message):
    toaster = ToastNotifier()
    # 固定图标路径：相对于脚本所在目录的 ../assets/images/bg.png
    script_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(script_dir, '..', 'assets', 'images','icon','bg.png')
    if not os.path.exists(icon_path):
        icon_path = None  # 如果文件不存在，则不使用图标
    toaster.show_toast(title, message, duration=5, icon_path=icon_path, threaded=True)

if __name__ == "__main__":
    send_notification("流萤桌宠", "插件已关闭\n感谢你的使用！")
    target_processes = [
        "流萤桌宠.exe",
        "工具组件.exe",
        "AI.exe",
        "插件.exe",
        "关闭程序.exe"
    ]
    print("正在终止进程及释放资源...")
    kill_process_and_children(target_processes)
    print("操作完成。")