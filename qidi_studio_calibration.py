"""QIDI Studio temperature tower, retraction test and flow rate test generator (GUI, or command line with --help)."""
import argparse
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qidi_profiles as qp  # noqa: E402
import printer_upload as pu  # noqa: E402
import flow_test as ft  # noqa: E402
import retraction_test as rt  # noqa: E402
import temp_tower as tt  # noqa: E402
from studio import cli_command, open_in_studio, studio_command  # noqa: E402
from tower_geometry import HERE as DATA_DIR  # noqa: E402

__version__ = "1.2.0"
APP_NAME = "QIDI Studio Calibration"
NO_PRINTER = "(no network printer set up in QIDI Studio)"

def filament_defaults(store, filament):
    """(start, end, step) suggested from the filament's recommended range, hottest at the bottom."""
    f = store.resolve("filament", filament)
    lo = int(qp.as_float(f.get("nozzle_temperature_range_low"), 0))
    hi = int(qp.as_float(f.get("nozzle_temperature_range_high"), 0))
    nominal = int(qp.as_float(f.get("nozzle_temperature"), 210))
    if not lo or not hi or hi <= lo:
        lo, hi = nominal - 20, nominal + 20
    step = 5
    lo -= (lo % step)
    hi -= (hi % step)
    return hi, lo, step


def safe_filename(s):
    return re.sub(r'[<>:"/\\|?*]+', "_", s).strip() or "temp_tower"


def default_output_dir(store):
    app = store.app_config.get("app", {}) if store and isinstance(store.app_config, dict) else {}
    for d in (app.get("last_export_path"), app.get("download_path"),
              os.path.join(os.path.expanduser("~"), "Downloads")):
        if d and os.path.isdir(d):
            return d
    return os.path.expanduser("~")


def default_output(store, filament, temps):
    short = filament.split("@")[0].strip()
    return os.path.join(default_output_dir(store),
                        safe_filename("Temp Tower %s %d-%d" % (short, temps[0], temps[-1])) + ".3mf")


def default_retraction_output(store, filament, values):
    short = filament.split("@")[0].strip()
    return os.path.join(default_output_dir(store), safe_filename(
        "Retraction Test %s %s-%smm" % (short, rt.fmt(values[0]), rt.fmt(values[-1]))) + ".gcode")


def default_flow_output(store, filament, offs):
    short = filament.split("@")[0].strip()
    return os.path.join(default_output_dir(store), safe_filename(
        "Flow Test %s %+g to %+g%%" % (short, offs[0], offs[-1])) + ".gcode")


def fmt_flow(v):
    return ("%.3f" % v).rstrip("0").rstrip(".")


def pick_network_printer(store, name=None):
    """A network printer saved in QIDI Studio, by name (default: the first one)."""
    printers = store.network_printers()
    if not printers:
        raise ValueError("No network printer is set up in QIDI Studio. Add one there (the Wi-Fi icon next to "
                         "the printer), or copy the .gcode to the printer yourself.")
    if not name:
        return printers[0]
    for p in printers:
        if p.get("name") == name:
            return p
    raise ValueError("No network printer called %r. QIDI Studio has: %s" % (
        name, ", ".join(p.get("name", "?") for p in printers)))


