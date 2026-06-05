"""
camera_control.py — control the station's USB camera via v4l2.

The camera is physically on the Ubuntu station box. In the split deployment the
dash server runs on a DIFFERENT machine, so v4l2-ctl is executed ON THE STATION
over SSH (set CAMERA_HOST). With CAMERA_HOST blank we fall back to running
v4l2-ctl locally (camera attached to this machine). Either way we can list
devices, read/set hardware controls (brightness, focus, zoom, white balance,
exposure, …) and list supported resolutions; changes hit the real device.

Fully fail-safe: if neither a remote host nor a local v4l2-ctl is available, or
no camera is attached, every call returns an empty/`None` result with
`available=False` — nothing raises. Stdlib only (subprocess + shlex).
"""
import re
import shlex
import shutil
import subprocess

from backend.config import CAMERA_HOST, CAMERA_SSH_OPTS

_V4L2 = shutil.which("v4l2-ctl")

# A control line, e.g.:
#   brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=0
_CTRL_RE = re.compile(r"^\s*(\w+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s*:\s*(.*)$")
_KV_RE = re.compile(r"(\w+)=(-?\d+)")
_MENU_ITEM_RE = re.compile(r"^\s+(\d+):\s*(.+)$")
_SIZE_RE = re.compile(r"Size:\s*Discrete\s+(\d+)x(\d+)")


def _remote_host():
    """The ssh target for the station's camera, or None to run locally."""
    return CAMERA_HOST or None


def available():
    # Remote: the camera is on the station — assume v4l2-ctl is installed there
    # (verified at deploy). Local: only if v4l2-ctl is installed on this box.
    return bool(_remote_host()) or _V4L2 is not None


def _run(args, timeout=8):
    """Run `v4l2-ctl <args>` — on the station over SSH when CAMERA_HOST is set,
    else locally. Returns stdout (str) or None on any failure."""
    host = _remote_host()
    if host:
        # Build a single remote command string; args are quoted so device paths
        # and control names can't break out of the v4l2-ctl invocation.
        remote_cmd = "v4l2-ctl " + " ".join(shlex.quote(str(a)) for a in args)
        cmd = ["ssh", *shlex.split(CAMERA_SSH_OPTS), host, remote_cmd]
    else:
        if not _V4L2:
            return None
        cmd = [_V4L2, *[str(a) for a in args]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:                                  # noqa: BLE001 - fail safe
        return None


def list_devices():
    """Return [{"name": ..., "path": "/dev/videoN"}] for capture devices."""
    out = _run(["--list-devices"])
    if not out:
        return []
    devices = []
    current_name = None
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            # "HD Pro Webcam C920 (usb-…):"
            current_name = line.strip().rstrip(":")
            current_name = re.sub(r"\s*\(.*\)$", "", current_name).strip()
        else:
            path = line.strip()
            # Keep only the primary /dev/video* node per device (lowest index).
            if path.startswith("/dev/video"):
                if not any(d["path"] == path for d in devices):
                    devices.append({"name": current_name or path, "path": path})
    # One entry per device name (first/lowest video node).
    seen = set()
    unique = []
    for d in devices:
        if d["name"] in seen:
            continue
        seen.add(d["name"])
        unique.append(d)
    return unique


def list_controls(device):
    """Return a list of control dicts: name, type, min, max, step, default,
    value, and (for menus) options. Empty list if unavailable."""
    out = _run(["-d", device, "--list-ctrls-menus"])
    if out is None:
        out = _run(["-d", device, "--list-ctrls"])
    if not out:
        return []
    controls = []
    for line in out.splitlines():
        m = _CTRL_RE.match(line)
        if m:
            name, ctype, rest = m.group(1), m.group(2), m.group(3)
            kv = {k: int(v) for k, v in _KV_RE.findall(rest)}
            controls.append({
                "name":    name,
                "type":    ctype,
                "min":     kv.get("min"),
                "max":     kv.get("max"),
                "step":    kv.get("step", 1),
                "default": kv.get("default"),
                "value":   kv.get("value"),
                "options": [] if ctype == "menu" else None,
            })
            continue
        mi = _MENU_ITEM_RE.match(line)
        if mi and controls and controls[-1]["type"] == "menu":
            controls[-1]["options"].append(
                {"value": int(mi.group(1)), "label": mi.group(2).strip()})
    return controls


def set_control(device, name, value):
    """Set one control. Returns True on success. Validates name is a known
    control to avoid passing arbitrary strings to the shell tool."""
    if not re.fullmatch(r"\w+", name or ""):
        return False
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    out = _run(["-d", device, "--set-ctrl", f"{name}={int(value)}"])
    return out is not None


def reset_controls(device):
    """Reset every control to its default. Returns the count reset."""
    n = 0
    for c in list_controls(device):
        if c.get("default") is not None and set_control(device, c["name"], c["default"]):
            n += 1
    return n


def list_resolutions(device):
    """Return distinct [{"width": w, "height": h}] the device supports."""
    out = _run(["-d", device, "--list-formats-ext"])
    if not out:
        return []
    seen = set()
    res = []
    for w, h in _SIZE_RE.findall(out):
        key = (int(w), int(h))
        if key in seen:
            continue
        seen.add(key)
        res.append({"width": int(w), "height": int(h)})
    res.sort(key=lambda r: r["width"] * r["height"], reverse=True)
    return res
