#!/usr/bin/env python3
"""
JAR Comprehensive Semantic Change Detector
===========================================
Compares adjacent JAR files (commit-1 vs commit-2, commit-2 vs commit-3, ...)
and captures EVERY semantic change between public/protected API surfaces.

Pure Python — NO JDK, NO javap, NO pip installs required.
Uses only Python built-ins: struct, zipfile, hashlib, csv, re, os.

Change categories detected
---------------------------
CLASS LEVEL
  CLASS_ADDED                  - new public/protected class introduced
  CLASS_REMOVED                - existing public/protected class deleted
  CLASS_KIND_CHANGED           - class <-> interface <-> enum <-> @interface
  CLASS_SUPERCLASS_CHANGED     - extends a different parent
  CLASS_INTERFACE_ADDED        - now implements an additional interface
  CLASS_INTERFACE_REMOVED      - no longer implements an interface
  CLASS_NOW_FINAL              - class sealed, cannot be subclassed
  CLASS_NO_LONGER_FINAL        - final removed, now subclassable
  CLASS_NOW_ABSTRACT           - cannot be instantiated any more
  CLASS_NO_LONGER_ABSTRACT     - now concrete
  CLASS_VISIBILITY_REDUCED     - public -> protected/pkg/private
  CLASS_VISIBILITY_INCREASED   - visibility widened (informational)
  CLASS_DEPRECATED             - marked @Deprecated

METHOD LEVEL
  METHOD_ADDED                 - new method on existing class
  METHOD_REMOVED               - method deleted from existing class
  METHOD_RETURN_TYPE_CHANGED   - return type changed
  METHOD_PARAMS_CHANGED        - parameter list changed (add/remove/reorder/type)
  METHOD_VISIBILITY_REDUCED    - public/protected -> narrower
  METHOD_VISIBILITY_INCREASED  - visibility widened (informational)
  METHOD_NOW_FINAL             - can no longer be overridden
  METHOD_NO_LONGER_FINAL       - final removed
  METHOD_NOW_STATIC            - instance -> static
  METHOD_NO_LONGER_STATIC      - static -> instance
  METHOD_NOW_ABSTRACT          - concrete -> abstract
  METHOD_NO_LONGER_ABSTRACT    - abstract -> concrete
  METHOD_EXCEPTION_ADDED       - new checked exception declared
  METHOD_EXCEPTION_REMOVED     - checked exception removed
  METHOD_BODY_CHANGED          - bytecode/implementation changed (same signature)
  METHOD_DEPRECATED            - marked @Deprecated

FIELD LEVEL
  FIELD_ADDED                  - new field on existing class
  FIELD_REMOVED                - field deleted from existing class
  FIELD_TYPE_CHANGED           - field type changed
  FIELD_VISIBILITY_REDUCED     - narrower visibility
  FIELD_VISIBILITY_INCREASED   - wider visibility (informational)
  FIELD_NOW_FINAL              - field sealed
  FIELD_NO_LONGER_FINAL        - final removed
  FIELD_NOW_STATIC             - instance -> static
  FIELD_NO_LONGER_STATIC       - static -> instance
  FIELD_DEPRECATED             - marked @Deprecated

Usage
------
  python jar_semantic_changes.py                     # uses ./jar/ folder
  python jar_semantic_changes.py --jar-dir ./jars
  python jar_semantic_changes.py --output out.csv
  python jar_semantic_changes.py --include-private    # also report private members
  python jar_semantic_changes.py --all-classes        # include pkg-private classes
"""

import struct, zipfile, hashlib, csv, re, os, sys, argparse
from dataclasses import dataclass, field
from typing      import Optional
from pathlib     import Path
from io          import BytesIO


# ============================================================================
# JVM .class binary parser  (JVMS §4)
# ============================================================================

# ---------- access flag tables ----------------------------------------------

_CLASS_FLAGS = {
    0x0001: "public",     0x0010: "final",      0x0020: "super",
    0x0200: "interface",  0x0400: "abstract",   0x1000: "synthetic",
    0x2000: "annotation", 0x4000: "enum",
}
_MEMBER_FLAGS = {
    0x0001: "public",     0x0002: "private",    0x0004: "protected",
    0x0008: "static",     0x0010: "final",      0x0020: "synchronized",
    0x0040: "volatile",   0x0080: "transient",  0x0100: "native",
    0x0200: "interface",  0x0400: "abstract",   0x0800: "strictfp",
    0x1000: "synthetic",  0x4000: "enum",
}
_PRIM = {
    'B':'byte','C':'char','D':'double','F':'float',
    'I':'int', 'J':'long','S':'short', 'V':'void', 'Z':'boolean',
}

