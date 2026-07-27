# Hardware BOM / Sourcing Notes

Updated: 2026-06-26

## Controller path

Preferred for the WiiM now-playing display: Raspberry Pi 5 + HUB75 Pi adapter/bonnet.

### Pi HUB75 bonnet/adapter

Primary known-good option:
- Adafruit RGB Matrix Bonnet for Raspberry Pi — product #3211
- Expected normal price: about $15-$20, not $50.
- Adafruit listing: https://www.adafruit.com/product/3211
- DigiKey listing: https://www.digikey.com/en/products/detail/adafruit-industries-llc/3211/8535237
- Jameco listing found in search around $19.75: https://www.jameco.com/z/3211-Adafruit-Industries-Adafruit-RGB-Matrix-Bonnet-for-Raspberry-Pi_2512735.html
- Electromaker listing found in search around $14.95: https://www.electromaker.io/shop/product/adafruit-rgb-matrix-bonnet-for-raspberry-pi

Good alternate:
- Adafruit RGB Matrix HAT + RTC for Raspberry Pi — product #2345
- Listing: https://www.adafruit.com/product/2345
- Product page says it supports Pi Zero/A+/B+/Pi 2/3/4/5 and the matrix library now supports Pi 5.
- Pros vs Bonnet: includes DS1307 RTC, full HAT form factor, riser included, no soldering on current revision.
- Cons vs Bonnet: larger board, RTC/battery mostly unnecessary for this always-networked WiiM display, may have more Pi 5 cooler/enclosure clearance implications.
- For this 64x32 1:16 panel, #2345 should be a fine substitute if #3211 is overpriced/unavailable.

Sourcing note:
- Avoid paying large Adafruit shipping for a small parts order if possible;
  batch Adafruit-only items or use DigiKey/Jameco/Electromaker/Amazon equivalents.
- Amazon listings around $50 look like reseller markup. Avoid unless shipping
  speed is worth the markup.
- Generic Amazon HUB75 Raspberry Pi adapter boards exist, but they need extra
  compatibility scrutiny before buying, especially with Raspberry Pi 5.

## Panel power

Required spec for one 64x32 Adafruit RGB matrix:
- 5V DC regulated/switching supply
- 4A minimum; 5A+ is fine
- Center-positive barrel if using barrel connector path
- Common barrel size: 5.5mm OD / 2.1mm ID

Canonical Adafruit supply:
- Adafruit #1466, 5V 4A switching power supply
- https://www.adafruit.com/product/1466
- DigiKey: https://www.digikey.com/en/products/detail/adafruit-industries-llc/1466/10670047
- Mouser: https://www.mouser.com/ProductDetail/Adafruit/1466

Amazon search terms:
- `5V 4A power supply 5.5mm 2.1mm center positive`
- `5V 5A power supply 5.5mm 2.1mm center positive`

Amazon candidates found 2026-06-26:
- Selected: BTF-LIGHTING 5V 6A supply for LED projects, because it provides modest headroom and includes the needed screw adapter. Verify selected variant is 5V, not 12V/24V: https://www.amazon.com/BTF-LIGHTING-Plastic-Adapter-Transformer-WS2812B/dp/B01D8FM4N4
- Security-01 AC to DC 5V 4A Power Supply Adapter, 5.5mm x 2.1mm, UL Listed: https://www.amazon.com/Power-Supply-Adapter-5-5mm-2-1mm/dp/B09JW28WQC
- HPMN 5V 5A Power Supply, 5.5x2.5mm / 5.5x2.1mm compatible, center-positive, UL Listed: https://www.amazon.com/HPMN-5V-5A-25W-Transformer/dp/B0DP5YSD8Z
- Mean Well GST25B05-P1J 5V 4A, 5.5x2.1mm center-positive, high-quality brand but may go unavailable: https://www.amazon.com/GST25B05-P1J-Desktop-Adapter-Positive-IEC320-C8/dp/B07K7THWRH

Avoid:
- 12V or 24V supplies — will likely destroy the panel.
- 3A supplies except possibly low-brightness bench testing; target 4A+.
- Unknown polarity supplies unless center-positive is explicit or verified with a meter.

## Barrel adapter

If connecting a barrel supply to panel/bonnet screw terminals:
- 5.5mm x 2.1mm female barrel jack to screw terminal adapter
- Adafruit #368: https://www.adafruit.com/product/368
- Amazon search term: `5.5mm 2.1mm female barrel jack screw terminal`
