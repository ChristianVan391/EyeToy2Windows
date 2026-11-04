"""
eyetoy.py -- Sony EyeToy (054C:0155) on Windows 11
Multi-transfer streaming with live preview and virtual webcam output.
"""

import ctypes
from ctypes import wintypes
import io
import queue
import sys
import threading
import time
import tkinter as tk

# --- Virtual webcam additions ---
import numpy as np
import pyvirtualcam
# ---------------------------------

try:
    from PIL import Image, ImageTk, ImageFile, ImageFilter
    ImageFile.LOAD_TRUNCATED_IMAGES = False
except ImportError:
    print("Pillow missing. Install with: python -m pip install pillow")
    sys.exit(1)


# ============================================================
# Configuration
# ============================================================

MODE = "live"       # "still" or "live"

IFACE_GUID = "{6B0F2C7E-3C6B-4D2E-9B1A-8C3E6F0D4A11}"
VID_PID = "vid_054c&pid_0155"
MI_STRING = "mi_00"

ALT_SETTING = 3
PIPE_ID = 0x81
NUM_PACKETS = 16          # Increased buffer size per transfer
NUM_TRANSFERS = 4         # Keep 4 transfers in flight simultaneously

TIMEOUT_MS = 5000
STILL_TIMEOUT_S = 15.0

ERROR_IO_PENDING = 997
ERROR_INVALID_PARAMETER = 87
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF

# ============================================================
# Windows structures
# ============================================================

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]

class USBD_ISO_PACKET_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Offset", wintypes.DWORD),
        ("Length", wintypes.DWORD),
        ("Status", wintypes.DWORD),
    ]

class WINUSB_PIPE_INFORMATION_EX(ctypes.Structure):
    _fields_ = [
        ("PipeType", ctypes.c_int),
        ("PipeId", ctypes.c_ubyte),
        ("MaximumPacketSize", ctypes.c_ushort),
        ("Interval", ctypes.c_ubyte),
        ("MaximumBytesPerInterval", wintypes.DWORD),
    ]

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]

class WINUSB_SETUP_PACKET(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("RequestType", ctypes.c_ubyte),
        ("Request", ctypes.c_ubyte),
        ("Value", ctypes.c_ushort),
        ("Index", ctypes.c_ushort),
        ("Length", ctypes.c_ushort),
    ]

# ============================================================
# DLLs and function declarations
# ============================================================

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winusb = ctypes.WinDLL("winusb", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
ole32.CLSIDFromString.restype = ctypes.c_long

setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]

setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]

setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [
    ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]

kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]

kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

winusb.WinUsb_Initialize.restype = wintypes.BOOL
winusb.WinUsb_Initialize.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE)]

winusb.WinUsb_Free.argtypes = [wintypes.HANDLE]

winusb.WinUsb_SetCurrentAlternateSetting.restype = wintypes.BOOL
winusb.WinUsb_SetCurrentAlternateSetting.argtypes = [wintypes.HANDLE, ctypes.c_ubyte]

winusb.WinUsb_QueryPipeEx.restype = wintypes.BOOL
winusb.WinUsb_QueryPipeEx.argtypes = [
    wintypes.HANDLE, ctypes.c_ubyte, ctypes.c_ubyte,
    ctypes.POINTER(WINUSB_PIPE_INFORMATION_EX)]

winusb.WinUsb_ControlTransfer.restype = wintypes.BOOL
winusb.WinUsb_ControlTransfer.argtypes = [
    wintypes.HANDLE, WINUSB_SETUP_PACKET, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]

winusb.WinUsb_RegisterIsochBuffer.restype = wintypes.BOOL
winusb.WinUsb_RegisterIsochBuffer.argtypes = [
    wintypes.HANDLE, ctypes.c_ubyte, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE)]

winusb.WinUsb_UnregisterIsochBuffer.restype = wintypes.BOOL
winusb.WinUsb_UnregisterIsochBuffer.argtypes = [wintypes.HANDLE]