def _flags(raw: int, tbl: dict) -> set:
    return {v for k,v in tbl.items() if raw & k}

def _jvm_type(d: str) -> str:
    """Single JVM type descriptor -> readable string."""
    if not d: return "?"
    dims, i = 0, 0
    while i < len(d) and d[i] == '[':
        dims += 1; i += 1
    core = d[i:]
    if core in _PRIM:
        base = _PRIM[core]
    elif core.startswith('L') and core.endswith(';'):
        base = core[1:-1].replace('/','.')
    else:
        base = core
    return base + '[]'*dims

def _params(desc: str) -> list:
    """Parse method descriptor params -> list of readable types."""
    m = re.match(r'\(([^)]*)\)', desc)
    if not m: return []
    raw, res, i = m.group(1), [], 0
    while i < len(raw):
        c = raw[i]
        if c == 'L':
            e = raw.index(';', i)
            res.append(_jvm_type(raw[i:e+1])); i = e+1
        elif c == '[':
            j = i
            while j < len(raw) and raw[j] == '[': j += 1
            if raw[j] == 'L':
                e = raw.index(';', j)
                res.append(_jvm_type(raw[i:e+1])); i = e+1
            else:
                res.append(_jvm_type(raw[i:j+1])); i = j+1
        else:
            res.append(_PRIM.get(c, c)); i += 1
    return res

def _ret(desc: str) -> str:
    m = re.match(r'\([^)]*\)(.*)', desc)
    return _jvm_type(m.group(1)) if m else "?"

# ---------- constant-pool reader --------------------------------------------

class _CP:
    def __init__(self, f, count):
        self._p = [None]
        i = 1
        while i < count:
            tag = struct.unpack('>B', f.read(1))[0]
            if   tag == 1:  # Utf8
                n = struct.unpack('>H', f.read(2))[0]
                self._p.append(('U', f.read(n).decode('utf-8','replace')))
            elif tag == 7:  # Class
                self._p.append(('C', struct.unpack('>H', f.read(2))[0]))
            elif tag == 8:  f.read(2);  self._p.append(None)   # String
            elif tag in (9,10,11): f.read(4); self._p.append(None)
            elif tag == 12: f.read(4); self._p.append(None)    # NameAndType
            elif tag in (3,4):  f.read(4); self._p.append(None)
            elif tag in (5,6):  f.read(8); self._p.append(None); self._p.append(None); i+=1
            elif tag == 15: f.read(3); self._p.append(None)
            elif tag in (16,19,20): f.read(2); self._p.append(None)
            elif tag in (17,18):    f.read(4); self._p.append(None)
            else: self._p.append(None)
            i += 1

    def utf8(self, idx):
        e = self._p[idx] if 0 < idx < len(self._p) else None
        return e[1] if e and e[0]=='U' else ''

    def cls(self, idx):
        e = self._p[idx] if 0 < idx < len(self._p) else None
        return self.utf8(e[1]) if e and e[0]=='C' else ''

# ---------- helper readers --------------------------------------------------

def _u1(f): return struct.unpack('>B', f.read(1))[0]
def _u2(f): return struct.unpack('>H', f.read(2))[0]
def _u4(f): return struct.unpack('>I', f.read(4))[0]

def _read_attrs(f, cp) -> dict:
    """Read attribute table -> {name: raw_bytes}."""
    out = {}
    for _ in range(_u2(f)):
        name = cp.utf8(_u2(f))
        body = f.read(_u4(f))
        out[name] = body
    return out

# ---------- data classes ----------------------------------------------------

@dataclass
class MethodInfo:
    name:        str
    descriptor:  str          # raw JVM descriptor  "(Ljava/lang/String;)V"
    access_flags:set
    return_type: str          # human-readable
    params:      list         # list of human-readable param types
    throws:      list         # binary class names of checked exceptions
    body_hash:   str          # MD5 of Code attribute bytes (empty if abstract/native)
    deprecated:  bool = False
    is_abstract: bool = False
    is_bridge:   bool = False

    @property
    def signature(self):
        """name(param1,param2,...) — used as stable identity key."""
        return f"{self.name}({','.join(self.params)})"


@dataclass
class FieldInfo:
    name:        str
    descriptor:  str
    access_flags:set
    field_type:  str
    deprecated:  bool = False


@dataclass
class ClassInfo:
    name:        str           # binary  "com/example/Foo"
    access_flags:set
    superclass:  Optional[str]
    interfaces:  list
    methods:     dict = field(default_factory=dict)  # key: "name:descriptor"
    fields:      dict = field(default_factory=dict)  # key: "name:descriptor"
    is_interface:bool = False
    is_annotation:bool= False
    is_enum:     bool = False
    deprecated:  bool = False

