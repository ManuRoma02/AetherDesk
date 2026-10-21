# AetherDesk Brand Identity & Design System

> **Official Brand System & Guidelines**  
> Maintained by Brand Guardian & UI Design Systems  
> Version: 1.0.0 — Codebase: `AetherDesk` (Windows Remote Desktop)

---

## 1. Brand Foundation

### 1.1 Brand Name & Etymology
* **Primary Mark:** **AetherDesk**
* **Pronunciation:** `/ˈiː.θər.dɛsk/`
* **Etymology:** Derived from *Aether* (the weightless, pure upper atmosphere through which light travels with zero resistance) + *Desk* (the immediate remote workspace).
* **Tagline:** *"Ultra-Light, Zero-Bloat Remote Access"*
* **Italian Tagline:** *"Il controllo remoto puro, invisibile e senza limiti"*

### 1.2 Purpose, Vision & Mission
* **Brand Purpose:** Liberate remote computing from heavy corporate bloatware, 5-minute disconnect timers, paywalled features, and background resource drain.
* **Brand Vision:** To become the reference open-source, local-first remote desktop utility for developers, students, system administrators, and power users worldwide.
* **Brand Mission:** Provide instant, unattended access to any Windows computer with military-grade zero-knowledge encryption, near-zero CPU footprint, and zero subscription costs.

### 1.3 Core Brand Values
1. **Ethereal Lightness (Zero Weight):** We reject multi-hundred-megabyte installer bloat. A single standalone `.exe` without installer dependencies is our promise.
2. **Uncompromising Security (Zero-Knowledge):** E2EE by default. All screen captures, keystrokes, clipboard data, and files are encrypted with AES-256-GCM derived via PBKDF2-SHA256. We never see user data.
3. **Radical Freedom:** 100% free, open source, no arbitrary 5-minute kickouts, no subscription tiers, no corporate nag screens.
4. **Architectural Honesty:** Transparent engineering. No simulated hype—just verifiable frame diffs, raw speed, and robust unattended Windows system services.

### 1.4 Brand Personality & Tone
* **Archetype:** *The Swift Navigator* & *The Transparent Craftsman*
* **Personality Traits:** Precise, fast, unpretentious, architectonic, rock-solid.
* **Voice & Tone:** Confident, technical, concise, pragmatic. Avoid marketing clichés (*"Meet the future of..."*, *"Revolutionary"*); focus on concrete engineering facts (*"30 FPS frame-differencing"*, *"O(1) streaming memory"*, *"AES-256-GCM"*).

---

## 2. Visual Identity System

### 2.1 The Logo Mark
The **AetherDesk** logo mark depicts a dual-portal geometric monogram **"A"**:
* **The Twin Slants:** Represent the two machines (Local Laptop & Remote Host).
* **The Glowing Laser Bridge:** Represents the zero-latency, encrypted high-speed link that unifies both environments into one cohesive workspace.
* **The Central Diamond Prism:** Signifies cryptographic integrity (Zero-Knowledge E2EE) and focus.

```
       ▲  [Apex Beacon - Cloud/LAN Rendezvous]
      / \
     /   \   [Host & Client Portal Rays]
    /  ◆  \  [Core Cryptographic Diamond Prism]
   ●=======● [High-Speed Ethereal Data Bridge]
  /         \
```

### 2.2 Color Tokens & Palette

| Token Name | Hex | RGB | Purpose / Usage |
| :--- | :--- | :--- | :--- |
| `--aether-bg-deep` | `#0a0c14` | `10, 12, 20` | Primary application canvas & outer dark frame |
| `--aether-surface-1` | `#141726` | `20, 23, 38` | Container card surfaces & control panels |
| `--aether-surface-2` | `#1e2235` | `30, 34, 53` | Input fields, active toolbar buttons, hover states |
| `--aether-border` | `#334155` | `51, 65, 85` | Subtle container strokes & divider lines |
| `--aether-cyan` | `#38bdf8` | `56, 189, 248` | Primary brand accent, laser ray, action buttons |
| `--aether-indigo` | `#6366f1` | `99, 102, 241` | Secondary gradient accent & session auth tone |
| `--aether-emerald` | `#10b981` | `16, 185, 129` | Connected state, verified status, active beacon |
| `--aether-coral` | `#f43f5e` | `244, 63, 94` | Disconnect action, security warnings, errors |
| `--aether-text-hero` | `#f8fafc` | `248, 250, 252` | Primary headlines, device ID numbers, high contrast |
| `--aether-text-muted` | `#94a3b8` | `148, 163, 184` | Subtitles, helper text, inactive labels |

### 2.3 Typography System
* **Primary UI Font:** `Segoe UI Variable`, `Segoe UI`, `Inter`, system-ui
* **Monospace Font (Device ID, Keys, Logs):** `JetBrains Mono`, `Consolas`, monospace
* **Type Scale:**
  * **Brand Headline (H1):** 22px / Bold / Letter-spacing +1.5px
  * **Device ID Display:** 22px / Bold / Monospace (`Consolas`) / Letter-spacing +2px
  * **Section Title (H2):** 14px / Bold / Uppercase / Letter-spacing +1px
  * **Body / Controls:** 11px / SemiBold
  * **Micro / Badges:** 9px / Regular

### 2.4 Asset Manifest
* [`assets/icon.svg`](file:///c:/Users/manue/Dropbox/ScreenMirroring/assets/icon.svg): Master vector squircle icon (512x512).
* [`assets/logo.svg`](file:///c:/Users/manue/Dropbox/ScreenMirroring/assets/logo.svg): Horizontal brand lockup with logotype and subtitle.
* [`assets/icon.png`](file:///c:/Users/manue/Dropbox/ScreenMirroring/assets/icon.png): Master 512x512 raster icon with subpixel anti-aliasing.
* [`assets/icon.ico`](file:///c:/Users/manue/Dropbox/ScreenMirroring/assets/icon.ico): Multi-resolution Windows executable and window icon (256, 128, 64, 48, 32, 16 px).

---

## 3. UI Component Patterns

### 3.1 Device ID Display Card
* Background: `#141726` with 1px border `#334155`
* ID Text: `#38bdf8` in 22pt bold monospace
* Grouping format: `XXX XXX XXX` (9 digits with readable triplets)
* Action: Immediate "Copy ID" button with transient confirmation badge

### 3.2 Status Beacon
* **Disconnected / Idle:** Hollow ring `#94a3b8`
* **Connecting / Handshake:** Breathing pulse `#38bdf8` (1.2s cycle)
* **Authenticated & Streaming:** Solid neon beacon `#10b981` with subtle ambient bloom
* **Error / Disconnected:** Solid coral beacon `#f43f5e`

### 3.3 Remote Viewer Toolbar
* Position: Top-pinned, auto-retractable or slim height (35px)
* Background: `#0f111a` with 50% backdrop opacity
* Action groups: System Keys (`Ctrl+Alt+Del`, `Win+D`) | File Transfer | Multi-Monitor Selector | Disconnect
