# Power Architecture Notes

Updated: 2026-06-26

## Goal

Eventually the WiiM now-playing display should have one external power input, while safely powering both:

- Raspberry Pi 5 controller
- 64x32 HUB75 RGB LED matrix panel

## Prototype recommendation

Use separate supplies at first:

- Pi 5: official/quality Raspberry Pi 5 USB-C supply, ideally 5V/5A class.
- Matrix: separate regulated 5V 4A+ supply through the Pi RGB Matrix Bonnet/panel power input.

Reason: this reduces variables during first-light debugging. If the panel flickers or the Pi undervolts, separate supplies make the failure mode obvious.

## Final single-source options

### Option A — Single AC input, two enclosed adapters inside enclosure

One external AC inlet/cord feeds two certified power adapters inside the enclosure:

- official Pi 5 USB-C PSU for the Pi
- 5V 4A+ LED matrix supply for the panel

Pros:
- safest electrically if using certified enclosed adapters
- preserves proper USB-C/PD behavior for Pi 5
- easiest to debug/replace

Cons:
- physically bulkier
- still two internal bricks
- enclosure must handle AC safely; strain relief and insulation matter

### Option B — One big external 5V DC brick, split internally

Use one external certified 5V supply sized for both loads, then split low-voltage DC inside the enclosure.

Suggested capacity:
- Minimum: 5V 10A
- Comfortable: 5V 12A
- Voltage target: 5.0-5.1V, regulated

Distribution:
- Input jack/switch/fuse -> 5V distribution block
- Branch 1: matrix/bonnet power, fused around 4-5A
- Branch 2: Pi 5 power via USB-C power input, using a high-current 5V USB-C/PD-capable path, fused around 5A
- Grounds common at the distribution point

Pros:
- one external low-voltage brick
- no mains wiring inside project enclosure
- cleaner final object

Cons:
- Pi 5 is picky: it expects a high-quality 5V/5A USB-C supply/negotiation path for full peripheral power
- cheap barrel-to-USB-C cables may not advertise/provide enough current
- voltage drop matters; use short, thick wires

### Option C — One open-frame/enclosed AC-to-5V PSU inside enclosure

Use something like a Mean Well enclosed 5V 10A supply mounted inside.

Pros:
- compact single-supply professional style
- enough current for Pi + panel

Cons:
- involves mains wiring inside the enclosure
- requires proper fused AC inlet, earth/insulation/strain relief, covered terminals, and safe mechanical design
- not recommended until enclosure design is mature

## Recommendation

- Stage 1/2: separate supplies.
- Final build target: Option B if we can source a reliable 5V 10A/12A external brick and a trustworthy Pi 5 USB-C high-current feed.
- If Pi 5 complains about undervoltage/current limits, fall back to Option A: single AC inlet/cord feeding the official Pi 5 PSU plus a separate matrix PSU inside the enclosure.

## Safety rules

- Never feed the panel with 12V/24V.
- Do not power the LED matrix from the Raspberry Pi.
- Do not backfeed the Pi through random GPIO/5V wiring unless deliberately designing and fusing that path.
- Use common ground between Pi/controller and matrix power.
- Use short, appropriately thick wire for 5V high-current runs.
- Add fuses or resettable protection per branch in the final build.
- Keep mains wiring out of the enclosure unless the design includes proper strain relief, insulation, fuse, and covered terminals.
