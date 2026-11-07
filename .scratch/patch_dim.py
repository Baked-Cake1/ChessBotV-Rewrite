p = 'new.py'
raw = open(p, 'rb').read()
lines = raw.split(b'\n')

def text(i):
    return lines[i].rstrip(b'\r').decode()

def ending(i):
    return b'\r' if lines[i].endswith(b'\r') else b''

DIM = "overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)"
pairs = [
    ("overlay.fill((0, 0, 0, 160))", "screen.blit(overlay, (0, 0))", "screen", "160"),
    ("overlay.fill((0, 0, 0, int(170 * e)))", "screen.blit(overlay, (0, 0))", "screen", "int(170 * e)"),
    ("overlay.fill((0, 0, 0, int(175 * e)))", "screen.blit(overlay, (0, 0))", "screen", "int(175 * e)"),
    ("overlay.fill((0, 0, 0, int(165 * e)))", "screen.blit(overlay, (0, 0))", "screen", "int(165 * e)"),
    ("overlay.fill((0, 0, 0, int(160*e)))", "screen.blit(overlay, (0,0))", "screen", "int(160 * e)"),
    ("overlay.fill((0, 0, 0, backdrop_alpha))", "destination.blit(overlay, (0, 0))", "destination", "backdrop_alpha"),
]
targets = []
for i in range(len(lines) - 2):
    if text(i).strip() != DIM:
        continue
    fill = text(i + 1).strip()
    blit = text(i + 2).strip()
    for want_fill, want_blit, target, alpha in pairs:
        if fill == want_fill and blit == want_blit:
            targets.append((i, target, alpha))
            break
print("holders found:", len(targets))
for i, target, alpha in sorted(targets, reverse=True):
    e = ending(i)
    lines[i:i + 3] = [
        f"    {target}.blit(_dialog_dim_surface({alpha}), (0, 0))".encode() + e
    ]
open(p, 'wb').write(b'\n'.join(lines))
