"""
Helpers for building image URL paths stored in the database.

In simulation mode  → all shoes share 5 placeholder images under /sim_images/
In actual mode      → each shoe has its own folder under /images/{batch}/{shoe}/
"""


def get_simulation_image_paths() -> dict:
    """Return URL paths to the shared simulation placeholder images."""
    return {
        "img_top":         "/sim_images/top.jpg",
        "img_left":        "/sim_images/left.jpg",
        "img_right":       "/sim_images/right.jpg",
        "img_angle_left":  "/sim_images/angle_left.jpg",
        "img_angle_right": "/sim_images/angle_right.jpg",
    }


def get_actual_image_paths(batch_id: str, shoe_id: str) -> dict:
    """Return URL paths for a real shoe's images (actual mode)."""
    base = f"/images/{batch_id}/{shoe_id}"
    return {
        "img_top":         f"{base}/top.jpg",
        "img_left":        f"{base}/left.jpg",
        "img_right":       f"{base}/right.jpg",
        "img_angle_left":  f"{base}/angle_left.jpg",
        "img_angle_right": f"{base}/angle_right.jpg",
    }


def get_table_photo_url(table_photo_id: str) -> str:
    """URL for a stored whole-table photo (served by the /images mount)."""
    return f"/images/table_photos/{table_photo_id}.jpg"


def get_table_photo_thumb_url(table_photo_id: str) -> str:
    """URL for the thumbnail of a whole-table photo (max side 1024px).
    The review modal shows the photo at ~340px tall; loading the multi-MB
    original there is what made the Table Photos modal feel slow."""
    return f"/images/table_photos/thumbs/{table_photo_id}.jpg"


def make_table_photo_thumb(src_path, thumb_path, max_side: int = 1024):
    """Write a JPEG thumbnail (longest side <= max_side) next to the original.
    Pillow-CPU-bound: call from a threadpool/worker, not the event loop."""
    from PIL import Image
    img = Image.open(src_path).convert("RGB")
    img.thumbnail((max_side, max_side))
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(thumb_path, "JPEG", quality=80)


def get_pair_url(pair_id: str) -> str:
    """URL for a stored cropped pair image (served by the /images mount)."""
    return f"/images/pairs/{pair_id}.jpg"