# ---------- .class parser ---------------------------------------------------

def parse_class_bytes(data: bytes, store_body: bool = True) -> Optional[ClassInfo]:
    """Parse raw .class bytes -> ClassInfo.  Returns None on any error."""
    try:
        f = BytesIO(data)
        if _u4(f) != 0xCAFEBABE: return None
        _u2(f); _u2(f)  # minor / major

        cp    = _CP(f, _u2(f))
        raw_f = _u2(f)
        name  = cp.cls(_u2(f))
        sup_r = cp.cls(_u2(f))
        sup   = None if sup_r in ('', 'java/lang/Object') else sup_r
        ifaces= [cp.cls(_u2(f)) for _ in range(_u2(f))]

        ci = ClassInfo(
            name=name, access_flags=_flags(raw_f, _CLASS_FLAGS),
            superclass=sup, interfaces=ifaces,
            is_interface =(bool(raw_f & 0x0200)),
            is_annotation=(bool(raw_f & 0x2000)),
            is_enum      =(bool(raw_f & 0x4000)),
        )

        # ── fields ──────────────────────────────────────────────────────────
        for _ in range(_u2(f)):
            ff  = _u2(f); fn = cp.utf8(_u2(f)); fd = cp.utf8(_u2(f))
            att = _read_attrs(f, cp)
            ci.fields[f"{fn}:{fd}"] = FieldInfo(
                name=fn, descriptor=fd,
                access_flags=_flags(ff, _MEMBER_FLAGS),
                field_type=_jvm_type(fd),
                deprecated="Deprecated" in att,
            )

        # ── methods ─────────────────────────────────────────────────────────
        for _ in range(_u2(f)):
            mf  = _u2(f); mn = cp.utf8(_u2(f)); md = cp.utf8(_u2(f))
            att = _read_attrs(f, cp)

            throws = []
            if "Exceptions" in att:
                eb = BytesIO(att["Exceptions"])
                for _ in range(_u2(eb)):
                    throws.append(cp.cls(_u2(eb)))

            # Hash the Code attribute bytes (captures implementation changes)
            body_hash = ""
            if "Code" in att and store_body:
                body_hash = hashlib.md5(att["Code"]).hexdigest()

            ci.methods[f"{mn}:{md}"] = MethodInfo(
                name=mn, descriptor=md,
                access_flags=_flags(mf, _MEMBER_FLAGS),
                return_type=_ret(md),
                params=_params(md),
                throws=throws,
                body_hash=body_hash,
                deprecated="Deprecated" in att,
                is_abstract=bool(mf & 0x0400),
                is_bridge  =bool(mf & 0x0040),
            )

        # ── class-level attributes ───────────────────────────────────────────
        ci.deprecated = "Deprecated" in _read_attrs(f, cp)
        return ci
    except Exception:
        return None


# ============================================================================
# JAR loader
# ============================================================================

def _is_visible(flags: set, include_private: bool, include_pkg: bool) -> bool:
    if 'private' in flags and not include_private: return False
    if 'public' in flags or 'protected' in flags:  return True
    if include_pkg: return True
    return False

def load_jar_api(jar_path: str,
                 include_private: bool = False,
                 include_pkg:     bool = False) -> dict:
    """
    Returns {binary_class_name: ClassInfo} for all visible classes in the JAR.
    Default: public + protected only.
    """
    api = {}
    try:
        with zipfile.ZipFile(jar_path, 'r') as zf:
            for entry in zf.namelist():
                if not entry.endswith('.class'): continue
                if 'META-INF' in entry or 'module-info' in entry: continue
                try:
                    ci = parse_class_bytes(zf.read(entry))
                except Exception:
                    continue
                if ci is None or not ci.name: continue
                if not _is_visible(ci.access_flags, include_private, include_pkg):
                    continue
                api[ci.name] = ci
    except zipfile.BadZipFile as e:
        print(f"  WARNING: Cannot open '{jar_path}': {e}")
    return api


# ============================================================================
# Change record
# ============================================================================

@dataclass
class Change:
    comparison:  str   # "commit-1 -> commit-2"
    from_jar:    str
    to_jar:      str
    category:    str   # CLASS / METHOD / FIELD
    change_type: str   # e.g. METHOD_REMOVED
    severity:    str   # CRITICAL / HIGH / MEDIUM / LOW / INFO
    class_name:  str
    member:      str   # method/field name+desc or ""
    old_value:   str
    new_value:   str
    description: str


