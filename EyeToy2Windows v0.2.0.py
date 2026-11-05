"""
EyeToy2Windows - Manual Driver Setup
Scans for the EyeToy, waits for you to install the driver, applies registry tweaks, and starts streaming.
"""

import ctypes
from ctypes import wintypes
import winreg
import io
import queue
import sys
import threading
import time
import tkinter as tk
import subprocess
import os

# --- Virtual webcam additions ---
import numpy as np
import pyvirtualcam
# ---------------------------------

try:
    from PIL import Image, ImageTk, ImageFile, ImageFilter
    ImageFile.LOAD_TRUNCATED_IMAGES = False
except ImportError:
    print("Error: Pillow is missing. Install it with: python -m pip install pillow")
    sys.exit(1)

# ============================================================
# Configuration
# ============================================================
MODE = "live"
CUSTOM_GUID = "{6B0F2C7E-3C6B-4D2E-9B1A-8C3E6F0D4A11}"
VID_PID = "vid_054c&pid_0155"
MI_STRING = "mi_00"

ALT_SETTING = 3
PIPE_ID = 0x81
NUM_PACKETS = 16
NUM_TRANSFERS = 4

TIMEOUT_MS = 5000
STILL_TIMEOUT_S = 15.0

ERROR_IO_PENDING = 997
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF

# ============================================================
# Windows structures
# ============================================================
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
    ]

class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_size_t),
    ]

class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_size_t),
    ]

class USBD_ISO_PACKET_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Offset", wintypes.DWORD), ("Length", wintypes.DWORD), ("Status", wintypes.DWORD),
    ]

class WINUSB_PIPE_INFORMATION_EX(ctypes.Structure):
    _fields_ = [
        ("PipeType", ctypes.c_int), ("PipeId", ctypes.c_ubyte),
        ("MaximumPacketSize", ctypes.c_ushort), ("Interval", ctypes.c_ubyte),
        ("MaximumBytesPerInterval", wintypes.DWORD),
    ]

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]

class WINUSB_SETUP_PACKET(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("RequestType", ctypes.c_ubyte), ("Request", ctypes.c_ubyte),
        ("Value", ctypes.c_ushort), ("Index", ctypes.c_ushort), ("Length", ctypes.c_ushort),
    ]

# ============================================================
# DLLs and functions
# ============================================================
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winusb = ctypes.WinDLL("winusb", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)

ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

cfgmgr32.CM_Get_DevNode_Status.restype = wintypes.DWORD
cfgmgr32.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG]
cfgmgr32.CM_Disable_DevNode.restype = wintypes.DWORD
cfgmgr32.CM_Disable_DevNode.argtypes = [wintypes.DWORD, wintypes.ULONG]
cfgmgr32.CM_Enable_DevNode.restype = wintypes.DWORD
cfgmgr32.CM_Enable_DevNode.argtypes = [wintypes.DWORD, wintypes.ULONG]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
kernel32.ResetEvent.restype = wintypes.BOOL

winusb.WinUsb_Initialize.restype = wintypes.BOOL
winusb.WinUsb_Free.argtypes = [wintypes.HANDLE]
winusb.WinUsb_SetCurrentAlternateSetting.restype = wintypes.BOOL
winusb.WinUsb_QueryPipeEx.restype = wintypes.BOOL
winusb.WinUsb_ControlTransfer.restype = wintypes.BOOL
winusb.WinUsb_RegisterIsochBuffer.restype = wintypes.BOOL
winusb.WinUsb_UnregisterIsochBuffer.restype = wintypes.BOOL
winusb.WinUsb_ReadIsochPipeAsap.restype = wintypes.BOOL
winusb.WinUsb_GetOverlappedResult.restype = wintypes.BOOL
winusb.WinUsb_AbortPipe.restype = wintypes.BOOL

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
DIGCF_ALLCLASSES = 0x04
GENERIC_RW = 0xC0000000
SHARE_RW = 3
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE = wintypes.HANDLE(-1).value

