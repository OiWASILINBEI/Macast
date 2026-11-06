"""Convert PNG assets into the multi-size icon.ico used for the exe,
and normalize the tray icon size. Runs in CI before PyInstaller."""
import os

from PIL import Image

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    '..', 'macast', 'assets')


def square(img):
    w, h = img.size
    s = min(w, h)
    return img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))


def main():
    app_src = os.path.join(BASE, 'app_icon_src.png')
    if os.path.exists(app_src):
        img = square(Image.open(app_src).convert('RGBA'))
        img.save(os.path.join(BASE, 'icon.ico'),
                 sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                        (64, 64), (128, 128), (256, 256)])
        print('icon.ico generated')

    tray = os.path.join(BASE, 'tray.png')
    if os.path.exists(tray):
        t = Image.open(tray).convert('RGBA')
        if max(t.size) > 256:
            t.thumbnail((256, 256), Image.LANCZOS)
            t.save(tray)
        print('tray.png normalized')


if __name__ == '__main__':
    main()
