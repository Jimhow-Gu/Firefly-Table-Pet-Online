import requests
import os
import sys
import schedule
import time
import tkinter as tk
from tkinter import messagebox, Label
from PIL import *
from tkinter import messagebox
from PIL import ImageTk, Image
import ctypes
import ctypes.wintypes


# 新增：获取工作区域尺寸（排除任务栏）
def get_work_area():
    class RECT(ctypes.Structure):
        _fields_ = [
            ('left', ctypes.c_long),
            ('top', ctypes.c_long),
            ('right', ctypes.c_long),
            ('bottom', ctypes.c_long)
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ('cbSize', ctypes.wintypes.DWORD),
            ('rcMonitor', RECT),
            ('rcWork', RECT),
            ('dwFlags', ctypes.wintypes.DWORD)
        ]

    monitor_info = MONITORINFO()
    monitor_info.cbSize = ctypes.sizeof(MONITORINFO)
    ctypes.windll.user32.GetMonitorInfoW(
        ctypes.windll.user32.MonitorFromWindow(0, 2),
        ctypes.byref(monitor_info)
    )
    return monitor_info.rcWork.right, monitor_info.rcWork.bottom


# 修改后的窗口定位函数
def center_window(root, width, height):
    work_right, work_bottom = get_work_area()
    x = work_right - width
    y = work_bottom - height
    root.geometry(f'{width}x{height}+{x}+{y}')


# 创建弹窗函数（其他部分保持不变）
def show_notification(message):
    popup = tk.Tk()
    popup.title("插件")
    popup.wm_attributes("-topmost", True)
    popup_width = 300
    popup_height = 100
    popup.overrideredirect(True)
    center_window(popup, popup_width, popup_height)
    label = Label(popup, text=message, font=('Arial', 14), padx=20, pady=20)
    label.pack()
    label.place(x=90, y=0)

    try:
        image = Image.open("bg.png")
        image = image.resize((67, 86), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        img_label = Label(popup, image=photo)
        img_label.image = photo
        img_label.pack(side=tk.LEFT, padx=10, pady=10)
    except FileNotFoundError:
        pass

    popup.after(5000, popup.destroy)
    popup.mainloop()


if __name__ == "__main__":
    show_notification(" 插件已关闭 \n感谢你的使用！")