# ---------------------------------------------------------------------- CLI
def run_cli(argv):
    for stream in (sys.stdout, sys.stderr):
        try:  # a preset name the console's code page can't show shouldn't crash the listing
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="qidi_studio_calibration", description=__doc__)
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--test", choices=["temperature", "retraction", "flow"], default="temperature",
                    help="which test to make (default: temperature)")
    ap.add_argument("--qidi-dir", help="QIDI Studio data folder (default: %s)" % qp.default_root().replace("%", "%%"))
    ap.add_argument("--printer", help="printer preset (default: the one selected in QIDI Studio)")
    ap.add_argument("--process", help="process preset (default: the one selected in QIDI Studio)")
    ap.add_argument("--filament", help="filament preset (default: the one selected in QIDI Studio)")
    ap.add_argument("--start", type=float,
                    help="first value: °C, mm of retraction, or %% flow change (default: from the presets / -20)")
    ap.add_argument("--end", type=float, help="last value (default: from the presets / 20)")
    ap.add_argument("--step", type=float, help="step between values (default: 5 °C, 0.2/0.5 mm, or 5 %%)")
    ap.add_argument("--wait", action="store_true", help="temperature: use M109 (wait) instead of M104")
    ap.add_argument("--no-settings", action="store_true",
                    help="temperature: don't embed presets; QIDI Studio keeps whatever is selected")
    ap.add_argument("--band-height", type=float, default=5.0, help="retraction: height of each band in mm")
    ap.add_argument("--flow", type=float, help="flow: the current flow ratio (default: the filament preset's)")
    ap.add_argument("--temp", type=int, help="retraction/flow: nozzle temperature (default: the filament preset's)")
    ap.add_argument("--send", nargs="?", const="", metavar="PRINTER",
                    help="retraction/flow: upload the G-code to a network printer saved in QIDI Studio "
                         "(default: the first)")
    ap.add_argument("--print-now", action="store_true",
                    help="retraction/flow: with --send, start printing once uploaded")
    ap.add_argument("--list", choices=["printer", "process", "filament"], help="list presets and exit")
    ap.add_argument("--open", action="store_true", help="open the result in QIDI Studio")
    ap.add_argument("-o", "--output", help="output file")
    ap.add_argument("--cli", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    store = qp.PresetStore(a.qidi_dir)
    sel_m, sel_p, sel_f = store.selected()
    machine = a.printer or sel_m
    if a.list:
        kind = {"printer": "machine"}.get(a.list, a.list)
        for n in store.names(kind):
            if kind == "machine" or not machine or store.compatible(kind, n, machine):
                print(n)
        return 0
    process = a.process or sel_p
    filament = a.filament or (sel_f[0] if sel_f else None)
    for kind, name in (("machine", machine), ("process", process), ("filament", filament)):
        if not name or not store.get(kind, name):
            sys.exit(f"{kind} preset not found: {name!r} (use --list)")

    def pick(value, default):
        return default if value is None else value

    if a.test in ("retraction", "flow"):
        target = pick_network_printer(store, a.send) if a.send is not None else None
        if a.test == "retraction":
            d_start, d_end, d_step = rt.defaults(store, machine)
            values = rt.lengths(pick(a.start, d_start), pick(a.end, d_end), pick(a.step, d_step))
            out = a.output or default_retraction_output(store, filament, values)
            r = rt.generate(out, values, store=store, machine=machine, process=process, filament=filament,
                            band_h=a.band_height, nozzle_temp=a.temp, progress=print)
            print("Wrote %s\n  %d bands of %g mm (%g mm tall), retraction from the bottom: %s mm" % (
                r["path"], r["bands"], a.band_height, r["height"], ", ".join(rt.fmt(x) for x in values)))
        else:
            offs = ft.offsets(pick(a.start, -20), pick(a.end, 20), pick(a.step, 5))
            out = a.output or default_flow_output(store, filament, offs)
            r = ft.generate(out, offs, store=store, machine=machine, process=process, filament=filament,
                            base_flow=a.flow, nozzle_temp=a.temp, progress=print)
            print("Wrote %s\n  %d patches, flow ratios from the back left: %s" % (
                r["path"], r["patches"], ", ".join(r["labels"])))
        if target:
            print("Sending to %s…" % pu.describe(target))
            pu.upload(target, out, start=a.print_now)
            print("Uploaded%s." % (" and started printing" if a.print_now else ""))
    else:
        d_start, d_end, d_step = filament_defaults(store, filament)
        temps = tt.temperatures(d_start if a.start is None else a.start, d_end if a.end is None else a.end,
                                d_step if a.step is None else a.step)
        out = a.output or default_output(store, filament, temps)
        r = tt.write_3mf(out, temps, store=store, machine=machine, process=process, filament=filament,
                         wait=a.wait, embed_settings=not a.no_settings)
        print("Wrote %s\n  %d floors, %g mm tall, %s at z = %s" % (
            r["path"], r["floors"], r["height"], r["command"],
            ", ".join("%g (%d°C)" % c for c in r["changes"])))
    if a.open:
        open_in_studio(out)
    return 0


# ---------------------------------------------------------------------- GUI
def run_gui():
    import queue
    import threading
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    try:  # crisp text on high-DPI Windows displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.title("%s %s" % (APP_NAME, __version__))
    try:
        root.iconphoto(True, tk.PhotoImage(file=os.path.join(DATA_DIR, "assets", "icon.png")))
    except tk.TclError:
        pass
    root.minsize(560, 0)

    state = {"store": None, "busy": False}
    v = {k: tk.StringVar() for k in ("dir", "printer", "process", "filament", "info",
                                     "start", "end", "step", "summary", "output",
                                     "r_start", "r_end", "r_step", "r_band", "r_temp", "r_summary", "r_output",
                                     "r_send", "f_start", "f_end", "f_step", "f_base", "f_temp", "f_summary",
                                     "f_output", "c_offset", "c_result")}
    v_wait = tk.BooleanVar(value=False)
    v_embed = tk.BooleanVar(value=True)
    v_all = tk.BooleanVar(value=False)
    v_print_now = tk.BooleanVar(value=False)
    v["dir"].set(qp.default_root())
    v["step"].set("5")
    v["r_band"].set("5")
    v["f_start"].set("-20")
    v["f_end"].set("20")
    v["f_step"].set("5")
    field_names = {"start": "the bottom temperature", "end": "the top temperature", "step": "the temperature step",
                   "r_start": "the bottom retraction", "r_end": "the top retraction", "r_step": "the retraction step",
                   "r_band": "the band height", "r_temp": "the nozzle temperature",
                   "f_start": "the first flow change", "f_end": "the last flow change", "f_step": "the flow step",
                   "f_base": "the current flow ratio", "f_temp": "the nozzle temperature"}

    def number(key, optional=False):
        """The number typed in a field, or a plain error naming the field."""
        text = v[key].get().strip()
        if not text and optional:
            return None
        try:
            return float(text)
        except ValueError:
            raise ValueError("Enter a number for %s." % field_names[key]) from None
    status = tk.StringVar()
    can_slice = cli_command() is not None

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    row = 0

    def label(parent, text, r):
        ttk.Label(parent, text=text).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)

    label(frm, "QIDI Studio folder", row)
    ttk.Entry(frm, textvariable=v["dir"]).grid(row=row, column=1, sticky="ew", pady=3)
    ttk.Button(frm, text="Browse…", command=lambda: browse_dir()).grid(row=row, column=2, padx=(6, 0))
    row += 1

    combos = {}
    for key, text in (("printer", "Printer"), ("process", "Process"), ("filament", "Filament")):
        label(frm, text, row)
        cb = ttk.Combobox(frm, textvariable=v[key], state="readonly", height=25)
        cb.grid(row=row, column=1, columnspan=2, sticky="ew", pady=3)
        combos[key] = cb
        row += 1
    ttk.Checkbutton(frm, text="Show presets for other printers too", variable=v_all,
                    command=lambda: refresh_lists()).grid(row=row, column=1, columnspan=2, sticky="w")
    row += 1
    ttk.Label(frm, textvariable=v["info"], foreground="#555").grid(row=row, column=1, columnspan=2,
                                                                    sticky="w", pady=(2, 8))
    row += 1

    tabs = ttk.Notebook(frm)
    tabs.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
    row += 1

    def spin_row(parent, r, title, fields, unit, extra=None):
        label(parent, title, r)
        box = ttk.Frame(parent)
        box.grid(row=r, column=1, columnspan=2, sticky="w")
        col = 0
        for key, text, lo, hi, inc in fields:
            if text:
                ttk.Label(box, text=text).grid(row=0, column=col, padx=(0 if col == 0 else 12, 4))
            sp = ttk.Spinbox(box, textvariable=v[key], from_=lo, to=hi, increment=inc, width=6,
                             command=lambda: update_summaries())
            sp.grid(row=0, column=col + 1)
            sp.bind("<KeyRelease>", lambda e: update_summaries())
            col += 2
        ttk.Label(box, text=unit).grid(row=0, column=col, padx=(4, 0))
        if extra and isinstance(extra[0], str):
            extra = [extra]
        for i, (text, command) in enumerate(extra or []):
            ttk.Button(box, text=text, command=command).grid(row=0, column=col + 1 + i, padx=(12 if i == 0 else 4, 0))

    def save_row(parent, r, key, kind):
        label(parent, "Save as", r)
        ttk.Entry(parent, textvariable=v[key]).grid(row=r, column=1, sticky="ew", pady=(10, 3))
        ttk.Button(parent, text="Browse…", command=lambda: browse_out(key, kind)).grid(
            row=r, column=2, padx=(6, 0), pady=(10, 3))

    # Temperature tower tab
    t_tab = ttk.Frame(tabs, padding=10)
    t_tab.columnconfigure(1, weight=1)
    tabs.add(t_tab, text="Temperature tower")
    spin_row(t_tab, 0, "Temperatures", [("start", "Bottom", 100, 500, 5), ("end", "Top", 100, 500, 5),
                                        ("step", "Step", 1, 50, 1)], "°C",
             ("Use filament range", lambda: apply_filament_range()))
    ttk.Label(t_tab, textvariable=v["summary"], foreground="#555").grid(row=1, column=1, columnspan=2,
                                                                         sticky="w", pady=(2, 8))
    label(t_tab, "Options", 2)
    ttk.Checkbutton(t_tab, text="Wait for each temperature (M109) instead of changing on the fly (M104)",
                    variable=v_wait).grid(row=2, column=1, columnspan=2, sticky="w")
    ttk.Checkbutton(t_tab, text="Embed the printer / process / filament presets in the 3MF",
                    variable=v_embed).grid(row=3, column=1, columnspan=2, sticky="w")
    save_row(t_tab, 4, "output", "3mf")

    # Retraction test tab
    r_tab = ttk.Frame(tabs, padding=10)
    r_tab.columnconfigure(1, weight=1)
    tabs.add(r_tab, text="Retraction test")
    spin_row(r_tab, 0, "Retraction", [("r_start", "Bottom", 0, 15, 0.1), ("r_end", "Top", 0, 15, 0.1),
                                      ("r_step", "Step", 0.05, 5, 0.05)], "mm",
             ("Use defaults", lambda: apply_retraction_defaults()))
    ttk.Label(r_tab, textvariable=v["r_summary"], foreground="#555").grid(row=1, column=1, columnspan=2,
                                                                           sticky="w", pady=(2, 8))
    spin_row(r_tab, 2, "Band height", [("r_band", "", 2, 20, 1)], "mm")
    spin_row(r_tab, 3, "Nozzle", [("r_temp", "", 100, 500, 5)],
             "°C   (use the best floor from your temperature tower)")
    send_combos = []

    def send_row(parent, r):
        # Both sliced tests share the chosen network printer and the "start printing" choice.
        label(parent, "Send to", r)
        box = ttk.Frame(parent)
        box.grid(row=r, column=1, columnspan=2, sticky="w")
        combo = ttk.Combobox(box, textvariable=v["r_send"], state="readonly", width=30)
        combo.grid(row=0, column=0)
        send_combos.append(combo)
        ttk.Checkbutton(box, text="Start printing right away", variable=v_print_now).grid(
            row=0, column=1, padx=(12, 0))

    def sliced_note(parent, r, why):
        ttk.Label(parent, foreground="#555", wraplength=560, justify="left", text=(
            why + " so this test is sliced for you with QIDI Studio and saved as ready-to-print G-code. "
            "QIDI Studio can preview it but can't send it, so use \"Generate & send to printer\" or copy it "
            "to the printer yourself." if can_slice else
            "QIDI Studio wasn't found. It's needed to slice this test.")).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))

    send_row(r_tab, 4)
    save_row(r_tab, 5, "r_output", "gcode")
    sliced_note(r_tab, 6, "QIDI Studio can't change retraction by height,")

    # Flow rate tab
    f_tab = ttk.Frame(tabs, padding=10)
    f_tab.columnconfigure(1, weight=1)
    tabs.insert(1, f_tab, text="Flow rate")  # suggested order: temperature, flow, retraction
    spin_row(f_tab, 0, "Flow change", [("f_start", "From", -50, 50, 1), ("f_end", "To", -50, 50, 1),
                                       ("f_step", "Step", 0.5, 20, 0.5)], "%",
             [("Coarse", lambda: set_flow_range(-20, 20, 5)), ("Fine", lambda: set_flow_range(-5, 5, 1))])
    ttk.Label(f_tab, textvariable=v["f_summary"], foreground="#555", wraplength=560, justify="left").grid(
        row=1, column=1, columnspan=2, sticky="w", pady=(2, 8))
    spin_row(f_tab, 2, "Current flow", [("f_base", "", 0.5, 1.5, 0.01)],
             "  (the filament's flow ratio; patches are labelled with the result)")
    spin_row(f_tab, 3, "Nozzle", [("f_temp", "", 100, 500, 5)], "°C")
    send_row(f_tab, 4)
    save_row(f_tab, 5, "f_output", "gcode")
    sliced_note(f_tab, 6, "QIDI Studio prints concentric top surfaces from the inside out,")

    # Flow calculator: current flow × (1 + best patch's change).
    ttk.Separator(f_tab).grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 6))
    label(f_tab, "Calculator", 8)
    calc = ttk.Frame(f_tab)
    calc.grid(row=8, column=1, columnspan=2, sticky="w")
    ttk.Label(calc, text="Best patch").grid(row=0, column=0, padx=(0, 4))
    calc_spin = ttk.Spinbox(calc, textvariable=v["c_offset"], from_=-50, to=50, increment=1, width=6)
    calc_spin.grid(row=0, column=1)
    # Recalculate whenever either number changes, however it was changed.
    for key in ("c_offset", "f_base"):
        v[key].trace_add("write", lambda *_: update_calculator())
    ttk.Label(calc, text="%  →  new flow ratio").grid(row=0, column=2, padx=(4, 4))
    ttk.Label(calc, textvariable=v["c_result"], font=("TkDefaultFont", 10, "bold")).grid(row=0, column=3)
    ttk.Button(calc, text="Use it for a fine pass", command=lambda: use_calculated_flow()).grid(
        row=0, column=4, padx=(12, 0))
    ttk.Label(f_tab, foreground="#555", wraplength=560, justify="left", text=(
        "Enter the flow change of your best patch (this also works with QIDI Studio's own flow test). "
        "It's worked out from the current flow above.")).grid(row=9, column=1, columnspan=2, sticky="w")
    v["c_offset"].set("0")

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=3, sticky="e", pady=(10, 0))
    gen_btn = ttk.Button(btns, text="Generate", command=lambda: generate(None))
    gen_btn.grid(row=0, column=0, padx=4)
    open_btn = ttk.Button(btns, text="Generate & open in QIDI Studio", command=lambda: generate("open"))
    open_btn.grid(row=0, column=1, padx=4)
    send_btn = ttk.Button(btns, text="Generate & send to printer", command=lambda: generate("send"))
    send_btn.grid(row=0, column=2, padx=4)
    row += 1
    ttk.Label(frm, textvariable=status, wraplength=620, justify="left").grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))

    auto = {"output": "", "r_output": "", "f_output": ""}

    def store():
        return state["store"]

    def current_tab():
        return ("temperature", "flow", "retraction")[tabs.index(tabs.select())]

    def sliced_tab():
        return current_tab() != "temperature"

    def network_printers():
        st = store()
        return st.network_printers() if st else []

    def update_buttons(_e=None):
        ok = not state["busy"] and (can_slice or not sliced_tab())
        gen_btn.state(["!disabled"] if ok else ["disabled"])
        open_btn.state(["!disabled"] if ok and studio_command() else ["disabled"])
        if sliced_tab():
            send_btn.grid()
            has_printer = v["r_send"].get() not in ("", NO_PRINTER)
            send_btn.state(["!disabled"] if ok and has_printer else ["disabled"])
        else:
            send_btn.grid_remove()

    def load_network_printers():
        names = [p.get("name", "?") for p in network_printers()]
        if v["r_send"].get() not in names:
            v["r_send"].set(names[0] if names else NO_PRINTER)
        for combo in send_combos:
            combo["values"] = names
            combo.state(["!disabled", "readonly"] if names else ["disabled"])

    def set_flow_range(start, end, step):
        v["f_start"].set("%g" % start)
        v["f_end"].set("%g" % end)
        v["f_step"].set("%g" % step)
        update_summaries()

    def calculated_flow():
        try:
            return ft.new_flow(float(v["f_base"].get()), float(v["c_offset"].get()))
        except ValueError:
            return None

    def update_calculator():
        new = calculated_flow()
        v["c_result"].set(fmt_flow(new) if new else "—")

    def use_calculated_flow():
        new = calculated_flow()
        if not new:
            messagebox.showerror(APP_NAME, "Enter a number for the current flow and the best patch's change.")
            return
        v["f_base"].set(fmt_flow(new))
        v["c_offset"].set("0")
        set_flow_range(-5, 5, 1)
        update_calculator()
        status.set("Current flow set to %s with a fine range (±5 %%). Generate again to narrow it down."
                   % fmt_flow(new))

    def load_store():
        try:
            state["store"] = qp.PresetStore(v["dir"].get().strip() or None)
        except Exception as e:  # noqa: BLE001
            state["store"] = None
            status.set("Could not read QIDI Studio presets: %s" % e)
            for cb in combos.values():
                cb["values"] = []
            return
        st = state["store"]
        sel_m, sel_p, sel_f = st.selected()
        machines = st.names("machine")
        combos["printer"]["values"] = machines
        v["printer"].set(sel_m if sel_m in machines else (machines[0] if machines else ""))
        refresh_lists(prefer_process=sel_p, prefer_filament=sel_f[0] if sel_f else None)
        load_network_printers()
        update_buttons()
        status.set("Loaded %d printer, %d process and %d filament presets from %s" % (
            len(machines), len(st.names("process")), len(st.names("filament")), st.root))

    def refresh_lists(prefer_process=None, prefer_filament=None):
        st = store()
        if not st:
            return
        m = v["printer"].get()
        for key, kind, prefer in (("process", "process", prefer_process),
                                  ("filament", "filament", prefer_filament)):
            names = [n for n in st.names(kind) if v_all.get() or not m or st.compatible(kind, n, m)]
            # User presets first, they're the ones people tune.
            names.sort(key=lambda n: (st.get(kind, n).system, n.lower()))
            combos[key]["values"] = names
            cur = prefer or v[key].get()
            if cur not in names:
                cur = names[0] if names else ""
            v[key].set(cur)
        apply_retraction_defaults()
        on_filament()

    def on_filament(_e=None):
        st = store()
        f = v["filament"].get()
        if not st or not f:
            v["info"].set("")
            return
        cfg = st.resolve("filament", f)
        v["info"].set("%s · recommended %s–%s °C · preset nozzle temp %s °C · retraction %s mm" % (
            qp.first(cfg.get("filament_type"), "?"), qp.first(cfg.get("nozzle_temperature_range_low"), "?"),
            qp.first(cfg.get("nozzle_temperature_range_high"), "?"), qp.first(cfg.get("nozzle_temperature"), "?"),
            rt.fmt(rt.preset_retraction(st, v["printer"].get(), f)) if v["printer"].get() else "?"))
        v["r_temp"].set(str(int(qp.as_float(cfg.get("nozzle_temperature"), 210))))
        v["f_temp"].set(v["r_temp"].get())
        v["f_base"].set(fmt_flow(ft.preset_flow(st, f)))
        apply_filament_range()

    def apply_filament_range():
        st = store()
        if not st or not v["filament"].get():
            return
        s, e, step = filament_defaults(st, v["filament"].get())
        v["start"].set(str(s))
        v["end"].set(str(e))
        v["step"].set(str(step))
        update_summaries()

    def apply_retraction_defaults():
        st = store()
        if not st or not v["printer"].get():
            return
        s, e, step = rt.defaults(st, v["printer"].get())
        v["r_start"].set(rt.fmt(s))
        v["r_end"].set(rt.fmt(e))
        v["r_step"].set(rt.fmt(step))
        update_summaries()

    def printer_height():
        st = store()
        if st and v["printer"].get():
            return qp.as_float(st.resolve("machine", v["printer"].get()).get("printable_height"), 0)
        return 0

    def auto_output(key, path):
        if not v[key].get() or v[key].get() == auto[key]:
            auto[key] = path
            v[key].set(path)

    def update_summaries():
        st = store()
        max_h = printer_height()
        # Temperature tower
        try:
            temps = tt.temperatures(*[int(v[k].get()) for k in ("start", "end", "step")])
            h = tt.tower_height(len(temps))
            msg = "%d floors, %g mm tall: %s" % (len(temps), h, " → ".join(map(str, temps)))
            if max_h and h > max_h:
                msg += "   ⚠ taller than the printer's %g mm" % max_h
            if st and v["filament"].get():
                auto_output("output", default_output(st, v["filament"].get(), temps))
        except ValueError as e:
            msg = str(e) if "step" in str(e) else "Enter whole numbers"
        v["summary"].set(msg)
        # Retraction test
        try:
            values = rt.lengths(*[float(v[k].get()) for k in ("r_start", "r_end", "r_step")])
            band = float(v["r_band"].get())
            h = rt.test_height(len(values), band)
            msg = "%d bands, %g mm tall: %s mm" % (len(values), h, " → ".join(rt.fmt(x) for x in values))
            if max_h and h > max_h:
                msg += "   ⚠ taller than the printer's %g mm" % max_h
            if st and v["filament"].get():
                auto_output("r_output", default_retraction_output(st, v["filament"].get(), values))
        except ValueError as e:
            msg = str(e) if "step" in str(e) or "negative" in str(e) else "Enter numbers"
        v["r_summary"].set(msg)
        # Flow rate test
        try:
            offs = ft.offsets(*[float(v[k].get()) for k in ("f_start", "f_end", "f_step")])
            base = float(v["f_base"].get())
            width, depth = ft.footprint(len(offs))
            msg = "%d patches (%g × %g mm): %s" % (len(offs), width, depth,
                                                  " → ".join(ft.flow_label(r) for r in ft.ratios(base, offs)))
            if st and v["printer"].get():
                x0, y0, x1, y1 = qp.bed_shape(st.resolve("machine", v["printer"].get()))
                if width > x1 - x0 - 10 or depth > y1 - y0 - 10:
                    msg += "   ⚠ too big for the bed, use fewer steps"
            if st and v["filament"].get():
                auto_output("f_output", default_flow_output(st, v["filament"].get(), offs))
        except ValueError as e:
            msg = str(e) if "%" in str(e) else "Enter numbers"
        v["f_summary"].set(msg)
        update_calculator()

    def browse_dir():
        d = filedialog.askdirectory(initialdir=v["dir"].get() or None, title="QIDI Studio data folder")
        if d:
            v["dir"].set(os.path.normpath(d))
            load_store()

    def browse_out(key, kind):
        cur = v[key].get()
        types = [("G-code", "*.gcode")] if kind == "gcode" else [("3MF project", "*.3mf")]
        p = filedialog.asksaveasfilename(defaultextension="." + kind, filetypes=types,
                                         initialdir=os.path.dirname(cur) or None,
                                         initialfile=os.path.basename(cur) or None)
        if p:
            v[key].set(os.path.normpath(p))

    def finish(out, message, error, open_after):
        state["busy"] = False
        update_buttons()
        if error:
            status.set("")
            messagebox.showerror(APP_NAME, error)
            return
        status.set(message)
        if open_after:
            try:
                open_in_studio(out)
            except OSError as e:
                messagebox.showerror(APP_NAME, str(e))

    def generate(action):
        """action: None (just save), "open" (open in QIDI Studio) or "send" (upload to the printer)."""
        open_after = action == "open"
        st = store()
        if not st:
            messagebox.showerror(APP_NAME, "QIDI Studio presets are not loaded.")
            return
        presets = dict(store=st, machine=v["printer"].get(), process=v["process"].get(),
                       filament=v["filament"].get())
        if not sliced_tab():
            try:
                temps = tt.temperatures(*[number(k) for k in ("start", "end", "step")])
                out = v["output"].get().strip() or default_output(st, presets["filament"], temps)
                if not out.lower().endswith(".3mf"):
                    out += ".3mf"
                r = tt.write_3mf(out, temps, wait=v_wait.get(), embed_settings=v_embed.get(), **presets)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return finish(None, None, str(e), False)
            return finish(out, "Saved %s\n%d floors, %g mm tall. Temperature changes (%s) at z = %s." % (
                r["path"], r["floors"], r["height"], r["command"],
                ", ".join("%g→%d°C" % c for c in r["changes"])), None, open_after)

        # The retraction and flow tests are sliced with QIDI Studio, then adjusted.
        try:
            if current_tab() == "retraction":
                values = rt.lengths(*[number(k) for k in ("r_start", "r_end", "r_step")])
                band = number("r_band")
                temp = number("r_temp", optional=True)
                temp = None if temp is None else int(temp)
                out = v["r_output"].get().strip() or default_retraction_output(st, presets["filament"], values)

                def make(progress):
                    r = rt.generate(out, values, band_h=band, nozzle_temp=temp, progress=progress, **presets)
                    return "Saved %s\n%d bands of %g mm (%g mm tall) at %s °C. Retraction from the bottom: %s mm." % (
                        r["path"], r["bands"], band, r["height"], r["temperature"],
                        ", ".join(rt.fmt(x) for x in values))
            else:
                offs = ft.offsets(*[number(k) for k in ("f_start", "f_end", "f_step")])
                base = number("f_base")
                temp = number("f_temp", optional=True)
                temp = None if temp is None else int(temp)
                out = v["f_output"].get().strip() or default_flow_output(st, presets["filament"], offs)

                def make(progress):
                    r = ft.generate(out, offs, base_flow=base, nozzle_temp=temp, progress=progress, **presets)
                    return ("Saved %s\n%d patches at %s °C, flow ratios from the back left: %s. Pick the patch with "
                            "the smoothest centre and set the filament's flow ratio to its number." % (
                                r["path"], r["patches"], r["temperature"], ", ".join(r["labels"])))
        except ValueError as e:
            return finish(None, None, str(e), False)
        if not out.lower().endswith(".gcode"):
            out += ".gcode"
        target = None
        if action == "send":
            try:
                target = pick_network_printer(st, v["r_send"].get())
            except ValueError as e:
                return finish(None, None, str(e), False)
            if v_print_now.get() and not messagebox.askyesno(
                    APP_NAME, "Start printing on %s as soon as it's uploaded?\n\nMake sure the bed is clear "
                    "and the right filament is loaded." % pu.describe(target)):
                return
        print_now = bool(target) and v_print_now.get()
        state["busy"] = True
        update_buttons()
        # Slicing takes a while, so it runs on a worker thread. Tk may only be used from
        # this thread, so the worker reports through a queue that the window polls.
        messages = queue.Queue()

        def work():
            try:
                summary = make(lambda msg: messages.put(("progress", msg)))
                if target:
                    messages.put(("progress", "Sending to %s…" % pu.describe(target)))
                    pu.upload(target, out, start=print_now)
                    summary += "\nUploaded to %s%s." % (pu.describe(target),
                                                        " and started printing" if print_now else
                                                        ". Start it from the printer's screen")
                messages.put(("done", summary))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                messages.put(("error", str(e)))

        def poll():
            try:
                while True:
                    kind, msg = messages.get_nowait()
                    if kind == "progress":
                        status.set(msg)
                    elif kind == "done":
                        return finish(out, msg, None, open_after)
                    else:
                        return finish(None, None, msg, False)
            except queue.Empty:
                root.after(100, poll)

        threading.Thread(target=work, daemon=True).start()
        poll()

    combos["printer"].bind("<<ComboboxSelected>>", lambda e: refresh_lists())
    combos["filament"].bind("<<ComboboxSelected>>", on_filament)
    combos["process"].bind("<<ComboboxSelected>>", lambda e: update_summaries())
    tabs.bind("<<NotebookTabChanged>>", update_buttons)
    load_store()
    update_buttons()
    root.mainloop()


