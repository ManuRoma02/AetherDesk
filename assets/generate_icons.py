"""
Generates ultra-high resolution PNG and Windows multi-resolution ICO assets for AetherDesk.
Strictly adheres to the Stitch Design Taste (Apple Dark Bento, Electric Sky Cyan, no purple slop).
"""
import os
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

def render_aether_icon(size=1024) -> Image.Image:
    scale = size / 512.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. Base Squircle with Apple Dark Bento Colors (#0e131f base, #252b3e border)
    pad = int(18 * scale)
    radius = int(108 * scale)
    rect_box = [pad, pad, size - pad, size - pad]

    # Draw rounded background
    draw.rounded_rectangle(
        rect_box,
        radius=radius,
        fill=(14, 19, 31, 255),
        outline=(37, 43, 62, 255),
        width=int(4 * scale)
    )

    # Inner hairline cyan rim
    inner_pad = pad + int(4 * scale)
    inner_radius = radius - int(4 * scale)
    draw.rounded_rectangle(
        [inner_pad, inner_pad, size - inner_pad, size - inner_pad],
        radius=inner_radius,
        fill=None,
        outline=(56, 189, 248, 90),
        width=int(2 * scale)
    )

    # Ambient radial core glow
    glow_canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_canvas)
    center_x, center_y = size // 2, int(270 * scale)
    for r in range(int(150 * scale), int(30 * scale), -int(12 * scale)):
        alpha = int((1.0 - (r / (150 * scale))) * 38)
        glow_draw.ellipse(
            [center_x - r, center_y - r, center_x + r, center_y + r],
            fill=(56, 189, 248, alpha)
        )
    img = Image.alpha_composite(img, glow_canvas)
    draw = ImageDraw.Draw(img)

    # 2. Subtle Precision Grid
    grid_col = (56, 189, 248, 20)
    for x_offset in [168, 256, 344]:
        x_s = int(x_offset * scale)
        draw.line([(x_s, pad + int(10 * scale)), (x_s, size - pad - int(10 * scale))], fill=grid_col, width=int(1.5 * scale))
    for y_offset in [128, 216, 296, 384]:
        y_s = int(y_offset * scale)
        draw.line([(pad + int(10 * scale), y_s), (size - pad - int(10 * scale), y_s)], fill=grid_col, width=int(1.5 * scale))

    # 3. Monogram "A" - Legs
    p_left_base = (int(164 * scale), int(376 * scale))
    p_apex = (int(256 * scale), int(136 * scale))
    p_right_base = (int(348 * scale), int(376 * scale))

    # Glow shadow pass for legs
    leg_shadow_width = int(34 * scale)
    draw.line([p_left_base, p_apex], fill=(2, 132, 199, 70), width=leg_shadow_width)
    draw.line([p_apex, p_right_base], fill=(2, 132, 199, 70), width=leg_shadow_width)

    # Core stroke for legs (Electric Sky Cyan)
    leg_stroke = int(26 * scale)
    draw.line([p_left_base, p_apex], fill=(56, 189, 248, 255), width=leg_stroke)
    draw.line([p_apex, p_right_base], fill=(56, 189, 248, 255), width=leg_stroke)

    # Smooth apex cap
    apex_r = leg_stroke // 2
    draw.ellipse([p_apex[0] - apex_r, p_apex[1] - apex_r, p_apex[0] + apex_r, p_apex[1] + apex_r], fill=(186, 230, 253, 255))
    draw.ellipse([p_left_base[0] - apex_r, p_left_base[1] - apex_r, p_left_base[0] + apex_r, p_left_base[1] + apex_r], fill=(56, 189, 248, 255))
    draw.ellipse([p_right_base[0] - apex_r, p_right_base[1] - apex_r, p_right_base[0] + apex_r, p_right_base[1] + apex_r], fill=(56, 189, 248, 255))

    # 4. Crossbar - The Laser Bridge
    p_bar_left = (int(132 * scale), int(296 * scale))
    p_bar_right = (int(380 * scale), int(296 * scale))
    beam_width = int(20 * scale)

    # Emerald to Cyan Laser Halo
    draw.line([p_bar_left, p_bar_right], fill=(16, 185, 129, 90), width=beam_width + int(14 * scale))
    draw.line([p_bar_left, p_bar_right], fill=(56, 189, 248, 255), width=beam_width)

    # 5. Core Refractive Diamond Prism
    d_x, d_y = int(256 * scale), int(296 * scale)
    d_w, d_h = int(32 * scale), int(36 * scale)
    diamond_outer = [
        (d_x, d_y - d_h),
        (d_x + d_w, d_y),
        (d_x, d_y + d_h),
        (d_x - d_w, d_y)
    ]
    draw.polygon(diamond_outer, fill=(248, 250, 252, 255))

    inner_w, inner_h = int(22 * scale), int(26 * scale)
    diamond_inner = [
        (d_x, d_y - inner_h),
        (d_x + inner_w, d_y),
        (d_x, d_y + inner_h),
        (d_x - inner_w, d_y)
    ]
    draw.polygon(diamond_inner, fill=(56, 189, 248, 255))

    # 6. Laser Beacons (Optical nodes)
    node_r = int(10 * scale)
    # Left beacon (Emerald Host)
    draw.ellipse([p_bar_left[0] - node_r*1.8, p_bar_left[1] - node_r*1.8, p_bar_left[0] + node_r*1.8, p_bar_left[1] + node_r*1.8], fill=(16, 185, 129, 95))
    draw.ellipse([p_bar_left[0] - node_r, p_bar_left[1] - node_r, p_bar_left[0] + node_r, p_bar_left[1] + node_r], fill=(16, 185, 129, 255))
    draw.ellipse([p_bar_left[0] - node_r//2, p_bar_left[1] - node_r//2, p_bar_left[0] + node_r//2, p_bar_left[1] + node_r//2], fill=(255, 255, 255, 255))

    # Right beacon (Sky Cyan Client)
    draw.ellipse([p_bar_right[0] - node_r*1.8, p_bar_right[1] - node_r*1.8, p_bar_right[0] + node_r*1.8, p_bar_right[1] + node_r*1.8], fill=(56, 189, 248, 95))
    draw.ellipse([p_bar_right[0] - node_r, p_bar_right[1] - node_r, p_bar_right[0] + node_r, p_bar_right[1] + node_r], fill=(56, 189, 248, 255))
    draw.ellipse([p_bar_right[0] - node_r//2, p_bar_right[1] - node_r//2, p_bar_right[0] + node_r//2, p_bar_right[1] + node_r//2], fill=(255, 255, 255, 255))

    # Apex beacon (Polar White Gateway)
    draw.ellipse([p_apex[0] - node_r*1.6, p_apex[1] - node_r*1.6, p_apex[0] + node_r*1.6, p_apex[1] + node_r*1.6], fill=(255, 255, 255, 110))
    draw.ellipse([p_apex[0] - node_r*0.9, p_apex[1] - node_r*0.9, p_apex[0] + node_r*0.9, p_apex[1] + node_r*0.9], fill=(255, 255, 255, 255))

    return img

def main():
    assets_dir = Path(__file__).parent
    assets_dir.mkdir(parents=True, exist_ok=True)

    print("Rendering AetherDesk Master Artwork (1024x1024)...")
    master = render_aether_icon(1024)

    # Master PNG
    master_path = assets_dir / "icon-1024.png"
    master.save(master_path, "PNG")
    print(f"Generated: {master_path}")

    # Standard 512x512 App Icon
    img_512 = master.resize((512, 512), Image.Resampling.LANCZOS)
    png_path = assets_dir / "icon.png"
    img_512.save(png_path, "PNG")
    print(f"Generated: {png_path}")

    # Windows Multi-Resolution ICO
    ico_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    ico_path = assets_dir / "icon.ico"
    master.save(ico_path, format="ICO", sizes=ico_sizes)
    print(f"Generated: {ico_path} with sizes: {ico_sizes}")

if __name__ == "__main__":
    main()