# Severity look-up table
_SEV = {
    # class
    "CLASS_REMOVED":               "CRITICAL",
    "CLASS_ADDED":                 "INFO",
    "CLASS_KIND_CHANGED":          "CRITICAL",
    "CLASS_SUPERCLASS_CHANGED":    "HIGH",
    "CLASS_INTERFACE_REMOVED":     "HIGH",
    "CLASS_INTERFACE_ADDED":       "INFO",
    "CLASS_NOW_FINAL":             "HIGH",
    "CLASS_NO_LONGER_FINAL":       "INFO",
    "CLASS_NOW_ABSTRACT":          "HIGH",
    "CLASS_NO_LONGER_ABSTRACT":    "INFO",
    "CLASS_VISIBILITY_REDUCED":    "CRITICAL",
    "CLASS_VISIBILITY_INCREASED":  "INFO",
    "CLASS_DEPRECATED":            "LOW",
    # method
    "METHOD_ADDED":                "INFO",
    "METHOD_REMOVED":              "CRITICAL",
    "METHOD_RETURN_TYPE_CHANGED":  "CRITICAL",
    "METHOD_PARAMS_CHANGED":       "CRITICAL",
    "METHOD_VISIBILITY_REDUCED":   "CRITICAL",
    "METHOD_VISIBILITY_INCREASED": "INFO",
    "METHOD_NOW_FINAL":            "HIGH",
    "METHOD_NO_LONGER_FINAL":      "INFO",
    "METHOD_NOW_STATIC":           "CRITICAL",
    "METHOD_NO_LONGER_STATIC":     "CRITICAL",
    "METHOD_NOW_ABSTRACT":         "HIGH",
    "METHOD_NO_LONGER_ABSTRACT":   "INFO",
    "METHOD_EXCEPTION_ADDED":      "MEDIUM",
    "METHOD_EXCEPTION_REMOVED":    "LOW",
    "METHOD_BODY_CHANGED":         "MEDIUM",
    "METHOD_DEPRECATED":           "LOW",
    "ABSTRACT_METHOD_ADDED":       "HIGH",
    # field
    "FIELD_ADDED":                 "INFO",
    "FIELD_REMOVED":               "CRITICAL",
    "FIELD_TYPE_CHANGED":          "CRITICAL",
    "FIELD_VISIBILITY_REDUCED":    "CRITICAL",
    "FIELD_VISIBILITY_INCREASED":  "INFO",
    "FIELD_NOW_FINAL":             "HIGH",
    "FIELD_NO_LONGER_FINAL":       "INFO",
    "FIELD_NOW_STATIC":            "CRITICAL",
    "FIELD_NO_LONGER_STATIC":      "CRITICAL",
    "FIELD_DEPRECATED":            "LOW",
}

_SEV_ORDER = {"CRITICAL":0,"HIGH":1,"MEDIUM":2,"LOW":3,"INFO":4}

_VIS_ORDER = {"public":0,"protected":1,"package-private":2,"private":3}

def _vis(f: set) -> str:
    for v in ("public","protected","private"):
        if v in f: return v
    return "package-private"

def _kind(ci: ClassInfo) -> str:
    if ci.is_annotation: return "@interface"
    if ci.is_interface:  return "interface"
    if ci.is_enum:       return "enum"
    return "class"

def _sev(t): return _SEV.get(t, "INFO")

def _cat(t):
    if t.startswith("CLASS"): return "CLASS"
    if t.startswith("METHOD") or t == "ABSTRACT_METHOD_ADDED": return "METHOD"
    return "FIELD"


# ============================================================================
# Comparison engine
# ============================================================================