# ============================================================
# Bridge / Sensor functions (Unchanged)
# ============================================================
def ov519_write(wh, reg, value):
    buf = (ctypes.c_ubyte * 1)(value & 0xFF)
    setup = WINUSB_SETUP_PACKET(0x40, 0x01, 0x0000, reg, 1)
    transferred = wintypes.DWORD(0)
    if not winusb.WinUsb_ControlTransfer(wh, setup, ctypes.cast(buf, ctypes.c_void_p), 1, ctypes.byref(transferred), None): raise ctypes.WinError()
    if transferred.value != 1: raise RuntimeError(f"Short bridge write at 0x{reg:02X}")

def ov519_read(wh, reg):
    buf = (ctypes.c_ubyte * 1)(0)
    setup = WINUSB_SETUP_PACKET(0xC0, 0x01, 0x0000, reg, 1)
    transferred = wintypes.DWORD(0)
    if not winusb.WinUsb_ControlTransfer(wh, setup, ctypes.cast(buf, ctypes.c_void_p), 1, ctypes.byref(transferred), None): raise ctypes.WinError()
    if transferred.value != 1: raise RuntimeError(f"Short bridge read at 0x{reg:02X}")
    return buf[0]

def ov519_write_mask(wh, reg, value, mask):
    if mask == 0xFF: ov519_write(wh, reg, value); return
    old = ov519_read(wh, reg)
    ov519_write(wh, reg, ((old & (~mask & 0xFF)) | (value & mask)))

def i2c_write(wh, reg, value):
    ov519_write(wh, 0x42, reg); ov519_write(wh, 0x45, value); ov519_write(wh, 0x47, 0x01); time.sleep(0.002)

def i2c_read(wh, reg):
    ov519_write(wh, 0x43, reg); ov519_write(wh, 0x47, 0x03); time.sleep(0.002)
    for _ in range(10):
        if ov519_read(wh, 0x47) & 0x01: break
        time.sleep(0.001)
    ov519_write(wh, 0x47, 0x05); time.sleep(0.002)
    for _ in range(10):
        if ov519_read(wh, 0x47) & 0x01: break
        time.sleep(0.001)
    value = ov519_read(wh, 0x45)
    ov519_write(wh, 0x42, 0xFF); ov519_write(wh, 0x45, 0x00); ov519_write(wh, 0x40, 0x01)
    return value

def i2c_write_mask(wh, reg, value, mask):
    if mask == 0xFF: i2c_write(wh, reg, value); return
    old = i2c_read(wh, reg)
    i2c_write(wh, reg, ((old & (~mask & 0xFF)) | (value & mask)))

def init_sensor(wh):
    print("\nInitializing OV519 bridge and OV7648 sensor...")
    startup = [
        (0x5A, 0x6D), (0x53, 0x9B), (0x54, 0xFF), (0x5D, 0x03), (0x49, 0x01),
        (0x48, 0x00), (0x72, 0xEE), (0x51, 0x0F), (0x51, 0x00), (0x22, 0x00),
    ]
    for reg, value in startup: ov519_write(wh, reg, value); time.sleep(0.005)
    time.sleep(0.020)
    ov519_write(wh, 0x41, 0x42); ov519_write(wh, 0x44, 0x43); i2c_write(wh, 0x12, 0x80); time.sleep(0.150)
    
    bridge_mode = [
        (0x5D, 0x03), (0x53, 0x9F), (0x54, 0x0F), (0xA2, 0x20), (0xA3, 0x18),
        (0xA4, 0x04), (0xA5, 0x28), (0x37, 0x00), (0x55, 0x02), (0x22, 0x1D),
        (0x17, 0x50), (0x40, 0xFF), (0x46, 0x00), (0x59, 0x04), (0xFF, 0x00),
    ]
    for reg, value in bridge_mode: ov519_write(wh, reg, value)
    
    i2c_write(wh, 0x12, 0x80); time.sleep(0.150); i2c_write(wh, 0x12, 0x14)
    ov519_write_mask(wh, 0x20, 0x10, 0x10)
    i2c_write_mask(wh, 0x14, 0x20, 0x20); i2c_write_mask(wh, 0x28, 0x00, 0x20)
    i2c_write_mask(wh, 0x2D, 0x40, 0x40); i2c_write_mask(wh, 0x67, 0xF0, 0xF0)
    i2c_write_mask(wh, 0x74, 0x20, 0x20); i2c_write_mask(wh, 0x12, 0x04, 0x04)
    
    i2c_write(wh, 0x11, 0x00); i2c_write(wh, 0x17, 0x1A); i2c_write(wh, 0x18, 0xBA)
    i2c_write(wh, 0x19, 0x03); i2c_write(wh, 0x1A, 0xF3)
    
    geometry = [
        (0x10, 320 >> 4), (0x11, 240 >> 3), (0x12, 0x00), (0x13, 0x00),
        (0x14, 0x00), (0x15, 0x00), (0x16, 0x00), (0x25, 0x03), (0x26, 0x00),
        (0xA4, 0x0C), (0x23, 0xFF),
    ]
    for reg, value in geometry: ov519_write(wh, reg, value)
    
    ov519_write(wh, 0x51, 0x0F); ov519_write(wh, 0x51, 0x00)
    ov519_write(wh, 0x22, 0x1D); ov519_write_mask(wh, 0x71, 0x01, 0x01)
    print("Sensor initialization complete.")