def _parent_pid(pid):
    """Parent process id on Windows (None if unknown)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                return entry.th32ParentProcessID
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))
    return None


def attach_console():
    """Let the windowed Windows build print when it's run from a terminal.

    The one-file build runs as two processes (a small unpacker, then this
    one), so the terminal is our grandparent rather than our parent.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.GetStdHandle.restype = ctypes.c_void_p
    handle = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    if handle and handle != ctypes.c_void_p(-1).value and k32.GetFileType(ctypes.c_void_p(handle)) in (1, 3):
        return  # FILE_TYPE_DISK / FILE_TYPE_PIPE: output is redirected, leave it there
    attached = k32.AttachConsole(-1)  # the parent's console
    if not attached:
        grandparent = _parent_pid(os.getppid())
        attached = bool(grandparent) and k32.AttachConsole(grandparent)
    if attached:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8")
        sys.stderr = sys.stdout
        print()  # the shell has already printed its prompt


def main():
    if len(sys.argv) == 1:
        return run_gui()
    attach_console()
    try:
        return run_cli(sys.argv[1:])
    except (ValueError, RuntimeError, OSError, KeyError) as e:
        # Report expected problems plainly. Left unhandled, the windowed app would show
        # an "Unhandled exception" dialog and sit waiting for a click.
        print("Error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