def compare_jars(comparison: str,
                 from_jar:   str,
                 to_jar:     str,
                 old_api:    dict,
                 new_api:    dict,
                 include_private: bool = False) -> list:

    changes = []

    def rec(ct, cls, member, ov, nv, desc):
        changes.append(Change(
            comparison=comparison, from_jar=os.path.basename(from_jar),
            to_jar=os.path.basename(to_jar),
            category=_cat(ct), change_type=ct, severity=_sev(ct),
            class_name=cls, member=member,
            old_value=str(ov), new_value=str(nv), description=desc,
        ))

    # ── removed & changed classes ──────────────────────────────────────────
    for cname, oc in old_api.items():

        if cname not in new_api:
            rec("CLASS_REMOVED", cname, "", cname, "<removed>",
                f"Type '{cname}' was removed.")
            continue

        nc = new_api[cname]

        # kind
        ok, nk = _kind(oc), _kind(nc)
        if ok != nk:
            rec("CLASS_KIND_CHANGED", cname, "", ok, nk,
                f"Type kind changed: {ok} -> {nk}.")

        # superclass
        if oc.superclass != nc.superclass:
            rec("CLASS_SUPERCLASS_CHANGED", cname, "",
                oc.superclass or "none", nc.superclass or "none",
                f"Superclass changed: '{oc.superclass}' -> '{nc.superclass}'.")

        # interfaces
        for iface in set(oc.interfaces) - set(nc.interfaces):
            rec("CLASS_INTERFACE_REMOVED", cname, "",
                iface, "<removed>",
                f"No longer implements '{iface}'.")
        for iface in set(nc.interfaces) - set(oc.interfaces):
            rec("CLASS_INTERFACE_ADDED", cname, "",
                "<not present>", iface,
                f"Now implements '{iface}'.")

        # final
        if 'final' not in oc.access_flags and 'final' in nc.access_flags:
            rec("CLASS_NOW_FINAL", cname,"","non-final","final",
                f"'{cname}' is now final (cannot be subclassed).")
        if 'final' in oc.access_flags and 'final' not in nc.access_flags:
            rec("CLASS_NO_LONGER_FINAL", cname,"","final","non-final",
                f"'{cname}' is no longer final.")

        # abstract
        if ('abstract' not in oc.access_flags and 'abstract' in nc.access_flags
                and not oc.is_interface):
            rec("CLASS_NOW_ABSTRACT", cname,"","concrete","abstract",
                f"'{cname}' is now abstract (cannot be instantiated).")
        if ('abstract' in oc.access_flags and 'abstract' not in nc.access_flags
                and not oc.is_interface):
            rec("CLASS_NO_LONGER_ABSTRACT", cname,"","abstract","concrete",
                f"'{cname}' is no longer abstract.")

        # visibility
        ov, nv = _vis(oc.access_flags), _vis(nc.access_flags)
        if _VIS_ORDER.get(nv,3) > _VIS_ORDER.get(ov,0):
            rec("CLASS_VISIBILITY_REDUCED", cname,"", ov, nv,
                f"Visibility reduced: {ov} -> {nv}.")
        elif _VIS_ORDER.get(nv,3) < _VIS_ORDER.get(ov,0):
            rec("CLASS_VISIBILITY_INCREASED", cname,"", ov, nv,
                f"Visibility increased: {ov} -> {nv}.")

        # deprecated
        if not oc.deprecated and nc.deprecated:
            rec("CLASS_DEPRECATED", cname,"","","@Deprecated",
                f"'{cname}' marked @Deprecated.")

        # methods
        _compare_methods(rec, cname, oc, nc, include_private, new_api)

        # fields
        _compare_fields(rec, cname, oc, nc, include_private)

    # ── added classes ──────────────────────────────────────────────────────
    for cname, nc in new_api.items():
        if cname not in old_api:
            rec("CLASS_ADDED", cname, "", "<not present>", cname,
                f"New type '{cname}' introduced.")
            # report all methods/fields of the new class
            for key, nm in nc.methods.items():
                if nm.is_bridge or nm.name == '<clinit>': continue
                rec("METHOD_ADDED", cname, nm.signature,
                    "<not present>", nm.signature,
                    f"New method '{nm.name}' in new class '{cname}'.")
            for key, nf in nc.fields.items():
                rec("FIELD_ADDED", cname, nf.name,
                    "<not present>", f"{nf.name}:{nf.field_type}",
                    f"New field '{nf.name}' in new class '{cname}'.")

    return changes


# ── method comparator ─────────────────────────────────────────────────────

def _is_member_visible(flags, include_private):
    if 'synthetic' in flags: return False
    if 'private' in flags:   return include_private
    return True


