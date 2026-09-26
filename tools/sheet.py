"""Compose screenshots into review sheets: python sheet.py out.png w1 img1 img2 ... (each scaled to width w1, cropped to max height)"""
import sys
from PIL import Image
out, width, maxh = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
ims = []
for f in sys.argv[4:]:
    im = Image.open(f).convert("RGB")
    r = width / im.width
    im = im.resize((width, int(im.height * r)))
    if im.height > maxh:
        im = im.crop((0, 0, width, maxh))
    ims.append(im)
H = max(i.height for i in ims)
sheet = Image.new("RGB", (sum(i.width for i in ims) + 16 * (len(ims) - 1), H), "#8a96a3")
x = 0
for i in ims:
    sheet.paste(i, (x, 0)); x += i.width + 16
sheet.save(out)
print(sheet.size)