# ============================================================
# NEW: Manual Setup & Scan Logic
# ============================================================
def find_device_node():
    """Scans for the EyeToy and returns its SP_DEVINFO_DATA and Instance ID."""
    hdev = setupapi.SetupDiGetClassDevsW(None, "USB", None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if hdev == INVALID_HANDLE: return None, None
    try:
        index = 0
        while True:
            did = SP_DEVINFO_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInfo(hdev, index, ctypes.byref(did)): break
            index += 1
            
            req_id = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInstanceIdW(hdev, ctypes.byref(did), None, 0, ctypes.byref(req_id))
            if req_id.value:
                id_buf = ctypes.create_unicode_buffer(req_id.value)
                setupapi.SetupDiGetDeviceInstanceIdW(hdev, ctypes.byref(did), id_buf, req_id.value, ctypes.byref(req_id))
                if VID_PID in id_buf.value.lower() and MI_STRING in id_buf.value.lower():
                    return did, id_buf.value
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return None, None

def apply_registry_tweaks(instance_id):
    """Writes DeviceInterfaceGUIDs to the registry so Python can find the camera."""
    print("Applying registry tweaks...")
    reg_path = f"SYSTEM\\CurrentControlSet\\Enum\\{instance_id}\\Device Parameters"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, reg_path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "DeviceInterfaceGUIDs", 0, winreg.REG_MULTI_SZ, [CUSTOM_GUID])
        print("Registry tweaks applied successfully.")
    except Exception as e:
        print(f"Error: failed to write registry: {e}")

def _find_path_by_guid(guid_str, target_devinfo):
    guid = GUID()
    ole32.CLSIDFromString(guid_str, ctypes.byref(guid))
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev == INVALID_HANDLE: return None
    try:
        index = 0
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), index, ctypes.byref(did)): break
            index += 1

            req = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0, ctypes.byref(req), None)
            if req.value:
                detail = ctypes.create_string_buffer(req.value)
                ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = (8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
                
                assoc = SP_DEVINFO_DATA()
                assoc.cbSize = ctypes.sizeof(assoc)
                
                if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), detail, req.value, ctypes.byref(req), ctypes.byref(assoc)):
                    if assoc.DevInst == target_devinfo.DevInst:
                        return ctypes.wstring_at(ctypes.addressof(detail) + 4)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return None

def find_interface_path(target_devinfo):
    """Finds the WinUSB path using our custom GUID, falling back to the generic WinUSB GUID."""
    print("Locating camera interface...")
    
    # Try our custom GUID first
    path = _find_path_by_guid(CUSTOM_GUID, target_devinfo)
    if path: return path
    
    # Fallback to default Microsoft WinUSB GUID
    winusb_guid = "{A5DCBF10-6530-11D2-901F-00C04FB951ED}"
    return _find_path_by_guid(winusb_guid, target_devinfo)

