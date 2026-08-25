import struct
import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from PIL import Image, ImageTk


# ---------------------------------------------------------------------------
# PKZ chunk walking
# ---------------------------------------------------------------------------

def header(buf, off):
    """Read a 16-byte chunk header: (type, version, flags, size)."""
    idw, ver, flags, reserved, size = struct.unpack_from(">IHHII", buf, off)
    return idw & 0x7FFFFFFF, ver, flags, size


def children(buf, off, size):
    """List the direct children chunks contained within [off, off+16+size)."""
    out = []
    p = off + 16
    end = off + 16 + size
    while p + 16 <= end:
        idw, ver, flags, reserved, csize = struct.unpack_from(">IHHII", buf, p)
        if not (idw & 0x80000000):
            break
        if p + 16 + csize > end:
            break
        out.append((idw & 0x7FFFFFFF, p, csize, flags, ver))
        p += 16 + csize
    return out


def top(buf):
    """Children of the file's root chunk."""
    cid, ver, flags, size = header(buf, 0)
    return children(buf, 0, size)


def find_child(kids, want_type):
    return next((k for k in kids if k[0] == want_type), None)


def asset_header(buf, off_138e):
    """Parse a ResourceHeader (0x138E) payload: returns (hash, asset_type, name)."""
    p = off_138e + 16
    h, atype = struct.unpack_from(">II", buf, p)
    name = buf[p + 28:p + 92].split(b"\x00", 1)[0].decode("latin1", errors="replace")
    return h, atype, name


def list_assets(buf):
    """List every top-level 0x0026 asset chunk with its ResourceHeader info."""
    out = []
    for cid, off, size, flags, ver in top(buf):
        if cid != 0x0026:
            continue
        kids = children(buf, off, size)
        if not kids:
            continue
        e138e = find_child(kids, 0x138E)
        if not e138e:
            continue
        h, atype, name = asset_header(buf, e138e[1])
        out.append((name, atype, h, off, size, kids))
    return out


def texture_registry(buf):
    """Build a hash -> texture-info dict by walking 0x0009 > 0x138D >
    [0x138E, 0x0191 > 0x0197] chunks."""
    byhash = {}
    for cid, off, size, flags, ver in top(buf):
        if cid != 0x0009:
            continue
        for c1, o1, s1, f1, v1 in children(buf, off, size):
            if c1 != 0x138D:
                continue
            sub = children(buf, o1, s1)
            e138e = find_child(sub, 0x138E)
            e191 = find_child(sub, 0x0191)
            if not e138e or not e191:
                continue
            tex_hash, atype, name = asset_header(buf, e138e[1])
            e197 = find_child(children(buf, e191[1], e191[2]), 0x0197)
            if not e197 or e197[2] < 20:
                continue
            # IMPORTANT: height comes first, then width
            height, width, mips, fmt, csize = struct.unpack_from(">5I", buf, e197[1] + 16)
            byhash[tex_hash] = {
                'name': name, 'hash': tex_hash,
                'w': width, 'h': height,
                'mips': mips, 'fmt': fmt, 'csize': csize,
                'meta_off': e197[1],
            }
    return byhash


def texture_payloads(buf):
    """Map ResourceHeader hash -> (payload_offset, payload_length) for every
    type-4 0x0026 asset's 0x0195 pixel-data child."""
    out = {}
    for name, atype, tex_hash, off, size, kids in list_assets(buf):
        if atype != 4:
            continue
        k195 = find_child(kids, 0x0195)
        if k195:
            out[tex_hash] = (k195[1] + 16, k195[2])
    return out


def discover_textures(buf):
    """Combine the registry and payload maps into one list of editable
    texture dicts."""
    reg = texture_registry(buf)
    payloads = texture_payloads(buf)
    textures = []
    for tex_hash, info in reg.items():
        if tex_hash not in payloads:
            continue
        poff, plen = payloads[tex_hash]
        t = dict(info)
        t['payload_off'] = poff
        t['payload_len'] = plen
        textures.append(t)
    return textures


