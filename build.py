"""Build dist/pixel_clau.exe — a single file, no Python needed on the target PC.

    python -m venv .venv-build
    .venv-build\\Scripts\\pip install pyinstaller pillow
    .venv-build\\Scripts\\python build.py
"""
import importlib.util, subprocess, sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('pixel_clau', HERE / 'pixel_clau.pyw')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

# icon: the idle sprite, drawn from the same cells the button uses, scaled with hard pixel edges
art = Image.new('RGBA', (18, 18), (0, 0, 0, 0))
d = ImageDraw.Draw(art)
for x, y, w, h, col in app.sprite_rects():
    d.rectangle([x, y, x + w - 1, y + h - 1], fill=col)
art = art.crop(art.getbbox())
side = max(art.size)
square = Image.new('RGBA', (side, side), (0, 0, 0, 0))
square.paste(art, ((side - art.width) // 2, (side - art.height) // 2))
icon = HERE / 'build' / 'icon.ico'
icon.parent.mkdir(exist_ok=True)
square.resize((256, 256), Image.NEAREST).save(icon, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])

subprocess.run([sys.executable, '-m', 'PyInstaller', '--onefile', '--noconsole', '--noconfirm',
                '--name', app.NAME, '--icon', str(icon), '--workpath', str(HERE / 'build'),
                '--specpath', str(HERE / 'build'), str(HERE / 'pixel_clau.pyw')], check=True)
print('->', HERE / 'dist' / f'{app.NAME}.exe')