def scan_and_setup():
    print("Scanning for Sony EyeToy...")
    devinfo, instance_id = find_device_node()
    
    if not devinfo:
        print("Error: EyeToy not found. Make sure it is plugged in.")
        return None
        
    print(f"Found EyeToy: {instance_id}")
    
    # Check if driver is installed (Status 28 = missing driver)
    status = wintypes.DWORD(0)
    problem = wintypes.DWORD(0)
    cfgmgr32.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), devinfo.DevInst, 0)
    
    if problem.value == 28:
        print()
        print("=" * 60)
        print("DRIVER INSTALLATION REQUIRED")
        print("=" * 60)
        print("Windows does not have a driver installed for this device.")
        print("Please follow these steps in Device Manager:")
        print()
        print("  1. Right-click the Windows Start button.")
        print("  2. Click 'Device Manager'.")
        print("  3. Find the EyeToy (it will have a yellow warning icon).")
        print("  4. Right-click it and select 'Update driver'.")
        print("  5. Click 'Browse my computer for drivers'.")
        print("  6. Click 'Let me pick from a list of available drivers'.")
        print("  7. Find 'Universal Serial Bus devices' and click 'Next'.")
        print("  8. Select 'WinUSB Device' and click 'Next'.")
        print("=" * 60)
        print()
        
        # We removed the ShellExecuteW line here!
        
        input("Press Enter here after you have finished installing the driver in Device Manager...")
        
        # Re-scan to confirm it installed
        print("\nRe-scanning to verify driver installation...")
        devinfo, instance_id = find_device_node()
        if not devinfo:
            print("Error: EyeToy disappeared. Unplug and replug it.")
            return None
            
        cfgmgr32.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), devinfo.DevInst, 0)
        if problem.value == 28:
            print("Error: driver still not installed. Please try again.")
            return None
        print("Driver successfully verified.")

    # Apply the registry tweaks
    apply_registry_tweaks(instance_id)
    
    # RESTART THE DEVICE so WinUSB re-reads the registry and creates the interface
    print("Restarting device to apply registry changes...")
    cfgmgr32.CM_Disable_DevNode(devinfo.DevInst, 0)
    time.sleep(1.5)
    cfgmgr32.CM_Enable_DevNode(devinfo.DevInst, 0)
    time.sleep(3.0) # Give Windows time to recreate the interface
    
    # Re-find the device node since restarting it changes its state slightly
    devinfo, instance_id = find_device_node()
    if not devinfo: return None

    # Finally, find the path
    path = find_interface_path(devinfo)
    if path:
        print(f"Camera interface path: {path}")
        return path
    else:
        print("Error: could not find the camera interface path.")
        return None

