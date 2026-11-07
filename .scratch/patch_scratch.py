p='new.py'
raw=open(p,'rb').read()
lines=raw.split(b'\n')
def text(i): return lines[i].rstrip(b'\r').decode()
def ending(i): return b'\r' if lines[i].endswith(b'\r') else b''
counts={"shell":0,"page":0,"reveal":0}
for i,l in enumerate(lines):
    t=text(i)
    if t.strip()=="content_surface = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)":
        lines[i]=b'        content_surface = _window_scratch_surface("settings_shell")'+ending(i)
        counts["shell"]+=1
    elif t.strip()=="overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)" and i+2 < len(lines) \
            and text(i+1).strip()=="overlay.fill((0, 0, 0, 0))" :
        pass
    elif t.strip()==")":  # noop
        pass
print(counts)
# outgoing page: two-line surface creation
for i in range(len(lines)-3):
    if text(i).strip()=="outgoing_surface = pygame.Surface(" and text(i+1).strip()=="(WIDTH, HEIGHT), pygame.SRCALPHA":
        e=ending(i)
        lines[i:i+2]=[b'            outgoing_surface = _window_scratch_surface("settings_page")'+e]
        counts["page"]+=1
# reveal overlay: the surface has no fill, blitted at the end of _draw_settings_menu_reveal
for i,l in enumerate(lines):
    if text(i).strip()=="overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)" and i+2 < len(lines):
        nxt=text(i+2).strip()
        if nxt.startswith("# Two soft sonar-like rings"):
            lines[i]=b'    overlay = _window_scratch_surface("settings_reveal")'+ending(i)
            counts["reveal"]+=1
print(counts)
open(p,'wb').write(b'\n'.join(lines))
