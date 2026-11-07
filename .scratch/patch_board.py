p='new.py'
raw=open(p,'rb').read()
lines=raw.split(b'\n')
def text(i): return lines[i].rstrip(b'\r').decode()
def ending(i): return b'\r' if lines[i].endswith(b'\r') else b''

# 1) tile highlight cache helper before draw_gui
idx = next(i for i,l in enumerate(lines) if text(i).startswith("def draw_gui("))
helper = '''_TILE_OVERLAY_CACHE = {}


def _tile_overlay(rgba):
    """Cached one-square translucent overlay (selection / flash / last move).

    The board loop used to allocate and fill a fresh SRCALPHA surface for
    every highlighted square on every frame. The overlays depend only on
    the square size and colour, so they are built once and blitted like
    before: same pixels, no per-frame allocation or fill.
    """
    key = (SQUARE_SIZE, tuple(rgba))
    surface = _TILE_OVERLAY_CACHE.get(key)
    if surface is None:
        if len(_TILE_OVERLAY_CACHE) > 96:
            _TILE_OVERLAY_CACHE.clear()
        surface = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
        surface.fill(tuple(rgba))
        _TILE_OVERLAY_CACHE[key] = surface
    return surface


'''.split('\n')
e=ending(idx)
lines[idx:idx]=[l.encode()+e for l in helper]

# 2) board square loop: replace the three ad-hoc surfaces with the cache
out=[]
i=0
replaced=0
while i < len(lines):
    t=text(i)
    if t.strip()=="s = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)" and \
       text(i+1).strip()=="s.fill((246, 199, 49, int(live_highlight_alpha)))" and \
       text(i+2).strip()=="screen.blit(s, sq_rect.topleft)":
        e=ending(i)
        out.append(b'                screen.blit(' + e)
        out.append(b'                    _tile_overlay((246, 199, 49, int(live_highlight_alpha))),' + e)
        out.append(b'                    sq_rect.topleft,' + e)
        out.append(b'                )' + e)
        i+=3; replaced+=1; continue
    if t.strip()=="s = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)" and \
       text(i+1).strip()=="s.fill(HIGHLIGHT_COLOR)" and \
       text(i+2).strip()=="screen.blit(s, sq_rect.topleft)":
        e=ending(i)
        out.append(b'                screen.blit(_tile_overlay(HIGHLIGHT_COLOR), sq_rect.topleft)' + e)
        i+=3; replaced+=1; continue
    if t.strip()=="s = pygame.Surface(" and \
       text(i+1).strip()=="(SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA" and \
       text(i+2).strip()==")" and \
       text(i+3).strip()=="s.fill((255, 45, 45, flash_alpha))" and \
       text(i+4).strip()=="screen.blit(s, sq_rect.topleft)":
        e=ending(i)
        out.append(b'                    screen.blit(' + e)
        out.append(b'                        _tile_overlay((255, 45, 45, flash_alpha)),' + e)
        out.append(b'                        sq_rect.topleft,' + e)
        out.append(b'                    )' + e)
        i+=5; replaced+=1; continue
    out.append(lines[i]); i+=1
print("highlight sites replaced:", replaced)
open(p,'wb').write(b'\n'.join(out))