# ============================================================
# Camera Streaming (Unchanged)
# ============================================================
def open_camera(path):
    handle = kernel32.CreateFileW(path, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    if handle == INVALID_HANDLE: raise ctypes.WinError()
    interface = wintypes.HANDLE()
    if not winusb.WinUsb_Initialize(handle, ctypes.byref(interface)):
        kernel32.CloseHandle(handle); raise ctypes.WinError()
    return handle, interface

def close_camera(handle, interface):
    if interface:
        try: winusb.WinUsb_SetCurrentAlternateSetting(interface, 0)
        except: pass
        winusb.WinUsb_Free(interface)
    if handle and handle != INVALID_HANDLE: kernel32.CloseHandle(handle)

class IsoTransfer:
    def __init__(self, wh, packet_size):
        self.buffer_size = NUM_PACKETS * packet_size
        self.buffer = ctypes.create_string_buffer(self.buffer_size)
        self.iso = wintypes.HANDLE()
        if not winusb.WinUsb_RegisterIsochBuffer(wh, PIPE_ID, self.buffer, self.buffer_size, ctypes.byref(self.iso)): raise ctypes.WinError()
        self.event = kernel32.CreateEventW(None, True, False, None)
        self.overlapped = OVERLAPPED()
        self.overlapped.hEvent = self.event
        self.packets = (USBD_ISO_PACKET_DESCRIPTOR * NUM_PACKETS)()
        self.packet_size = packet_size

    def submit(self, continue_stream):
        for i in range(NUM_PACKETS):
            self.packets[i].Offset = i * self.packet_size
            self.packets[i].Length = self.packet_size
            self.packets[i].Status = 0
        kernel32.ResetEvent(self.event)
        ok = winusb.WinUsb_ReadIsochPipeAsap(self.iso, 0, self.buffer_size, continue_stream, NUM_PACKETS, self.packets, ctypes.byref(self.overlapped))
        return ok or ctypes.get_last_error() == ERROR_IO_PENDING

    def cleanup(self):
        if self.iso: winusb.WinUsb_UnregisterIsochBuffer(self.iso)
        if self.event: kernel32.CloseHandle(self.event)

class FrameAssembler:
    def __init__(self):
        self.buf = bytearray()
        self.in_frame = False
    def reset(self):
        self.buf.clear()
        self.in_frame = False
    def feed_packet(self, packet, status):
        if status != 0 or not packet: return []
        if len(packet) >= 4 and packet[:3] == b"\xFF\xFF\xFF":
            marker = packet[3]
            if marker == 0x50:
                if self.in_frame: self.reset()
                self.in_frame = True
                if len(packet) > 16: self.buf.extend(packet[16:])
                return []
            elif marker == 0x51:
                if not self.in_frame: return []
                frame = bytes(self.buf)
                self.reset()
                idx_start = frame.find(b"\xFF\xD8")
                if idx_start == -1: return []
                eoi = frame.rfind(b"\xFF\xD9")
                if eoi < 0: return []
                if (eoi - idx_start) < 1000: return []
                return [frame[idx_start:eoi + 2]]
        if self.in_frame: self.buf.extend(packet)
        return []

def push_latest(q, frame):
    try:
        while True: q.get_nowait()
    except queue.Empty: pass
    q.put_nowait(frame)

def capture_loop(wh, frame_queue, mode, stop_flag):
    winusb.WinUsb_SetCurrentAlternateSetting(wh, ALT_SETTING)
    pipe = WINUSB_PIPE_INFORMATION_EX()
    winusb.WinUsb_QueryPipeEx(wh, ALT_SETTING, 0, ctypes.byref(pipe))
    packet_size = max(int(pipe.MaximumPacketSize), int(pipe.MaximumBytesPerInterval))
    print(f"Streaming started (packet size: {packet_size}).")

    transfers = [IsoTransfer(wh, packet_size) for _ in range(NUM_TRANSFERS)]
    handles = (wintypes.HANDLE * NUM_TRANSFERS)(*[t.event for t in transfers])
    assembler = FrameAssembler()
    frames = 0
    consecutive_timeouts = 0  # <--- Track temporary errors

    for i, t in enumerate(transfers): t.submit(continue_stream=(i > 0))

    try:
        while not stop_flag.is_set():
            wait_res = kernel32.WaitForMultipleObjects(NUM_TRANSFERS, handles, False, TIMEOUT_MS)
            
            # --- HANDLE TIMEOUTS (System Lag) ---
            if wait_res == WAIT_TIMEOUT:
                consecutive_timeouts += 1
                if consecutive_timeouts > 10: # 10 timeouts (50 seconds) means it's actually gone
                    print("\n[USB] Too many timeouts. Assuming camera is disconnected.")
                    break
                # Try resubmitting the transfers to keep the pipe alive
                for t in transfers:
                    t.submit(continue_stream=True)
                continue
                
            if wait_res == WAIT_FAILED or wait_res >= NUM_TRANSFERS:
                break

            # We got a successful transfer, reset the strike counter
            consecutive_timeouts = 0 
            
            t = transfers[wait_res]
            transferred = wintypes.DWORD(0)
            
            # --- HANDLE OVERLAPPED FAILURES ---
            if not winusb.WinUsb_GetOverlappedResult(wh, ctypes.byref(t.overlapped), ctypes.byref(transferred), False):
                err = ctypes.get_last_error()
                # 1167 = ERROR_DEVICE_NOT_CONNECTED (Physical unplug)
                # 995 = ERROR_OPERATION_ABORTED (We stopped it)
                if err in (1167, 995): 
                    break
                else:
                    # Temporary USB stall, try to resubmit
                    t.submit(continue_stream=True)
                    continue

            for p in range(NUM_PACKETS):
                length = int(t.packets[p].Length)
                status = int(t.packets[p].Status)
                if status != 0 or length <= 0: continue
                packet = t.buffer[int(t.packets[p].Offset):int(t.packets[p].Offset) + length]
                
                for frame_bytes in assembler.feed_packet(packet, status):
                    frames += 1
                    try:
                        # 1. Decode JPEG
                        img = Image.open(io.BytesIO(frame_bytes))
                        
                        # --- THE FIX ---
                        # Force PIL to fully load the image. If the USB dropped packets 
                        # and the JPEG is incomplete, this throws an exception.
                        img.load() 
                        # ----------------
                        
                        # 2. Resize
                        img = img.resize((640, 480), Image.BICUBIC)
                        
                        # 3. Convert to NumPy ONCE
                        img_array = np.array(img)
                        
                        # 4. Flip BGR to RGB instantly using NumPy slicing
                        img_array = img_array[:, :, ::-1]
                        
                        # 5. Put the finished array in the queue
                        push_latest(frame_queue, img_array)
                    except Exception as e:
                        # If the frame is broken, we just silently drop it and grab the next one
                        pass

            if not t.submit(continue_stream=True):
                t.submit(continue_stream=False)
    finally:
        for t in transfers: t.cleanup()
        try: winusb.WinUsb_SetCurrentAlternateSetting(wh, 0)
        except: pass
        print("\n[USB] Camera disconnected or stream stopped.")

# ============================================================
# UI App
# ============================================================
class App:
    def __init__(self, mode, frame_queue, worker):
        self.q = frame_queue
        self.worker = worker
        self.win = tk.Tk()
        self.win.title(f"EyeToy2Windows — v0.1.1")
        self.win.geometry("640x480")
        self.win.configure(bg="black")
        self.lbl = tk.Label(self.win, text="Waiting for EyeToy frames...", fg="white", bg="black", font=("Arial", 14))
        self.lbl.pack(expand=True, fill="both")
        self.photo = None
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)
        self.win.after(15, self.poll) # Increased refresh rate to ~60fps
        try:
            self.vcam = pyvirtualcam.Camera(width=640, height=480, fps=30)
            print(f"Virtual camera started: {self.vcam.device}")
        except Exception as e:
            print("\n[WARNING] OBS Virtual Camera not found. Is OBS installed and running?")
            print("The preview window will still work, but other apps won't see the camera.")
            self.vcam = None

    def poll(self):
        # Check if the background thread died (e.g., unplugged)
        if not self.worker.is_alive():
            print("\n[USB] Camera disconnected. Closing window...")
            self.on_close()
            return

        try: 
            frame_array = self.q.get_nowait()
        except queue.Empty: 
            frame_array = None

        if frame_array is not None:
            try:
                # The image is already processed (RGB, resized). Just display and send!
                if self.vcam is not None:
                    self.vcam.send(frame_array)
                    self.vcam.sleep_until_next_frame()
                
                image = Image.fromarray(frame_array)
                self.photo = ImageTk.PhotoImage(image)
                self.lbl.config(image=self.photo, text="")
            except Exception: 
                pass
        self.win.after(15, self.poll)
        
    def on_close(self):
        if self.vcam is not None:
            try: 
                self.vcam.close()
            except: 
                pass
        self.win.destroy()

