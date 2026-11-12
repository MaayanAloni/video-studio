"""Show the whole clip while it samples, not just its first frame.

ComfyUI's latent previewer reduces a 5-D video latent with `x0 = x0[0, :, 0]`,
i.e. it previews frame 0 and throws the rest away. For video that's close to
useless — you cannot see motion, composition drift, or the saturation runaway
that the Apple MPS corruption bug causes.

This patches the previewer to decode several slices spread across the clip and
tile them into a contact sheet, so every preview shows the whole timeline
evolving. Purely a display change: sampling is untouched.
"""
import math

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

COLS = 4
MAX_TILES = 12

try:
    import latent_preview
    from PIL import Image

    def _tile(images):
        if len(images) == 1:
            return images[0]
        cols = min(COLS, len(images))
        rows = math.ceil(len(images) / cols)
        w, h = images[0].size
        sheet = Image.new("RGB", (w * cols, h * rows), (12, 14, 20))
        for i, im in enumerate(images):
            sheet.paste(im, ((i % cols) * w, (i // cols) * h))
        return sheet

    def _patch(cls):
        original = cls.decode_latent_to_preview

        def decode_latent_to_preview(self, x0):
            # (B, C, T, H, W) -> tile T; anything else is a still image already
            if getattr(x0, "ndim", 0) != 5 or x0.shape[2] < 2:
                return original(self, x0)
            frames = x0.shape[2]
            n = min(frames, MAX_TILES)
            idx = [round(i * (frames - 1) / max(1, n - 1)) for i in range(n)]
            try:
                return _tile([original(self, x0[:, :, t:t + 1]) for t in idx])
            except Exception:       # never let a preview break a render
                return original(self, x0)

        cls.decode_latent_to_preview = decode_latent_to_preview

    for name in ("Latent2RGBPreviewer", "TAESDPreviewerImpl"):
        cls = getattr(latent_preview, name, None)
        if cls is not None and hasattr(cls, "decode_latent_to_preview"):
            _patch(cls)
    print("[video-studio] preview patched: contact sheet across the clip", flush=True)
except Exception as e:  # noqa: BLE001 - a broken preview must never block startup
    print(f"[video-studio] preview patch skipped: {e}", flush=True)
