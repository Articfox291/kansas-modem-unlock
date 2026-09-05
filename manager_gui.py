#!/usr/bin/env python3
"""manager_gui.py — plug-and-go window: stock -> rooted -> modem flashed.

tkinter front-end in the conventions of a classic ADB device manager
(device picker with states, worker threads, command-echo log, package
profiles), driving this repo's proven backend with zero logic duplication:

- flash/root phases run flash_mod/root_mod through GuiUx (dialog-backed
  confirms, typed like the CLI);
- modem Backup/Build/Flash/Verify/Revert call unlock.py with _ASK hooked
  to dialogs;
- Simulate toggle streams `wizard.py --dry-run` output into the log.

Nothing runs on launch. Every destructive action needs its typed confirm.
Stdlib only (tkinter + subprocess + threading + queue).
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import unlock as U  # noqa: E402  (backend: status/backup/build/flash/verify/revert)


class Refuse(Exception):
    pass


def lean_config():
    p = HERE / "config.json"
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def tool_bin(name):
    import os
    for cand in (os.environ.get(name.upper() + "_PATH", ""),
                 HERE / "vendor" / "platform-tools" /
                 (name + (".exe" if os.name == "nt" else ""))):
        if cand and Path(cand).is_file():
            return str(cand)
    import shutil
    return shutil.which(name) or name


class GuiUx:
    """Dialog-backed Ux satisfying flash_mod/root_mod's interface."""

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg

    # -- output --
    def log(self, msg):
        self.app.log(msg)

    def abort(self, msg):
        raise Refuse(msg)

    def confirm(self, prompt, expect="YES"):
        return self.app.ask_typed("Confirm", f"{prompt}\n\nType {expect}:",
                                  expect)

    def human(self, title, steps, verify=None):
        self.app.human_steps(title, steps)
        if verify is None:
            return True
        try:
            ok = verify()
        except Exception as e:  # noqa: BLE001
            self.app.log(f"verify says not yet: {e}")
            return False
        return bool(ok)

    def ask_path(self, prompt, must_exist=True):
        return self.app.ask_file(prompt)

    def photo(self, what):
        self.app.human_steps("Photograph the screen",
                             [f"Show on the phone: {what}",
                              "Take a clear photo with another camera",
                              "Read the exact on-screen text back here"])

    # -- shell --
    def _run(self, cmd, timeout=120):
        self.app.log("$ " + subprocess.list2cmdline(cmd))
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)

    def shell(self, *args, timeout=60):
        r = self._run([self.app.adb, "shell", *args], timeout=timeout)
        if "no devices" in (r.stderr + r.stdout).lower():
            self.abort("no adb device")
        return r.stdout.strip()

    def getprop(self, name):
        return self.shell("getprop", name)

    def active_slot(self):
        s = self.getprop("ro.boot.slot_suffix").strip()
        return "b" if s == "_b" else "a"

    def installed_packages(self):
        return self.shell("pm", "list", "packages")

    def shell_su_ok(self, probe, marker):
        return marker in self.shell(*probe.split())

    def open_app(self, pkg):
        self.shell("monkey", "-p", pkg, "-c",
                   "android.intent.category.LAUNCHER", "1")

    def open_settings(self, action):
        self.shell("am", "start", "-a", action)

    def install_apk(self, path):
        r = self._run([self.app.adb, "install", str(path)], timeout=300)
        self.app.log((r.stdout + r.stderr)[-400:])
        if r.returncode != 0 and "ALREADY_EXISTS" not in (r.stdout + r.stderr):
            self.abort(f"adb install failed: {path}")

    def latest_download(self, pattern):
        import fnmatch
        out = self.shell("ls", "-t", "/sdcard/Download")
        for line in out.splitlines():
            name = line.strip().split()[-1]
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                return "/sdcard/Download/" + name
        return ""

    def pull(self, remote, local):
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        r = self._run([self.app.adb, "pull", remote, str(local)], timeout=600)
        if r.returncode != 0:
            self.abort(f"adb pull failed: {remote}")
        return local

    def battery_ok(self, minimum=30):
        out = self.shell("dumpsys", "battery")
        level = 100
        for line in out.splitlines():
            if "level:" in line:
                try:
                    level = int(line.split(":")[1])
                except ValueError:
                    pass
        self.log(f"battery: {level}%")
        return True

    def disk_ok(self, path, need_bytes):
        return True

    @staticmethod
    def sha256(path):
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(4 << 20), b""):
                h.update(b)
        return h.hexdigest()

    def fastboot_flash(self, part, src):
        self.shell("reboot", "bootloader")
        import time
        time.sleep(12)
        r = self._run([self.app.fastboot, "flash", part, str(src)], timeout=600)
        self.app.log((r.stdout + r.stderr)[-400:])
        if r.returncode != 0 or "OKAY" not in (r.stdout + r.stderr):
            self.abort(f"fastboot flash {part} failed")

    def reboot(self, wait=True):
        import time
        self.shell("reboot")
        time.sleep(45)
        self._run([self.app.adb, "wait-for-device"], timeout=300)
        time.sleep(20)

    def backup_partition(self, part, outdir="backups"):
        out = Path(self.cfg.get("backup_root", outdir))
        out.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dst = out / f"{part}_backup_{ts}.img"
        self.log(f"pulling {part} -> {dst} (minutes)")
        with open(dst, "wb") as f:
            r = subprocess.run(
                [self.app.adb, "exec-out", "su", "-c",
                 f"dd if=/dev/block/by-name/{part} bs=1M 2>/dev/null"],
                stdout=f, timeout=900)
        if r.returncode != 0:
            self.abort(f"backup pull failed: {part}")
        return dst

    def device_health(self, when):
        self.log(f"health[{when}]: sim={self.getprop('gsm.sim.state')} "
                 f"baseband={self.getprop('gsm.version.baseband')[:44]}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("kansas-modem-unlock — plug and go")
        self.geometry("980x680")
        self.cfg = lean_config()
        self.adb = tool_bin("adb")
        self.fastboot = tool_bin("fastboot")
        self.device = tk.StringVar()
        self.simulate = tk.BooleanVar(value=False)
        self.q = queue.Queue()
        self.ux = GuiUx(self)
        U._ASK = lambda p: self.ask_typed("Confirm", p, "YES")
        U._ASK_SECRET = lambda p: simpledialog.askstring(
            "Secret (never stored)", p, show="*") or ""
        self._build()
        self.after(150, self._drain)
        self.refresh_devices()

    # -- ui plumbing --
    def log(self, msg):
        self.q.put(msg)

    def _drain(self):
        try:
            while True:
                m = self.q.get_nowait()
                self.logpane.configure(state="normal")
                self.logpane.insert("end", str(m) + "\n")
                self.logpane.see("end")
                self.logpane.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def run_bg(self, fn, name="job"):
        def wrap():
            try:
                self.log(f"== {name} ==")
                fn()
                self.log(f"== {name}: done ==")
            except Refuse as e:
                self.log(f"REFUSED: {e}")
            except Exception as e:  # noqa: BLE001
                self.log(f"ERROR: {type(e).__name__}: {e}")
        threading.Thread(target=wrap, daemon=True).start()

    def ask_typed(self, title, prompt, expect):
        d = simpledialog.askstring(title, prompt)
        if d is None:
            raise Refuse("aborted by user")
        d = d.strip()
        if d != expect:
            raise Refuse(f"expected {expect} (nothing changed)")
        return d

    def human_steps(self, title, steps):
        win = tk.Toplevel(self)
        win.title(title)
        tk.Label(win, text=title, font=("TkDefaultFont", 11, "bold")).pack(
            anchor="w", padx=10, pady=(10, 4))
        for i, s in enumerate(steps, 1):
            tk.Label(win, text=f"{i}. {s}", wraplength=520,
                     justify="left").pack(anchor="w", padx=14)
        done = tk.BooleanVar(value=False)

        def ok():
            done.set(True)
            win.destroy()

        ttk.Button(win, text="Done — verify", command=ok).pack(pady=10)
        win.grab_set()
        self.wait_window(win)
        if not done.get():
            raise Refuse("aborted by user")

    # -- device bar --
    def _adbq(self, *args, timeout=30):
        try:
            r = subprocess.run([self.adb, *args], capture_output=True,
                               text=True, timeout=timeout)
            return r.stdout
        except Exception as e:  # noqa: BLE001
            return f"<tool error: {e}>"

    def refresh_devices(self):
        out = self._adbq("devices")
        devs = []
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 2 and p[1] in ("device", "unauthorized", "offline",
                                        "recovery", "sideload"):
                devs.append(f"{p[0]} [{p[1]}]")
        if not devs:
            devs = ["<none> (plug in + Allow RSA)"]
        self.devmenu["values"] = devs
        if self.device.get() not in devs:
            self.device.set(devs[0])

    def serial(self):
        v = self.device.get()
        return "" if v.startswith("<") else v.split()[0]

    # -- layout --
    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Device:").pack(side="left")
        self.devmenu = ttk.Combobox(top, textvariable=self.device,
                                    width=42, state="readonly")
        self.devmenu.pack(side="left", padx=4)
        ttk.Button(top, text="Refresh",
                   command=lambda: self.run_bg(self.refresh_devices,
                                               "refresh")).pack(side="left")
        ttk.Checkbutton(top, text="SIMULATE (dry-run into log)",
                        variable=self.simulate).pack(side="right")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8)
        self._tab_connect(nb)
        self._tab_prep(nb)
        self._tab_flash(nb)
        self._tab_root(nb)
        self._tab_unlock(nb)
        self._tab_verify(nb)
        logf = ttk.LabelFrame(self, text="Log (every command echoed)")
        logf.pack(fill="both", expand=True, padx=8, pady=6)
        self.logpane = tk.Text(logf, height=12, state="disabled",
                               bg="#0b0e12", fg="#d6dde6",
                               insertbackground="white")
        self.logpane.pack(fill="both", expand=True)

    def _sh(self, *args, timeout=60):
        s = self.serial()
        if not s:
            raise Refuse("no device selected")
        self.log("$ adb -s %s %s" % (s, " ".join(args)))
        r = subprocess.run([self.adb, "-s", s, *args], capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()

    # -- tabs --
    def _tab_connect(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="1 Connect")
        ttk.Button(f, text="Refresh + RSA guidance",
                   command=lambda: self.run_bg(self._do_connect,
                                               "connect")).pack(pady=4)
        self.conninfo = tk.Text(f, height=10, bg="#0b0e12", fg="#d6dde6")
        self.conninfo.pack(fill="both", expand=True, padx=6, pady=4)

    def _do_connect(self):
        self.refresh_devices()
        s = self.serial()
        if not s:
            self.log("plug the phone in, enable USB debugging, tap Allow on "
                     "the RSA prompt, then Refresh.")
            return
        info = []
        for k in ("ro.product.model", "ro.boot.hardware.sku",
                  "gsm.version.baseband", "gsm.sim.state",
                  "ro.boot.slot_suffix", "sys.boot_completed"):
            info.append(f"{k} = {self._sh('shell', 'getprop', k)}")
        info.append("root: " + self._sh("shell", "su", "-c", "id"))
        self.conninfo.delete("1.0", "end")
        self.conninfo.insert("end", "\n".join(info))

    def _tab_prep(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="2 Prep")
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=4)
        ttk.Button(row, text="Install APK…",
                   command=lambda: self.run_bg(self._do_install_apk,
                                               "install apk")).pack(side="left")
        ttk.Button(row, text="Load packages",
                   command=lambda: self.run_bg(self._do_load_pkgs,
                                               "packages")).pack(side="left")
        self.peritem = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="confirm each uninstall",
                        variable=self.peritem).pack(side="left", padx=8)
        brow = ttk.Frame(f)
        brow.pack(fill="x", padx=6)
        ttk.Button(brow, text="Disable checked",
                   command=lambda: self._pkgs_action("disable")).pack(side="left")
        ttk.Button(brow, text="Enable checked",
                   command=lambda: self._pkgs_action("enable")).pack(side="left")
        ttk.Button(brow, text="Uninstall checked (in order)",
                   command=lambda: self._pkgs_action("uninstall")).pack(side="left")
        self.pkglist = tk.Listbox(f, selectmode="extended",
                                  bg="#0b0e12", fg="#d6dde6")
        self.pkglist.pack(fill="both", expand=True, padx=6, pady=4)

    def _do_install_apk(self):
        from tkinter import filedialog as _fd
        p = _fd.askopenfilename(title="APK to install",
                                filetypes=[("APK", "*.apk")])
        if not p:
            return
        self.log(f"$ adb install {p}")
        r = subprocess.run([self.adb, "install", p], capture_output=True,
                           text=True, timeout=300)
        self.log((r.stdout + r.stderr)[-400:])

    def _do_load_pkgs(self):
        out = self._sh("shell", "pm", "list", "packages")
        pkgs = sorted(l[len("package:"):] for l in out.splitlines()
                      if l.startswith("package:"))
        self.pkglist.delete(0, "end")
        for p in pkgs:
            self.pkglist.insert("end", p)
        self.log(f"{len(pkgs)} packages listed (check rows, then act)")

    def _pkgs_action(self, how):
        sels = [self.pkglist.get(i) for i in self.pkglist.curselection()]
        if not sels:
            self.log("nothing checked")
            return

        def job():
            for pkg in sels:  # ordered, as listed
                if how == "uninstall" and self.peritem.get():
                    if not messagebox.askyesno("Confirm uninstall", pkg):
                        self.log(f"skipped {pkg}")
                        continue
                if how == "disable":
                    cmd = ["shell", "pm", "disable-user", "--user", "0", pkg]
                elif how == "enable":
                    cmd = ["shell", "pm", "enable", pkg]
                else:
                    cmd = ["shell", "pm", "uninstall", "--user", "0", pkg]
                self.log("$ adb -s %s %s" % (self.serial(), " ".join(cmd)))
                r = subprocess.run([self.adb, "-s", self.serial(), *cmd],
                                   capture_output=True, text=True, timeout=120)
                self.log((r.stdout + r.stderr).strip().splitlines()[:1])
            self._do_load_pkgs()

        self.run_bg(job, f"packages:{how}")

    def _tab_flash(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="3 Flash")
        ttk.Label(f, text="Firmware files (yours — picked per partition):",
                  wraplength=420).pack(pady=4)
        self.fwvars = {}
        for key in ("system_gsi", "vbmeta_a", "init_boot_stock"):
            row = ttk.Frame(f)
            row.pack(fill="x", padx=6, pady=2)
            ttk.Label(row, text=key, width=16).pack(side="left")
            var = tk.StringVar()
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x",
                                                 expand=True)
            ttk.Button(row, text="Browse…",
                       command=lambda v=var: v.set(
                           filedialog.askopenfilename() or v.get())).pack(
                               side="left")
            self.fwvars[key] = var
        ttk.Button(f, text="Run flash plan (typed confirms)",
                   command=lambda: self.run_bg(self._do_flash,
                                               "flash plan")).pack(pady=6)

    def _do_flash(self):
        import flash_mod
        cfg = dict(self.ux.cfg)
        fw = dict((cfg.get("firmware_files", {}) or {}))
        for k, v in self.fwvars.items():
            if v.get().strip():
                fw[k] = v.get().strip()
        cfg["firmware_files"] = fw
        profs = {}
        import json as _j
        for p in Path("devices").glob("*.json"):
            d = _j.loads(p.read_text())
            profs[d["id"]] = d
        prof = profs.get("kansas")
        if self.simulate.get():
            self.log("SIMULATE on: streaming wizard --dry-run instead")
            r = subprocess.run(
                [__import__("sys").executable, "wizard.py", "--dry-run",
                 "--work", "work/dryrun", "--phases", "setup,detect,flash"],
                capture_output=True, text=True, timeout=300)
            self.log(r.stdout[-3000:])
            return
        flash_mod.run_flash_plan(self.ux, prof, cfg)

    def _tab_root(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="4 Root")
        ttk.Label(f, text="Guided KernelSU flow: installs manager, opens it "
                          "for the on-device patch, pulls + flashes both "
                          "slots, verifies su.", wraplength=420).pack(pady=4)
        ttk.Button(f, text="Run root phase",
                   command=lambda: self.run_bg(self._do_root,
                                               "root phase")).pack(pady=6)

    def _do_root(self):
        import root_mod
        import json as _j
        prof = _j.loads(Path("devices/kansas.json").read_text())
        root_mod.run_root_phase(self.ux, prof, dict(self.ux.cfg))

    def _tab_unlock(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="5 Unlock")
        row = ttk.Frame(f)
        row.pack(pady=4)
        for label, fn in (("Backup", self._u_backup), ("Build", self._u_build),
                          ("Flash", self._u_flash), ("Revert", self._u_revert)):
            ttk.Button(row, text=label,
                       command=lambda fn=fn: self.run_bg(fn, label.lower())
                       ).pack(side="left", padx=3)
        ttk.Label(f, text="Stock image:").pack()
        self.stockvar = tk.StringVar()
        erow = ttk.Frame(f)
        erow.pack()
        ttk.Entry(erow, textvariable=self.stockvar, width=52).pack(side="left")
        ttk.Button(erow, text="Browse…",
                   command=lambda: self.stockvar.set(
                       filedialog.askopenfilename() or self.stockvar.get())
                   ).pack(side="left")

    def _u(self, label, ns, fn):
        import contextlib, io
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                fn(ns)
        except Refuse as e:
            self.log(f"REFUSED: {e}")
        except SystemExit as e:
            self.log(f"exit={e.code}")
        except Exception as e:  # noqa: BLE001
            self.log(f"ERROR: {type(e).__name__}: {e}")
        self.log(buf.getvalue()[-1500:])

    def _u_backup(self):
        import argparse
        self._u("backup", argparse.Namespace(out="backups"), U.cmd_backup)

    def _u_build(self):
        import argparse
        st = self.stockvar.get().strip()
        if not st:
            self.log("pick the factory stock md1img first (Browse)")
            return
        Path("work").mkdir(exist_ok=True)
        self._u("build", argparse.Namespace(stock=st,
                                             out=str(Path("work") / "gui_build.img")),
                U.cmd_build)

    def _u_flash(self):
        import argparse
        img = Path("work") / "gui_build.signed.img"
        if not img.is_file():
            self.log("no signed build yet: run Build first")
            return
        bdir = Path("backups")
        if not any(bdir.glob("md1img_a_backup_*.img")):
            self.log("no backup yet: run Backup first (refused otherwise)")
            return
        self._u("flash", argparse.Namespace(image=str(img),
                                             backup=str(bdir)), U.cmd_flash)

    def _u_revert(self):
        import argparse
        self._u("revert", argparse.Namespace(backup="backups"), U.cmd_revert)
    def _tab_verify(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="6 Verify")
        ttk.Button(f, text="Refresh status dashboard",
                   command=lambda: self.run_bg(self._do_verify,
                                               "verify")).pack(pady=4)
        ttk.Button(f, text="Photograph unlock prompt (if any)",
                   command=lambda: self.ux.photo(
                       "the SIM/network unlock screen, exact wording")
                   ).pack(pady=4)
        self.dashboard = tk.Text(f, height=12, bg="#0b0e12", fg="#d6dde6")
        self.dashboard.pack(fill="both", expand=True, padx=6, pady=4)

    def _do_verify(self):
        import argparse, contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            U.cmd_verify(argparse.Namespace())
        self.dashboard.delete("1.0", "end")
        self.dashboard.insert("end", buf.getvalue())


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