# ---------------------------------------------------------------------------
# GX texture decoders (numpy-accelerated)
# ---------------------------------------------------------------------------

def detile(blocks, w, h, tile_w, tile_h):
    bw = (w + tile_w - 1) // tile_w
    bh = (h + tile_h - 1) // tile_h
    img = blocks.reshape(bh, bw, tile_h, tile_w, -1).transpose(0, 2, 1, 3, 4)
    img = img.reshape(bh * tile_h, bw * tile_w, blocks.shape[-1])
    return img[:h, :w]


def rgb565_np(c):
    r = ((c >> 11) & 31).astype(np.uint16)
    g = ((c >> 5) & 63).astype(np.uint16)
    b = (c & 31).astype(np.uint16)
    return np.stack([r * 255 // 31, g * 255 // 63, b * 255 // 31], axis=-1).astype(np.uint8)


def decode_cmpr(buf, w, h):
    """GX CMPR: 8x8 macrotiles, each made of four 4x4 DXT1 blocks in
    TL, TR, BL, BR order."""
    bw = (w + 7) // 8
    bh = (h + 7) // 8
    n = bw * bh * 4
    need = n * 8
    if len(buf) < need:
        raise ValueError(f"CMPR data too short: {len(buf)} < {need}")

    a = np.frombuffer(buf[:need], np.uint8).reshape(n, 8)
    c0 = (a[:, 0].astype(np.uint16) << 8) | a[:, 1]
    c1 = (a[:, 2].astype(np.uint16) << 8) | a[:, 3]
    p0 = rgb565_np(c0).astype(np.int16)
    p1 = rgb565_np(c1).astype(np.int16)
    opaque = c0 > c1

    pal = np.zeros((n, 4, 4), np.uint8)
    pal[:, 0, :3] = p0; pal[:, 0, 3] = 255
    pal[:, 1, :3] = p1; pal[:, 1, 3] = 255
    p2 = np.where(opaque[:, None], (2 * p0 + p1) // 3, (p0 + p1) // 2)
    p3 = np.where(opaque[:, None], (p0 + 2 * p1) // 3, 0)
    pal[:, 2, :3] = p2.astype(np.uint8); pal[:, 2, 3] = 255
    pal[:, 3, :3] = p3.astype(np.uint8)
    pal[:, 3, 3] = np.where(opaque, 255, 0).astype(np.uint8)

    idx = a[:, 4:8]
    selectors = np.stack([(idx >> 6) & 3, (idx >> 4) & 3, (idx >> 2) & 3, idx & 3], axis=-1)
    texel = pal[np.arange(n)[:, None, None], selectors]

    texel = texel.reshape(bh * bw, 2, 2, 4, 4, 4).transpose(0, 1, 3, 2, 4, 5)
    tiles = texel.reshape(bh * bw, 8, 8, 4)
    return detile(tiles, w, h, 8, 8)


def encode_cmpr(rgba, w, h):
    """Encode an (h, w, 4) uint8 RGBA numpy array back into a CMPR byte
    string. Pure-Python per-block encoder (simple min/max heuristic)."""
    out = bytearray()
    for ty in range(0, h, 8):
        for tx in range(0, w, 8):
            for sy, sx in ((0, 0), (0, 4), (4, 0), (4, 4)):
                block = []
                for yy in range(4):
                    for xx in range(4):
                        x, y = tx + sx + xx, ty + sy + yy
                        if x < w and y < h:
                            block.append(tuple(int(v) for v in rgba[y, x]))
                        else:
                            block.append((0, 0, 0, 0))
                out += _encode_cmpr_block(block)
    return bytes(out)


def _encode_cmpr_block(pixels):
    has_alpha = any(p[3] < 128 for p in pixels)
    opaque = [(p[0], p[1], p[2]) for p in pixels if p[3] >= 128] or [(0, 0, 0)]
    rs = [p[0] for p in opaque]; gs = [p[1] for p in opaque]; bs = [p[2] for p in opaque]
    rmax, rmin = max(rs), min(rs)
    gmax, gmin = max(gs), min(gs)
    bmax, bmin = max(bs), min(bs)

    def pack565(r, g, b):
        return ((r * 31 // 255) << 11) | ((g * 63 // 255) << 5) | (b * 31 // 255)

    c0 = pack565(rmax, gmax, bmax)
    c1 = pack565(rmin, gmin, bmin)
    if has_alpha:
        if c0 > c1:
            c0, c1 = c1, c0
    else:
        if c0 <= c1:
            c1 = max(0, c1 - 1)

    def unpack(v):
        return (((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31)

    p0, p1 = unpack(c0), unpack(c1)
    if c0 > c1:
        p2 = tuple((2 * p0[i] + p1[i]) // 3 for i in range(3))
        p3 = tuple((p0[i] + 2 * p1[i]) // 3 for i in range(3))
    else:
        p2 = tuple((p0[i] + p1[i]) // 2 for i in range(3))
        p3 = (0, 0, 0)
    colors = [p0, p1, p2, p3]

    def closest(px):
        pr, pg, pb, pa = px
        if has_alpha and pa < 128:
            return 3
        limit = 3 if has_alpha else 4
        best_i, best_d = 0, None
        for i in range(limit):
            cr, cg, cb = colors[i]
            d = (cr - pr) ** 2 + (cg - pg) ** 2 + (cb - pb) ** 2
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        return best_i

    rows = bytearray(4)
    for yy in range(4):
        row = 0
        for xx in range(4):
            idx = closest(pixels[yy * 4 + xx])
            row |= idx << (6 - 2 * xx)
        rows[yy] = row
    return struct.pack('>HH', c0, c1) + bytes(rows)


def decode_i8(buf, w, h):
    bw = (w + 7) // 8
    bh = (h + 3) // 4
    need = bw * bh * 32
    if len(buf) < need:
        raise ValueError(f"I8 data too short: {len(buf)} < {need}")
    a = np.frombuffer(buf[:need], np.uint8).reshape(bw * bh, 4, 8, 1)
    return detile(a, w, h, 8, 4)[:, :, 0]


def encode_i8(alpha, w, h):
    """Encode an (h, w) uint8 numpy array into I8 tiled bytes."""
    out = bytearray()
    for ty in range(0, h, 4):
        for tx in range(0, w, 8):
            block = bytearray(32)
            for yy in range(4):
                for xx in range(8):
                    x, y = tx + xx, ty + yy
                    if x < w and y < h:
                        block[yy * 8 + xx] = int(alpha[y, x])
            out += block
    return bytes(out)


def decode_rgb5a3(buf, w, h):
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    need = bw * bh * 32
    if len(buf) < need:
        raise ValueError(f"RGB5A3 data too short: {len(buf)} < {need}")
    a = np.frombuffer(buf[:need], dtype=">u2").reshape(bw * bh, 4, 4)
    c = a.reshape(-1)
    out = np.zeros((c.size, 4), np.uint8)
    opaque = (c & 0x8000) != 0
    out[opaque, 0] = ((c[opaque] >> 10) & 31) * 255 // 31
    out[opaque, 1] = ((c[opaque] >> 5) & 31) * 255 // 31
    out[opaque, 2] = (c[opaque] & 31) * 255 // 31
    out[opaque, 3] = 255
    out[~opaque, 0] = ((c[~opaque] >> 8) & 15) * 17
    out[~opaque, 1] = ((c[~opaque] >> 4) & 15) * 17
    out[~opaque, 2] = (c[~opaque] & 15) * 17
    out[~opaque, 3] = ((c[~opaque] >> 12) & 7) * 255 // 7
    blocks = out.reshape(bw * bh, 4, 4, 4)
    return detile(blocks, w, h, 4, 4)


def decode_ia8(buf, w, h):
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    need = bw * bh * 32
    if len(buf) < need:
        raise ValueError(f"IA8 data too short: {len(buf)} < {need}")
    a = np.frombuffer(buf[:need], np.uint8).reshape(bw * bh, 4, 4, 2)
    out = np.zeros((bw * bh, 4, 4, 4), np.uint8)
    out[..., 0] = a[..., 1]
    out[..., 1] = a[..., 1]
    out[..., 2] = a[..., 1]
    out[..., 3] = a[..., 0]
    return detile(out, w, h, 4, 4)


def decode_rgba8(buf, w, h):
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    need = bw * bh * 64
    if len(buf) < need:
        raise ValueError(f"RGBA8 data too short: {len(buf)} < {need}")
    a = np.frombuffer(buf[:need], np.uint8).reshape(bw * bh, 2, 4, 4, 2)
    out = np.zeros((bw * bh, 4, 4, 4), np.uint8)
    out[..., 3] = a[:, 0, :, :, 0]
    out[..., 0] = a[:, 0, :, :, 1]
    out[..., 1] = a[:, 1, :, :, 0]
    out[..., 2] = a[:, 1, :, :, 1]
    return detile(out, w, h, 4, 4)


def decode_texture(payload, t):
    """Decode any supported format into an (h, w, 4) uint8 RGBA numpy array."""
    w, h, fmt = t['w'], t['h'], t['fmt']
    if fmt in (3, 4, 5):
        img = decode_cmpr(payload, w, h)
        if fmt == 4:
            alpha_offset = t['csize']
            alpha = decode_i8(payload[alpha_offset:], w, h)
            if alpha.shape == img.shape[:2]:
                img = img.copy()
                img[:, :, 3] = alpha
        return img
    if fmt == 2:
        return decode_rgb5a3(payload, w, h)
    if fmt == 1:
        return decode_ia8(payload, w, h)
    if fmt == 0:
        return decode_rgba8(payload, w, h)
    raise NotImplementedError(f"Format {fmt} is not supported")


ENCODABLE_FORMATS = {3, 4}  # only CMPR / CMPR_A can be written back for now
FORMAT_NAMES = {0: 'RGBA8', 1: 'IA8', 2: 'RGB5A3', 3: 'CMPR', 4: 'CMPR_A', 5: 'CMPR'}


def safe_filename(s):
    s = re.sub(r'[<>:"/\\|?*]+', '_', s).strip(' .')
    return s or 'unnamed'


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class SSCRTextureEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SSCR Textures Editor")
        self.root.geometry("760x600")

        self.pkz_path = None
        self.pkz_data = None
        self.textures = []
        self.selected = None
        self.selected_idx = None
        self.preview_img = None
        self.current_decoded_img = None
        self.custom_img = None
        self.edited_indices = set()
        self.visible_indices = []

        self.build_ui()

    def build_ui(self):
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill='x')
        ttk.Button(top_frame, text="Open .pkz", command=self.open_pkz).pack(side='left')
        self.lbl_file = ttk.Label(top_frame, text="No file open")
        self.lbl_file.pack(side='left', padx=10)

        mid = ttk.Frame(self.root, padding=10)
        mid.pack(fill='both', expand=True)

        left = ttk.Frame(mid)
        left.pack(side='left', fill='y')
        ttk.Label(left, text="Textures found:").pack(anchor='w')

        search_frame = ttk.Frame(left)
        search_frame.pack(fill='x', pady=(0, 5))
        ttk.Label(search_frame, text="Search:").pack(side='left')
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *args: self.refresh_listbox_labels())
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side='left', fill='x', expand=True, padx=5)

        self.listbox = tk.Listbox(left, width=55, height=28)
        self.listbox.pack(fill='y', expand=True)
        self.listbox.bind('<<ListboxSelect>>', self.on_select_texture)

        right = ttk.Frame(mid, padding=10)
        right.pack(side='left', fill='both', expand=True)

        ttk.Label(right, text="Preview:").pack(anchor='w')
        self.canvas = tk.Canvas(right, width=300, height=300, bg='#222')
        self.canvas.pack(pady=5)

        btns = ttk.Frame(right)
        btns.pack(pady=10)
        ttk.Button(btns, text="Export image (PNG)...", command=self.export_image).pack(side='left', padx=5)
        ttk.Button(btns, text="Load custom image...", command=self.load_custom_image).pack(side='left', padx=5)
        ttk.Button(btns, text="Apply to this texture", command=self.apply_edit).pack(side='left', padx=5)
        ttk.Button(btns, text="Save modified .pkz...", command=self.save_pkz).pack(side='left', padx=5)

        self.lbl_info = ttk.Label(right, text="", justify='left')
        self.lbl_info.pack(anchor='w', pady=10)

    def open_pkz(self):
        path = filedialog.askopenfilename(filetypes=[("PKZ files", "*.pkz"), ("All files", "*.*")])
        if not path:
            return
        with open(path, 'rb') as f:
            data = f.read()

        self.pkz_path = path
        self.pkz_data = bytearray(data)
        self.lbl_file.config(text=os.path.basename(path) + "  (scanning...)")
        self.root.update_idletasks()

        self.textures = discover_textures(bytes(data))
        self.lbl_file.config(text=os.path.basename(path))
        self.edited_indices = set()
        self.refresh_listbox_labels()

        if not self.textures:
            messagebox.showinfo("Info", "No texture found in this file.")
        else:
            n_ok = sum(1 for t in self.textures if t['fmt'] in FORMAT_NAMES)
            print(f"{len(self.textures)} textures found, {n_ok} with a known format.")

    def refresh_listbox_labels(self):
        query = self.search_var.get().strip().lower() if hasattr(self, 'search_var') else ''
        self.listbox.delete(0, 'end')
        self.visible_indices = []  # maps listbox row -> index into self.textures
        for i, t in enumerate(self.textures):
            if query and query not in t['name'].lower():
                continue
            self.visible_indices.append(i)
            fmt_name = FORMAT_NAMES.get(t['fmt'], f"Fmt{t['fmt']}")
            editable = t['fmt'] in ENCODABLE_FORMATS
            marker = 'OK' if editable else ('RO' if t['fmt'] in FORMAT_NAMES else '??')
            edited = ' *EDITED*' if i in self.edited_indices else ''
            self.listbox.insert(
                'end',
                f"[{marker}] #{i} {t['name']}  {t['w']}x{t['h']}  {fmt_name}  mips={t['mips']}{edited}"
            )

    def on_select_texture(self, event):
        sel = self.listbox.curselection()
        if not sel:
            return
        row = sel[0]
        idx = self.visible_indices[row]
        t = self.textures[idx]
        self.selected = t
        self.selected_idx = idx
        self.custom_img = None
        self.current_decoded_img = None

        if t['fmt'] not in FORMAT_NAMES:
            self.canvas.delete('all')
            self.canvas.create_text(150, 150, fill='white', width=280, justify='center',
                                     text=f"Format {t['fmt']} is not recognized.")
            self.lbl_info.config(text=f"#{idx} {t['name']}\n{t['w']}x{t['h']}  format={t['fmt']} (unknown)")
            return

        payload = bytes(self.pkz_data[t['payload_off']:t['payload_off'] + t['payload_len']])
        try:
            rgba = decode_texture(payload, t)
            rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
            img = Image.fromarray(rgba, 'RGBA')
        except Exception as e:
            self.canvas.delete('all')
            self.lbl_info.config(text=f"Decoding error: {e}")
            return

        self.current_decoded_img = img
        self.show_preview(img)
        fmt_name = FORMAT_NAMES[t['fmt']]
        editable_note = "" if t['fmt'] in ENCODABLE_FORMATS else "\n(preview only -- editing this format isn't supported yet)"
        self.lbl_info.config(
            text=f"#{idx} {t['name']}\n"
                 f"{t['w']}x{t['h']}  format={fmt_name}  mips={t['mips']}\n"
                 f"payload offset: 0x{t['payload_off']:X}, length: {t['payload_len']} bytes{editable_note}"
        )

    def show_preview(self, img):
        display = img.copy()
        display.thumbnail((300, 300))
        self.preview_img = ImageTk.PhotoImage(display)
        self.canvas.delete('all')
        self.canvas.create_image(150, 150, image=self.preview_img)

    def export_image(self):
        if self.current_decoded_img is None:
            messagebox.showwarning("Warning", "Select a texture with a valid preview first.")
            return
        default_name = safe_filename(self.selected['name']) + ".png" if self.selected else "texture.png"
        path = filedialog.asksaveasfilename(defaultextension=".png", initialfile=default_name,
                                             filetypes=[("PNG files", "*.png")])
        if not path:
            return
        self.current_decoded_img.save(path)
        messagebox.showinfo("Exported", f"Image exported to:\n{path}\n\n"
                                          f"Edit it while keeping EXACTLY the same resolution "
                                          f"({self.selected['w']}x{self.selected['h']}),\n"
                                          f"then use 'Load custom image...' to bring it back in.")

    def load_custom_image(self):
        if self.selected is None:
            messagebox.showwarning("Warning", "Select a texture in the list first.")
            return
        if self.selected['fmt'] not in ENCODABLE_FORMATS:
            messagebox.showwarning("Warning", "This texture's format can't be written back yet (preview only).")
            return
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not path:
            return
        img = Image.open(path)
        self.custom_img = img
        self.show_preview(img)
        self.lbl_info.config(text=self.lbl_info.cget('text') + "\n\n>>> Custom image loaded, ready to apply <<<")

    def apply_edit(self):
        if self.selected is None:
            messagebox.showwarning("Warning", "Select a texture first.")
            return
        if self.custom_img is None:
            messagebox.showwarning("Warning", "Load a custom image first.")
            return

        t = self.selected
        idx = self.selected_idx
        w, h, fmt = t['w'], t['h'], t['fmt']

        img = self.custom_img
        if img.size != (w, h):
            img = img.resize((w, h), Image.LANCZOS)
        img = img.convert('RGBA')
        rgba = np.array(img)

        try:
            color_bytes = encode_cmpr(rgba, w, h)
        except Exception as e:
            messagebox.showerror("Error", f"CMPR encoding failed: {e}")
            return

        base_offset = t['payload_off']
        self.pkz_data[base_offset:base_offset + len(color_bytes)] = color_bytes

        if fmt == 4:
            alpha = rgba[:, :, 3]
            try:
                alpha_bytes = encode_i8(alpha, w, h)
            except Exception as e:
                messagebox.showerror("Error", f"Alpha encoding failed: {e}")
                return
            alpha_offset = base_offset + t['csize']
            self.pkz_data[alpha_offset:alpha_offset + len(alpha_bytes)] = alpha_bytes

        self.edited_indices.add(idx)
        self.refresh_listbox_labels()
        self.custom_img = None
        messagebox.showinfo("Applied", f"Texture #{idx} ({t['name']}) updated in memory.\n\n"
                                        f"You can now select and edit ANOTHER texture, then click\n"
                                        f"'Save modified .pkz...' once you're done with all your edits.")

    def save_pkz(self):
        if not self.edited_indices:
            if not messagebox.askyesno("No edits", "No texture has been 'Applied' yet.\n"
                                                     "Save an identical copy of the original anyway?"):
                return
        save_path = filedialog.asksaveasfilename(defaultextension=".pkz",
                                                   initialfile=os.path.basename(self.pkz_path),
                                                   filetypes=[("PKZ files", "*.pkz")])
        if not save_path:
            return
        with open(save_path, 'wb') as f:
            f.write(self.pkz_data)
        messagebox.showinfo("Done", f"File saved:\n{save_path}\n\n"
                                     f"{len(self.edited_indices)} texture(s) modified in this file.")


def main():
    root = tk.Tk()
    app = SSCRTextureEditorApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
