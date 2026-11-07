p='new.py'
raw=open(p,'rb').read()
lines=raw.split(b'\n')
def text(i): return lines[i].rstrip(b'\r').decode()
def ending(i): return b'\r' if lines[i].endswith(b'\r') else b''

# 1) import gc + disable cyclic GC during the hot loop (near the other imports)
idx = next(i for i,l in enumerate(lines) if text(i)=="import ctypes")
lines[idx] = lines[idx] + b'\r' if lines[idx].endswith(b'\r') else lines[idx]
insert_at = next(i for i,l in enumerate(lines) if text(i)=="import io")
e = ending(insert_at)
lines[insert_at:insert_at] = [b"import gc" + e]

# 2) after pygame init, keep cyclic GC off: live Advisor/Auto-play sessions
#    create a steady stream of short-lived dicts/surfaces whose gen0 sweeps
#    showed up as periodic frame spikes. Reference counting still frees
#    everything that does not form a cycle, and the main loop collects
#    explicitly (throttled) below.
anchor = next(i for i,l in enumerate(lines) if text(i).startswith("screen = pygame.display.set_mode"))
e = ending(anchor)
block = '''# --- Garbage collector policy ---
# ChessBotV allocates a lot of short-lived objects per frame (arrow sample
# vectors, cached-surface keys, poll result dicts). CPython's cyclic GC runs
# a gen0 sweep every ~700 allocations and a gen2 sweep periodically after
# that, which showed up as one-frame hitches during Advisor Mode. Reference
# counting already frees everything that is not part of a reference cycle,
# so cyclic collection is disabled here and performed explicitly from the
# main loop on a slow, fixed cadence instead (see ``_maintenance_collect``).
_GC_MAINTENANCE_SECONDS = 4.0


def _maintenance_collect(force=False):
    """Explicit, throttled cyclic collection for the pygame main loop."""
    global _gc_last_collect_at
    now = time.monotonic()
    if not force and now - _gc_last_collect_at < _GC_MAINTENANCE_SECONDS:
        return False
    _gc_last_collect_at = now
    gc.collect()
    return True


_gc_last_collect_at = time.monotonic()
gc.disable()
'''.split('\n')
lines[anchor:anchor] = [l.encode() + e for l in block]
open(p,'wb').write(b'\n'.join(lines))