def _compare_methods(rec, cname, oc, nc, include_private, new_api):

    # Build signature -> key mappings for old & new  (handles descriptor changes)
    def sig_map(cls_info):
        out = {}
        for k, m in cls_info.methods.items():
            if m.is_bridge or m.name == '<clinit>': continue
            sig = m.signature           # "name(p1,p2)"
            out.setdefault(sig, []).append(k)
        return out

    old_sig = sig_map(oc)
    new_sig = sig_map(nc)

    # removed methods (by signature)
    for sig, keys in old_sig.items():
        for key in keys:
            om = oc.methods[key]
            if not _is_member_visible(om.access_flags, include_private): continue
            if sig not in new_sig:
                rec("METHOD_REMOVED", cname, sig,
                    f"{om.name}{om.descriptor}", "<removed>",
                    f"Method '{om.name}' ({','.join(om.params)}) removed from '{cname}'.")

    # added methods
    for sig, keys in new_sig.items():
        for key in keys:
            nm = nc.methods[key]
            if not _is_member_visible(nm.access_flags, include_private): continue
            if sig not in old_sig:
                change_type = "ABSTRACT_METHOD_ADDED" \
                    if (nm.is_abstract and (nc.is_interface or 'abstract' in nc.access_flags)) \
                    else "METHOD_ADDED"
                rec(change_type, cname, sig, "<not present>", sig,
                    f"{'Abstract m' if change_type=='ABSTRACT_METHOD_ADDED' else 'M'}ethod "
                    f"'{nm.name}' ({','.join(nm.params)}) added to '{cname}'.")

    # changed methods (same human-readable signature)
    for sig in set(old_sig) & set(new_sig):
        for old_key in old_sig[sig]:
            om = oc.methods.get(old_key)
            if om is None: continue
            if not _is_member_visible(om.access_flags, include_private): continue

            # Find best matching new method for this signature
            nm = None
            for new_key in new_sig[sig]:
                nm = nc.methods.get(new_key)
                if nm: break
            if nm is None: continue

            # ── return type ─────────────────────────────────────────────
            if om.return_type != nm.return_type:
                rec("METHOD_RETURN_TYPE_CHANGED", cname, sig,
                    om.return_type, nm.return_type,
                    f"Return type of '{om.name}' changed: "
                    f"'{om.return_type}' -> '{nm.return_type}'.")

            # ── parameter list ───────────────────────────────────────────
            if om.descriptor != nm.descriptor and om.params != nm.params:
                rec("METHOD_PARAMS_CHANGED", cname, sig,
                    f"({', '.join(om.params)})", f"({', '.join(nm.params)})",
                    f"Parameters of '{om.name}' changed: "
                    f"({', '.join(om.params)}) -> ({', '.join(nm.params)}).")

            # ── visibility ───────────────────────────────────────────────
            ov, nv = _vis(om.access_flags), _vis(nm.access_flags)
            if _VIS_ORDER.get(nv,3) > _VIS_ORDER.get(ov,0):
                rec("METHOD_VISIBILITY_REDUCED", cname, sig, ov, nv,
                    f"Visibility of '{om.name}' reduced: {ov} -> {nv}.")
            elif _VIS_ORDER.get(nv,3) < _VIS_ORDER.get(ov,0):
                rec("METHOD_VISIBILITY_INCREASED", cname, sig, ov, nv,
                    f"Visibility of '{om.name}' increased: {ov} -> {nv}.")

            # ── final ────────────────────────────────────────────────────
            if 'final' not in om.access_flags and 'final' in nm.access_flags:
                rec("METHOD_NOW_FINAL", cname, sig, "non-final","final",
                    f"Method '{om.name}' is now final (cannot be overridden).")
            if 'final' in om.access_flags and 'final' not in nm.access_flags:
                rec("METHOD_NO_LONGER_FINAL", cname, sig, "final","non-final",
                    f"Method '{om.name}' is no longer final.")

            # ── static ───────────────────────────────────────────────────
            if 'static' in om.access_flags and 'static' not in nm.access_flags:
                rec("METHOD_NO_LONGER_STATIC", cname, sig, "static","instance",
                    f"Method '{om.name}' changed from static to instance.")
            if 'static' not in om.access_flags and 'static' in nm.access_flags:
                rec("METHOD_NOW_STATIC", cname, sig, "instance","static",
                    f"Method '{om.name}' changed from instance to static.")

            # ── abstract ─────────────────────────────────────────────────
            if not om.is_abstract and nm.is_abstract:
                rec("METHOD_NOW_ABSTRACT", cname, sig, "concrete","abstract",
                    f"Method '{om.name}' is now abstract.")
            if om.is_abstract and not nm.is_abstract:
                rec("METHOD_NO_LONGER_ABSTRACT", cname, sig, "abstract","concrete",
                    f"Method '{om.name}' is no longer abstract (now has implementation).")

            # ── checked exceptions ───────────────────────────────────────
            added_exc = set(nm.throws) - set(om.throws)
            if added_exc:
                rec("METHOD_EXCEPTION_ADDED", cname, sig,
                    str(sorted(om.throws)), str(sorted(nm.throws)),
                    f"New checked exception(s) on '{om.name}': {sorted(added_exc)}.")
            removed_exc = set(om.throws) - set(nm.throws)
            if removed_exc:
                rec("METHOD_EXCEPTION_REMOVED", cname, sig,
                    str(sorted(om.throws)), str(sorted(nm.throws)),
                    f"Checked exception(s) removed from '{om.name}': {sorted(removed_exc)}.")

            # ── body / implementation ────────────────────────────────────
            if (om.body_hash and nm.body_hash
                    and om.body_hash != nm.body_hash
                    and not om.is_abstract and not nm.is_abstract):
                rec("METHOD_BODY_CHANGED", cname, sig,
                    om.body_hash[:8], nm.body_hash[:8],
                    f"Implementation (bytecode) of '{om.name}' changed.")

            # ── deprecated ───────────────────────────────────────────────
            if not om.deprecated and nm.deprecated:
                rec("METHOD_DEPRECATED", cname, sig,
                    "not-deprecated","@Deprecated",
                    f"Method '{om.name}' marked @Deprecated.")