winusb.WinUsb_ReadIsochPipeAsap.restype = wintypes.BOOL
winusb.WinUsb_ReadIsochPipeAsap.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL,
    wintypes.DWORD, ctypes.POINTER(USBD_ISO_PACKET_DESCRIPTOR),
    ctypes.POINTER(OVERLAPPED)]

winusb.WinUsb_GetOverlappedResult.restype = wintypes.BOOL
winusb.WinUsb_GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL]

winusb.WinUsb_AbortPipe.restype = wintypes.BOOL
winusb.WinUsb_AbortPipe.argtypes = [wintypes.HANDLE, ctypes.c_ubyte]

# ============================================================
# Constants
# ============================================================

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

GENERIC_RW = 0xC0000000
SHARE_RW = 3
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000

INVALID_HANDLE = wintypes.HANDLE(-1).value

# ============================================================
# Bridge / Sensor functions
# ============================================================

def ov519_write(wh, reg, value):
    buf = (ctypes.c_ubyte * 1)(value & 0xFF)
    setup = WINUSB_SETUP_PACKET(0x40, 0x01, 0x0000, reg, 1)
    transferred = wintypes.DWORD(0)
    ok = winusb.WinUsb_ControlTransfer(
        wh, setup, ctypes.cast(buf, ctypes.c_void_p), 1,
        ctypes.byref(transferred), None)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    if transferred.value != 1:
        raise RuntimeError(f"Short bridge write at 0x{reg:02X}")

def ov519_read(wh, reg):
    buf = (ctypes.c_ubyte * 1)(0)
    setup = WINUSB_SETUP_PACKET(0xC0, 0x01, 0x0000, reg, 1)
    transferred = wintypes.DWORD(0)
    ok = winusb.WinUsb_ControlTransfer(
        wh, setup, ctypes.cast(buf, ctypes.c_void_p), 1,
        ctypes.byref(transferred), None)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    if transferred.value != 1:
        raise RuntimeError(f"Short bridge read at 0x{reg:02X}")
    return buf[0]

def ov519_write_mask(wh, reg, value, mask):
    if mask == 0xFF:
        ov519_write(wh, reg, value)
        return
    old = ov519_read(wh, reg)
    ov519_write(wh, reg, ((old & (~mask & 0xFF)) | (value & mask)))

def i2c_write(wh, reg, value):
    ov519_write(wh, 0x42, reg)
    ov519_write(wh, 0x45, value)
    ov519_write(wh, 0x47, 0x01)
    time.sleep(0.002)

def i2c_read(wh, reg):
    ov519_write(wh, 0x43, reg)
    ov519_write(wh, 0x47, 0x03)
    time.sleep(0.002)
    for _ in range(10):
        if ov519_read(wh, 0x47) & 0x01: break
        time.sleep(0.001)
    ov519_write(wh, 0x47, 0x05)
    time.sleep(0.002)
    for _ in range(10):
        if ov519_read(wh, 0x47) & 0x01: break
        time.sleep(0.001)
    value = ov519_read(wh, 0x45)
    ov519_write(wh, 0x42, 0xFF)
    ov519_write(wh, 0x45, 0x00)
    ov519_write(wh, 0x40, 0x01)
    return value

def i2c_write_mask(wh, reg, value, mask):
    if mask == 0xFF:
        i2c_write(wh, reg, value)
        return
    old = i2c_read(wh, reg)
    i2c_write(wh, reg, ((old & (~mask & 0xFF)) | (value & mask)))

