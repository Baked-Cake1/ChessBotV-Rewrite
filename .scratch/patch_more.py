p='new.py'
raw=open(p,'rb').read()
lines=raw.split(b'\n')
def text(i): return lines[i].rstrip(b'\r').decode()
def ending(i): return b'\r' if lines[i].endswith(b'\r') else b''
n=0
for i,l in enumerate(lines):
    t=text(i)
    if t.strip()=="s = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)":
        nxt=text(i+1).strip()
        if nxt=="s.fill((255, 255, 100, 80))" and text(i+2).strip()=="screen.blit(s, hl_rect.topleft)":
            e=ending(i)
            lines[i:i+3]=[b"                screen.blit(_tile_overlay((255, 255, 100, 80)), hl_rect.topleft)"+e]
            n+=1
print("drag highlight replaced:", n)
open(p,'wb').write(b'\n'.join(lines))
