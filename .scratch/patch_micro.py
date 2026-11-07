p='new.py'
raw=open(p,'rb').read()

# 1) Cache the prototyped user32 DLL: _user32() rebuilt a WinDLL and every
#    argtype/restype on each call (called from the Advisor mouse gate and
#    always-on-top paths).
old = '''def _user32():
    """user32 with correctly-typed prototypes, or None off Windows."""
    if os.name != "nt":
        return None
    from ctypes import wintypes
'''
new = '''_USER32_CACHE = [None, False]


def _user32():
    """user32 with correctly-typed prototypes, or None off Windows.

    The prototyped handle is cached because this is called from the Advisor
    mouse gate and other per-frame helpers; rebuilding a WinDLL and its
    argtypes on every call is pure overhead.
    """
    if os.name != "nt":
        return None
    cached = _USER32_CACHE[0]
    if cached is not None:
        return cached
    from ctypes import wintypes
'''
assert old in raw.decode() or True
text = raw.decode()
assert old in text
text = text.replace(old, new, 1)
# end of _user32: find the return statement of that function and store the cache
marker = "    get_long.restype = ctypes.c_ssize_t\n"
i = text.index(marker, text.index("def _user32("))
# the function ends with a final "    return user32" line
tail = text.index("\n\n", i)
block = text[i:tail]
assert "return user32" in block, block[-400:]
text = text[:i] + block.replace("    return user32", "    _USER32_CACHE[0] = user32") + text[tail:]
# add the return right after storing
text = text.replace("    _USER32_CACHE[0] = user32", "    _USER32_CACHE[0] = user32\r\n    return user32", 1)
open(p,'wb').write(text.encode())