def init_sensor(wh):
    print("\n--- Initializing OV519 Bridge & OV7648 ---")
    startup = [
        (0x5A, 0x6D), (0x53, 0x9B), (0x54, 0xFF), (0x5D, 0x03),
        (0x49, 0x01), (0x48, 0x00), (0x72, 0xEE), (0x51, 0x0F),
        (0x51, 0x00), (0x22, 0x00),
    ]
    for reg, value in startup:
        ov519_write(wh, reg, value)
        time.sleep(0.005)
    time.sleep(0.020)
    ov519_write(wh, 0x41, 0x42)
    ov519_write(wh, 0x44, 0x43)
    i2c_write(wh, 0x12, 0x80)
    time.sleep(0.150)
    
    bridge_mode = [
        (0x5D, 0x03), (0x53, 0x9F), (0x54, 0x0F), (0xA2, 0x20),
        (0xA3, 0x18), (0xA4, 0x04), (0xA5, 0x28), (0x37, 0x00),
        (0x55, 0x02), (0x22, 0x1D), (0x17, 0x50), (0x40, 0xFF),
        (0x46, 0x00), (0x59, 0x04), (0xFF, 0x00),
    ]
    for reg, value in bridge_mode: ov519_write(wh, reg, value)
    
    i2c_write(wh, 0x12, 0x80); time.sleep(0.150); i2c_write(wh, 0x12, 0x14)
    ov519_write_mask(wh, 0x20, 0x10, 0x10)
    
    i2c_write_mask(wh, 0x14, 0x20, 0x20)
    i2c_write_mask(wh, 0x28, 0x00, 0x20)
    i2c_write_mask(wh, 0x2D, 0x40, 0x40)
    i2c_write_mask(wh, 0x67, 0xF0, 0xF0)
    i2c_write_mask(wh, 0x74, 0x20, 0x20)
    i2c_write_mask(wh, 0x12, 0x04, 0x04)
    
    # Removed the bad registers (0x13, 0x24, 0x25) that caused the green screen!
    
    i2c_write(wh, 0x11, 0x00)
    i2c_write(wh, 0x17, 0x1A); i2c_write(wh, 0x18, 0xBA)
    i2c_write(wh, 0x19, 0x03); i2c_write(wh, 0x1A, 0xF3)
    
    geometry = [
        (0x10, 320 >> 4), (0x11, 240 >> 3), (0x12, 0x00),
        (0x13, 0x00), (0x14, 0x00), (0x15, 0x00),
        (0x16, 0x00), (0x25, 0x03), (0x26, 0x00),
        (0xA4, 0x0C), (0x23, 0xFF),
    ]
    for reg, value in geometry: ov519_write(wh, reg, value)
    
    ov519_write(wh, 0x51, 0x0F); ov519_write(wh, 0x51, 0x00)
    ov519_write(wh, 0x22, 0x1D); ov519_write_mask(wh, 0x71, 0x01, 0x01)
    print("[OK] Initialization complete")

def find_mi00_path():
    guid = GUID()
    ole32.CLSIDFromString(IFACE_GUID, ctypes.byref(guid))
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev == INVALID_HANDLE: raise ctypes.WinError(ctypes.get_last_error())
    try:
        index = 0
        while True:
            did = SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), index, ctypes.byref(did)):
                break
            required = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0, ctypes.byref(required), None)
            if required.value:
                detail = ctypes.create_string_buffer(required.value)
                ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = (8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
                if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), detail, required.value, ctypes.byref(required), None):
                    path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
                    if VID_PID in path.lower() and MI_STRING in path.lower(): return path
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return None