# ── field comparator ─────────────────────────────────────────────────────

def _compare_fields(rec, cname, oc, nc, include_private):

    for key, of in oc.fields.items():
        if not _is_member_visible(of.access_flags, include_private): continue

        if key not in nc.fields:
            rec("FIELD_REMOVED", cname, of.name,
                f"{of.name}:{of.field_type}", "<removed>",
                f"Field '{of.name}' (type: {of.field_type}) removed from '{cname}'.")
            continue

        nf = nc.fields[key]

        if of.field_type != nf.field_type:
            rec("FIELD_TYPE_CHANGED", cname, of.name,
                of.field_type, nf.field_type,
                f"Field '{of.name}' type changed: '{of.field_type}' -> '{nf.field_type}'.")

        ov, nv = _vis(of.access_flags), _vis(nf.access_flags)
        if _VIS_ORDER.get(nv,3) > _VIS_ORDER.get(ov,0):
            rec("FIELD_VISIBILITY_REDUCED", cname, of.name, ov, nv,
                f"Field '{of.name}' visibility reduced: {ov} -> {nv}.")
        elif _VIS_ORDER.get(nv,3) < _VIS_ORDER.get(ov,0):
            rec("FIELD_VISIBILITY_INCREASED", cname, of.name, ov, nv,
                f"Field '{of.name}' visibility increased: {ov} -> {nv}.")

        if 'final' not in of.access_flags and 'final' in nf.access_flags:
            rec("FIELD_NOW_FINAL", cname, of.name, "non-final","final",
                f"Field '{of.name}' is now final.")
        if 'final' in of.access_flags and 'final' not in nf.access_flags:
            rec("FIELD_NO_LONGER_FINAL", cname, of.name, "final","non-final",
                f"Field '{of.name}' is no longer final.")

        if 'static' in of.access_flags and 'static' not in nf.access_flags:
            rec("FIELD_NO_LONGER_STATIC", cname, of.name, "static","instance",
                f"Field '{of.name}' changed from static to instance.")
        elif 'static' not in of.access_flags and 'static' in nf.access_flags:
            rec("FIELD_NOW_STATIC", cname, of.name, "instance","static",
                f"Field '{of.name}' changed from instance to static.")

        if not of.deprecated and nf.deprecated:
            rec("FIELD_DEPRECATED", cname, of.name,
                "not-deprecated","@Deprecated",
                f"Field '{of.name}' marked @Deprecated.")

    # added fields
    for key, nf in nc.fields.items():
        if not _is_member_visible(nf.access_flags, include_private): continue
        if key not in oc.fields:
            rec("FIELD_ADDED", cname, nf.name,
                "<not present>", f"{nf.name}:{nf.field_type}",
                f"New field '{nf.name}' (type: {nf.field_type}) added to '{cname}'.")


# ============================================================================
# CSV writer
# ============================================================================

CSV_HEADERS = [
    "Comparison", "From JAR", "To JAR",
    "Category", "Change Type", "Severity",
    "Class", "Member",
    "Old Value", "New Value",
    "Description",
]


def write_csv(all_changes: list, path: str):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        w.writeheader()
        for c in all_changes:
            w.writerow({
                "Comparison":  c.comparison,
                "From JAR":    c.from_jar,
                "To JAR":      c.to_jar,
                "Category":    c.category,
                "Change Type": c.change_type,
                "Severity":    c.severity,
                "Class":       c.class_name,
                "Member":      c.member,
                "Old Value":   c.old_value,
                "New Value":   c.new_value,
                "Description": c.description,
            })
    print(f"\n  CSV saved to: {path}")


def write_summary(per_pair: list):
    """per_pair = list of (label, from_jar, to_jar, [Change])"""
    SEV  = ("CRITICAL","HIGH","MEDIUM","LOW","INFO")
    CATS = ("CLASS","METHOD","FIELD")
    print("\n" + "="*72)
    print("  FULL SEMANTIC CHANGE SUMMARY")
    print("="*72)
    grand = 0
    for label, from_jar, to_jar, changes in per_pair:
        grand += len(changes)
        sev_cnt = {s:0 for s in SEV}
        cat_cnt = {c:0 for c in CATS}
        for ch in changes:
            sev_cnt[ch.severity] = sev_cnt.get(ch.severity,0) + 1
            cat_cnt[ch.category] = cat_cnt.get(ch.category,0) + 1
        print(f"\n  {label}")
        print(f"    {os.path.basename(from_jar)}  ->  {os.path.basename(to_jar)}")
        print(f"    Total : {len(changes)}")
        print(f"    By severity : " +
              "  ".join(f"{s}={sev_cnt[s]}" for s in SEV if sev_cnt[s]))
        print(f"    By category : " +
              "  ".join(f"{c}={cat_cnt[c]}" for c in CATS if cat_cnt[c]))
    print(f"\n  GRAND TOTAL: {grand} changes across all comparisons")
    print("="*72)


