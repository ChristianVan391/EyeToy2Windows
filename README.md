# EyeToy Webcam for Windows 11

Turns a PS2 EyeToy (VID 054C / PID 0155) into a standard webcam that works with Discord, Zoom, OBS, etc.

This is a pure Python solution using WinUSB. No custom kernel drivers required.

## Setup

**1. Get the Virtual Camera working**

Windows needs OBS to create a Virtual Camera.

- Install [OBS Studio](https://obsproject.com/).
- Open it once, click **Start Virtual Camera** in the bottom right, then click **Stop Virtual Camera**.
- You can close OBS now.

**2. Unlock virtual cameras in Windows 11**

Windows 11 blocks virtual cameras by default (in most cases).

- Open Command Prompt as Administrator.
- Run this:

```cmd
reg add "HKLM\SOFTWARE\Microsoft\Windows Media Foundation\Platform" /v EnableFrameServerMode /t REG_DWORD /d 1 /f
```

**3. Force the WinUSB driver**

- Plug in the EyeToy.
- Open Device Manager (right-click the Windows Start menu icon). Find the camera (it might be named "EyeToy USB camera Namtai" or "MI_00"). It should be under Other devices if it does not have a driver. If not, check other areas.
- Right-click → Update driver → Browse my computer → Let me pick from a list.
- Uncheck **Show compatible hardware**.
- Select **Universal Serial Bus devices** on the left, and **WinUSB Device** on the right. Install it.
- The camera will now be listed under **Universal Serial Bus devices**.

**4. Add the Device Interface GUID**

We need to add a registry key so Python can find the camera.

In Device Manager, right-click the camera, go to Properties, and open the Details tab.

From the Property dropdown, select **Device instance path**.

You will see a long string that looks exactly like this:

```text
USB\VID_054C&PID_0155&MI_00\6&AC1633A&0&0000
```

You only need the unique ID at the very end. In the example above, the ID is `6&AC1633A&0&0000` (everything after the `MI_00\`).

Open Command Prompt as Administrator. Copy the command below, and replace `YOUR_ID_HERE` with the unique ID you just found:

```cmd
reg add "HKLM\SYSTEM\CurrentControlSet\Enum\USB\VID_054C&PID_0155&MI_00\YOUR_ID_HERE\Device Parameters" /v DeviceInterfaceGUIDs /t REG_MULTI_SZ /d "{6B0F2C7E-3C6B-4D2E-9B1A-8C3E6F0D4A11}" /f
```

(So, using the example above, the finished command would look like:)

```cmd
reg add "HKLM\SYSTEM\CurrentControlSet\Enum\USB\VID_054C&PID_0155&MI_00\6&AC1633A&0&0000\Device Parameters" /v DeviceInterfaceGUIDs /t REG_MULTI_SZ /d "{6B0F2C7E-3C6B-4D2E-9B1A-8C3E6F0D4A11}" /f
```

Finally, go back to Device Manager, right-click the camera, select **Disable device**, then right-click and select **Enable device** to restart it.

## Usage

1. Double-click `EyeToy_Webcam.exe`.
2. A terminal and a preview window will open.
3. Go to Discord/Zoom/Camera app and select **OBS Virtual Camera** as your video source.

You can close the preview window to stop the stream.

## Antivirus Warning

Windows Defender or Chrome might flag the `.exe` as suspicious. This can happen because of PyInstaller packaging Python into a single file.

If it gets blocked, investigate the detection and verify the executable's source before deciding whether to allow it.
