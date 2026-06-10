"""
Shared helpers for reading LS-DYNA binout files via lasso-python.

lasso is imported lazily inside the functions so the backend can start
without it installed (the binout endpoints then return a clear 500).
All readers run with stderr redirected to silence the Lsda.__del__ noise
(read-mode fw attribute bug in lasso).
"""

import contextlib
import io


def open_binout(pattern):
    """Open Binout, patching _Diskfile.packsize on IndexError (lasso < 2.0.4).

    lasso < 2.0.4 ships a packsize list too short for 8-byte offset files;
    extend it to 9 entries indexed by byte-width: 1→B, 2→H, 4→i, 8→q.
    Harmless no-op on lasso >= 2.0.4.
    """
    from lasso.dyna import Binout
    import lasso.dyna.lsda_py3 as _lsda_mod

    try:
        return Binout(pattern)
    except IndexError:
        _lsda_mod._Diskfile.packsize = ['', 'B', 'H', '', 'i', '', '', '', 'q']
        return Binout(pattern)


def sym_key(k):
    """Normalise a bytes-or-str symbol key to str."""
    return k.decode("utf-8") if isinstance(k, bytes) else k


def sym_child(sym, name):
    """Return child Symbol by name (handles bytes and str keys)."""
    for k, v in sym.children.items():
        if sym_key(k) == name:
            return v
    return None


def safe_lread(sym):
    """Read raw data from a Symbol; return None on any error (e.g. unknown type code)."""
    try:
        return sym.lread()
    except Exception:
        return None


def build_binout_index(glob_pattern):
    """
    Probe every entry/variable in the binout file(s) matching glob_pattern.

    Returns the entries list:
        [{name, variables: [{name, per_entity}], ids: [...] | None}, ...]
    """
    import numpy as np

    entries = []
    with contextlib.redirect_stderr(io.StringIO()):
        binout = None
        try:
            binout = open_binout(glob_pattern)

            for entry_name in binout.read():
                try:
                    vars_all = binout.read(entry_name)
                    if not isinstance(vars_all, list):
                        continue

                    # Time array — needed to validate plottable shape
                    t = binout.read(entry_name, "time")
                    n_steps = len(t)

                    # Entity IDs (optional)
                    ids_list = None
                    if "ids" in vars_all:
                        try:
                            raw_ids = binout.read(entry_name, "ids")
                            if isinstance(raw_ids, np.ndarray):
                                # Reduce to a flat 1-D sequence regardless of lasso version:
                                # may be (n_steps, n_entities), (n_entities,), or object array.
                                flat = raw_ids
                                while hasattr(flat, 'ndim') and flat.ndim > 1:
                                    flat = flat[0]
                                flat_list = list(flat.tolist() if hasattr(flat, 'tolist') else flat)
                                # If elements are still lists/tuples (object array edge-case), unwrap one level
                                if flat_list and isinstance(flat_list[0], (list, tuple)):
                                    flat_list = list(flat_list[0])
                                if entry_name == "rcforc" and "side" in vars_all:
                                    raw_side = binout.read(entry_name, "side")
                                    fs = raw_side
                                    while hasattr(fs, 'ndim') and fs.ndim > 1:
                                        fs = fs[0]
                                    side_list = list(fs.tolist() if hasattr(fs, 'tolist') else fs)
                                    if side_list and isinstance(side_list[0], (list, tuple)):
                                        side_list = list(side_list[0])
                                    ids_list = [
                                        f"{i}m" if j else f"{i}s"
                                        for i, j in zip(flat_list, side_list)
                                    ]
                                else:
                                    ids_list = [str(x) for x in flat_list]
                        except Exception:
                            pass

                    # Probe each variable for plottability
                    plottable_vars = []
                    for vname in vars_all:
                        if vname == "time":
                            continue
                        try:
                            d = binout.read(entry_name, vname)
                            if not isinstance(d, np.ndarray):
                                continue
                            if d.shape[0] != n_steps:
                                continue
                            plottable_vars.append({"name": vname, "per_entity": d.ndim == 2})
                        except Exception:
                            continue

                    if plottable_vars:
                        entries.append({
                            "name": entry_name,
                            "variables": plottable_vars,
                            "ids": ids_list,
                        })

                except Exception:
                    continue

        finally:
            if binout is not None:
                del binout  # trigger __del__ while stderr is redirected

    return entries