# ============================================================================
# CLI
# ============================================================================

def _commit_num(p: str) -> int:
    m = re.search(r'(\d+)\s*$', Path(p).stem)
    return int(m.group(1)) if m else 0


def resolve_jars(args):
    if args.jars:
        jars = args.jars
    else:
        jar_dir = Path(args.jar_dir) if args.jar_dir else Path("jar")
        if not jar_dir.is_dir():
            print(f"ERROR: directory '{jar_dir}' not found.")
            print("       Put your JARs in ./jar/ or use --jar-dir <path>.")
            sys.exit(1)
        jars = sorted([str(p) for p in jar_dir.glob('*.jar')],
                      key=_commit_num)
        if not jars:
            print(f"ERROR: No .jar files in '{jar_dir}'"); sys.exit(1)
        print(f"\n  Found {len(jars)} JAR(s) in '{jar_dir}' (sorted by commit number):")
        for j in jars:
            print(f"    {os.path.basename(j)}  (commit #{_commit_num(j)})")
    if len(jars) < 2:
        print("ERROR: Need at least 2 JARs."); sys.exit(1)
    labels = args.labels if args.labels else [Path(j).stem for j in jars]
    if len(labels) != len(jars):
        print("ERROR: --labels count must match JAR count."); sys.exit(1)
    return jars, labels


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Comprehensive JAR semantic change detector.\n"
            "Pure Python -- no JDK, no pip packages.\n\n"
            "Default: reads ./jar/semantic-app-commit-<N>.jar"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--jars',    nargs='+', metavar='JAR',
                   help='Explicit ordered list of JARs (oldest -> newest)')
    g.add_argument('--jar-dir', metavar='DIR',
                   help='Folder with JARs (default: ./jar)')
    ap.add_argument('--labels', nargs='+', metavar='LABEL',
                    help='Custom label per JAR')
    ap.add_argument('--output', default='semantic_changes.csv',
                    help='Output CSV path (default: semantic_changes.csv)')
    ap.add_argument('--include-private', action='store_true',
                    help='Also report changes to private members')
    ap.add_argument('--all-classes', action='store_true',
                    help='Include package-private classes (not just public/protected)')
    args = ap.parse_args()

    jars, labels = resolve_jars(args)
    pairs = len(jars) - 1
    print(f"\n  Analysing {len(jars)} JARs ({pairs} comparisons) ...\n")

    all_changes = []
    per_pair    = []

    for i in range(pairs):
        old_jar, new_jar = jars[i], jars[i+1]
        label = f"{labels[i]}  ->  {labels[i+1]}"
        print(f"  [{i+1}/{pairs}]  {label}")

        print("    Loading old ... ", end="", flush=True)
        old_api = load_jar_api(old_jar, args.include_private, args.all_classes)
        print(f"{len(old_api)} classes")

        print("    Loading new ... ", end="", flush=True)
        new_api = load_jar_api(new_jar, args.include_private, args.all_classes)
        print(f"{len(new_api)} classes")

        print("    Comparing   ... ", end="", flush=True)
        chgs = compare_jars(label, old_jar, new_jar, old_api, new_api,
                            args.include_private)

        # sort within this pair: severity, category, class, type
        chgs.sort(key=lambda c: (
            _SEV_ORDER.get(c.severity, 9),
            c.category, c.class_name, c.change_type,
        ))
        print(f"{len(chgs)} change(s)  "
              f"[CRITICAL={sum(1 for c in chgs if c.severity=='CRITICAL')}  "
              f"HIGH={sum(1 for c in chgs if c.severity=='HIGH')}  "
              f"MEDIUM={sum(1 for c in chgs if c.severity=='MEDIUM')}  "
              f"LOW={sum(1 for c in chgs if c.severity=='LOW')}  "
              f"INFO={sum(1 for c in chgs if c.severity=='INFO')}]")

        all_changes.extend(chgs)
        per_pair.append((label, old_jar, new_jar, chgs))

    write_summary(per_pair)
    write_csv(all_changes, args.output)


if __name__ == '__main__':
    main()