import zlib
import struct
import math

def create_png(width, height, get_pixel):
    # RGBA image generator
    raw_data = bytearray()
    for y in range(height):
        raw_data.append(0) # filter type 0: None
        for x in range(width):
            r, g, b, a = get_pixel(x, y, width, height)
            raw_data.extend([r, g, b, a])
    
    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xffffffff
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', crc)
    
    header = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw_data), 9)
    return header + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')

def render_mixer_icon(x, y, w, h):
    # Normalized coords 0.0 to 1.0
    nx = x / float(w)
    ny = y / float(h)
    
    # Corner radius check (rounded square app icon)
    r = 0.22
    cx = nx if nx < 0.5 else 1.0 - nx
    cy = ny if ny < 0.5 else 1.0 - ny
    
    in_box = True
    if cx < r and cy < r:
        dist = math.hypot(r - cx, r - cy)
        if dist > r:
            # Transparent outside rounded box
            return 0, 0, 0, 0
    
    # Border / Frame
    is_border = False
    if cx < 0.04 or cy < 0.04 or (cx < r and cy < r and math.hypot(r - cx, r - cy) > r - 0.04):
        is_border = True
        
    # Background gradient: Dark Cubase Graphite (#1e2025 to #121316)
    bg_lum = 0.12 - 0.04 * ny
    r_val = int(255 * (bg_lum * 1.1))
    g_val = int(255 * (bg_lum * 1.15))
    b_val = int(255 * (bg_lum * 1.25))
    
    if is_border:
        return 55, 60, 70, 255

    # 3 Fader tracks: left (orange), center (cyan, master), right (orange)
    # Track positions (x centers): 0.28, 0.50, 0.72
    tracks = [
        {'x': 0.28, 'val': 0.65, 'color': (229, 155, 36), 'active': True},
        {'x': 0.50, 'val': 0.82, 'color': (0, 210, 255), 'active': True}, # Center / Master / Cyan
        {'x': 0.72, 'val': 0.45, 'color': (229, 155, 36), 'active': True}
    ]
    
    # Top/Bottom margins for fader groove: y from 0.20 to 0.80
    groove_top = 0.22
    groove_bot = 0.80
    groove_w = 0.05
    
    # Check if inside any fader track
    for t in tracks:
        tx = t['x']
        tc = t['color']
        fader_y = groove_bot - t['val'] * (groove_bot - groove_top)
        
        # Fader groove (slot)
        if abs(nx - tx) <= groove_w / 2.0 and groove_top <= ny <= groove_bot:
            # Groove background: very dark slot
            gr, gg, gb = 12, 13, 16
            # Active lit bar below fader level
            if ny >= fader_y:
                # Glowing LED trail
                glow = 0.8 + 0.2 * (ny - fader_y) / (groove_bot - fader_y + 0.001)
                gr = int(tc[0] * glow * 0.7)
                gg = int(tc[1] * glow * 0.7)
                gb = int(tc[2] * glow * 0.7)
            r_val, g_val, b_val = gr, gg, gb
            
        # Subtle LED indicator dots alongside the center track
        if tx == 0.50 and (abs(nx - (tx - 0.07)) < 0.015 or abs(nx - (tx + 0.07)) < 0.015):
            for dot_i in range(5):
                dot_y = groove_top + dot_i * (groove_bot - groove_top) / 4.0
                if abs(ny - dot_y) < 0.018:
                    if dot_y >= fader_y:
                        r_val, g_val, b_val = tc[0], tc[1], tc[2]
                    else:
                        r_val, g_val, b_val = 40, 45, 52

        # Fader Knob (Cap): width ~0.16, height ~0.10
        cap_w = 0.14 if tx != 0.50 else 0.17
        cap_h = 0.09
        if abs(nx - tx) <= cap_w / 2.0 and abs(ny - fader_y) <= cap_h / 2.0:
            # Metallic fader cap with horizontal line
            cap_edge = (abs(nx - tx) > cap_w / 2.0 - 0.015 or abs(ny - fader_y) > cap_h / 2.0 - 0.015)
            if cap_edge:
                r_val, g_val, b_val = 30, 32, 36
            elif abs(ny - fader_y) < 0.015:
                # Center indicator line on knob
                r_val, g_val, b_val = tc[0], tc[1], tc[2]
            else:
                # Brushed metal cap body
                grad = 140 - int(abs(ny - fader_y) / (cap_h / 2.0) * 40)
                r_val, g_val, b_val = grad, grad + 5, grad + 10

    # Top indicator LED (small header accent at y=0.12)
    if 0.10 <= ny <= 0.13:
        if abs(nx - 0.50) <= 0.18:
            # Header accent bar: MixConsole brand color (orange)
            r_val, g_val, b_val = 229, 155, 36

    # Bottom accent lines
    if 0.88 <= ny <= 0.90 and abs(nx - 0.50) <= 0.25:
        r_val, g_val, b_val = 50, 55, 65

    return r_val, g_val, b_val, 255

for size in [16, 48, 128]:
    png_bytes = create_png(size, size, render_mixer_icon)
    import os
    icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    os.makedirs(icons_dir, exist_ok=True)
    path = os.path.join(icons_dir, f"icon{size}.png")
    with open(path, "wb") as f:
        f.write(png_bytes)
    print(f"Generated {path}")
