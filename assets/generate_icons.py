"""Generates high-resolution PNG and Windows ICO assets for AetherDesk."""
import math
from pathlib import Path
from PIL import Image, ImageDraw

def render_aether_icon(size=1024) -> Image.Image:
    # 4x supersampling for ultra-crisp anti-aliasing
    scale = size / 512.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. Base Squircle with Dark Obsidian Gradient simulation
    # Rounded rect bounds
    pad = int(18 * scale)
    radius = int(110 * scale)
    rect_box = [pad, pad, size - pad, size - pad]

    # Draw layered background for gradient effect
    draw.rounded_rectangle(rect_box, radius=radius, fill=(15, 17, 26, 255), outline=(56, 189, 248, 140), width=int(5 * scale))

    # Inner ambient radial glow
    center_x, center_y = size // 2, size // 2
    for r in range(int(160 * scale), int(40 * scale), -int(10 * scale)):
        alpha = int((1.0 - (r / (160 * scale))) * 45)
        glow_box = [center_x - r, center_y - r, center_x + r, center_y + r]
        draw.ellipse(glow_box, fill=(56, 189, 248, alpha))

    # 2. Tech grid lines (subtle)
    for x_offset in [160, 256, 352]:
        x_scaled = int(x_offset * scale)
        draw.line([(x_scaled, pad + int(10 * scale)), (x_scaled, size - pad - int(10 * scale))], fill=(255, 255, 255, 16), width=int(1.5 * scale))
    for y_offset in [160, 256, 352]:
        y_scaled = int(y_offset * scale)
        draw.line([(pad + int(10 * scale), y_scaled), (size - pad - int(10 * scale), y_scaled)], fill=(255, 255, 255, 16), width=int(1.5 * scale))

    # 3. Monogram "A" - Legs
    p_left_base = (int(168 * scale), int(376 * scale))
    p_apex = (int(256 * scale), int(136 * scale))
    p_right_base = (int(344 * scale), int(376 * scale))

    # Glow shadow for legs
    leg_shadow_width = int(36 * scale)
    draw.line([p_left_base, p_apex], fill=(99, 102, 241, 70), width=leg_shadow_width)
    draw.line([p_apex, p_right_base], fill=(168, 85, 247, 70), width=leg_shadow_width)

    # Core stroke for legs
    leg_stroke = int(24 * scale)
    draw.line([p_left_base, p_apex], fill=(56, 189, 248, 255), width=leg_stroke)
    draw.line([p_apex, p_right_base], fill=(129, 140, 248, 255), width=leg_stroke)

    # 4. Crossbar - The Laser Beam
    p_bar_left = (int(130 * scale), int(295 * scale))
    p_bar_right = (int(382 * scale), int(295 * scale))
    beam_width = int(20 * scale)

    # Laser halo
    draw.line([p_bar_left, p_bar_right], fill=(16, 185, 129, 90), width=beam_width + int(16 * scale))
    # Laser solid core
    draw.line([p_bar_left, p_bar_right], fill=(52, 211, 153, 255), width=beam_width)

    # 5. Core Diamond Prism
    d_x, d_y = int(256 * scale), int(295 * scale)
    d_w, d_h = int(32 * scale), int(38 * scale)
    diamond_pts = [
        (d_x, d_y - d_h),
        (d_x + d_w, d_y),
        (d_x, d_y + d_h),
        (d_x - d_w, d_y)
    ]
    # Diamond glow
    draw.polygon(diamond_pts, fill=(255, 255, 255, 255))
    inner_diamond = [
        (d_x, d_y - int(24 * scale)),
        (d_x + int(20 * scale), d_y),
        (d_x, d_y + int(24 * scale)),
        (d_x - int(20 * scale), d_y)
    ]
    draw.polygon(inner_diamond, fill=(56, 189, 248, 255))

    # 6. Laser Nodes (Beacons)
    node_r = int(11 * scale)
    for pt, col in [(p_bar_left, (16, 185, 129)), (p_bar_right, (56, 189, 248)), (p_apex, (255, 255, 255))]:
        draw.ellipse([pt[0] - node_r*1.8, pt[1] - node_r*1.8, pt[0] + node_r*1.8, pt[1] + node_r*1.8], fill=(col[0], col[1], col[2], 90))
        draw.ellipse([pt[0] - node_r, pt[1] - node_r, pt[0] + node_r, pt[1] + node_r], fill=(col[0], col[1], col[2], 255))

    return img

def main():
    assets_dir = Path(__file__).parent
    assets_dir.mkdir(parents=True, exist_ok=True)

    print("Generating AetherDesk icon assets...")
    img_1024 = render_aether_icon(1024)

    # Export high-res PNG (512x512)
    img_512 = img_1024.resize((512, 512), Image.Resampling.LANCZOS)
    png_path = assets_dir / "icon.png"
    img_512.save(png_path, "PNG")
    print(f"Saved: {png_path}")

    # Export multi-resolution ICO for Windows
    ico_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    ico_path = assets_dir / "icon.ico"
    img_1024.save(ico_path, format="ICO", sizes=ico_sizes)
    print(f"Saved: {ico_path} with sizes: {ico_sizes}")

if __name__ == "__main__":
    main()