def open_camera():
    path = find_mi00_path()
    if not path: raise RuntimeError("MI_00 not found.")
    print("[USB] Opening:", path)
    handle = kernel32.CreateFileW(path, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    if handle == INVALID_HANDLE: raise ctypes.WinError(ctypes.get_last_error())
    interface = wintypes.HANDLE()
    if not winusb.WinUsb_Initialize(handle, ctypes.byref(interface)):
        err = ctypes.get_last_error(); kernel32.CloseHandle(handle); raise ctypes.WinError(err)
    return handle, interface

def close_camera(handle, interface):
    if interface:
        try: winusb.WinUsb_SetCurrentAlternateSetting(interface, 0)
        except: pass
        winusb.WinUsb_Free(interface)
    if handle and handle != INVALID_HANDLE: kernel32.CloseHandle(handle)

# ============================================================
# Isochronous Multi-Transfer Setup
# ============================================================

class IsoTransfer:
    def __init__(self, wh, packet_size):
        self.buffer_size = NUM_PACKETS * packet_size
        self.buffer = ctypes.create_string_buffer(self.buffer_size)
        self.iso = wintypes.HANDLE()
        if not winusb.WinUsb_RegisterIsochBuffer(wh, PIPE_ID, self.buffer, self.buffer_size, ctypes.byref(self.iso)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.event = kernel32.CreateEventW(None, True, False, None)
        if not self.event: raise ctypes.WinError(ctypes.get_last_error())
        self.overlapped = OVERLAPPED()
        self.overlapped.hEvent = self.event
        self.packets = (USBD_ISO_PACKET_DESCRIPTOR * NUM_PACKETS)()
        self.packet_size = packet_size
        self.active = False

    def submit(self, continue_stream):
        for i in range(NUM_PACKETS):
            self.packets[i].Offset = i * self.packet_size
            self.packets[i].Length = self.packet_size
            self.packets[i].Status = 0
        kernel32.ResetEvent(self.event)
        ok = winusb.WinUsb_ReadIsochPipeAsap(
            self.iso, 0, self.buffer_size, continue_stream,
            NUM_PACKETS, self.packets, ctypes.byref(self.overlapped))
        if not ok:
            error = ctypes.get_last_error()
            if error != ERROR_IO_PENDING:
                return False, error
        self.active = True
        return True, 0

    def cleanup(self):
        if self.iso: winusb.WinUsb_UnregisterIsochBuffer(self.iso)
        if self.event: kernel32.CloseHandle(self.event)

# ============================================================
# Frame Assembler
# ============================================================

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
            if marker == 0x50:  # SOF
                if self.in_frame: self.reset()
                self.in_frame = True
                if len(packet) > 16: self.buf.extend(packet[16:])
                return []
            elif marker == 0x51:  # EOF
                if not self.in_frame: return []
                frame = bytes(self.buf)
                self.reset()
                idx_start = frame.find(b"\xFF\xD8")
                if idx_start == -1: return []
                eoi = frame.rfind(b"\xFF\xD9")
                if eoi < 0: return []
                if (eoi - idx_start) < 1000: return []
                return [frame[idx_start:eoi + 2]]

        if self.in_frame:
            self.buf.extend(packet)
        return []

# ============================================================
# Capture Loop
# ============================================================

def push_latest(q, frame):
    try:
        while True: q.get_nowait()
    except queue.Empty: pass
    q.put_nowait(frame)

def capture_loop(wh, frame_queue, mode, stop_flag):
    print("[USB] Setting Alternate Setting 3...")
    if not winusb.WinUsb_SetCurrentAlternateSetting(wh, ALT_SETTING):
        raise ctypes.WinError(ctypes.get_last_error())
        
    pipe = WINUSB_PIPE_INFORMATION_EX()
    winusb.WinUsb_QueryPipeEx(wh, ALT_SETTING, 0, ctypes.byref(pipe))
    packet_size = max(int(pipe.MaximumPacketSize), int(pipe.MaximumBytesPerInterval))
    print(f"[USB] Packet size: {packet_size}. Using {NUM_PACKETS} packets/transfer, {NUM_TRANSFERS} transfers.")

    transfers = []
    try:
        for _ in range(NUM_TRANSFERS):
            transfers.append(IsoTransfer(wh, packet_size))
        
        handles = (wintypes.HANDLE * NUM_TRANSFERS)(*[t.event for t in transfers])
        assembler = FrameAssembler()
        frames = 0
        start_time = time.monotonic()

        for i, t in enumerate(transfers):
            ok, err = t.submit(continue_stream=(i > 0))
            if not ok:
                print(f"[ERR] Initial submit failed: {err}")
                return

        while not stop_flag.is_set():
            if mode == "still" and (time.monotonic() - start_time) > STILL_TIMEOUT_S and frames == 0:
                print("[WARN] No frame within timeout.")
                return

            wait_res = kernel32.WaitForMultipleObjects(NUM_TRANSFERS, handles, False, TIMEOUT_MS)
            if wait_res == WAIT_TIMEOUT:
                print("[ERR] Timeout waiting for transfers.")
                break
            if wait_res == WAIT_FAILED or wait_res >= NUM_TRANSFERS:
                break

            idx = wait_res
            t = transfers[idx]

            transferred = wintypes.DWORD(0)
            if not winusb.WinUsb_GetOverlappedResult(wh, ctypes.byref(t.overlapped), ctypes.byref(transferred), False):
                err = ctypes.get_last_error()
                if err != 995: # Ignore aborted on shutdown
                    print(f"[ERR] GetOverlappedResult failed: {err}")
                break

            for p in range(NUM_PACKETS):
                length = int(t.packets[p].Length)
                status = int(t.packets[p].Status)
                if status != 0 or length <= 0: continue
                
                offset = int(t.packets[p].Offset)
                packet = t.buffer[offset:offset + length]
                
                for frame in assembler.feed_packet(packet, status):
                    frames += 1
                    push_latest(frame_queue, frame)
                    if mode == "still": return

            ok, err = t.submit(continue_stream=True)
            if not ok:
                ok, err = t.submit(continue_stream=False)
                if not ok:
                    print(f"[ERR] Resubmit failed: {err}")
                    break

    finally:
        for t in transfers:
            t.cleanup()
        try: winusb.WinUsb_SetCurrentAlternateSetting(wh, 0)
        except: pass

# ============================================================
# UI and Virtual Webcam Output
# ============================================================

class App:
    def __init__(self, mode, frame_queue):
        self.q = frame_queue
        self.win = tk.Tk()
        self.win.title(f"Sony EyeToy — {mode.capitalize()}")
        self.win.geometry("640x480")
        self.win.configure(bg="black")

        self.lbl = tk.Label(self.win, text="Waiting for EyeToy frames...", fg="white", bg="black", font=("Arial", 14))
        self.lbl.pack(expand=True, fill="both")

        self.photo = None
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)
        self.win.after(30, self.poll)

        # --- Initialize the virtual webcam ---
        self.vcam = pyvirtualcam.Camera(width=640, height=480, fps=30)
        print(f"[VCAM] Virtual camera started: {self.vcam.device}")

    def poll(self):
        try:
            frame = self.q.get_nowait()
        except queue.Empty:
            frame = None

        if frame is not None:
            try:
                eoi = frame.rfind(b"\xFF\xD9")
                if eoi >= 0:
                    frame = frame[:eoi + 2]

                image = Image.open(io.BytesIO(frame))
                image.load()
                image = image.convert("RGB")

                # Fix BGR -> RGB
                r, g, b = image.split()
                image = Image.merge("RGB", (b, g, r))
                
                # Sharpen and upscale
                image = image.filter(ImageFilter.SHARPEN)
                image = image.resize((640, 480), Image.BICUBIC)

                # --- Send frame to virtual webcam ---
                vcam_frame = np.asarray(image)
                self.vcam.send(vcam_frame)
                self.vcam.sleep_until_next_frame()

                # Update the Tkinter preview window
                self.photo = ImageTk.PhotoImage(image)
                self.lbl.config(image=self.photo, text="")

            except Exception:
                pass

        self.win.after(30, self.poll)

    def on_close(self):
        try:
            self.vcam.close()
        except Exception:
            pass
        self.win.destroy()

# ============================================================
# Main
# ============================================================

def main():
    print(f"=== Sony EyeToy {MODE} ===")
    handle = wh = None
    worker = None
    stop_flag = threading.Event()

    try:
        handle, wh = open_camera()
        init_sensor(wh)
        frame_queue = queue.Queue()

        worker = threading.Thread(
            target=capture_loop,
            args=(wh, frame_queue, MODE, stop_flag),
            daemon=True)
        worker.start()

        App(MODE, frame_queue).win.mainloop()

    finally:
        stop_flag.set()
        if wh:
            try: winusb.WinUsb_AbortPipe(wh, PIPE_ID)
            except: pass
        if worker and worker.is_alive(): worker.join(timeout=2.0)
        if handle or wh: close_camera(handle, wh)
        print("=== Done ===")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n[FATAL] {type(exc).__name__}: {exc}")
        input("Press Enter to close...")