# ============================================================
# Main
# ============================================================
def main():
    print(f"Sony EyeToy — {MODE.capitalize()}")
    
    # --- ADDED: Force Administrator privileges ---
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("\n[ERROR] This script must be run as Administrator.")
        print("Please close this window, right-click Command Prompt, and select 'Run as administrator'.")
        input("Press Enter to exit...")
        sys.exit(1)
    # ---------------------------------------------
    
    # Run the scan and setup routine
    path = scan_and_setup()
    if not path:
        input("Setup failed. Press Enter to exit...")
        return

    handle = wh = None
    worker = None
    stop_flag = threading.Event()

    try:
        handle, wh = open_camera(path)
        init_sensor(wh)
        frame_queue = queue.Queue()

        worker = threading.Thread(target=capture_loop, args=(wh, frame_queue, MODE, stop_flag), daemon=True)
        worker.start()

        App(MODE, frame_queue, worker).win.mainloop()

    finally:
        stop_flag.set()
        if wh:
            try: 
                winusb.WinUsb_AbortPipe(wh, PIPE_ID)
            except: 
                pass
        if worker and worker.is_alive(): 
            worker.join(timeout=2.0)
        if handle or wh: 
            close_camera(handle, wh)
        print("Done.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nError: {type(exc).__name__}: {exc}")
        input("Press Enter to close...")