def read_binout_series(glob_pattern, entry, variable, requested_ids=None):
    """
    Read one variable's time series from the binout file(s) matching glob_pattern
    by traversing the lsda Symbol tree directly (robust against binout.read()
    failures on unknown type codes).

    For per-entity variables `requested_ids` (list of ID label strings) filters
    which entity columns are returned, capped at 10.

    Returns: { time: [...], series: [{id: str, values: [...]}] }
    Raises ValueError when the entry/variable holds no readable data.
    """
    import numpy as np

    result = {}
    with contextlib.redirect_stderr(io.StringIO()):
        binout = None
        try:
            binout = open_binout(glob_pattern)
            root = binout.lsda_root

            # Locate the entry directory
            entry_sym = sym_child(root, entry)
            if entry_sym is None:
                raise ValueError(f"Entry '{entry}' not found in binout.")

            # Collect (time_value, data_tuple) pairs from each state directory
            time_raw = []
            data_raw = []
            for k, subdir in entry_sym.children.items():
                if sym_key(k) == "metadata":
                    continue
                var_sym  = sym_child(subdir, variable)
                time_sym = sym_child(subdir, "time")
                if var_sym is None or time_sym is None:
                    continue
                t = safe_lread(time_sym)
                d = safe_lread(var_sym)
                if t is None or d is None or len(t) == 0:
                    continue
                time_raw.append(float(t[0]))
                data_raw.append(d)

            if not data_raw:
                raise ValueError(f"No readable data found for {entry}/{variable}.")

            # Sort by time
            order    = list(np.argsort(time_raw))
            time_arr = [time_raw[i] for i in order]
            data_sorted = [data_raw[i] for i in order]

            # Build data array — scalar (len 1 per step) or per-entity
            first = data_sorted[0]
            if len(first) == 1:
                # Scalar time-series
                values = [float(d[0]) for d in data_sorted]
                result = {
                    "time": time_arr,
                    "series": [{"id": variable, "values": values}],
                }
            else:
                # Per-entity: data_sorted is list of tuples, each of length n_entities
                n_ent = len(first)
                data_2d = [[float(d[col]) for d in data_sorted] for col in range(n_ent)]

                # Get entity ID labels.
                # Primary: metadata/ids (matsum, rcforc, sleout, …)
                # Fallback: first timestep dir/ids (rbdout stores IDs per-timestep)
                id_labels = [str(i) for i in range(n_ent)]  # positional last-resort
                ids_raw = None
                meta_sym = sym_child(entry_sym, "metadata")
                if meta_sym is not None:
                    ids_sym = sym_child(meta_sym, "ids")
                    if ids_sym is not None:
                        ids_raw = safe_lread(ids_sym)
                if ids_raw is None:
                    # Look in first timestep directory
                    for k, subdir in entry_sym.children.items():
                        if sym_key(k) == "metadata":
                            continue
                        ids_sym = sym_child(subdir, "ids")
                        if ids_sym is not None:
                            ids_raw = safe_lread(ids_sym)
                            break
                if ids_raw is not None:
                    # lasso may return 2D (n_steps × n_entities) — flatten to first row
                    if hasattr(ids_raw, 'ndim') and ids_raw.ndim == 2:
                        ids_raw = ids_raw[0]
                    if len(ids_raw) == n_ent:
                        if entry == "rcforc":
                            side_raw = None
                            if meta_sym is not None:
                                side_sym = sym_child(meta_sym, "side")
                                if side_sym is not None:
                                    side_raw = safe_lread(side_sym)
                            if side_raw is not None and hasattr(side_raw, 'ndim') and side_raw.ndim == 2:
                                side_raw = side_raw[0]
                            if side_raw is not None and len(side_raw) == n_ent:
                                id_labels = [
                                    f"{i}m" if j else f"{i}s"
                                    for i, j in zip(ids_raw, side_raw)
                                ]
                            else:
                                id_labels = [str(x) for x in ids_raw]
                        else:
                            id_labels = [str(x) for x in ids_raw]

                # Filter to requested IDs, cap at 10
                if requested_ids:
                    cols = [i for i, lbl in enumerate(id_labels) if lbl in requested_ids][:10]
                else:
                    cols = list(range(min(n_ent, 10)))

                result = {
                    "time": time_arr,
                    "series": [
                        {"id": id_labels[i], "values": data_2d[i]}
                        for i in cols
                    ],
                }

        finally:
            if binout is not None:
                del binout  # trigger __del__ while stderr is redirected

    return result
