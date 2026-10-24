# Design System: AetherDesk

## 1. Visual Theme & Atmosphere
A restrained, high-density telepresence workspace fusing Apple Dark aesthetics with aerospace avionics rigor. The atmosphere is clinical, disciplined, and whisper-quiet—like an air-gapped mission control terminal. Surfaces simulate cold anodized slate and matte obsidian glass (`#0B0F17` to `#131A29`), punctuated exclusively by a single laser-calibrated Electric Sky Cyan accent (`#38BDF8`) and real-time operational emerald indicators (`#10B981`). Interactions are immediate, tactile, and free of gratuitous ornamentation.

## 2. Color Palette & Functional Roles
- **Canvas Base (`#0B0F17`)**: Primary background canvas, deep cold neutral slate preventing eye fatigue.
- **Surface Elevation 1 (`#131A29`)**: Bento container surface, elevated panel fill.
- **Surface Elevation 2 (`#1A2336`)**: Interactive card hover state and nested pill background.
- **Whisper Stroke (`#252B3E`)**: 1px structural hairline borders defining spatial zones.
- **Stroke Active (`#38BDF8`)**: Dynamic focus ring and verified connection highlight.
- **Primary Kinetic Accent (`#38BDF8`)**: Electric Sky Cyan. Single brand accent for primary actions, laser links, and selected states. Saturation: 76%.
- **Telemetry Emerald (`#10B981`)**: Connection established, active host broadcast, low-latency transmission.
- **Telemetry Amber (`#F59E0B`)**: Broker negotiation, standby handshake, NAT traversal.
- **Text Polar (`#F8FAFC`)**: High-contrast primary headers, active credentials, button labels.
- **Text Titanium (`#94A3B8`)**: Secondary instructions, port metadata, latency readouts.

*(Banned: AI neon purple, magenta button glows, oversaturated gradients, and pitch black #000000).*

## 3. Typographic Architecture
- **Display & Headings**: `Space Grotesk` / `Segoe UI` (Track-tight `-0.02em`, weight 700/800, authoritative and architectural).
- **Interface Body**: `Geist` / `Segoe UI Variable` (Relaxed leading, clean neutral grotesque, max 60ch per block).
- **Tabular Data & Telemetry**: `JetBrains Mono` / `Cascadia Code` (Strictly tabular numbers for 9-digit Device IDs, passwords, FPS, and encryption ciphers).
- **Banned**: `Inter` as a creative display font, decorative script fonts, generic serif fonts (`Times`, `Georgia`).

## 4. Logo & App Icon Identity ("The Aether Portal")
- **Conceptual Glyph**: The Monolithic Dual-Portal "A"—two precision telepresence pylons converging upward, unified by a zero-latency horizontal photon bridge with a central refractive diamond prism.
- **Geometry & Safe Zone**: Strictly bound within the inner 80% circle of the canvas (410px on a 512px grid). Never clipped by iOS squircles or Windows taskbar tiles.
- **Lighting & Material**: Frosted deep obsidian base (`#0F111A`), hairline cyan boundary rim (`#38BDF8`), pure cyan-to-emerald laser bridge with micro-beacons.
- **Variants**:
  1. *Master App Icon (Dark Bento)*: Full squircle depth with hairline rim.
  2. *Monochrome Vector*: Single-color silhouette (`#FFFFFF` on transparent) for system tray and taskbar.
  3. *Brand Lockup*: Glyph + `AETHER` (Bold White) `DESK` (Electric Sky Cyan) + pill badge `ULTRA-LIGHT REMOTE`.

## 5. Component Stylings
- **Buttons**:
  - *Primary Kinetic*: Solid `#38BDF8` background, `#0B0F17` bold typography, 8px radius. Instant -1px tactile compression on active click.
  - *Secondary Bento*: Dark `#1A2336` fill, 1px `#252B3E` hairline border, `#F8FAFC` text. Hover transitions to `#252B3E` background and `#38BDF8` stroke.
- **Bento Cards**: Elevated containers (`#131A29`) with 12px radius, enclosed by a 1px `#252B3E` border. Internal padding: `16px` to `24px`.
- **Inputs & Key Entry**: Inset dark surface (`#0B0F17`), 8px radius. Instant active border illumination in `#38BDF8` with a 1px glow ring on focus.
- **Status Indicators**: 8px live status dots with soft breathing halos for active network states.
- **Session Floating Toolbar**: Translucent dark capsule toolbar docked to viewport top with grouped controls.

## 6. Motion Philosophy & Micro-Interactions
- **Zero-Lag Tactility**: Button hover and focus states trigger instantly without sluggish easing delays.
- **Feedback Precision**: Click triggers immediate state change, followed by smooth status transition.
- **State Machine Transitions**: Connection stages (`DISCONNECTED` -> `NEGOTIATING` -> `AUTHENTICATING` -> `STREAMING`) reflected via color pips and status text.

## 7. Anti-Patterns (Banned)
- NO decorative consumer emojis (smileys, reactions) anywhere in the interface or branding. Functional typographic unicode glyphs (such as arrows, status pips, and security shields) are permitted for compact operational buttons.
- NO purple/neon gradients or cyber-synthwave aesthetics.
- NO pure black (`#000000`) surfaces.
- NO ambiguous rounded-blob illustrations.
- NO generic AI copywriting ("Unleash ultra fast desktop").
- NO overlapping or unaligned text elements.
