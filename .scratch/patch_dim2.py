p = 'new.py'
raw = open(p, 'rb').read()
lines = raw.split(b'\n')
def text(i): return lines[i].rstrip(b'\r').decode()
def ending(i): return b'\r' if lines[i].endswith(b'\r') else b''
DIM = "overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)"
n = 0
for i in range(len(lines) - 2):
    if text(i).strip() != DIM:
        continue
    fill = text(i + 1).strip()
    if fill in ("overlay.fill((0,0,0, int(160*e)))", "overlay.fill((0,0,0, int(160 * e)))"):
        blit = text(i + 2).strip()
        assert blit in ("screen.blit(overlay, (0,0))", "screen.blit(overlay, (0, 0))"), blit
        e = ending(i)
        lines[i:i+3] = [b"    screen.blit(_dialog_dim_surface(int(160 * e)), (0, 0))" + e]
        n += 1
print("fixed", n)

# insert the cached-surface helpers before _draw_settings_menu_reveal
idx = next(i for i, l in enumerate(lines) if text(i).startswith("def _draw_settings_menu_reveal"))
helper = '''_DIALOG_DIM_CACHE = {}
_WINDOW_SCRATCH_SURFACES = {


}

def _dialog_dim_surface(alpha):
    """Cached full-window black sheet for a dialog backdrop alpha.

    Modals and the Settings shell used to allocate and clear a full-window
    SRCALPHA surface every frame just to blit one flat colour. Backdrop alpha
    animates in small integer steps, so the sheet is built once per value and
    blitted again; the resulting pixels are identical.
    """
    alpha = max(0, min(255, int(alpha)))
    surface = _DIALOG_DIM_CACHE.get(alpha)
    if surface is None:
        if len(_DIALOG_DIM_CACHE) > 64:
            _DIALOG_DIM_CACHE.clear()
        surface = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        surface.fill((0, 0, 0, alpha))
        _DIALOG_DIM_CACHE[alpha] = surface
    return surface


def _window_scratch_surface(name="scratch"):
    """A reused, cleared full-window SRCALPHA surface for offscreen frames."""
    surface = _WINDOW_SCRATCH_SURFACES.get(name)
    if surface is None:
        surface = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        _WINDOW_SCRATCH_SURFACES[name] = surface
    else:
        surface.fill((0, 0, 0, 0))
    return surface

'''.split('\n')
# keep the original line ending of the anchor
e = ending(idx)
block = [l.encode() + e for l in helper]
lines[idx:idx] = block
open(p, 'wb').write(b'\n'.join(lines))
